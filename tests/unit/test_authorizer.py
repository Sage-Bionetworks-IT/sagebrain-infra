import json
import os
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# SYNAPSE_TEAM_ID is read at import time, so set it before the first import.
os.environ.setdefault("SYNAPSE_TEAM_ID", "273957")

LAMBDA_DIR = str(Path(__file__).parents[2] / "src" / "lambda_authorizer")
if LAMBDA_DIR not in sys.path:
    sys.path.insert(0, LAMBDA_DIR)

import authorizer as auth_module  # noqa: E402


@pytest.fixture(autouse=True)
def clear_token_cache():
    auth_module._token_cache.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

METHOD_ARN = "arn:aws:execute-api:us-east-1:123456789012:abc123/prod/POST/ask"
EXPECTED_RESOURCE = "arn:aws:execute-api:us-east-1:123456789012:abc123/*/*"


def _urlopen_mock(*responses):
    """
    Return a side_effect list for urlopen, where each item is either:
      - a dict  → successful 200 response with that JSON body
      - an exception instance → raised directly
    """
    side_effects = []
    for resp in responses:
        if isinstance(resp, Exception):
            side_effects.append(resp)
        else:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(resp).encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            side_effects.append(mock_resp)
    return side_effects


def _event(token="Bearer real-token"):
    # REQUEST authorizer: credentials arrive in event["headers"]
    return {"headers": {"authorization": token}, "methodArn": METHOD_ARN}


# /userProfile response shapes (PATs and OAuth tokens with 'view' scope)
MEMBER_USERPROFILE = {"ownerId": "999", "userName": "testuser"}
ANON_USERPROFILE = {}  # no ownerId → treated as unauthenticated

IS_MEMBER = {"isMember": True}
NOT_MEMBER = {"isMember": False}

# OIDC userinfo response shape (OAuth tokens with 'openid' scope, no 'view')
# sub is pairwise-opaque; userid is the numeric Synapse ID.
MEMBER_USERINFO_OIDC = {
    "userid": "999",
    "sub": "opaque-pairwise-id",
    "email": "test@example.com",
}


def _view_scope_forbidden():
    """403 from /userProfile when the OAuth token lacks 'view' scope."""
    return urllib.error.HTTPError(
        url=None, code=403, msg="Forbidden", hdrs=None, fp=None
    )


# ---------------------------------------------------------------------------
# Allow path — PAT (view scope: /userProfile works directly)
# ---------------------------------------------------------------------------


@patch("authorizer.urllib.request.urlopen")
def test_pat_member_returns_allow(mock_urlopen):
    mock_urlopen.side_effect = _urlopen_mock(MEMBER_USERPROFILE, IS_MEMBER)

    result = auth_module.handler(_event(), {})

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"
    assert result["principalId"] == "999"


@patch("authorizer.urllib.request.urlopen")
def test_allow_policy_wildcards_api_resource(mock_urlopen):
    mock_urlopen.side_effect = _urlopen_mock(MEMBER_USERPROFILE, IS_MEMBER)

    result = auth_module.handler(_event(), {})

    assert result["policyDocument"]["Statement"][0]["Resource"] == EXPECTED_RESOURCE


@patch("authorizer.urllib.request.urlopen")
def test_allow_context_contains_user_id(mock_urlopen):
    mock_urlopen.side_effect = _urlopen_mock(MEMBER_USERPROFILE, IS_MEMBER)

    result = auth_module.handler(_event(), {})

    assert result["context"]["user_id"] == "999"


# ---------------------------------------------------------------------------
# Allow path — OAuth token (openid only: /userProfile 403 → OIDC fallback)
# ---------------------------------------------------------------------------


@patch("authorizer.urllib.request.urlopen")
def test_oauth_openid_only_falls_back_to_oidc_and_allows(mock_urlopen):
    """OAuth token without 'view' scope: /userProfile 403 → OIDC userid → allow."""
    mock_urlopen.side_effect = _urlopen_mock(
        _view_scope_forbidden(), MEMBER_USERINFO_OIDC, IS_MEMBER
    )

    result = auth_module.handler(_event(), {})

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"
    assert result["principalId"] == "999"


@patch("authorizer.urllib.request.urlopen")
def test_oauth_openid_only_non_member_denied(mock_urlopen):
    """OAuth token falls back to OIDC but user is not in team → deny."""
    mock_urlopen.side_effect = _urlopen_mock(
        _view_scope_forbidden(), MEMBER_USERINFO_OIDC, NOT_MEMBER
    )

    result = auth_module.handler(_event(), {})

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


