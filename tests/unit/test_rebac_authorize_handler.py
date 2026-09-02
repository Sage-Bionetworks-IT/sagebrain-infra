import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

LAMBDA_DIR = str(Path(__file__).parents[2] / "src" / "lambda_rebac")
if LAMBDA_DIR not in sys.path:
    sys.path.insert(0, LAMBDA_DIR)


@pytest.fixture
def module(monkeypatch):
    monkeypatch.setenv(
        "NEPTUNE_ENDPOINT", "test-neptune.cluster.us-east-1.neptune.amazonaws.com"
    )
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AVP_POLICY_STORE_ID", "ps-0123456789abcdef")
    monkeypatch.setenv("AVP_NAMESPACE", "SageBrain")
    monkeypatch.setenv("INFERRED_EDGE_MODE", "intersection")

    sys.modules.pop("authorize", None)
    with patch("boto3.client") as mock_client:
        mock_avp = MagicMock()
        mock_client.return_value = mock_avp
        import authorize as auth

        importlib.reload(auth)
        yield auth, mock_avp


def _event(body: dict) -> dict:
    return {"body": json.dumps(body)}


def _neptune_response(bindings: list[dict]):
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"results": {"bindings": bindings}}
    return mock_resp


def _grant_row(
    principal: str = "https://sagebionetworks.org/governance/principal-9000001",
    permission: str = "https://sagebionetworks.org/governance/DOWNLOAD",
    binding_type: str = "https://sagebionetworks.org/governance/Direct",
):
    return {
        "grant": {"value": "https://sagebionetworks.org/governance/grant-001"},
        "grantPrincipal": {"value": principal},
        "permission": {"value": permission},
        "bindingType": {"value": binding_type},
        "accessRequirement": {"value": "https://sagebionetworks.org/governance/AR-42"},
    }


def _patch_auth_primitives(auth):
    session = MagicMock()
    session.get_credentials.return_value = MagicMock()
    return patch("authorize.botocore.session.Session", return_value=session), patch(
        "authorize.SigV4Auth"
    )


def test_allows_when_grant_matches_principal_action(module):
    auth, mock_avp = module
    mock_avp.is_authorized.return_value = {
        "decision": "ALLOW",
        "determiningPolicies": [],
    }
    session_patch, sigv4_patch = _patch_auth_primitives(auth)

    with session_patch, sigv4_patch, patch("authorize.requests.post") as mock_post:
        mock_post.return_value = _neptune_response([_grant_row()])
        response = auth.handler(
            _event(
                {
                    "principal_id": "9000001",
                    "action": "DOWNLOAD",
                    "resource_id": "syn10081783",
                }
            ),
            {},
        )

    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["decision"] == "ALLOW"
    mock_avp.is_authorized.assert_called_once()


def test_denies_when_no_matching_governance_grant(module):
    auth, mock_avp = module
    session_patch, sigv4_patch = _patch_auth_primitives(auth)

    with session_patch, sigv4_patch, patch("authorize.requests.post") as mock_post:
        mock_post.return_value = _neptune_response(
            [
                _grant_row(
                    principal="https://sagebionetworks.org/governance/principal-123"
                )
            ]
        )
        response = auth.handler(
            _event(
                {
                    "principal_id": "9000001",
                    "action": "DOWNLOAD",
                    "resource_id": "syn10081783",
                }
            ),
            {},
        )

    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["decision"] == "DENY"
    assert body["reason"] == "no_matching_governance_grant"
    mock_avp.is_authorized.assert_not_called()


@pytest.mark.parametrize(
    "inferred_edge_mode,avp_decisions,expected",
    [
        ("intersection", ["ALLOW", "DENY"], "DENY"),
        ("union", ["ALLOW", "DENY"], "ALLOW"),
    ],
)
def test_inferred_edge_modes_merge_decisions(
    module, inferred_edge_mode, avp_decisions, expected
):
    auth, mock_avp = module
    mock_avp.is_authorized.side_effect = [
        {"decision": decision, "determiningPolicies": []} for decision in avp_decisions
    ]
    session_patch, sigv4_patch = _patch_auth_primitives(auth)

    with session_patch, sigv4_patch, patch("authorize.requests.post") as mock_post:
        mock_post.return_value = _neptune_response(
            [
                _grant_row(
                    binding_type="https://sagebionetworks.org/governance/Direct"
                ),
                _grant_row(
                    binding_type="https://sagebionetworks.org/governance/Inferred"
                ),
            ]
        )
        response = auth.handler(
            _event(
                {
                    "principal_id": "9000001",
                    "action": "DOWNLOAD",
                    "resource_id": "syn10081783",
                    "inferred_edge_mode": inferred_edge_mode,
                }
            ),
            {},
        )

    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["decision"] == expected


def test_fails_closed_when_avp_is_unavailable(module):
    auth, mock_avp = module
    mock_avp.is_authorized.side_effect = ClientError(
        {
            "Error": {
                "Code": "InternalServerException",
                "Message": "service unavailable",
            }
        },
        "IsAuthorized",
    )
    session_patch, sigv4_patch = _patch_auth_primitives(auth)

    with session_patch, sigv4_patch, patch("authorize.requests.post") as mock_post:
        mock_post.return_value = _neptune_response([_grant_row()])
        response = auth.handler(
            _event(
                {
                    "principal_id": "9000001",
                    "action": "DOWNLOAD",
                    "resource_id": "syn10081783",
                }
            ),
            {},
        )

    body = json.loads(response["body"])
    assert response["statusCode"] == 503
    assert body["decision"] == "DENY"
    assert body["reason"] == "authorization_unavailable"
