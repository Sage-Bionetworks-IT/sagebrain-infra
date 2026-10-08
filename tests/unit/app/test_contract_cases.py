"""T114 — the FastAPI app satisfies every tests/contract/cases.yaml vector, against `expect`.

The same vectors pin the legacy Lambdas (tests/unit/test_job_api_handlers.py, which asserts
`legacy` where a deviation is recorded). Here `legacy` is never consulted.
"""

import json

import pytest

import sagebrain_core
from sagebrain_api import jobs
from sagebrain_api.routers import ask, query
from tests.contract.cases import (
    assert_subset,
    check_response,
    job_id_for,
    load_cases,
    request_body,
    request_path,
)

from .conftest import MACHINE_KEY, SOURCE_IP, USER_ID, VALID_AUTH

CASES = load_cases()


def _headers(case):
    headers = dict(case["request"].get("headers") or {})
    auth = case.get("auth", "valid")
    has_auth = any(k.lower() == "authorization" for k in headers)
    if auth == "valid" and not has_auth:
        headers.update(VALID_AUTH)
    elif auth == "bad_bearer":
        headers["Authorization"] = "Bearer not-a-real-token"
    if case["request"]["method"] == "POST":
        headers.setdefault("Content-Type", "application/json")
    return headers


def _decode(response):
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json() if response.content else None
    return None


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_app_matches_contract(case, client, tables, queues):
    api = case["api"]
    item = (case.get("given") or {}).get("item")
    if item:
        tables[api].items[item["job_id"]] = item

    req = case["request"]
    response = client.request(
        req["method"],
        request_path(case, job_id_for(case)),
        content=request_body(case) if req["method"] == "POST" else None,
        headers=_headers(case),
    )
    body = _decode(response)
    check_response(case["expect"], response.status_code, body, response.headers)
    assert response.headers["access-control-allow-origin"] == "*"  # FR-6

    if response.status_code != 202:
        assert tables[api].puts == [] and queues[api].sent == []
        return

    expect = case["expect"]
    (stored,) = tables[api].puts
    (message,) = queues[api].sent
    enqueued = json.loads(message["MessageBody"])
    assert message["QueueUrl"] == f"https://sqs.example/{api}"
    assert stored["job_id"] == body["job_id"] == enqueued["job_id"]
    if "stored_subset" in expect:
        assert_subset(expect["stored_subset"], stored, "stored item")
    if "enqueued_subset" in expect:
        assert_subset(expect["enqueued_subset"], enqueued, "SQS message")
    if "enqueued_keys" in expect:
        assert sorted(enqueued) == sorted(expect["enqueued_keys"]), sorted(enqueued)
    if "user_id" in enqueued:
        assert enqueued["user_id"] == USER_ID


# ---------------------------------------------------------------------------
# Exact storage / message shapes (data-model.md; the workers rely on these)
# ---------------------------------------------------------------------------


@pytest.fixture
def frozen_time(monkeypatch):
    monkeypatch.setattr(jobs.time, "time", lambda: 1_700_000_000.5)


def _post(client, api, body, headers=None):
    return client.post(f"/{api}", json=body, headers={**VALID_AUTH, **(headers or {})})


def test_query_job_item_and_message_shape(client, tables, queues, frozen_time):
    _post(client, "query", {"query": "SELECT 1"}, {"User-Agent": "ua/1"})

    (item,) = tables["query"].puts
    assert set(item) == {"job_id", "status", "created_at", "ttl"}
    assert item["status"] == "pending"
    assert item["created_at"] == 1_700_000_000
    assert item["ttl"] == 1_700_000_000 + sagebrain_core.limits.JOB_TTL_SECONDS

    (message,) = queues["query"].bodies()
    assert set(message) == {"job_id", "query", "source", "source_ip", "user_agent"}
    assert message["source_ip"] == SOURCE_IP
    assert message["user_agent"] == "ua/1"


