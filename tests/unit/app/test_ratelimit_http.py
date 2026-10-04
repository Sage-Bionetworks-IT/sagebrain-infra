"""Rate admission over HTTP (parity matrix rows 1, 2). Bucket maths is tested in tests/unit/core."""

import pytest

from sagebrain_core import limits

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
