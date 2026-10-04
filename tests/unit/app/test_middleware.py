"""T116 — CORS, body limit (413), timeout (504), 500 handler, access log (parity rows 4, 10-12, 14)."""

import json
import logging
import time

import pytest

from sagebrain_api import jobs
from sagebrain_core import limits

from .conftest import SOURCE_IP, USER_ID, VALID_AUTH

ALLOWED_HEADERS = ["Content-Type", "Authorization", "X-Source", "x-api-key"]


def _assert_cors(response):
    assert response.headers["access-control-allow-origin"] == "*"


# ---------------------------------------------------------------------------
# CORS (row 10)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/query", "/ask", "/query/j1", "/ask/j1"])
def test_preflight(client, path):
    response = client.options(
        path,
        headers={
            "Origin": "https://example.org",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": ",".join(ALLOWED_HEADERS).lower(),
        },
    )
    assert response.status_code == 200
    _assert_cors(response)
    methods = {
        m.strip() for m in response.headers["access-control-allow-methods"].split(",")
    }
    assert methods == {"POST", "GET", "OPTIONS"}
    allowed = {
        h.strip().lower()
        for h in response.headers["access-control-allow-headers"].split(",")
    }
    assert {h.lower() for h in ALLOWED_HEADERS} <= allowed
    assert response.headers["access-control-max-age"] == "600"


def test_preflight_rejects_unlisted_header(client):
    response = client.options(
        "/query",
        headers={
            "Origin": "https://example.org",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-something-else",
        },
    )
    assert response.status_code == 400


def test_simple_request_with_origin_gets_acao(client):
    response = client.get("/healthz", headers={"Origin": "https://example.org"})
    _assert_cors(response)


# ---------------------------------------------------------------------------
# Body limit (row 11, D-5)
# ---------------------------------------------------------------------------


def _assert_413(response):
    assert response.status_code == 413
    assert response.json() == {"message": "Request Too Long"}
    _assert_cors(response)


def test_content_length_over_limit_is_413(client, queues):
    body = b"x" * (limits.MAX_BODY_BYTES + 1)
    _assert_413(client.post("/query", content=body, headers=VALID_AUTH))
    assert queues["query"].sent == []


def test_over_limit_is_413_before_auth(client, synapse):
    body = b"x" * (limits.MAX_BODY_BYTES + 1)
    _assert_413(client.post("/query", content=body))
    assert synapse.calls == []


def test_body_at_limit_is_accepted_by_the_limit(client):
    # Exactly MAX_BODY_BYTES passes the size check (and then fails validation on length).
    query = "a" * (limits.MAX_BODY_BYTES - len('{"query": ""}'))
    body = json.dumps({"query": query}).encode()
    assert len(body) == limits.MAX_BODY_BYTES
    response = client.post("/query", content=body, headers=VALID_AUTH)
    assert response.status_code == 400
    assert response.json() == {
        "error": "Query exceeds maximum length of 8000 characters"
    }


