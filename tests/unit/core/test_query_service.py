"""run_query = validate + admit + execute, for in-process callers (the agent)."""

from unittest.mock import patch

import pytest

from sagebrain_core import query_service
from sagebrain_core.errors import QueryRejected, RateLimited
from sagebrain_core.neptune import NeptuneConfig, NeptuneResult
from sagebrain_core.ratelimit import LocalLimiter

CONFIG = NeptuneConfig(endpoint="reader.example", region="us-east-1")


@pytest.fixture
def execute():
    with patch.object(query_service, "execute_query") as execute:
        execute.return_value = NeptuneResult(
            "RESULT", "application/sparql-results+json", 200, 1.0
        )
        yield execute


def _run(query="  SELECT 1  ", **overrides):
    kwargs = dict(
        principal="3412345",
        source="agent",
        job_id="ask-job-1",
        limiter=LocalLimiter(),
        config=CONFIG,
    )
    kwargs.update(overrides)
    return query_service.run_query(query, **kwargs)


def test_executes_validated_query_with_attribution(execute):
    result = _run()
    assert result.text == "RESULT"
    args, kwargs = execute.call_args
    assert args == ("SELECT 1",)
    assert kwargs["principal"] == "3412345"
    assert kwargs["source"] == "agent"
    assert kwargs["job_id"] == "ask-job-1"
    assert kwargs["config"] == CONFIG


def test_rejects_over_length_without_executing(execute):
    with pytest.raises(QueryRejected, match="maximum length of 8000"):
        _run("a" * 8001)
    execute.assert_not_called()


def test_rate_limited_without_executing(execute):
    limiter = LocalLimiter()
    for _ in range(20):
        _run(limiter=limiter)
    execute.reset_mock()
    with pytest.raises(RateLimited):
        _run(limiter=limiter)
    execute.assert_not_called()


def test_invalid_query_does_not_consume_rate_budget(execute):
    limiter = LocalLimiter()
    for _ in range(50):
        with pytest.raises(QueryRejected):
            _run("", limiter=limiter)
    _run(limiter=limiter)  # still admitted


def test_defaults_to_process_limiter_and_env_config(execute, monkeypatch):
    monkeypatch.setenv("NEPTUNE_ENDPOINT", "ep.example")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    query_service.run_query("SELECT 1", principal="p", source="agent", job_id="j")
    assert execute.call_args.kwargs["config"] == NeptuneConfig(
        "ep.example", "us-east-1"
    )
