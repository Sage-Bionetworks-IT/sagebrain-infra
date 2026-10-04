"""T104 — token buckets and admission (global + per-principal)."""

import pytest

from sagebrain_core import limits
from sagebrain_core.errors import RateLimited
from sagebrain_core.ratelimit import LocalLimiter, admit


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def limiter(clock):
    return LocalLimiter(clock=clock)


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


def test_bucket_keys_use_hash_tags(limiter):
    admit("query", "alice", limiter)
    assert set(limiter.keys()) == {"{rl:query}:global", "{rl:query}:u:alice"}


def test_unknown_api_rejected(limiter):
    with pytest.raises(ValueError):
        admit("other", "alice", limiter)
