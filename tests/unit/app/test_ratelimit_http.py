"""Rate admission over HTTP (parity matrix rows 1, 2). Bucket maths is tested in tests/unit/core."""

import asyncio
import json

import pytest
from botocore.exceptions import ClientError as BotoClientError
from fastapi.testclient import TestClient

from sagebrain_core import limits
from sagebrain_core.ratelimit import DynamoLimiter, LocalLimiter

from ...conftest import RATE_LIMIT_TABLE
from .conftest import MACHINE_KEY, VALID_AUTH

QUERY = {"query": "SELECT 1"}


def _drain(limiter, key, burst):
    for _ in range(burst):
        assert limiter.acquire(key, 1e-9, burst).allowed


def test_global_bucket_exhausted_is_429(client, limiter):
    _drain(limiter, "{rl:query}:global", limits.GLOBAL_BURST_PER_API)
    response = client.post("/query", json=QUERY, headers=VALID_AUTH)
    assert response.status_code == 429
    assert response.json() == {"message": "Too Many Requests"}
    assert response.headers["access-control-allow-origin"] == "*"
    assert int(response.headers["retry-after"]) >= 1


def test_global_bucket_is_charged_before_auth(client, limiter, synapse):
    """Unauthenticated floods drain the global bucket, and never reach Synapse once it's empty."""
    for _ in range(limits.GLOBAL_BURST_PER_API):
        assert client.post("/query", json=QUERY).status_code == 401
    calls = len(synapse.calls)
    assert client.post("/query", json=QUERY, headers=VALID_AUTH).status_code == 429
    assert len(synapse.calls) == calls


def test_per_principal_bucket(client):
    for _ in range(limits.USER_BURST):
        assert client.get("/query/j1", headers=VALID_AUTH).status_code == 404
    response = client.get("/query/j1", headers=VALID_AUTH)
    assert response.status_code == 429
    # A different principal still gets through.
    assert (
        client.get("/query/j1", headers={"x-api-key": MACHINE_KEY}).status_code == 404
    )


def test_rejected_auth_does_not_charge_a_principal_bucket(client, limiter):
    client.post("/query", json=QUERY, headers={"Authorization": "Bearer nope"})
    assert limiter.keys() == ["{rl:query}:global"]


def test_status_polls_charge_the_same_buckets_as_submit(client, limiter):
    client.get("/ask/j1", headers=VALID_AUTH)
    assert set(limiter.keys()) == {"{rl:ask}:global", "{rl:ask}:u:3412345"}


def test_query_and_ask_buckets_are_independent(client, limiter):
    _drain(limiter, "{rl:query}:global", limits.GLOBAL_BURST_PER_API)
    assert (
        client.post("/ask", json={"question": "Why?"}, headers=VALID_AUTH).status_code
        == 202
    )


@pytest.mark.parametrize(
    "path", ["/healthz", "/api", "/api/openapi.json", "/api/openapi.yaml"]
)
def test_health_and_docs_are_not_charged(client, limiter, path):
    for _ in range(limits.GLOBAL_BURST_PER_API + 1):
        assert client.get(path).status_code == 200
    assert limiter.keys() == []


# ---------------------------------------------------------------------------
# Phase 1d — the shared DynamoDB limiter behind HTTP (moto; parity rows 1-3)
# ---------------------------------------------------------------------------


class DownClient:
    def update_item(self, **kwargs):
        raise BotoClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "UpdateItem",
        )


def test_dynamodb_outage_fails_open_never_500(make_app, capsys):
    """Row 3: the limiter's table is down, the request still goes through, degraded is logged."""
    limiter = DynamoLimiter("rate-limits", client=DownClient(), clock=lambda: 1000.0)
    with TestClient(make_app(limiter=limiter)) as client:
        response = client.post("/query", json=QUERY, headers=VALID_AUTH)
    assert response.status_code == 202
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["event"] for e in events if e.get("event") == "ratelimit_degraded"] == [
        "ratelimit_degraded"
    ]


def test_limiter_runs_off_the_event_loop(make_app):
    """A DynamoDB round trip must not block the event loop (like the jobs table calls)."""
    on_loop = []

    class Recording(LocalLimiter):
        def acquire(self, key, rate, burst):
            try:
                asyncio.get_running_loop()
                on_loop.append(key)
            except RuntimeError:
                pass
            return super().acquire(key, rate, burst)

    with TestClient(make_app(limiter=Recording())) as client:
        assert client.get("/query/j1", headers=VALID_AUTH).status_code == 404
    assert on_loop == []


def test_two_tasks_share_the_principal_bucket(make_app, rate_limit_table):
    """Row 2 over HTTP: two app tasks on one table; alice's bucket is one bucket."""
    clock = lambda: 1000.0  # noqa: E731
    apps = [
        make_app(limiter=DynamoLimiter(RATE_LIMIT_TABLE, clock=clock)) for _ in range(2)
    ]
    with TestClient(apps[0]) as a, TestClient(apps[1]) as b:
        for i in range(limits.USER_BURST):
            assert (a if i % 2 else b).get(
                "/query/j1", headers=VALID_AUTH
            ).status_code == 404
        assert a.get("/query/j1", headers=VALID_AUTH).status_code == 429
        assert b.get("/query/j1", headers=VALID_AUTH).status_code == 429
        other = {"x-api-key": MACHINE_KEY}
        assert b.get("/query/j1", headers=other).status_code == 404


def test_app_uses_local_limiter_without_a_table(make_app):
    app = make_app(limiter=None)
    assert isinstance(app.state.limiter, LocalLimiter)


def test_app_uses_dynamo_limiter_from_settings(make_app, settings):
    configured = settings.model_copy(
        update={
            "rate_limit_table_name": "app-dev-rate-limits",
            "rate_limit_fallback_instances": 3,
        }
    )
    app = make_app(settings=configured, limiter=None)
    assert isinstance(app.state.limiter, DynamoLimiter)
    assert app.state.limiter.table_name == "app-dev-rate-limits"
    assert app.state.limiter.fallback_instances == 3
