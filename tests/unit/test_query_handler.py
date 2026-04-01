import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

LAMBDA_DIR = str(Path(__file__).parents[2] / "src" / "lambda")
if LAMBDA_DIR not in sys.path:
    sys.path.insert(0, LAMBDA_DIR)


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv(
        "NEPTUNE_ENDPOINT", "test-neptune.cluster.us-east-1.neptune.amazonaws.com"
    )
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("JOB_TABLE_NAME", "test-job-table")


@pytest.fixture
def mock_table():
    table = MagicMock()
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = table
        yield table


@pytest.fixture
def handler(mock_table):
    sys.modules.pop("query", None)
    import query as q

    importlib.reload(q)
    q._dynamodb.Table.return_value = mock_table
    return q.handler


def sparql_response(body: dict, status: int = 200):
    mock = MagicMock()
    mock.status_code = status
    mock.text = json.dumps(body)
    mock.headers = {"Content-Type": "application/sparql-results+json"}
    mock.raise_for_status = MagicMock()
    return mock


def make_sqs_event(*jobs):
    """Build a minimal SQS event with one record per job dict."""
    return {"Records": [{"body": json.dumps(job)} for job in jobs]}


def default_job(**overrides):
    base = {
        "job_id": "abc-123",
        "query": "SELECT * WHERE { ?s ?p ?o } LIMIT 5",
        "source": "direct",
        "source_ip": "1.2.3.4",
        "user_agent": "test-agent",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Status transitions — pending → running → complete
# ---------------------------------------------------------------------------


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_sets_running_before_neptune_call(
    mock_session, mock_sigv4, mock_post, handler, mock_table
):
    mock_post.return_value = sparql_response({"results": {"bindings": []}})

    handler(make_sqs_event(default_job()), {})

    calls = mock_table.update_item.call_args_list
    # First update must be status=running
    first_values = calls[0].kwargs["ExpressionAttributeValues"]
    assert first_values[":status"] == "running"


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_sets_complete_with_results_on_success(
    mock_session, mock_sigv4, mock_post, handler, mock_table
):
    result_body = {"results": {"bindings": [{"s": {"value": "x"}}]}}
    mock_post.return_value = sparql_response(result_body)

    handler(make_sqs_event(default_job()), {})

    calls = mock_table.update_item.call_args_list
    final_values = calls[-1].kwargs["ExpressionAttributeValues"]
    assert final_values[":status"] == "complete"
    assert json.loads(final_values[":results"]) == result_body


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_sets_complete_with_content_type(
    mock_session, mock_sigv4, mock_post, handler, mock_table
):
    mock_post.return_value = sparql_response({"results": {"bindings": []}})

    handler(make_sqs_event(default_job()), {})

    final_values = mock_table.update_item.call_args_list[-1].kwargs[
        "ExpressionAttributeValues"
    ]
    assert final_values[":content_type"] == "application/sparql-results+json"


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_sets_error_on_neptune_http_error(
    mock_session, mock_sigv4, mock_post, handler, mock_table
):
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.headers = {}
    mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
        "bad request", response=mock_resp
    )
    mock_post.return_value = mock_resp

    with pytest.raises(requests.exceptions.HTTPError):  # noqa: F821
        handler(make_sqs_event(default_job()), {})

    final_values = mock_table.update_item.call_args_list[-1].kwargs[
        "ExpressionAttributeValues"
    ]
    assert final_values[":status"] == "error"
    assert "bad request" in final_values[":error"]


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_sets_error_on_unexpected_exception(
    mock_session, mock_sigv4, mock_post, handler, mock_table
):
    mock_post.side_effect = Exception("connection refused")

    with pytest.raises(Exception, match="connection refused"):
        handler(make_sqs_event(default_job()), {})

    final_values = mock_table.update_item.call_args_list[-1].kwargs[
        "ExpressionAttributeValues"
    ]
    assert final_values[":status"] == "error"
    assert "connection refused" in final_values[":error"]


