"""T115 — Synapse / machine-key auth (FR-5). Cases ported from tests/unit/test_authorizer.py.

The Lambda authorizer returned an IAM policy; the port returns a Principal or raises
AuthDenied (-> 401) / AuthUnavailable (-> AUTH_TRANSIENT_STATUS). Policy-document cases
(`test_allow_policy_wildcards_api_resource`) have no equivalent and are not ported.
"""

import asyncio
import logging

import httpx
import pytest

from sagebrain_api.auth import AuthDenied, Authenticator, AuthUnavailable, Principal
from sagebrain_core import limits

from .conftest import AUTH_API, MACHINE_KEY, REPO_API, TEAM_ID, USER_ID, VALID_AUTH

MEMBER_USERPROFILE = (200, {"ownerId": "999", "userName": "testuser"})
ANON_USERPROFILE = (200, {})
IS_MEMBER = (200, {"isMember": True})
NOT_MEMBER = (200, {"isMember": False})
MEMBER_USERINFO_OIDC = (
    200,
    {"userid": "999", "sub": "opaque-pairwise-id", "email": "test@example.com"},
)
VIEW_SCOPE_FORBIDDEN = (403, {"reason": "insufficient scope"})


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def authenticator(synapse, clock):
    return Authenticator(
        client=httpx.AsyncClient(transport=httpx.MockTransport(synapse)),
        team_id=TEAM_ID,
        machine_api_key=MACHINE_KEY,
        repo_api=REPO_API,
        auth_api=AUTH_API,
        clock=clock,
    )


def authenticate(authenticator, headers):
    return asyncio.run(authenticator.authenticate(httpx.Headers(headers)))


def bearer(token="real-token"):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Allow path — PAT (view scope: /userProfile works directly)
# ---------------------------------------------------------------------------


def test_pat_member_is_allowed(authenticator, synapse):
    synapse.script = [MEMBER_USERPROFILE, IS_MEMBER]
    assert authenticate(authenticator, bearer()) == Principal("999", "bearer")
    assert synapse.paths() == [
        "/repo/v1/userProfile",
        f"/repo/v1/team/{TEAM_ID}/member/999/membershipStatus",
    ]
    # The token goes to /userProfile only; membershipStatus is public.
    assert synapse.calls[0].headers["authorization"] == "Bearer real-token"
    assert "authorization" not in synapse.calls[1].headers


# ---------------------------------------------------------------------------
# Allow path — OAuth token (openid only: /userProfile 403 -> OIDC fallback)
# ---------------------------------------------------------------------------


def test_oauth_openid_only_falls_back_to_oidc_userid(authenticator, synapse, caplog):
    synapse.script = [VIEW_SCOPE_FORBIDDEN, MEMBER_USERINFO_OIDC, IS_MEMBER]
    with caplog.at_level(logging.INFO, logger="sagebrain_api"):
        principal = authenticate(authenticator, bearer())
    # `userid`, never the pairwise-opaque `sub`.
    assert principal == Principal("999", "bearer")
    assert synapse.paths()[1] == "/auth/v1/oauth2/userinfo"
    assert '"event": "oidc_userinfo"' in caplog.text
    assert "opaque-pairwise-id" not in caplog.text


def test_oauth_openid_only_non_member_denied(authenticator, synapse):
    synapse.script = [VIEW_SCOPE_FORBIDDEN, MEMBER_USERINFO_OIDC, NOT_MEMBER]
    with pytest.raises(AuthDenied):
        authenticate(authenticator, bearer())


@pytest.mark.parametrize("status", [401, 403])
def test_oidc_rejects_token(authenticator, synapse, status):
    synapse.script = [VIEW_SCOPE_FORBIDDEN, (status, {})]
    with pytest.raises(AuthDenied):
        authenticate(authenticator, bearer())


def test_oidc_without_userid_denied(authenticator, synapse):
    synapse.script = [VIEW_SCOPE_FORBIDDEN, (200, {"sub": "opaque"})]
    with pytest.raises(AuthDenied):
        authenticate(authenticator, bearer())


# ---------------------------------------------------------------------------
# Deny path — expected auth failures
# ---------------------------------------------------------------------------


def test_no_owner_id_denied(authenticator, synapse):
    synapse.script = [ANON_USERPROFILE]
    with pytest.raises(AuthDenied):
        authenticate(authenticator, bearer())


