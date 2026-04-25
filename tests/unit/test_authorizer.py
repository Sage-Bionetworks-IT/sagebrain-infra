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
    return {"authorizationToken": token, "methodArn": METHOD_ARN}


MEMBER_PROFILE = {"ownerId": "999", "userName": "testuser"}
ANON_PROFILE = {"ownerId": "273950", "userName": "anonymous"}
IS_MEMBER = {"isMember": True}
NOT_MEMBER = {"isMember": False}


# ---------------------------------------------------------------------------
# Allow path
# ---------------------------------------------------------------------------


@patch("authorizer.urllib.request.urlopen")
def test_valid_member_returns_allow(mock_urlopen):
    mock_urlopen.side_effect = _urlopen_mock(MEMBER_PROFILE, IS_MEMBER)

    result = auth_module.handler(_event(), {})

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"
    assert result["principalId"] == "999"


@patch("authorizer.urllib.request.urlopen")
def test_allow_policy_wildcards_api_resource(mock_urlopen):
    mock_urlopen.side_effect = _urlopen_mock(MEMBER_PROFILE, IS_MEMBER)

    result = auth_module.handler(_event(), {})

    assert result["policyDocument"]["Statement"][0]["Resource"] == EXPECTED_RESOURCE


@patch("authorizer.urllib.request.urlopen")
def test_allow_context_contains_user_id(mock_urlopen):
    mock_urlopen.side_effect = _urlopen_mock(MEMBER_PROFILE, IS_MEMBER)

    result = auth_module.handler(_event(), {})

    assert result["context"]["user_id"] == "999"


# ---------------------------------------------------------------------------
# Deny path — expected auth failures
# ---------------------------------------------------------------------------


@patch("authorizer.urllib.request.urlopen")
def test_anonymous_profile_returns_deny(mock_urlopen):
    mock_urlopen.side_effect = _urlopen_mock(ANON_PROFILE)

    result = auth_module.handler(_event(), {})

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


@patch("authorizer.urllib.request.urlopen")
def test_non_member_returns_deny(mock_urlopen):
    mock_urlopen.side_effect = _urlopen_mock(MEMBER_PROFILE, NOT_MEMBER)

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
def test_synapse_401_on_membership_returns_deny(mock_urlopen):
    mock_urlopen.side_effect = _urlopen_mock(
        MEMBER_PROFILE,
        urllib.error.HTTPError(
            url=None, code=401, msg="Unauthorized", hdrs=None, fp=None
        ),
    )

    result = auth_module.handler(_event(), {})

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


# ---------------------------------------------------------------------------
# Transient errors — must propagate (not cached as Deny)
# ---------------------------------------------------------------------------


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

    mock_urlopen.side_effect = _urlopen_mock(ANON_PROFILE)
    secret_token = "super-secret-pat-value"

    with caplog.at_level(logging.WARNING, logger="root"):
        auth_module.handler(_event(token=f"Bearer {secret_token}"), {})

    for record in caplog.records:
        assert secret_token not in record.getMessage()


@patch("authorizer.urllib.request.urlopen")
def test_token_not_logged_on_allow(mock_urlopen, caplog):
    import logging

    mock_urlopen.side_effect = _urlopen_mock(MEMBER_PROFILE, IS_MEMBER)
    secret_token = "super-secret-pat-value"

    with caplog.at_level(logging.INFO, logger="root"):
        auth_module.handler(_event(token=f"Bearer {secret_token}"), {})

    for record in caplog.records:
        assert secret_token not in record.getMessage()