# ---------------------------------------------------------------------------
# DynamoDB key — job_id is threaded through correctly
# ---------------------------------------------------------------------------


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_update_item_uses_correct_job_id(
    mock_session, mock_sigv4, mock_post, handler, mock_table
):
    mock_post.return_value = sparql_response({"results": {"bindings": []}})

    handler(make_sqs_event(default_job(job_id="job-xyz")), {})

    for c in mock_table.update_item.call_args_list:
        assert c.kwargs["Key"] == {"job_id": "job-xyz"}


# ---------------------------------------------------------------------------
# Neptune request construction
# ---------------------------------------------------------------------------


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_neptune_called_with_sparql_query(
    mock_session, mock_sigv4, mock_post, handler, mock_table
):
    mock_post.return_value = sparql_response({"results": {"bindings": []}})
    query_str = "SELECT * WHERE { ?s ?p ?o } LIMIT 5"

    handler(make_sqs_event(default_job(query=query_str)), {})

    from urllib.parse import unquote_plus

    mock_post.assert_called_once()
    sent_body = mock_post.call_args.kwargs.get("data") or mock_post.call_args[1].get(
        "data"
    )
    assert query_str in unquote_plus(sent_body)


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_sigv4_auth_applied(mock_session, mock_sigv4, mock_post, handler, mock_table):
    mock_post.return_value = sparql_response({"results": {"bindings": []}})

    handler(make_sqs_event(default_job()), {})

    mock_sigv4.assert_called_once()
    mock_sigv4.return_value.add_auth.assert_called_once()


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_neptune_url_uses_endpoint_env_var(
    mock_session, mock_sigv4, mock_post, handler, mock_table
):
    mock_post.return_value = sparql_response({"results": {"bindings": []}})

    handler(make_sqs_event(default_job()), {})

    called_url = mock_post.call_args.kwargs.get("url") or mock_post.call_args[0][0]
    assert "test-neptune.cluster.us-east-1.neptune.amazonaws.com" in called_url
    assert called_url.endswith("/sparql")


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_duration_ms_stored_as_decimal(
    mock_session, mock_sigv4, mock_post, handler, mock_table
):
    """DynamoDB rejects plain floats — _update_job must convert them to Decimal."""
    from decimal import Decimal

    mock_post.return_value = sparql_response({"results": {"bindings": []}})

    handler(make_sqs_event(default_job()), {})

    final_values = mock_table.update_item.call_args_list[-1].kwargs[
        "ExpressionAttributeValues"
    ]
    assert isinstance(final_values[":duration_ms"], Decimal)


# ---------------------------------------------------------------------------
# Optional SQS message fields default correctly
# ---------------------------------------------------------------------------


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_missing_optional_fields_default(
    mock_session, mock_sigv4, mock_post, handler, mock_table
):
    """source/source_ip/user_agent are optional in the SQS message."""
    mock_post.return_value = sparql_response({"results": {"bindings": []}})
    minimal_job = {"job_id": "min-1", "query": "SELECT * WHERE { ?s ?p ?o } LIMIT 1"}

    # Should not raise
    handler(make_sqs_event(minimal_job), {})

    final_values = mock_table.update_item.call_args_list[-1].kwargs[
        "ExpressionAttributeValues"
    ]
    assert final_values[":status"] == "complete"


# ---------------------------------------------------------------------------
# Batch: multiple SQS records processed in one invocation
# ---------------------------------------------------------------------------


@patch("query.requests.post")
@patch("query.SigV4Auth")
@patch("query.botocore.session.Session")
def test_processes_multiple_records(
    mock_session, mock_sigv4, mock_post, handler, mock_table
):
    mock_post.return_value = sparql_response({"results": {"bindings": []}})
    job_a = default_job(job_id="job-a")
    job_b = default_job(job_id="job-b")

    handler(make_sqs_event(job_a, job_b), {})

    assert mock_post.call_count == 2
    job_ids_written = [
        c.kwargs["Key"]["job_id"] for c in mock_table.update_item.call_args_list
    ]
    assert "job-a" in job_ids_written
    assert "job-b" in job_ids_written