def test_non_member_denied(authenticator, synapse):
    synapse.script = [MEMBER_USERPROFILE, NOT_MEMBER]
    with pytest.raises(AuthDenied):
        authenticate(authenticator, bearer())


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "notabearer"},
        {"Authorization": ""},
        {"Authorization": "Bearer "},
        {},
        {
            "Authorization": "ApiKey"
        },  # D-4: the old dummy header alone is not a credential
    ],
)
def test_missing_or_malformed_credentials_denied(authenticator, synapse, headers):
    with pytest.raises(AuthDenied):
        authenticate(authenticator, headers)
    assert synapse.calls == []


def test_synapse_401_on_profile_denied(authenticator, synapse):
    synapse.script = [(401, {})]
    with pytest.raises(AuthDenied):
        authenticate(authenticator, bearer())


@pytest.mark.parametrize("status", [400, 404])
def test_membership_400_or_404_denied(authenticator, synapse, status):
    synapse.script = [MEMBER_USERPROFILE, (status, {})]
    with pytest.raises(AuthDenied):
        authenticate(authenticator, bearer())


# ---------------------------------------------------------------------------
# Transient errors — AuthUnavailable (AUTH_TRANSIENT_STATUS), never cached
# ---------------------------------------------------------------------------


def test_synapse_error_on_membership_is_transient(authenticator, synapse):
    synapse.script = [MEMBER_USERPROFILE, (500, {})]
    with pytest.raises(AuthUnavailable):
        authenticate(authenticator, bearer())


def test_synapse_5xx_on_profile_is_transient(authenticator, synapse):
    synapse.script = [(500, {})]
    with pytest.raises(AuthUnavailable):
        authenticate(authenticator, bearer())


def test_network_error_is_transient(authenticator, synapse):
    synapse.script = [httpx.ConnectError("connection refused")]
    with pytest.raises(AuthUnavailable):
        authenticate(authenticator, bearer())


def test_synapse_timeout_is_transient_and_not_cached(authenticator, synapse):
    synapse.script = [httpx.ReadTimeout("slow")]
    with pytest.raises(AuthUnavailable):
        authenticate(authenticator, bearer("valid-token"))
    # Synapse recovers: the user gets in, because the failure wasn't cached.
    assert authenticate(authenticator, bearer("valid-token")).id == USER_ID


def test_each_synapse_call_times_out_after_5s(authenticator, synapse):
    synapse.script = [MEMBER_USERPROFILE, IS_MEMBER]
    authenticate(authenticator, bearer())
    for request in synapse.calls:
        timeout = request.extensions["timeout"]
        assert set(timeout.values()) == {limits.SYNAPSE_CALL_TIMEOUT_SECONDS}


def test_whole_auth_step_times_out(authenticator, synapse, monkeypatch):
    monkeypatch.setattr(limits, "AUTH_TIMEOUT_SECONDS", 0.05)

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"ownerId": USER_ID})

    authenticator._client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    with pytest.raises(AuthUnavailable):
        authenticate(authenticator, bearer())


def test_auth_timeout_is_the_spec_value():
    assert limits.AUTH_TIMEOUT_SECONDS == 10
    assert limits.SYNAPSE_CALL_TIMEOUT_SECONDS == 5


# ---------------------------------------------------------------------------
# Cache — successes only, 300 s, keyed by sha256(token)
# ---------------------------------------------------------------------------


def test_success_is_cached(authenticator, synapse):
    authenticate(authenticator, bearer("valid-token"))
    calls = len(synapse.calls)
    assert authenticate(authenticator, bearer("valid-token")).id == USER_ID
    assert len(synapse.calls) == calls


def test_cache_expires_after_ttl(authenticator, synapse, clock):
    authenticate(authenticator, bearer("valid-token"))
    clock.t += limits.AUTH_CACHE_TTL_SECONDS - 1
    authenticate(authenticator, bearer("valid-token"))
    calls = len(synapse.calls)
    clock.t += 2
    authenticate(authenticator, bearer("valid-token"))
    assert len(synapse.calls) == calls + 2


