"""T143 — DynamoDB outage falls back to a local bucket (parity row 3)."""

import json
import math

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from sagebrain_core import limits, ratelimit
from sagebrain_core.errors import RateLimited
from sagebrain_core.ratelimit import (
    BUCKET_TTL_SECONDS,
    DEGRADED_LOG_INTERVAL_SECONDS,
    DynamoLimiter,
    LocalLimiter,
    admit,
)

from ...conftest import RATE_LIMIT_TABLE
from .test_ratelimit import Clock


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "UpdateItem")


class FlakyClient:
    """Raises the queued errors first, then behaves like the real (moto) client."""

    def __init__(self, real, errors=()):
        self.real = real
        self.errors = list(errors)
        self.calls = 0

    def update_item(self, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.real.update_item(**kwargs)


class DownClient:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def update_item(self, **kwargs):
        self.calls += 1
        raise self.error


@pytest.fixture
def clock():
    return Clock()


def _degraded(capsys):
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    return [line for line in lines if line.get("event") == "ratelimit_degraded"]


OUTAGES = [
    _client_error("ProvisionedThroughputExceededException"),
    _client_error("ThrottlingException"),
    _client_error("RequestLimitExceeded"),
    _client_error("InternalServerError"),
    _client_error("ResourceNotFoundException"),
    _client_error("AccessDeniedException"),
    ReadTimeoutError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com"),
    ConnectTimeoutError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com"),
    EndpointConnectionError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com"),
]


@pytest.mark.parametrize("error", OUTAGES, ids=lambda e: type(e).__name__)
def test_outage_fails_open_to_local_bucket_at_rate_over_instances(error, clock, capsys):
    limiter = DynamoLimiter(
        RATE_LIMIT_TABLE, client=DownClient(error), clock=clock, fallback_instances=2
    )
    allowed = sum(limiter.acquire("k", rate=50, burst=100).allowed for _ in range(80))
    assert allowed == 50  # burst 100 / 2 instances
    clock.t += 1.0
    allowed = sum(limiter.acquire("k", rate=50, burst=100).allowed for _ in range(80))
    assert allowed == 25  # 50 rps / 2 instances


def test_outage_logs_ratelimit_degraded(clock, capsys):
    error = _client_error("ThrottlingException")
    limiter = DynamoLimiter(
        RATE_LIMIT_TABLE, client=DownClient(error), clock=clock, fallback_instances=4
    )
    assert limiter.acquire("{rl:query}:global", rate=50, burst=100).allowed
    (event,) = _degraded(capsys)
    assert event == {
        "event": "ratelimit_degraded",
        "table": RATE_LIMIT_TABLE,
        "key": "{rl:query}:global",
        "error": "ThrottlingException",
        "instances": 4,
        "fallback_rate": 12.5,
        "fallback_burst": 25,
        "suppressed": 0,
        "timestamp": clock.t,
    }


def test_timeout_is_logged_by_exception_class(clock, capsys):
    limiter = DynamoLimiter(
        RATE_LIMIT_TABLE,
        client=DownClient(ReadTimeoutError(endpoint_url="https://ddb")),
        clock=clock,
    )
    limiter.acquire("k", rate=5, burst=20)
    assert _degraded(capsys)[0]["error"] == "ReadTimeoutError"


def test_degraded_log_is_throttled_per_limiter(clock, capsys):
    """An outage at 50 rps must not write 50 log lines a second."""
    limiter = DynamoLimiter(
        RATE_LIMIT_TABLE,
        client=DownClient(_client_error("ThrottlingException")),
        clock=clock,
    )
    for _ in range(10):
        limiter.acquire("k", rate=50, burst=100)
    assert len(_degraded(capsys)) == 1
    clock.t += DEGRADED_LOG_INTERVAL_SECONDS
    limiter.acquire("k", rate=50, burst=100)
    (event,) = _degraded(capsys)
    assert event["suppressed"] == 9


def test_conditional_check_failed_is_a_rejection_not_a_degradation(
    rate_limit_table, clock, capsys
):
    client = FlakyClient(rate_limit_table)
    limiter = DynamoLimiter(
        RATE_LIMIT_TABLE, client=client, clock=clock, fallback_instances=2
    )
    for _ in range(limits.USER_BURST):
        admit("query", "alice", limiter)
    with pytest.raises(RateLimited) as exc:
        admit("query", "alice", limiter)
    assert exc.value.scope == "principal"
    assert _degraded(capsys) == []
    # The local fallback was never charged: an outage now still gets its own full share.
    limiter._client = DownClient(_client_error("ThrottlingException"))
    for _ in range(limits.USER_BURST // 2):
        admit("query", "alice", limiter)


def test_shared_bucket_resumes_after_the_outage(rate_limit_table, clock, capsys):
    client = FlakyClient(
        rate_limit_table, errors=[_client_error("ThrottlingException")]
    )
    limiter = DynamoLimiter(RATE_LIMIT_TABLE, client=client, clock=clock)
    assert limiter.acquire("k", rate=5, burst=3).allowed  # degraded, local
    assert len(_degraded(capsys)) == 1
    allowed = sum(limiter.acquire("k", rate=5, burst=3).allowed for _ in range(5))
    assert allowed == 3  # the shared bucket, untouched by the outage
    assert _degraded(capsys) == []


def test_unexpected_client_errors_still_fail_open(clock, capsys):
    limiter = DynamoLimiter(
        RATE_LIMIT_TABLE,
        client=DownClient(_client_error("ValidationException")),
        clock=clock,
    )
    assert limiter.acquire("k", rate=5, burst=20).allowed
    assert _degraded(capsys)[0]["error"] == "ValidationException"


def test_fallback_instances_must_be_positive():
    with pytest.raises(ValueError):
        DynamoLimiter(RATE_LIMIT_TABLE, fallback_instances=0)


# ---------------------------------------------------------------------------
# T144 — expires_at for the table's TTL; reads never trust TTL
# ---------------------------------------------------------------------------


def _item(client, key="k"):
    return client.get_item(TableName=RATE_LIMIT_TABLE, Key={"key": {"S": key}})["Item"]


@pytest.mark.parametrize("calls", [1, 3])  # the refill branch, then the consume branch
def test_bucket_items_carry_expires_at(rate_limit_table, clock, calls):
    limiter = DynamoLimiter(RATE_LIMIT_TABLE, client=rate_limit_table, clock=clock)
    for _ in range(calls):
        limiter.acquire("k", rate=5, burst=20)
    item = _item(rate_limit_table)
    # An integer epoch second (what DynamoDB TTL reads), at least one horizon after the
    # bucket is full again, so TTL only ever deletes buckets that are already full.
    full_at = float(item["tat"]["N"])
    assert item["expires_at"]["N"].isdigit()
    assert int(item["expires_at"]["N"]) >= full_at + BUCKET_TTL_SECONDS
    assert (
        int(item["expires_at"]["N"]) == math.ceil(clock.t + 20 / 5) + BUCKET_TTL_SECONDS
    )


def test_expired_item_that_ttl_has_not_deleted_yet_refills(rate_limit_table, clock):
    """TTL deletion is lazy (up to days), so an expired item is still read as live state."""
    drained_at = clock.t - 2 * BUCKET_TTL_SECONDS
    rate_limit_table.put_item(
        TableName=RATE_LIMIT_TABLE,
        Item={
            "key": {"S": "k"},
            "tat": {"N": str(drained_at + 20 / 5)},  # empty bucket, long ago
            "expires_at": {"N": str(int(drained_at) + 4 + BUCKET_TTL_SECONDS)},
        },
    )
    limiter = DynamoLimiter(RATE_LIMIT_TABLE, client=rate_limit_table, clock=clock)
    allowed = sum(limiter.acquire("k", rate=5, burst=20).allowed for _ in range(25))
    assert allowed == 20
    assert int(_item(rate_limit_table)["expires_at"]["N"]) > clock.t


def test_unexpired_item_is_honoured_whatever_expires_at_says(rate_limit_table, clock):
    """The bucket state is `tat`; a stale or odd `expires_at` never refills a bucket early."""
    rate_limit_table.put_item(
        TableName=RATE_LIMIT_TABLE,
        Item={
            "key": {"S": "k"},
            "tat": {"N": str(clock.t + 20 / 5)},  # drained just now
            "expires_at": {"N": "1"},  # long past
        },
    )
    limiter = DynamoLimiter(RATE_LIMIT_TABLE, client=rate_limit_table, clock=clock)
    decision = limiter.acquire("k", rate=5, burst=20)
    assert not decision.allowed
    assert decision.retry_after == pytest.approx(1 / 5)


# ---------------------------------------------------------------------------
# T145 — backend selection: RATE_LIMIT_TABLE_NAME unset means LocalLimiter
# ---------------------------------------------------------------------------


def test_no_table_name_means_local_limiter():
    assert isinstance(ratelimit.limiter_from_env({}), LocalLimiter)
    assert isinstance(
        ratelimit.limiter_from_env({"RATE_LIMIT_TABLE_NAME": ""}), LocalLimiter
    )
    assert isinstance(ratelimit.make_limiter(None), LocalLimiter)


def test_table_name_means_dynamo_limiter():
    limiter = ratelimit.limiter_from_env(
        {"RATE_LIMIT_TABLE_NAME": "app-dev-rate-limits"}
    )
    assert isinstance(limiter, DynamoLimiter)
    assert limiter.table_name == "app-dev-rate-limits"
    assert limiter.fallback_instances == ratelimit.DEFAULT_FALLBACK_INSTANCES


def test_fallback_instances_from_env():
    limiter = ratelimit.limiter_from_env(
        {"RATE_LIMIT_TABLE_NAME": "t", "RATE_LIMIT_FALLBACK_INSTANCES": "10"}
    )
    assert limiter.fallback_instances == 10
    with pytest.raises(ValueError):
        ratelimit.limiter_from_env(
            {"RATE_LIMIT_TABLE_NAME": "t", "RATE_LIMIT_FALLBACK_INSTANCES": "0"}
        )


def test_default_limiter_is_built_once_from_env(monkeypatch):
    monkeypatch.setattr(ratelimit, "_default_limiter", None)
    monkeypatch.setattr(ratelimit, "_client", None)
    monkeypatch.setenv("RATE_LIMIT_TABLE_NAME", "app-dev-rate-limits")
    first = ratelimit.default_limiter()
    assert isinstance(first, DynamoLimiter)
    assert ratelimit.default_limiter() is first
    assert (
        ratelimit._client is None
    )  # the boto client is created on first use, not here


def test_default_limiter_is_local_without_a_table(monkeypatch):
    monkeypatch.setattr(ratelimit, "_default_limiter", None)
    monkeypatch.delenv("RATE_LIMIT_TABLE_NAME", raising=False)
    assert isinstance(ratelimit.default_limiter(), LocalLimiter)


def test_client_is_module_level_and_reused(rate_limit_table, clock):
    a = DynamoLimiter(RATE_LIMIT_TABLE, clock=clock)
    b = DynamoLimiter(RATE_LIMIT_TABLE, clock=clock)
    a.acquire("k", rate=5, burst=20)
    b.acquire("k", rate=5, burst=20)
    assert ratelimit._client is rate_limit_table


def test_client_uses_short_timeouts_and_no_retries():
    config = ratelimit.AWS_CLIENT_CONFIG
    assert (config.connect_timeout, config.read_timeout) == (2, 5)
    assert config.retries == {"mode": "standard", "total_max_attempts": 1}


def test_client_honours_aws_endpoint_url(monkeypatch):
    monkeypatch.setattr(ratelimit, "_client", None)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localstack:4566")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    assert ratelimit._dynamodb_client().meta.endpoint_url == "http://localstack:4566"
