"""T103 — execute_query: SigV4 POST to Neptune, 60 s timeout, sparql_query audit log."""

import json
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, unquote_plus

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


# ---------------------------------------------------------------------------
# Spec 004: graph scoping (FR-11, FR-13, FR-16, D-1, D-8)
# ---------------------------------------------------------------------------

SCOPED = NeptuneConfig(
    endpoint="reader.example",
    region="us-east-1",
    graph_scoping=True,
    registry_table="app-dev-neptune-pipeline-loads",
)
LIVE = ["urn:sagebrain:nf:2026-07-28", "urn:sagebrain:reactome:v97"]


@pytest.fixture
def live():
    with patch("sagebrain_core.neptune.live_graphs", return_value=LIVE) as live:
        yield live


def _form(post):
    return parse_qs(post.call_args.kwargs["data"])


def test_unscoped_request_sends_only_the_query(post, capsys):
    """D-1 old behaviour: no dataset, so Neptune reads the union of every named graph."""
    post.return_value = _response()
    _run()
    assert set(_form(post)) == {"query"}
    (event,) = _audit_events(capsys)
    assert "graphs" not in event


def test_scoped_request_sends_each_graph_as_both_lists(post, live):
    post.return_value = _response()
    _run(config=SCOPED)

    live.assert_called_once_with("app-dev-neptune-pipeline-loads")
    form = _form(post)
    assert form["query"] == ["SELECT * WHERE { ?s ?p ?o } LIMIT 1"]
    # Both: default-graph-uri alone empties GRAPH patterns, named-graph-uri alone empties
    # the default graph (research Q4).
    assert form["default-graph-uri"] == LIVE
    assert form["named-graph-uri"] == LIVE


def test_scoped_request_is_signed_over_the_dataset(post, live):
    post.return_value = _response()
    with patch("sagebrain_core.neptune.AWSRequest") as aws_request:
        aws_request.return_value.headers = {}
        _run(config=SCOPED)
    signed_body = aws_request.call_args.kwargs["data"]
    assert signed_body == post.call_args.kwargs["data"]
    assert "default-graph-uri" in signed_body


def test_scoped_audit_event_records_graphs(post, live, capsys):
    """D-8: every sparql_query line records which graphs the query could see."""
    post.return_value = _response()
    _run(config=SCOPED)
    (event,) = _audit_events(capsys)
    assert event["graphs"] == LIVE


def test_registry_error_fails_the_query_without_calling_neptune(post, capsys):
    # FR-13: fail closed — never fall back to the union.
    with patch(
        "sagebrain_core.neptune.live_graphs", side_effect=RuntimeError("registry down")
    ):
        with pytest.raises(RuntimeError):
            _run(config=SCOPED)
    post.assert_not_called()
    (event,) = _audit_events(capsys)
    assert event["status_code"] == 500
    assert event["graphs"] is None


def test_scoping_config_from_env(monkeypatch):
    monkeypatch.setenv("NEPTUNE_ENDPOINT", "ep.example")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("GRAPH_SCOPING", "true")
    monkeypatch.setenv("LOAD_TABLE_NAME", "loads")
    config = NeptuneConfig.from_env()
    assert config.graph_scoping is True
    assert config.registry_table == "loads"


@pytest.mark.parametrize("value", [None, "false", "False", "1", ""])
def test_scoping_is_off_unless_exactly_true(monkeypatch, value):
    monkeypatch.setenv("NEPTUNE_ENDPOINT", "ep.example")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    if value is None:
        monkeypatch.delenv("GRAPH_SCOPING", raising=False)
    else:
        monkeypatch.setenv("GRAPH_SCOPING", value)
    assert NeptuneConfig.from_env().graph_scoping is False
