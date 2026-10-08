"""T104/T142 — token buckets and admission (global + per-principal).

Every bucket case runs against both limiters: `LocalLimiter` and `DynamoLimiter` on a moto
table (frozen, injected clock; no network).
"""

import pytest

from sagebrain_core import limits
from sagebrain_core.errors import RateLimited
from sagebrain_core.ratelimit import (
    DynamoLimiter,
    LocalLimiter,
    admit,
    admit_global,
    admit_principal,
)

from ...conftest import RATE_LIMIT_TABLE


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture(params=["local", "dynamo"])
def limiter(request, clock):
    if request.param == "local":
        return LocalLimiter(clock=clock)
    client = request.getfixturevalue("rate_limit_table")
    return DynamoLimiter(RATE_LIMIT_TABLE, client=client, clock=clock)


def keys(limiter):
    """Bucket keys a limiter has written (a moto Scan for DynamoDB; tests only)."""
    if isinstance(limiter, LocalLimiter):
        return limiter.keys()
    items = limiter._client.scan(TableName=RATE_LIMIT_TABLE)["Items"]
    return [item["key"]["S"] for item in items]


def test_burst_then_reject(limiter):
    for _ in range(100):
        assert limiter.acquire("k", rate=50, burst=100).allowed
    decision = limiter.acquire("k", rate=50, burst=100)
    assert not decision.allowed
    assert decision.retry_after == pytest.approx(1 / 50)


def test_refills_at_rate(limiter, clock):
    for _ in range(100):
        limiter.acquire("k", rate=50, burst=100)
    clock.t += 1.0
    allowed = sum(limiter.acquire("k", rate=50, burst=100).allowed for _ in range(60))
    assert allowed == 50


def test_refill_capped_at_burst(limiter, clock):
    limiter.acquire("k", rate=50, burst=100)
    clock.t += 3600
    allowed = sum(limiter.acquire("k", rate=50, burst=100).allowed for _ in range(150))
    assert allowed == 100


def test_keys_are_independent(limiter):
    for _ in range(100):
        limiter.acquire("a", rate=50, burst=100)
    assert limiter.acquire("b", rate=50, burst=100).allowed


def test_admit_global_bucket_shared_across_principals(limiter, clock):
    # 20 principals x 5 requests = 100 = global burst; per-user burst (20) never hit.
    for i in range(20):
        for _ in range(5):
            admit("query", f"user-{i}", limiter)
    with pytest.raises(RateLimited) as exc:
        admit("query", "user-new", limiter)
    assert exc.value.scope == "global"


def test_admit_per_principal_bucket(limiter):
    for _ in range(limits.USER_BURST):
        admit("query", "alice", limiter)
    with pytest.raises(RateLimited) as exc:
        admit("query", "alice", limiter)
    assert exc.value.scope == "principal"
    assert exc.value.retry_after == pytest.approx(1 / limits.USER_RATE_RPS)
    admit("query", "bob", limiter)  # other users unaffected


def test_admit_machine_principal_uses_machine_limits(limiter):
    for _ in range(limits.MACHINE_BURST):
        admit("query", "machine", limiter)
    with pytest.raises(RateLimited):
        admit("query", "machine", limiter)


def test_query_and_ask_buckets_independent(limiter):
    for _ in range(limits.USER_BURST):
        admit("query", "alice", limiter)
    admit("ask", "alice", limiter)


def test_bucket_key_format(limiter):
    admit("query", "alice", limiter)
    assert set(keys(limiter)) == {"{rl:query}:global", "{rl:query}:u:alice"}


def test_unknown_api_rejected(limiter):
    with pytest.raises(ValueError):
        admit("other", "alice", limiter)


def test_admit_global_charges_only_the_global_bucket(limiter):
    # HTTP charges the global bucket before auth, when the principal isn't known yet.
    admit_global("query", limiter)
    assert keys(limiter) == ["{rl:query}:global"]


def test_admit_principal_charges_only_the_principal_bucket(limiter):
    admit_principal("ask", "alice", limiter)
    assert keys(limiter) == ["{rl:ask}:u:alice"]


def test_admit_global_rejects_when_empty(limiter):
    for _ in range(limits.GLOBAL_BURST_PER_API):
        admit_global("query", limiter)
    with pytest.raises(RateLimited) as exc:
        admit_global("query", limiter)
    assert exc.value.scope == "global"


def test_split_admission_unknown_api_rejected(limiter):
    with pytest.raises(ValueError):
        admit_global("other", limiter)
    with pytest.raises(ValueError):
        admit_principal("other", "alice", limiter)


