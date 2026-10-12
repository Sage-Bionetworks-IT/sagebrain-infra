import hashlib
import time
from typing import Callable

from sagebrain_core import limits


class TokenCache:
    """Per-task cache of successful bearer authentications: sha256(token) -> user id.

    Only successes are stored (FR-5.4), so a Synapse outage or a denial is never remembered.
    Raw tokens are never held in memory beyond the request.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._entries: dict[str, tuple[str, float]] = {}

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def get(self, token: str) -> str | None:
        key = self._key(token)
        entry = self._entries.get(key)
        if entry is None:
            return None
        user_id, expires_at = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            return None
        return user_id

    def put(self, token: str, user_id: str) -> None:
        expires_at = self._clock() + limits.AUTH_CACHE_TTL_SECONDS
        self._entries[self._key(token)] = (user_id, expires_at)