def test_denial_is_not_cached(authenticator, synapse):
    synapse.script = [MEMBER_USERPROFILE, NOT_MEMBER]
    with pytest.raises(AuthDenied):
        authenticate(authenticator, bearer("valid-token"))
    assert authenticate(authenticator, bearer("valid-token")).id == USER_ID


def test_cache_does_not_hold_raw_tokens(authenticator):
    authenticate(authenticator, bearer("valid-token"))
    assert "valid-token" not in repr(authenticator._cache._entries)


# ---------------------------------------------------------------------------
# Machine key (D-4)
# ---------------------------------------------------------------------------


def test_api_key_alone_is_enough(authenticator, synapse):
    principal = authenticate(authenticator, {"x-api-key": MACHINE_KEY})
    assert principal == Principal("machine", "apiKey")
    assert synapse.calls == []


def test_api_key_with_dummy_authorization_header_still_works(authenticator):
    headers = {"x-api-key": MACHINE_KEY, "Authorization": "ApiKey"}
    assert authenticate(authenticator, headers).id == "machine"


def test_wrong_api_key_rejected_even_with_valid_bearer(authenticator, synapse):
    with pytest.raises(AuthDenied):
        authenticate(authenticator, {"x-api-key": "wrong", **bearer("valid-token")})
    assert synapse.calls == []


def test_api_key_path_disabled_when_unconfigured(synapse):
    authenticator = Authenticator(
        client=httpx.AsyncClient(transport=httpx.MockTransport(synapse)),
        team_id=TEAM_ID,
        machine_api_key=None,
        repo_api=REPO_API,
        auth_api=AUTH_API,
    )
    with pytest.raises(AuthDenied):
        authenticate(authenticator, {"x-api-key": ""})
    with pytest.raises(AuthDenied):
        authenticate(authenticator, {"x-api-key": "anything"})


# ---------------------------------------------------------------------------
# Log hygiene — credentials never logged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("member", [True, False])
def test_token_never_logged(authenticator, synapse, caplog, member):
    synapse.script = [MEMBER_USERPROFILE, IS_MEMBER if member else NOT_MEMBER]
    secret = "super-secret-pat-value"
    with caplog.at_level(logging.DEBUG):
        try:
            authenticate(authenticator, bearer(secret))
        except AuthDenied:
            pass
    assert caplog.records, "expected auth_allow / auth_deny log lines"
    assert secret not in caplog.text


def test_api_key_never_logged(authenticator, caplog):
    with caplog.at_level(logging.DEBUG):
        authenticate(authenticator, {"x-api-key": MACHINE_KEY})
        with pytest.raises(AuthDenied):
            authenticate(authenticator, {"x-api-key": "wrong-key-value"})
    assert MACHINE_KEY not in caplog.text
    assert "wrong-key-value" not in caplog.text


# ---------------------------------------------------------------------------
# Over HTTP (D-10: every failure is 401 {"message": "Unauthorized"} with CORS)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer not-a-real-token"},
        {"Authorization": "Bearer outsider"},  # valid Synapse user, not in the team
        {"x-api-key": "wrong", **VALID_AUTH},
    ],
    ids=["none", "bad_bearer", "non_member", "bad_key_with_valid_bearer"],
)
def test_http_denial_is_401(client, headers):
    response = client.get("/ask/j1", headers=headers)
    assert response.status_code == 401
    assert response.json() == {"message": "Unauthorized"}
    assert response.headers["access-control-allow-origin"] == "*"


def test_http_transient_failure_uses_default_status(client, synapse):
    synapse.script = [(503, {})]
    response = client.get("/ask/j1", headers=VALID_AUTH)
    assert response.status_code == 500
    assert response.json() == {"message": "Internal server error"}
    assert response.headers["access-control-allow-origin"] == "*"


def test_http_transient_status_is_configurable(make_app, settings, synapse):
    from fastapi.testclient import TestClient

    app = make_app(settings=settings.model_copy(update={"auth_transient_status": 503}))
    synapse.script = [httpx.ConnectError("down")]
    with TestClient(app) as client:
        response = client.get("/ask/j1", headers=VALID_AUTH)
    assert response.status_code == 503
    assert response.json() == {"message": "Service Unavailable"}


def test_http_machine_key_alone(client):
    response = client.get("/query/j1", headers={"x-api-key": MACHINE_KEY})
    assert response.status_code == 404  # authenticated; the job just doesn't exist
