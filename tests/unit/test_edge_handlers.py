"""Edge-stack submit/status handlers (src/lambda_edge/jobs.py) against the shared vectors.

Every handler case is checked against `expect`, which carries the spec 001 deviations.
"""

import base64
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.contract.cases import (
    assert_subset,
    check_response,
    job_id_for,
    load_cases,
    request_body,
)

JOBS_PY = Path(__file__).parents[2] / "src" / "lambda_edge" / "jobs.py"
HANDLERS = {
    ("query", "POST"): "submit_query",
    ("query", "GET"): "query_status",
    ("ask", "POST"): "submit_question",
    ("ask", "GET"): "ask_status",
}

USER_ID = "3412345"
SOURCE_IP = "198.51.100.7"


@pytest.fixture
def jobs(monkeypatch):
    """jobs.py with its DynamoDB table and SQS client replaced by mocks."""
    spec = importlib.util.spec_from_file_location("edge_jobs_under_test", JOBS_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    table, sqs = MagicMock(), MagicMock()
    dynamodb = MagicMock()
    dynamodb.Table.return_value = table
    monkeypatch.setattr(module, "_dynamodb", lambda: dynamodb)
    monkeypatch.setattr(module, "_sqs", lambda: sqs)
    module.table, module.sqs, module.dynamodb = table, sqs, dynamodb
    return module


def _event(case, job_id):
    req = case["request"]
    event = {
        "httpMethod": req["method"],
        "headers": dict(req.get("headers") or {}),
        "body": request_body(case) if req["method"] == "POST" else None,
        "requestContext": {
            "authorizer": {"user_id": USER_ID},
            "identity": {"sourceIp": SOURCE_IP},
        },
    }
    if req["method"] == "GET":
        event["pathParameters"] = {"job_id": job_id}
    return event


def _invoke(jobs, case, monkeypatch, event=None):
    api = case["api"]
    monkeypatch.setenv("JOB_TABLE_NAME", f"{api}-jobs")
    monkeypatch.setenv("JOB_QUEUE_URL", f"https://sqs.example/{api}")
    item = (case.get("given") or {}).get("item")
    jobs.table.get_item.return_value = {"Item": item} if item else {}
    handler = getattr(jobs, HANDLERS[(api, case["request"]["method"])])
    return handler(event or _event(case, job_id_for(case)), None)


CASES = load_cases(layer="handler")


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_edge_handler_matches_contract(case, jobs, monkeypatch):
    expect = case["expect"]
    response = _invoke(jobs, case, monkeypatch)
    body = json.loads(response["body"])
    check_response(expect, response["statusCode"], body, response["headers"])
    assert response["headers"]["Access-Control-Allow-Origin"] == "*"
    assert response["headers"]["Content-Type"] == "application/json"

    if response["statusCode"] != 202:
        jobs.table.put_item.assert_not_called()
        jobs.sqs.send_message.assert_not_called()
        return

    jobs.dynamodb.Table.assert_called_with(f"{case['api']}-jobs")
    stored = jobs.table.put_item.call_args.kwargs["Item"]
    message = jobs.sqs.send_message.call_args.kwargs
    enqueued = json.loads(message["MessageBody"])

    assert message["QueueUrl"] == f"https://sqs.example/{case['api']}"
    assert stored["job_id"] == body["job_id"] == enqueued["job_id"]
    if "user_id" in enqueued:
        assert enqueued["user_id"] == USER_ID
    if "stored_subset" in expect:
        assert_subset(expect["stored_subset"], stored, "stored item")
    if "enqueued_subset" in expect:
        assert_subset(expect["enqueued_subset"], enqueued, "SQS message")
    if "enqueued_keys" in expect:
        assert sorted(enqueued) == sorted(expect["enqueued_keys"]), sorted(enqueued)


# ---------------------------------------------------------------------------
# Exact storage / message shapes (the data-model contract the workers rely on)
# ---------------------------------------------------------------------------


def _submit(jobs, api, monkeypatch, body=None, headers=None, raw=None):
    case = {
        "api": api,
        "request": {"method": "POST", "path": f"/{api}", "headers": headers},
    }
    if raw is not None:
        case["request"]["body"] = raw
    else:
        case["request"]["json"] = body
    return _invoke(jobs, case, monkeypatch)


def test_query_job_item_and_message_shape(jobs, monkeypatch):
    monkeypatch.setattr("time.time", lambda: 1_700_000_000.5)
    _submit(jobs, "query", monkeypatch, {"query": "SELECT 1"}, {"User-Agent": "ua/1"})

    item = jobs.table.put_item.call_args.kwargs["Item"]
    assert item == {
        "job_id": item["job_id"],
        "status": "pending",
        "created_at": 1_700_000_000,
        "ttl": 1_700_000_000 + 86400,
    }
    message = json.loads(jobs.sqs.send_message.call_args.kwargs["MessageBody"])
    assert message == {
        "job_id": item["job_id"],
        "query": "SELECT 1",
        "source": "direct",
        "source_ip": SOURCE_IP,
        "user_agent": "ua/1",
        "user_id": USER_ID,
    }


def test_ask_job_item_and_message_carry_no_credential(jobs, monkeypatch):
    monkeypatch.setattr("time.time", lambda: 1_700_000_000.5)
    _submit(
        jobs,
        "ask",
        monkeypatch,
        {"question": "Why?"},
        {"Authorization": "Bearer secret-token", "x-api-key": "secret-key"},
    )

    item = jobs.table.put_item.call_args.kwargs["Item"]
    assert item == {
        "job_id": item["job_id"],
        "status": "pending",
        "question": "Why?",
        "created_at": 1_700_000_000,
        "ttl": 1_700_000_000 + 86400,
    }
    body = jobs.sqs.send_message.call_args.kwargs["MessageBody"]
    assert json.loads(body) == {
        "job_id": item["job_id"],
        "question": "Why?",
        "user_id": USER_ID,
        "source_ip": SOURCE_IP,
    }
    assert "secret" not in body


@pytest.mark.parametrize(
    "api,event_name", [("query", "query_submitted"), ("ask", "question_submitted")]
)
def test_submit_logs_audit_event(jobs, monkeypatch, caplog, api, event_name):
    caplog.set_level("INFO")
    _submit(jobs, api, monkeypatch, {api if api == "query" else "question": "x"})
    events = [
        json.loads(r.message) for r in caplog.records if r.message.startswith("{")
    ]
    submitted = [e for e in events if e.get("event") == event_name]
    assert len(submitted) == 1
    assert submitted[0]["user_id"] == USER_ID
    assert submitted[0]["source_ip"] == SOURCE_IP


def test_source_ip_is_never_taken_from_a_client_header(jobs, monkeypatch):
    _submit(
        jobs,
        "query",
        monkeypatch,
        {"query": "SELECT 1"},
        {"X-Forwarded-For": "203.0.113.9"},
    )
    message = json.loads(jobs.sqs.send_message.call_args.kwargs["MessageBody"])
    assert message["source_ip"] == SOURCE_IP


# ---------------------------------------------------------------------------
# Body size (D-5): measured in bytes of the raw body, before JSON parsing
# ---------------------------------------------------------------------------


def _padded_body(size: int, field="query") -> str:
    skeleton = json.dumps({field: "SELECT 1", "pad": ""})
    body = json.dumps({field: "SELECT 1", "pad": "x" * (size - len(skeleton))})
    assert len(body.encode()) == size
    return body


def test_body_at_max_bytes_is_accepted(jobs, monkeypatch):
    response = _submit(jobs, "query", monkeypatch, raw=_padded_body(262144))
    assert response["statusCode"] == 202


@pytest.mark.parametrize("api,field", [("query", "query"), ("ask", "question")])
def test_body_one_byte_over_max_is_413(jobs, monkeypatch, api, field):
    response = _submit(jobs, api, monkeypatch, raw=_padded_body(262145, field))
    assert response["statusCode"] == 413
    assert json.loads(response["body"]) == {"message": "Request Too Long"}
    jobs.sqs.send_message.assert_not_called()


def test_body_limit_counts_utf8_bytes_not_characters(jobs, monkeypatch):
    # 131073 two-byte characters: under the limit in characters, over it in bytes
    raw = json.dumps({"query": "SELECT 1", "pad": "é" * 131073}, ensure_ascii=False)
    response = _submit(jobs, "query", monkeypatch, raw=raw)
    assert response["statusCode"] == 413


def test_base64_encoded_body_is_decoded(jobs, monkeypatch):
    case = {"api": "query", "request": {"method": "POST", "path": "/query"}}
    event = _event(case, None)
    event["body"] = base64.b64encode(b'{"query": "SELECT 1"}').decode()
    event["isBase64Encoded"] = True
    response = _invoke(jobs, case, monkeypatch, event)
    assert response["statusCode"] == 202


def test_non_utf8_body_is_invalid_json(jobs, monkeypatch):
    case = {"api": "query", "request": {"method": "POST", "path": "/query"}}
    event = _event(case, None)
    event["body"] = base64.b64encode(b'{"query": "\xff"}').decode()
    event["isBase64Encoded"] = True
    response = _invoke(jobs, case, monkeypatch, event)
    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": "Request body must be valid JSON"}


# ---------------------------------------------------------------------------
# Status: job_id bounds (D-3) and the poll hint (D-15)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("api", ["query", "ask"])
@pytest.mark.parametrize("job_id", ["", "   ", None])
def test_status_blank_job_id(jobs, monkeypatch, api, job_id):
    case = {"api": api, "request": {"method": "GET", "path": f"/{api}/x"}}
    event = {"pathParameters": {"job_id": job_id}}
    response = _invoke(jobs, case, monkeypatch, event)
    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": "Missing job_id"}
    jobs.table.get_item.assert_not_called()


def test_status_job_id_at_max_length_is_looked_up(jobs, monkeypatch):
    case = {"api": "query", "request": {"method": "GET", "path": "/query/x"}}
    response = _invoke(
        jobs, case, monkeypatch, {"pathParameters": {"job_id": "j" * 128}}
    )
    assert response["statusCode"] == 404
    jobs.table.get_item.assert_called_once_with(Key={"job_id": "j" * 128})


@pytest.mark.parametrize("api", ["query", "ask"])
@pytest.mark.parametrize("status", ["complete", "error"])
def test_finished_jobs_have_no_retry_after(jobs, monkeypatch, api, status):
    case = {
        "api": api,
        "request": {"method": "GET", "path": f"/{api}/{{job_id}}"},
        "given": {"item": {"job_id": "done", "status": status}},
    }
    response = _invoke(jobs, case, monkeypatch)
    assert response["statusCode"] == 200
    assert "Retry-After" not in response["headers"]


# ---------------------------------------------------------------------------
# AWS client timeouts stay under the 10 s integration timeout (parity matrix row 4)
# ---------------------------------------------------------------------------


def test_aws_client_worst_case_is_under_the_integration_timeout(jobs):
    from sagebrain_core import limits

    config = jobs._AWS_CONFIG
    attempts = config.retries["total_max_attempts"]
    worst = attempts * (config.connect_timeout + config.read_timeout)
    assert worst < limits.REQUEST_TIMEOUT_SECONDS