# ---------------------------------------------------------------------------
# Deny path — expected auth failures
# ---------------------------------------------------------------------------


@patch("authorizer.urllib.request.urlopen")
def test_no_owner_id_returns_deny(mock_urlopen):
    mock_urlopen.side_effect = _urlopen_mock(ANON_USERPROFILE)

    result = auth_module.handler(_event(), {})

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


@patch("authorizer.urllib.request.urlopen")
def test_non_member_returns_deny(mock_urlopen):
    mock_urlopen.side_effect = _urlopen_mock(MEMBER_USERPROFILE, NOT_MEMBER)

    result = auth_module.handler(_event(), {})

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


def test_missing_bearer_prefix_returns_deny():
    result = auth_module.handler(_event(token="notabearer"), {})

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


def test_empty_token_returns_deny():
    result = auth_module.handler(_event(token=""), {})

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


@patch("authorizer.urllib.request.urlopen")
def test_synapse_401_on_profile_returns_deny(mock_urlopen):
    mock_urlopen.side_effect = _urlopen_mock(
        urllib.error.HTTPError(
            url=None, code=401, msg="Unauthorized", hdrs=None, fp=None
        )
    )

    result = auth_module.handler(_event(), {})

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


@patch("authorizer.urllib.request.urlopen")
def test_membership_400_returns_deny(mock_urlopen):
    # Synapse returns 400 when the userId is not found or malformed.
    # Treat as a clean deny rather than crashing into a 500.
    mock_urlopen.side_effect = _urlopen_mock(
        MEMBER_USERPROFILE,
        urllib.error.HTTPError(
            url=None, code=400, msg="Bad Request", hdrs=None, fp=None
        ),
    )

    result = auth_module.handler(_event(), {})

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


# ---------------------------------------------------------------------------
# Transient errors — must propagate (not cached as Deny)
# ---------------------------------------------------------------------------


@patch("authorizer.urllib.request.urlopen")
def test_synapse_error_on_membership_propagates(mock_urlopen):
    # 5xx errors on the membership endpoint are unexpected transient failures and
    # should propagate so valid users aren't locked out during a Synapse outage.
    mock_urlopen.side_effect = _urlopen_mock(
        MEMBER_USERPROFILE,
        urllib.error.HTTPError(
            url=None, code=500, msg="Internal Server Error", hdrs=None, fp=None
        ),
    )

    with pytest.raises(urllib.error.HTTPError):
        auth_module.handler(_event(), {})


@patch("authorizer.urllib.request.urlopen")
def test_synapse_5xx_propagates(mock_urlopen):
    mock_urlopen.side_effect = _urlopen_mock(
        urllib.error.HTTPError(
            url=None, code=500, msg="Internal Server Error", hdrs=None, fp=None
        )
    )

    with pytest.raises(urllib.error.HTTPError):
        auth_module.handler(_event(), {})


@patch("authorizer.urllib.request.urlopen")
def test_network_error_propagates(mock_urlopen):
    mock_urlopen.side_effect = _urlopen_mock(
        urllib.error.URLError("connection timed out")
    )

    with pytest.raises(urllib.error.URLError):
        auth_module.handler(_event(), {})


# ---------------------------------------------------------------------------
# Token safety — token must never appear in log output
# ---------------------------------------------------------------------------


@patch("authorizer.urllib.request.urlopen")
def test_token_not_logged_on_deny(mock_urlopen, caplog):
    import logging

    mock_urlopen.side_effect = _urlopen_mock(ANON_USERPROFILE)
    secret_token = "super-secret-pat-value"

    with caplog.at_level(logging.WARNING, logger="root"):
        auth_module.handler(_event(token=f"Bearer {secret_token}"), {})

    for record in caplog.records:
        assert secret_token not in record.getMessage()


@patch("authorizer.urllib.request.urlopen")
def test_token_not_logged_on_allow(mock_urlopen, caplog):
    import logging

    mock_urlopen.side_effect = _urlopen_mock(MEMBER_USERPROFILE, IS_MEMBER)
    secret_token = "super-secret-pat-value"

    with caplog.at_level(logging.INFO, logger="root"):
        auth_module.handler(_event(token=f"Bearer {secret_token}"), {})

    for record in caplog.records:
        assert secret_token not in record.getMessage()