def test_chunked_body_over_limit_is_413(client, queues):
    def chunks():
        for _ in range(limits.MAX_BODY_BYTES // 4096 + 2):
            yield b" " * 4096

    response = client.post("/query", content=chunks(), headers=VALID_AUTH)
    _assert_413(response)
    assert queues["query"].sent == []


def test_body_limit_is_the_spec_value():
    assert limits.MAX_BODY_BYTES == 256 * 1024


# ---------------------------------------------------------------------------
# Request timeout (row 4)
# ---------------------------------------------------------------------------


class SlowTable:
    def __init__(self, seconds):
        self.seconds = seconds

    def get_item(self, Key):
        time.sleep(self.seconds)
        return {}


def test_slow_store_is_504(make_app, monkeypatch, queues, settings):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(limits, "REQUEST_TIMEOUT_SECONDS", 0.2)
    app = make_app(
        query_jobs=jobs.JobStore(SlowTable(1.0), queues["query"], "q"),
    )
    with TestClient(app) as client:
        start = time.monotonic()
        response = client.get("/query/j1", headers=VALID_AUTH)
        elapsed = time.monotonic() - start
    assert response.status_code == 504
    assert response.json() == {"message": "Endpoint request timed out"}
    _assert_cors(response)
    assert elapsed < 0.9, "the 504 must not wait for the slow call to finish"


def test_request_timeout_is_the_spec_value():
    assert limits.REQUEST_TIMEOUT_SECONDS == 10


def test_aws_client_timeouts():
    # Inside the 10 s request budget: connect 2 s, read 5 s (parity row 4).
    assert jobs.AWS_CLIENT_CONFIG.connect_timeout == 2
    assert jobs.AWS_CLIENT_CONFIG.read_timeout == 5


# ---------------------------------------------------------------------------
# Unhandled errors (row 14)
# ---------------------------------------------------------------------------


class BrokenTable:
    def get_item(self, Key):
        raise RuntimeError("dynamodb exploded")


def test_unhandled_error_is_500_and_logged(make_app, queues, caplog):
    from fastapi.testclient import TestClient

    app = make_app(query_jobs=jobs.JobStore(BrokenTable(), queues["query"], "q"))
    with caplog.at_level(logging.ERROR), TestClient(app) as client:
        response = client.get("/query/j1", headers=VALID_AUTH)
    assert response.status_code == 500
    assert response.json() == {"message": "Internal server error"}
    _assert_cors(response)
    assert "dynamodb exploded" not in response.text
    (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert record.exc_info and "dynamodb exploded" in caplog.text


@pytest.mark.parametrize(
    "method, path, status, message",
    [
        ("GET", "/does-not-exist", 404, "Not Found"),
        ("DELETE", "/query", 405, "Method Not Allowed"),
    ],
)
def test_routing_errors_use_gateway_body(client, method, path, status, message):
    response = client.request(method, path, headers=VALID_AUTH)
    assert response.status_code == status
    assert response.json() == {"message": message}
    _assert_cors(response)


# ---------------------------------------------------------------------------
# Access log (row 12)
# ---------------------------------------------------------------------------

ACCESS_LOG_KEYS = {
    # API Gateway json_with_standard_fields
    "caller",
    "httpMethod",
    "ip",
    "protocol",
    "requestTime",
    "resourcePath",
    "responseLength",
    "status",
    "user",
    # added
    "requestId",
    "api",
    "durationMs",
}


def _access_lines(caplog):
    return [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "sagebrain_api.access"
    ]


def test_access_log_fields(client, caplog):
    with caplog.at_level(logging.INFO, logger="sagebrain_api.access"):
        response = client.get(
            "/query/j1",
            headers={**VALID_AUTH, "X-Forwarded-For": "203.0.113.9, " + SOURCE_IP},
        )
    (line,) = _access_lines(caplog)
    assert set(line) == ACCESS_LOG_KEYS
    assert line["httpMethod"] == "GET"
    assert line["resourcePath"] == "/query/{job_id}"
    assert line["status"] == 404
    assert line["ip"] == SOURCE_IP
    assert line["user"] == USER_ID
    assert line["caller"] == "bearer"
    assert line["api"] == "query"
    assert line["protocol"] == "HTTP/1.1"
    assert line["responseLength"] == len(response.content)
    assert isinstance(line["durationMs"], float)
    assert line["requestId"]
    assert "valid-token" not in json.dumps(line)


def test_access_log_for_unauthenticated_and_edge_responses(client, caplog):
    with caplog.at_level(logging.INFO, logger="sagebrain_api.access"):
        client.get("/ask/j1")
        client.post("/ask", content=b"x" * (limits.MAX_BODY_BYTES + 1))
        client.get("/nowhere")
    lines = _access_lines(caplog)
    assert [line["status"] for line in lines] == [401, 413, 404]
    assert [line["user"] for line in lines] == [None, None, None]
    assert lines[0]["api"] == "ask"
    assert lines[2]["resourcePath"] == "/nowhere"


def test_request_ids_are_unique(client, caplog):
    with caplog.at_level(logging.INFO, logger="sagebrain_api.access"):
        client.get("/healthz")
        client.get("/healthz")
    ids = [line["requestId"] for line in _access_lines(caplog)]
    assert len(set(ids)) == 2