# ---------------------------------------------------------------------------
# T142 — the DynamoDB buckets are shared: every instance on one table charges the same item
# ---------------------------------------------------------------------------


@pytest.fixture
def instances(rate_limit_table, clock):
    """Two limiter instances (two ECS tasks / Lambda instances) on one table, one clock."""
    return [
        DynamoLimiter(RATE_LIMIT_TABLE, client=rate_limit_table, clock=clock)
        for _ in range(2)
    ]


def test_two_instances_share_a_bucket(instances):
    a, b = instances
    allowed = sum(
        (a if i % 2 else b).acquire("k", rate=50, burst=100).allowed for i in range(150)
    )
    assert allowed == 100
    decision = a.acquire("k", rate=50, burst=100)
    assert not decision.allowed
    assert decision.retry_after == pytest.approx(1 / 50)


def test_shared_bucket_refills_once_for_all_instances(instances, clock):
    a, b = instances
    for _ in range(100):
        a.acquire("k", rate=50, burst=100)
    clock.t += 1.0
    allowed = sum(
        (a if i % 2 else b).acquire("k", rate=50, burst=100).allowed for i in range(60)
    )
    assert allowed == 50


def test_parity_row1_global_bucket_shared_across_instances(instances, clock):
    """Row 1: 100 burst / 50 rps per API, summed over every instance."""
    a, b = instances
    for i in range(limits.GLOBAL_BURST_PER_API):
        admit_global("query", a if i % 2 else b)
    for limiter in (a, b):
        with pytest.raises(RateLimited) as exc:
            admit_global("query", limiter)
        assert exc.value.scope == "global"
    admit_global("ask", b)  # the ask bucket is separate
    clock.t += 1.0
    admitted = 0
    for i in range(limits.GLOBAL_BURST_PER_API):
        try:
            admit_global("query", a if i % 2 else b)
            admitted += 1
        except RateLimited:
            pass
    assert admitted == limits.GLOBAL_RATE_PER_API_RPS


def test_parity_row2_principal_isolated_across_instances(instances):
    """Row 2: alice drains her bucket on one instance; it's empty on the other, bob's isn't."""
    a, b = instances
    for _ in range(limits.USER_BURST):
        admit_principal("query", "alice", a)
    with pytest.raises(RateLimited) as exc:
        admit_principal("query", "alice", b)
    assert exc.value.scope == "principal"
    assert exc.value.retry_after == pytest.approx(1 / limits.USER_RATE_RPS)
    admit_principal("query", "bob", b)


def test_one_update_item_per_call_in_steady_state(rate_limit_table, clock):
    """No reads: each admitted or rejected call on a busy bucket is a single UpdateItem."""
    calls = []
    rate_limit_table.meta.events.register(
        "before-call.dynamodb", lambda model, **kw: calls.append(model.name)
    )
    limiter = DynamoLimiter(RATE_LIMIT_TABLE, client=rate_limit_table, clock=clock)
    limiter.acquire("k", rate=5, burst=3)  # creates the item
    calls.clear()
    for _ in range(4):  # 2 admitted, 2 rejected
        limiter.acquire("k", rate=5, burst=3)
    assert calls == ["UpdateItem"] * 4


def test_rejection_does_not_write(rate_limit_table, clock):
    limiter = DynamoLimiter(RATE_LIMIT_TABLE, client=rate_limit_table, clock=clock)
    for _ in range(3):
        limiter.acquire("k", rate=5, burst=3)
    before = rate_limit_table.get_item(
        TableName=RATE_LIMIT_TABLE, Key={"key": {"S": "k"}}
    )
    assert not limiter.acquire("k", rate=5, burst=3).allowed
    after = rate_limit_table.get_item(
        TableName=RATE_LIMIT_TABLE, Key={"key": {"S": "k"}}
    )
    assert after["Item"] == before["Item"]


def test_legacy_agent_principal_uses_machine_limits(limiter):
    """Every legacy {authorization} agent job shares this one principal across instances."""
    from sagebrain_core.ratelimit import LEGACY_AGENT_PRINCIPAL

    assert LEGACY_AGENT_PRINCIPAL == "legacy-apigw"
    for _ in range(limits.MACHINE_BURST):
        admit_principal("query", LEGACY_AGENT_PRINCIPAL, limiter)
    with pytest.raises(RateLimited) as exc:
        admit_principal("query", LEGACY_AGENT_PRINCIPAL, limiter)
    assert exc.value.retry_after == pytest.approx(1 / limits.MACHINE_RATE_RPS)
