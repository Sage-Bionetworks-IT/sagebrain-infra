"""Characterization tests for the current submit/status Lambdas (src/lambda, src/lambda_agent).

Driven by the shared vectors in tests/contract/cases.yaml so the FastAPI app (Phase 1) is
held to exactly the same behaviour. Where a case documents an accepted deviation, the
Lambda is checked against `legacy` instead of `expect`.
"""

import builtins
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.contract.cases import (
    assert_subset,
    check_response,
    job_id_for,
    load_cases,
    request_body,
)

SRC = Path(__file__).parents[2] / "src"
LAMBDA_DIRS = {"query": SRC / "lambda", "ask": SRC / "lambda_agent"}

USER_ID = "3412345"
SOURCE_IP = "198.51.100.7"


def _load(api: str, name: str, monkeypatch):
    """Import <dir>/<name>.py under a unique module name with boto3 mocked.

    Both Lambda dirs have submit.py/status.py, so a plain import would collide in sys.modules.
    """
    monkeypatch.setenv("JOB_TABLE_NAME", f"{api}-jobs")
    monkeypatch.setenv("JOB_QUEUE_URL", f"https://sqs.example/{api}")
    table, sqs = MagicMock(), MagicMock()
    with patch("boto3.resource") as resource, patch("boto3.client", return_value=sqs):
        resource.return_value.Table.return_value = table
        spec = importlib.util.spec_from_file_location(
            f"{api}_{name}_under_test", LAMBDA_DIRS[api] / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module, table, sqs


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


def _invoke(case, monkeypatch):
    api = case["api"]
    is_submit = case["request"]["method"] == "POST"
    module, table, sqs = _load(api, "submit" if is_submit else "status", monkeypatch)

    item = (case.get("given") or {}).get("item")
    table.get_item.return_value = {"Item": item} if item else {}

    response = module.handler(_event(case, job_id_for(case)), None)
    return response, table, sqs


CASES = load_cases(layer="handler")


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_lambda_handler_matches_contract(case, monkeypatch):
    expect = case.get("legacy") or case["expect"]

    if "raises" in expect:
        with pytest.raises(getattr(builtins, expect["raises"])):
            _invoke(case, monkeypatch)
        return

    response, table, sqs = _invoke(case, monkeypatch)
    body = json.loads(response["body"])
    check_response(expect, response["statusCode"], body, response["headers"])
    assert response["headers"]["Access-Control-Allow-Origin"] == "*"
    assert response["headers"]["Content-Type"] == "application/json"

    if response["statusCode"] != 202:
        table.put_item.assert_not_called()
        sqs.send_message.assert_not_called()
        return

    stored = table.put_item.call_args.kwargs["Item"]
    message = sqs.send_message.call_args.kwargs
    enqueued = json.loads(message["MessageBody"])

    assert message["QueueUrl"] == f"https://sqs.example/{case['api']}"
    assert stored["job_id"] == body["job_id"] == enqueued["job_id"]
    if "stored_subset" in expect:
        assert_subset(expect["stored_subset"], stored, "stored item")
    if "enqueued_subset" in expect:
        assert_subset(expect["enqueued_subset"], enqueued, "SQS message")
    if "enqueued_keys" in expect:
        assert sorted(enqueued) == sorted(expect["enqueued_keys"]), sorted(enqueued)


# ---------------------------------------------------------------------------
# Exact storage / message shapes (the data-model contract the workers rely on)
# ---------------------------------------------------------------------------


def _submit(api, body, monkeypatch, headers=None):
    case = {
        "api": api,
        "request": {
            "method": "POST",
            "path": f"/{api}",
            "json": body,
            "headers": headers,
        },
    }
    return _invoke(case, monkeypatch)


def test_query_job_item_and_message_shape(monkeypatch):
    monkeypatch.setattr("time.time", lambda: 1_700_000_000.5)
    _, table, sqs = _submit(
        "query", {"query": "SELECT 1"}, monkeypatch, {"User-Agent": "ua/1"}
    )

    item = table.put_item.call_args.kwargs["Item"]
    assert set(item) == {"job_id", "status", "created_at", "ttl"}
    assert item["status"] == "pending"
    assert item["created_at"] == 1_700_000_000
    assert item["ttl"] == 1_700_000_000 + 86400

    message = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
    assert set(message) == {"job_id", "query", "source", "source_ip", "user_agent"}
    assert message["source_ip"] == SOURCE_IP
    assert message["user_agent"] == "ua/1"


def test_ask_job_item_and_message_shape(monkeypatch):
    monkeypatch.setattr("time.time", lambda: 1_700_000_000.5)
    _, table, sqs = _submit(
        "ask", {"question": "Why?"}, monkeypatch, {"authorization": "Bearer t"}
    )

    item = table.put_item.call_args.kwargs["Item"]
    assert set(item) == {"job_id", "status", "question", "created_at", "ttl"}
    assert item["ttl"] == 1_700_000_000 + 86400

    message = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
    assert message == {
        "job_id": item["job_id"],
        "question": "Why?",
        "authorization": "Bearer t",
    }


def test_ask_does_not_forward_api_key(monkeypatch):
    """Legacy F8: only Authorization is forwarded, so machine-key /ask jobs can't call /query.

    Resolved by design in the FastAPI service (spec 001 D-8): the agent calls the shared query
    service in-process with the caller's user_id, so no credential is forwarded at all.
    """
    _, _, sqs = _submit(
        "ask",
        {"question": "Why?"},
        monkeypatch,
        {"x-api-key": "secret", "Authorization": "ApiKey"},
    )
    message = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
    assert message["authorization"] == "ApiKey"
    assert "secret" not in json.dumps(message)


def test_submit_logs_audit_event(monkeypatch, caplog):
    caplog.set_level("INFO")
    _submit("query", {"query": "SELECT 1"}, monkeypatch, {"X-Source": "agent"})
    events = [
        json.loads(r.message) for r in caplog.records if r.message.startswith("{")
    ]
    submitted = [e for e in events if e.get("event") == "query_submitted"]
    assert len(submitted) == 1
    assert submitted[0]["user_id"] == USER_ID
    assert submitted[0]["source"] == "agent"
    assert submitted[0]["source_ip"] == SOURCE_IP


def test_status_blank_job_id(monkeypatch):
    module, table, _ = _load("query", "status", monkeypatch)
    response = module.handler({"pathParameters": {"job_id": "  "}}, None)
    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": "Missing job_id"}
    table.get_item.assert_not_called()
