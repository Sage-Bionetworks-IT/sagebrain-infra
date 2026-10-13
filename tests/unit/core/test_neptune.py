"""T103 — execute_query: SigV4 POST to Neptune, 60 s timeout, sparql_query audit log."""

import json
from unittest.mock import MagicMock, patch
from urllib.parse import unquote_plus

import pytest
import requests

from sagebrain_core import limits
from sagebrain_core.neptune import NeptuneConfig, execute_query

CONFIG = NeptuneConfig(endpoint="reader.example", region="us-east-1")


def _response(status=200, text='{"results":{"bindings":[]}}', content_type=None):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.headers = {"Content-Type": content_type} if content_type else {}
    if status >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            f"{status} Client Error", response=resp
        )
    return resp


@pytest.fixture
def post():
    with patch("sagebrain_core.neptune.requests.post") as post, patch(
        "sagebrain_core.neptune.SigV4Auth"
    ) as sigv4, patch("sagebrain_core.neptune.botocore.session.Session"):
        post.sigv4 = sigv4
        yield post


def _run(**overrides):
    kwargs = dict(
        config=CONFIG,
        job_id="job-1",
        source="direct",
        principal="3412345",
        source_ip="198.51.100.7",
        user_agent="ua/1",
    )
    kwargs.update(overrides)
    return execute_query("SELECT * WHERE { ?s ?p ?o } LIMIT 1", **kwargs)


def _audit_events(capsys):
    lines = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    return [e for e in map(json.loads, lines) if e.get("event") == "sparql_query"]


def test_posts_signed_form_to_sparql_endpoint(post):
    post.return_value = _response()
    _run()

    args, kwargs = post.call_args
    assert args[0] == "https://reader.example:8182/sparql"
    assert "SELECT * WHERE" in unquote_plus(kwargs["data"])
    assert kwargs["headers"]["Accept"] == "application/sparql-results+json"
    post.sigv4.assert_called_once()
    assert post.sigv4.call_args.args[1:] == ("neptune-db", "us-east-1")


def test_uses_spec_timeout(post):
    post.return_value = _response()
    _run()
    assert (
        post.call_args.kwargs["timeout"] == limits.NEPTUNE_QUERY_TIMEOUT_SECONDS == 60
    )


def test_returns_result(post):
    post.return_value = _response(text="RESULT", content_type="text/csv")
    result = _run()
    assert result.text == "RESULT"
    assert result.content_type == "text/csv"
    assert result.status_code == 200
    assert result.duration_ms >= 0


def test_content_type_defaults(post):
    post.return_value = _response()
    assert _run().content_type == "application/sparql-results+json"


def test_audit_event_fields(post, capsys):
    post.return_value = _response()
    _run(source="agent")
    (event,) = _audit_events(capsys)
    assert set(event) == {
        "event",
        "job_id",
        "query",
        "query_length",
        "source",
        "principal",
        "source_ip",
        "user_agent",
        "status_code",
        "duration_ms",
        "timestamp",
    }
    assert event["source"] == "agent"
    assert event["principal"] == "3412345"
    assert event["status_code"] == 200
    assert event["query_length"] == len("SELECT * WHERE { ?s ?p ?o } LIMIT 1")


def test_http_error_logged_with_neptune_status_and_reraised(post, capsys):
    post.return_value = _response(status=400)
    with pytest.raises(requests.exceptions.HTTPError):
        _run()
    (event,) = _audit_events(capsys)
    assert event["status_code"] == 400


def test_transport_error_logged_as_500_and_reraised(post, capsys):
    post.side_effect = requests.exceptions.ConnectTimeout("timed out")
    with pytest.raises(requests.exceptions.ConnectTimeout):
        _run()
    (event,) = _audit_events(capsys)
    assert event["status_code"] == 500


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("NEPTUNE_ENDPOINT", "ep.example")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    assert NeptuneConfig.from_env() == NeptuneConfig("ep.example", "eu-west-1")
