import asyncio
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Callable, Literal, Mapping

import httpx

from sagebrain_core import limits
from sagebrain_core.ratelimit import MACHINE_PRINCIPAL

from .cache import TokenCache

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    id: str  # Synapse user id, or "machine"
    method: Literal["bearer", "apiKey"]


class AuthDenied(Exception):
    """Expected auth failure -> 401. Never include the credential value in the message."""


class AuthUnavailable(Exception):
    """Synapse couldn't answer (timeout, 5xx, network) -> AUTH_TRANSIENT_STATUS. Not cached."""


class _SynapseError(Exception):
    """An unexpected Synapse status; treated as transient, like the Lambda's re-raise."""


class Authenticator:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        team_id: str,
        machine_api_key: str | None,
        repo_api: str,
        auth_api: str,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._client = client
        self._team_id = team_id
        self._machine_api_key = machine_api_key or ""
        self._repo_api = repo_api
        self._auth_api = auth_api
        self._cache = TokenCache(clock)

    async def authenticate(self, headers: Mapping[str, str]) -> Principal:
        """x-api-key if present (D-4: alone is enough), else Authorization: Bearer <token>."""
        try:
            async with asyncio.timeout(limits.AUTH_TIMEOUT_SECONDS):
                principal = await self._authenticate(headers)
        except AuthDenied as e:
            log.warning(json.dumps({"event": "auth_deny", "reason": str(e)}))
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, _SynapseError) as e:
            # Exception type only: messages can carry request details.
            log.warning(
                json.dumps({"event": "auth_unavailable", "reason": type(e).__name__})
            )
            raise AuthUnavailable(type(e).__name__) from e
        log.info(json.dumps({"event": "auth_allow", "principal": principal.id}))
        return principal

    async def _authenticate(self, headers: Mapping[str, str]) -> Principal:
        api_key = headers.get("x-api-key", "")
        if api_key:
            # A wrong key is rejected even if a valid bearer token is also sent.
            return self._validate_api_key(api_key)

        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        if token:
            return Principal(await self._validate_synapse_token(token), "bearer")

        raise AuthDenied(
            "no recognised credential (x-api-key or Authorization: Bearer)"
        )

    # -- machine key ---------------------------------------------------------

    def _validate_api_key(self, provided: str) -> Principal:
        if not self._machine_api_key:
            raise AuthDenied("x-api-key auth not configured")
        if not hmac.compare_digest(provided.encode(), self._machine_api_key.encode()):
            raise AuthDenied("invalid x-api-key")
        return Principal(MACHINE_PRINCIPAL, "apiKey")

    # -- Synapse bearer (PATs and OAuth tokens) -------------------------------

    async def _validate_synapse_token(self, token: str) -> str:
        cached = self._cache.get(token)
        if cached:
            return cached
        user_id = await self._get_synapse_user_id(token)
        await self._check_team_membership(user_id)
        self._cache.put(token, user_id)
        return user_id

    async def _get(self, url: str, token: str | None = None) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return await self._client.get(
            url, headers=headers, timeout=limits.SYNAPSE_CALL_TIMEOUT_SECONDS
        )

    async def _get_synapse_user_id(self, token: str) -> str:
        """/userProfile first (PATs, OAuth with 'view'); on 403 fall back to OIDC userinfo."""
        response = await self._get(f"{self._repo_api}/userProfile", token)
        if response.status_code == 200:
            user_id = response.json().get("ownerId")
            if not user_id:
                raise AuthDenied("unauthenticated")
            return str(user_id)
        if response.status_code == 401:
            raise AuthDenied("invalid token")
        if response.status_code == 403:
            # OAuth token without the 'view' scope; userinfo only needs 'openid'.
            return await self._userinfo_oidc(token)
        raise _SynapseError(response.status_code)

    async def _userinfo_oidc(self, token: str) -> str:
        """Uses the numeric 'userid' claim — 'sub' is pairwise-opaque per OAuth client."""
        response = await self._get(f"{self._auth_api}/oauth2/userinfo", token)
        if response.status_code in (401, 403):
            raise AuthDenied("invalid token")
        if response.status_code != 200:
            raise _SynapseError(response.status_code)
        data = response.json()
        user_id = data.get("userid")
        log.info(
            json.dumps(
                {
                    "event": "oidc_userinfo",
                    "has_userid": user_id is not None,
                    "has_sub": data.get("sub") is not None,
                    "sub_preview": (data.get("sub") or "")[:8] or None,
                }
            )
        )
        if not user_id:
            raise AuthDenied("unauthenticated")
        return str(user_id)

    async def _check_team_membership(self, user_id: str) -> None:
        # membershipStatus is public: the caller's token is not sent.
        response = await self._get(
            f"{self._repo_api}/team/{self._team_id}/member/{user_id}/membershipStatus"
        )
        if response.status_code in (400, 404):
            raise AuthDenied("not a team member")
        if response.status_code != 200:
            raise _SynapseError(response.status_code)
        if not response.json().get("isMember"):
            raise AuthDenied("not a team member")
