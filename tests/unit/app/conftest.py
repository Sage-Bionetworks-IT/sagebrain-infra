"""Fixtures for the FastAPI app: fake DynamoDB/SQS/Synapse, a fresh limiter per test."""

import json
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from sagebrain_api.jobs import JobStore
from sagebrain_api.main import create_app
from sagebrain_api.settings import Settings
from sagebrain_core.ratelimit import LocalLimiter

USER_ID = "3412345"
NON_MEMBER_ID = "777"
TEAM_ID = "3605470"
MACHINE_KEY = "machine-key-for-tests"
REPO_API = "https://synapse.test/repo/v1"
AUTH_API = "https://synapse.test/auth/v1"
SOURCE_IP = "198.51.100.7"

# Bearer tokens the fake Synapse knows -> Synapse user id.
TOKENS = {"valid-token": USER_ID, "caller-token": USER_ID, "outsider": NON_MEMBER_ID}
VALID_AUTH = {"Authorization": "Bearer valid-token"}


class FakeTable:
    def __init__(self):
        self.items = {}
        self.puts = []
        self.gets = 0

    def put_item(self, Item):
        self.puts.append(Item)
        self.items[Item["job_id"]] = Item

    def get_item(self, Key):
        self.gets += 1
        item = self.items.get(Key["job_id"])
        return {"Item": item} if item else {}


class FakeSqs:
    def __init__(self):
        self.sent = []

    def send_message(self, QueueUrl, MessageBody):
        self.sent.append({"QueueUrl": QueueUrl, "MessageBody": MessageBody})

    def bodies(self):
        return [json.loads(m["MessageBody"]) for m in self.sent]


class FakeSynapse:
    """httpx.MockTransport handler. `script` (responses or exceptions) is consumed first, in order,
    like the urlopen side_effect lists in tests/unit/test_authorizer.py; then default behaviour.
    """

    def __init__(self):
        self.script = []
        self.calls = []
        self.members = {USER_ID}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            status, body = step
            return httpx.Response(status, json=body)

        path = urlsplit(str(request.url)).path
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if path.endswith("/userProfile"):
            if token in TOKENS:
                return httpx.Response(200, json={"ownerId": TOKENS[token]})
            return httpx.Response(401, json={"reason": "invalid token"})
        if path.endswith("/membershipStatus"):
            user_id = path.split("/member/")[1].split("/")[0]
            return httpx.Response(200, json={"isMember": user_id in self.members})
        return httpx.Response(404, json={})

    def paths(self):
        return [urlsplit(str(r.url)).path for r in self.calls]


@pytest.fixture
def swagger_dir(tmp_path):
    root = tmp_path / "swagger-ui"
    root.mkdir()
    (root / "swagger-ui.css").write_text("/* css */")
    (root / "swagger-ui-bundle.js").write_text(
        "window.SwaggerUIBundle = function () {};"
    )
    (root / "favicon-32x32.png").write_bytes(b"\x89PNG")
    return root


@pytest.fixture
def settings(swagger_dir):
    return Settings(
        synapse_team_id=TEAM_ID,
        machine_api_key=MACHINE_KEY,
        query_job_table_name="query-jobs",
        query_job_queue_url="https://sqs.example/query",
        ask_job_table_name="ask-jobs",
        ask_job_queue_url="https://sqs.example/ask",
        public_base_url="https://api.test.example",
        synapse_repo_api=REPO_API,
        synapse_auth_api=AUTH_API,
        swagger_ui_dir=swagger_dir,
    )


@pytest.fixture
def synapse():
    return FakeSynapse()


@pytest.fixture
def tables():
    return {"query": FakeTable(), "ask": FakeTable()}


@pytest.fixture
def queues():
    return {"query": FakeSqs(), "ask": FakeSqs()}


@pytest.fixture
def limiter():
    # Frozen clock: buckets never refill mid-test, so counts are exact.
    return LocalLimiter(clock=lambda: 1000.0)


@pytest.fixture
def make_app(settings, synapse, tables, queues, limiter):
    def _make(**overrides):
        app_settings = overrides.pop("settings", settings)
        kwargs = dict(
            query_jobs=JobStore(
                tables["query"], queues["query"], settings.query_job_queue_url
            ),
            ask_jobs=JobStore(tables["ask"], queues["ask"], settings.ask_job_queue_url),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(synapse)),
            limiter=limiter,
        )
        kwargs.update(overrides)
        return create_app(app_settings, **kwargs)

    return _make


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    # The ALB appends the caller's address as the rightmost X-Forwarded-For entry.
    with TestClient(app, headers={"X-Forwarded-For": SOURCE_IP}) as c:
        yield c
