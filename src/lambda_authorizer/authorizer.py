import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request

log = logging.getLogger()
log.setLevel(logging.INFO)

SYNAPSE_AUTH_API = "https://repo-prod.prod.sagebase.org/auth/v1"
SYNAPSE_REPO_API = "https://repo-prod.prod.sagebase.org/repo/v1"

TEAM_ID = os.getenv("SYNAPSE_TEAM_ID")
if not TEAM_ID:
    raise RuntimeError("Missing required environment variable: SYNAPSE_TEAM_ID")

# Optional — if set, callers may authenticate with x-api-key: <key>.
# Machine clients must ALSO send Authorization: ApiKey so API Gateway's identity
# source requirement is satisfied (see RequestAuthorizer in the CDK stack).
# Leave unset (or empty) to disable the API-key path.
_MACHINE_API_KEY = os.getenv("MACHINE_API_KEY", "")

# Lambda-instance token cache: token -> (principal_id, expires_monotonic)
# Warm instances reuse this to avoid redundant Synapse API calls.
_token_cache: dict = {}
_CACHE_TTL = 300  # seconds


class _AuthDenied(Exception):
    """Expected auth failure. Never include the credential value in the message."""


def handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    method_arn = event["methodArn"]

    try:
        principal = _authenticate(headers)
        log.info(json.dumps({"event": "auth_allow", "principal": principal}))
        return _policy(principal, "Allow", method_arn)
    except _AuthDenied as e:
        log.warning(json.dumps({"event": "auth_deny", "reason": str(e)}))
        return _policy("anonymous", "Deny", method_arn)
    # Transient errors (Synapse timeout / 5xx / network) propagate uncaught.
    # API Gateway treats a Lambda error as Unauthorized and does NOT cache the
    # result, so valid users are not locked out during a Synapse outage.


def _authenticate(headers: dict) -> str:
    api_key = headers.get("x-api-key", "")
    auth = headers.get("authorization", "")

    if api_key:
        return _validate_api_key(api_key)

    if auth.lower().startswith("bearer "):
        return _validate_synapse_token(auth[7:])

    raise _AuthDenied("no recognised credential (x-api-key or Authorization: Bearer)")


# ---------------------------------------------------------------------------
# API-key path  (machine / service clients)
# ---------------------------------------------------------------------------


def _validate_api_key(provided: str) -> str:
    if not _MACHINE_API_KEY:
        raise _AuthDenied("x-api-key auth not configured")
    if not hmac.compare_digest(provided, _MACHINE_API_KEY):
        raise _AuthDenied("invalid x-api-key")
    return "machine"


# ---------------------------------------------------------------------------
# Synapse Bearer path  (PATs and OAuth JWTs both accepted)
# ---------------------------------------------------------------------------


def _validate_synapse_token(token: str) -> str:
    cached = _token_cache.get(token)
    if cached:
        principal_id, expires_at = cached
        if time.monotonic() < expires_at:
            return principal_id

    user_id = _get_synapse_user_id(token)
    _check_team_membership(user_id)

    _token_cache[token] = (user_id, time.monotonic() + _CACHE_TTL)
    return user_id


def _get_synapse_user_id(token: str) -> str:
    """Return the numeric Synapse user ID for any valid token type.

    Tries /userProfile first (PATs and OAuth tokens with the 'view' scope).
    Falls back to OIDC userinfo for OAuth tokens that only carry 'openid' scope.
    The OIDC 'sub' claim is pairwise-opaque per client and cannot be used for
    team membership checks — only the 'userid' claim carries the numeric ID.
    """
    req = urllib.request.Request(
        f"{SYNAPSE_REPO_API}/userProfile",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        user_id = data.get("ownerId")
        if not user_id:
            raise _AuthDenied("unauthenticated")
        return str(user_id)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise _AuthDenied("invalid token") from e
        if e.code == 403:
            # OAuth token lacks the 'view' scope required by /userProfile.
            # Fall back to OIDC userinfo which only needs 'openid'.
            return _userinfo_oidc(token)
        raise


def _userinfo_oidc(token: str) -> str:
    """Get numeric Synapse user ID via OIDC userinfo for OAuth-only tokens.
    Uses the 'userid' claim — 'sub' is pairwise-opaque per client."""
    req = urllib.request.Request(
        f"{SYNAPSE_AUTH_API}/oauth2/userinfo",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise _AuthDenied("invalid token") from e
        raise
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
        raise _AuthDenied("unauthenticated")
    return str(user_id)


def _check_team_membership(user_id: str) -> None:
    # The membershipStatus endpoint is public — no auth required.
    req = urllib.request.Request(
        f"{SYNAPSE_REPO_API}/team/{TEAM_ID}/member/{user_id}/membershipStatus",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            raise _AuthDenied("not a team member") from e
        raise
    if not data.get("isMember"):
        raise _AuthDenied("not a team member")


# ---------------------------------------------------------------------------
# Policy helper
# ---------------------------------------------------------------------------


def _policy(principal_id: str, effect: str, method_arn: str) -> dict:
    # Wildcard across all methods/stages so one cached policy covers the whole API.
    parts = method_arn.split(":")
    region, account = parts[3], parts[4]
    api_id = parts[5].split("/")[0]
    resource = f"arn:aws:execute-api:{region}:{account}:{api_id}/*/*"

    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource,
                }
            ],
        },
        # Passed to downstream Lambdas as event["requestContext"]["authorizer"].
        # Never include the credential here — context is visible in X-Ray traces.
        "context": {"user_id": principal_id},
    }