def test_query_user_agent_defaults_unknown(client, queues):
    # httpx's TestClient sends its own User-Agent; an explicitly absent one is "unknown".
    client.headers.pop("user-agent", None)
    _post(client, "query", {"query": "SELECT 1"})
    assert queues["query"].bodies()[0]["user_agent"] == "unknown"


def test_ask_job_item_and_message_shape(client, tables, queues, frozen_time):
    """D-8: {job_id, question, user_id, source_ip}; no credential is enqueued."""
    _post(client, "ask", {"question": "Why?"})

    (item,) = tables["ask"].puts
    assert set(item) == {"job_id", "status", "question", "created_at", "ttl"}
    assert item["ttl"] == 1_700_000_000 + sagebrain_core.limits.JOB_TTL_SECONDS

    (message,) = queues["ask"].bodies()
    assert message == {
        "job_id": item["job_id"],
        "question": "Why?",
        "user_id": USER_ID,
        "source_ip": SOURCE_IP,
    }
    assert "valid-token" not in json.dumps(message)


def test_ask_with_machine_key_enqueues_machine_principal(client, queues):
    """F8 resolved: a machine-key /ask job is attributed to `machine`, and the key isn't enqueued."""
    response = client.post(
        "/ask", json={"question": "Why?"}, headers={"x-api-key": MACHINE_KEY}
    )
    assert response.status_code == 202
    (message,) = queues["ask"].bodies()
    assert message["user_id"] == "machine"
    assert MACHINE_KEY not in json.dumps(message)


def test_submit_logs_audit_events(client, caplog):
    caplog.set_level("INFO", logger="sagebrain_api")
    _post(client, "query", {"query": "SELECT 1"}, {"X-Source": "agent"})
    _post(client, "ask", {"question": "Why?"})

    events = {}
    for record in caplog.records:
        if record.getMessage().startswith("{"):
            event = json.loads(record.getMessage())
            events[event.get("event")] = event
    assert events["query_submitted"] == {
        "event": "query_submitted",
        "job_id": events["query_submitted"]["job_id"],
        "user_id": USER_ID,
        "source_ip": SOURCE_IP,
        "source": "agent",
    }
    assert events["question_submitted"] == {
        "event": "question_submitted",
        "job_id": events["question_submitted"]["job_id"],
        "user_id": USER_ID,
        "source_ip": SOURCE_IP,
    }


@pytest.mark.parametrize("api", ["query", "ask"])
@pytest.mark.parametrize("job_id", ["%20%20", "x" * 129])
def test_blank_or_long_job_id_is_400(client, tables, api, job_id):
    """D-3: blank or over 128 characters -> 400 Missing job_id, no table read."""
    response = client.get(f"/{api}/{job_id}", headers=VALID_AUTH)
    assert response.status_code == 400
    assert response.json() == {"error": "Missing job_id"}
    assert tables[api].gets == 0


def test_job_id_at_max_length_is_looked_up(client, tables):
    response = client.get("/query/" + "x" * 128, headers=VALID_AUTH)
    assert response.status_code == 404
    assert tables["query"].gets == 1


def test_trailing_slash_is_not_redirected(client):
    # D-3: no `/query/` route; FastAPI's slash redirect would turn it into a 307.
    response = client.get("/query/", headers=VALID_AUTH, follow_redirects=False)
    assert response.status_code == 404


def test_source_ip_is_rightmost_forwarded_for(client, queues):
    """data-model.md: the ALB appends the caller; a client-supplied leftmost entry is ignored."""
    _post(
        client,
        "query",
        {"query": "SELECT 1"},
        {"X-Forwarded-For": "203.0.113.9, 192.0.2.44"},
    )
    assert queues["query"].bodies()[0]["source_ip"] == "192.0.2.44"


def test_handlers_reuse_sagebrain_core():
    """Validation and admission are the shared service's, not re-implemented in the HTTP layer."""
    assert query.validate_query is sagebrain_core.validation.validate_query
    assert ask.validate_question is sagebrain_core.validation.validate_question
    assert query.validate_source is sagebrain_core.validation.validate_source
