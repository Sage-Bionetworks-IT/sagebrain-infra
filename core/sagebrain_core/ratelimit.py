"""Token-bucket admission shared by the HTTP API and in-process callers.

Bucket keys are namespaced per API (`{rl:query}:global`, `{rl:query}:u:<principal>`); the
DynamoDB-backed limiter (tasks.md Phase 1d) stores one item per key.
"""

import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from . import limits
from .errors import RateLimited

APIS = ("query", "ask")
MACHINE_PRINCIPAL = "machine"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: float = 0.0


class Limiter(Protocol):
    def acquire(self, key: str, rate: float, burst: int) -> Decision: ...


class LocalLimiter:
    """In-process token buckets. Correct per process only; Phase 1d adds the shared DynamoDB limiter."""

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


_default_limiter = LocalLimiter()


def default_limiter() -> Limiter:
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
    if principal == MACHINE_PRINCIPAL:
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
