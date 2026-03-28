import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

LAMBDA_DIR = str(Path(__file__).parents[2] / "src" / "lambda")
if LAMBDA_DIR not in sys.path:
    sys.path.insert(0, LAMBDA_DIR)


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv(
        "NEPTUNE_ENDPOINT", "test-neptune.cluster.us-east-1.neptune.amazonaws.com"
    )
    monkeypatch.setenv("AWS_REGION", "us-east-1")


@pytest.fixture
def handler():
    sys.modules.pop("query", None)
    import query as q

    importlib.reload(q)
    return q.handler


def sparql_response(body: dict, status: int = 200):
    mock = MagicMock()
    mock.status_code = status
    mock.text = json.dumps(body)
    mock.headers = {"Content-Type": "application/sparql-results+json"}
    mock.raise_for_status = MagicMock()
    return mock


def make_event(body: dict | None = None):
    return {"body": json.dumps(body) if body is not None else None}


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_success_returns_200(mock_session, mock_sigv4, mock_post, handler):
    mock_post.return_value = sparql_response({"results": {"bindings": []}})

    response = handler(make_event({"query": "SELECT * WHERE { ?s ?p ?o } LIMIT 1"}), {})

    assert response["statusCode"] == 200


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_success_forwards_neptune_content_type(
    mock_session, mock_sigv4, mock_post, handler
):
    mock_post.return_value = sparql_response({"results": {"bindings": []}})

    response = handler(make_event({"query": "SELECT * WHERE { ?s ?p ?o } LIMIT 1"}), {})

    assert response["headers"]["Content-Type"] == "application/sparql-results+json"


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_success_includes_cors_header(mock_session, mock_sigv4, mock_post, handler):
    mock_post.return_value = sparql_response({"results": {"bindings": []}})

    response = handler(make_event({"query": "SELECT * WHERE { ?s ?p ?o } LIMIT 1"}), {})

    assert response["headers"]["Access-Control-Allow-Origin"] == "*"


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_default_query_used_when_none_provided(
    mock_session, mock_sigv4, mock_post, handler
):
    mock_post.return_value = sparql_response({"results": {"bindings": []}})

    response = handler(make_event({}), {})

    assert response["statusCode"] == 200
    called_body = (
        mock_post.call_args.kwargs.get("data") or mock_post.call_args.args[1]
        if mock_post.call_args.args
        else mock_post.call_args.kwargs["data"]
    )
    assert "SELECT" in called_body


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_null_body_uses_default_query(mock_session, mock_sigv4, mock_post, handler):
    mock_post.return_value = sparql_response({"results": {"bindings": []}})

    response = handler({"body": None}, {})

    assert response["statusCode"] == 200


# ---------------------------------------------------------------------------
# Query length guard
# ---------------------------------------------------------------------------


def test_query_exceeding_max_length_returns_400(handler):
    response = handler(make_event({"query": "S" * 8001}), {})

    assert response["statusCode"] == 400
    assert "exceeds maximum length" in json.loads(response["body"])["error"]


def test_query_exceeding_max_length_includes_cors(handler):
    response = handler(make_event({"query": "S" * 8001}), {})

    assert response["headers"]["Access-Control-Allow-Origin"] == "*"


def test_query_at_exact_max_length_is_accepted(handler):
    with patch("query.requests.post") as mock_post, patch("query.SigV4Auth"), patch(
        "query.botocore.session.Session"
    ):
        mock_post.return_value = sparql_response({"results": {"bindings": []}})
        response = handler(make_event({"query": "S" * 8000}), {})

    assert response["statusCode"] == 200


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_neptune_called_with_post(mock_session, mock_sigv4, mock_post, handler):
    mock_post.return_value = sparql_response({"results": {"bindings": []}})

    handler(make_event({"query": "SELECT * WHERE { ?s ?p ?o } LIMIT 1"}), {})

    mock_post.assert_called_once()
    # requests.post is the only HTTP method used — assert no other method called
    assert mock_post.call_count == 1


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_sigv4_auth_is_applied(mock_session, mock_sigv4, mock_post, handler):
    mock_post.return_value = sparql_response({"results": {"bindings": []}})

    handler(make_event({"query": "SELECT * WHERE { ?s ?p ?o } LIMIT 1"}), {})

    mock_sigv4.assert_called_once()
    mock_sigv4.return_value.add_auth.assert_called_once()


# ---------------------------------------------------------------------------
# Invalid JSON body
# ---------------------------------------------------------------------------


def test_invalid_json_body_returns_400(handler):
    response = handler({"body": "not valid json"}, {})

    assert response["statusCode"] == 400
    assert "valid JSON" in json.loads(response["body"])["error"]


def test_invalid_json_includes_cors(handler):
    response = handler({"body": "not valid json"}, {})

    assert response["headers"]["Access-Control-Allow-Origin"] == "*"


# ---------------------------------------------------------------------------
# Neptune HTTP error
# ---------------------------------------------------------------------------


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_neptune_http_error_returns_upstream_status(
    mock_session, mock_sigv4, mock_post, handler
):
    import requests

    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
        "bad request", response=mock_resp
    )
    mock_post.return_value = mock_resp

    response = handler(make_event({"query": "SELECT * WHERE { ?s ?p ?o }"}), {})

    assert response["statusCode"] == 400
    assert response["headers"]["Access-Control-Allow-Origin"] == "*"


# ---------------------------------------------------------------------------
# Unexpected exception
# ---------------------------------------------------------------------------


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_unexpected_exception_returns_500(mock_session, mock_sigv4, mock_post, handler):
    mock_post.side_effect = Exception("connection refused")

    response = handler(make_event({"query": "SELECT * WHERE { ?s ?p ?o }"}), {})

    assert response["statusCode"] == 500
    assert "connection refused" in json.loads(response["body"])["error"]
    assert response["headers"]["Access-Control-Allow-Origin"] == "*"
