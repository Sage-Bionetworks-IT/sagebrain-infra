"""Token-bucket admission shared by the HTTP API and in-process callers.

Bucket keys are namespaced per API (`{rl:query}:global`, `{rl:query}:u:<principal>`);
`DynamoLimiter` stores one item per key in the shared rate-limit table.
"""

import json
import math
import os
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Protocol

import botocore.session
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from . import limits
from .errors import RateLimited

APIS = ("query", "ask")
MACHINE_PRINCIPAL = "machine"
# Agent jobs from the legacy API Gateway /ask Lambda carry no user id, so every one of them
# shares this principal across all instances. It gets the machine bucket until the /ask
# cutover retires that message shape (spec 001 D-12).
LEGACY_AGENT_PRINCIPAL = "legacy-apigw"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: float = 0.0


class Limiter(Protocol):
    def acquire(self, key: str, rate: float, burst: int) -> Decision: ...


class LocalLimiter:
    """In-process token buckets. Correct per process only; `DynamoLimiter` shares them."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._buckets: dict = {}
        self._lock = threading.Lock()

    def acquire(self, key: str, rate: float, burst: int) -> Decision:
        with self._lock:
            now = self._clock()
            tokens, last = self._buckets.get(key, (float(burst), now))
            tokens = min(float(burst), tokens + (now - last) * rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                return Decision(True)
            self._buckets[key] = (tokens, now)
            return Decision(False, (1.0 - tokens) / rate)

    def keys(self):
        return list(self._buckets)


# Same connect/read timeouts as the app's other AWS calls (parity row 4). No retries: a
# failing call falls back to the local bucket at once instead of retrying into a throttle.
AWS_CLIENT_CONFIG = Config(
    connect_timeout=2,
    read_timeout=5,
    retries={"mode": "standard", "total_max_attempts": 1},
)
_client = None


def _dynamodb_client():
    """Module-level and lazy, so a warm Lambda or a long-lived task reuses one client."""
    global _client
    if _client is None:
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        _client = botocore.session.get_session().create_client(
            "dynamodb", region_name=region, config=AWS_CLIENT_CONFIG
        )
    return _client


# Decimal resolution of stored bucket times (1 ns), so every sum DynamoDB does is exact.
_QUANTUM = Decimal("1e-9")
_CONSUME, _REFILL = 0, 1
_MAX_ATTEMPTS = 4
_HINT_LIMIT = 10_000

# Deployment knobs, not contract thresholds (the rates and bursts all come from `limits`):
# how many processes share the buckets when DynamoDB is unreachable, and how often each
# process logs that it is degraded.
DEFAULT_FALLBACK_INSTANCES = 2
DEGRADED_LOG_INTERVAL_SECONDS = 60
# How long a bucket item outlives the moment it is full again. A missing item and a full
# bucket admit the same calls, so TTL deletion is unobservable; it only stops idle
# per-principal items piling up. Not a threshold, so it isn't in `limits`.
BUCKET_TTL_SECONDS = 3600


class DynamoLimiter:
    """Token buckets shared by every process that uses the same table.

    DynamoDB update expressions have `+` and `-` but no multiply or `min`, so a bucket is
    stored as one number, `tat`: the time it will be full again (GCRA, the same maths as
    `LocalLimiter`). A call is admitted while `tat <= now + (burst - 1) / rate`, and admitting
    adds one interval (`1 / rate`). Each attempt is one conditional UpdateItem, either:

    - consume: `tat BETWEEN now AND limit` -> `tat = tat + interval`, or
    - refill: missing or `tat < now` (the bucket is full) -> `tat = now + interval`.

    A failed condition returns the item (`ALL_OLD`), which either rejects the call with
    `retry_after = tat - limit`, or shows that the other branch applies. A per-process hint
    picks the likely branch first, so the steady state is one UpdateItem per call; there's
    never a read. Every write sets `expires_at` (the table's TTL attribute) past the time the
    bucket is full again; reads ignore it, because TTL deletion is lazy. `now` comes from the
    injected clock, which must be wall time (shared across hosts), not `time.monotonic`.

    If DynamoDB fails (an error, throttling or a timeout), the call fails open to a
    per-process `LocalLimiter` at `rate / fallback_instances` and logs `ratelimit_degraded`.
    A failed condition is an ordinary rejection, not a failure.
    """

    def __init__(
        self,
        table_name: str,
        *,
        client=None,
        clock: Callable[[], float] = time.time,
        fallback_instances: int = DEFAULT_FALLBACK_INSTANCES,
    ):
        if fallback_instances < 1:
            raise ValueError("fallback_instances must be at least 1")
        self.table_name = table_name
        self.fallback_instances = fallback_instances
        self._client = client
        self._clock = clock
        self._hint: dict = {}
        self._fallback = LocalLimiter(clock=clock)
        self._last_degraded_log: float | None = None
        self._suppressed = 0

    def acquire(self, key: str, rate: float, burst: int) -> Decision:
        try:
            return self._acquire_shared(key, rate, burst)
        except (BotoCoreError, ClientError) as error:
            return self._degraded(key, rate, burst, error)

    def _acquire_shared(self, key: str, rate: float, burst: int) -> Decision:
        client = self._client or _dynamodb_client()
        now = Decimal(repr(self._clock())).quantize(_QUANTUM)
        interval = (Decimal(1) / Decimal(repr(rate))).quantize(_QUANTUM)
        limit = now + (burst - 1) * interval
        # After any admitted call `tat <= now + burst * interval`: the bucket is full by then.
        expires_at = math.ceil(now + burst * interval) + BUCKET_TTL_SECONDS
        hint = self._hint.get(key)
        first = _CONSUME if hint is not None and hint >= now else _REFILL
        for attempt in range(_MAX_ATTEMPTS):
            branch = first if attempt % 2 == 0 else 1 - first
            try:
                response = client.update_item(
                    **self._update(key, branch, now, interval, limit, expires_at),
                    ReturnValues="UPDATED_NEW",
                    ReturnValuesOnConditionCheckFailure="ALL_OLD",
                )
            except ClientError as error:
                if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
                item = error.response.get("Item")
                tat = Decimal(item["tat"]["N"]) if item and "tat" in item else None
                self._remember(key, tat)
                if tat is not None and tat > limit:
                    return Decision(False, float(tat - limit))
                continue  # the other branch applies to the state we were shown
            self._remember(key, Decimal(response["Attributes"]["tat"]["N"]))
            return Decision(True)
        # Other instances changed the item between every attempt: treat it as contention.
        return Decision(False, float(interval))

    def _update(self, key, branch, now, interval, limit, expires_at) -> dict:
        common = {"TableName": self.table_name, "Key": {"key": {"S": key}}}
        if branch == _CONSUME:
            return {
                **common,
                "UpdateExpression": "SET tat = tat + :interval, expires_at = :expires_at",
                "ConditionExpression": "tat BETWEEN :now AND :limit",
                "ExpressionAttributeValues": {
                    ":interval": {"N": str(interval)},
                    ":now": {"N": str(now)},
                    ":limit": {"N": str(limit)},
                    ":expires_at": {"N": str(expires_at)},
                },
            }
        return {
            **common,
            "UpdateExpression": "SET tat = :tat, expires_at = :expires_at",
            "ConditionExpression": "attribute_not_exists(tat) OR tat < :now",
            "ExpressionAttributeValues": {
                ":tat": {"N": str(now + interval)},
                ":now": {"N": str(now)},
                ":expires_at": {"N": str(expires_at)},
            },
        }

    def _degraded(self, key, rate, burst, error) -> Decision:
        rate, burst = (
            rate / self.fallback_instances,
            math.ceil(burst / self.fallback_instances),
        )
        now = self._clock()
        last = self._last_degraded_log
        if last is None or now - last >= DEGRADED_LOG_INTERVAL_SECONDS:
            if isinstance(error, ClientError):
                code = error.response["Error"].get("Code", "ClientError")
            else:
                code = type(error).__name__
            print(
                json.dumps(
                    {
                        "event": "ratelimit_degraded",
                        "table": self.table_name,
                        "key": key,
                        "error": code,
                        "instances": self.fallback_instances,
                        "fallback_rate": rate,
                        "fallback_burst": burst,
                        "suppressed": self._suppressed,
                        "timestamp": now,
                    }
                )
            )
            self._last_degraded_log, self._suppressed = now, 0
        else:
            self._suppressed += 1
        return self._fallback.acquire(key, rate, burst)

    def _remember(self, key, tat) -> None:
        if len(self._hint) >= _HINT_LIMIT:
            self._hint.clear()
        self._hint[key] = tat


def make_limiter(
    table_name: str | None, fallback_instances: int = DEFAULT_FALLBACK_INSTANCES
) -> Limiter:
    """The shared DynamoDB limiter if a table is configured, else in-process buckets."""
    if not table_name:
        return LocalLimiter()
    return DynamoLimiter(table_name, fallback_instances=fallback_instances)


def limiter_from_env(environ=os.environ) -> Limiter:
    """`RATE_LIMIT_TABLE_NAME` (unset: `LocalLimiter`), `RATE_LIMIT_FALLBACK_INSTANCES`."""
    return make_limiter(
        environ.get("RATE_LIMIT_TABLE_NAME"),
        int(environ.get("RATE_LIMIT_FALLBACK_INSTANCES", DEFAULT_FALLBACK_INSTANCES)),
    )


_default_limiter: Limiter | None = None


def default_limiter() -> Limiter:
    """The process-wide limiter for callers that don't inject one (the agent worker)."""
    global _default_limiter
    if _default_limiter is None:
        _default_limiter = limiter_from_env()
    return _default_limiter


def admit(api: str, principal: str, limiter: Limiter | None = None) -> None:
    """Charge the API's global bucket, then the principal's bucket. Raises RateLimited."""
    admit_global(api, limiter)
    admit_principal(api, principal, limiter)


def admit_global(api: str, limiter: Limiter | None = None) -> None:
    """Charge only the API's global bucket. The HTTP layer calls this before auth."""
    _check_api(api)
    decision = (limiter or default_limiter()).acquire(
        f"{{rl:{api}}}:global",
        limits.GLOBAL_RATE_PER_API_RPS,
        limits.GLOBAL_BURST_PER_API,
    )
    if not decision.allowed:
        raise RateLimited("global", decision.retry_after)


def admit_principal(api: str, principal: str, limiter: Limiter | None = None) -> None:
    """Charge only the principal's bucket. The HTTP layer calls this after auth."""
    _check_api(api)
    if principal in (MACHINE_PRINCIPAL, LEGACY_AGENT_PRINCIPAL):
        rate, burst = limits.MACHINE_RATE_RPS, limits.MACHINE_BURST
    else:
        rate, burst = limits.USER_RATE_RPS, limits.USER_BURST
    decision = (limiter or default_limiter()).acquire(
        f"{{rl:{api}}}:u:{principal}", rate, burst
    )
    if not decision.allowed:
        raise RateLimited("principal", decision.retry_after)


def _check_api(api: str) -> None:
    if api not in APIS:
        raise ValueError(f"unknown api {api!r}; expected one of {APIS}")
