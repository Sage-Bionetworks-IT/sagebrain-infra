"""Spec 001 Phase 1c — the agent worker calls sagebrain_core.run_query in-process (T121–T123).

Offline: Strands is stubbed, Bedrock is replaced by a scripted fake Agent that calls the tools,
DynamoDB writes go to an in-memory dict, and Neptune is patched at run_query/execute_query.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from sagebrain_core import limits, ratelimit
from sagebrain_core.errors import QueryRejected, RateLimited
from sagebrain_core.neptune import NeptuneResult

AGENT_PY = Path(__file__).parents[2] / "src" / "lambda_agent" / "agent.py"
SELECT = "SELECT ?s WHERE { ?s ?p ?o } LIMIT 1"
RESULT = NeptuneResult(
    text='{"results":{"bindings":[]}}',
    content_type="application/sparql-results+json",
    status_code=200,
    duration_ms=1.0,
)
NEW_MESSAGE = {
    "job_id": "agent-job-1",
    "question": "q?",
    "user_id": "3412345",
    "source_ip": "198.51.100.7",
}
LEGACY_TOKEN = "Bearer legacy-secret-token"
LEGACY_MESSAGE = {
    "job_id": "agent-job-2",
    "question": "q?",
    "authorization": LEGACY_TOKEN,
}


def _stub_strands():
    strands = types.ModuleType("strands")
    strands.Agent = MagicMock(name="Agent")
    strands.tool = lambda fn: fn
    models = types.ModuleType("strands.models")
    bedrock = types.ModuleType("strands.models.bedrock")
    bedrock.BedrockModel = MagicMock(name="BedrockModel")
    return {
        "strands": strands,
        "strands.models": models,
        "strands.models.bedrock": bedrock,
    }


@pytest.fixture
def agent(monkeypatch):
    """A fresh import of agent.py per test, with no query-API URL in the environment."""
    monkeypatch.setenv("JOB_TABLE_NAME", "agent-jobs")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("NEPTUNE_QUERY_URL", raising=False)
    monkeypatch.delenv("NEPTUNE_QUERY_STATUS_URL", raising=False)
    for name, module in _stub_strands().items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("agent_worker_under_test", AGENT_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    jobs: dict = {}

    def update_job(job_id, **fields):
        # Snapshot lists so later in-place appends don't rewrite history.
        jobs.setdefault(job_id, {}).update(
            {k: list(v) if isinstance(v, list) else v for k, v in fields.items()}
        )

    monkeypatch.setattr(module, "_update_job", update_job)
    module.jobs = jobs
    return module


class ScriptedAgent:
    """Stands in for strands.Agent: each call runs `script(tools)` and returns its answer."""

    instances: list = []

    def __init__(self, script):
        self.script = script

    def factory(self, *, model, system_prompt, tools):
        self.tools = {t.__name__: t for t in tools}
        ScriptedAgent.instances.append(self)
        return self

    def __call__(self, question):
        return self.script(self.tools)


def _use_agent(agent, monkeypatch, script):
    scripted = ScriptedAgent(script)
    monkeypatch.setattr(agent, "Agent", scripted.factory)
    return scripted


def _run(agent, message):
    agent.handler({"Records": [{"body": json.dumps(message)}]}, None)
    return agent.jobs[message["job_id"]]


def _no_http():
    return patch.object(
        requests.Session,
        "request",
        side_effect=AssertionError("the agent worker must not make HTTP calls"),
    )


# ---------------------------------------------------------------------------
# T121 — the tool calls run_query in-process and records the same steps
# ---------------------------------------------------------------------------


def test_module_imports_without_query_api_urls(agent):
    assert not hasattr(agent, "NEPTUNE_QUERY_URL")
    assert not hasattr(agent, "requests")


def test_query_tool_calls_run_query_as_the_caller(agent, monkeypatch):
    _use_agent(agent, monkeypatch, lambda tools: tools["query_neptune"](SELECT))
    run_query = MagicMock(return_value=RESULT)
    monkeypatch.setattr(agent, "run_query", run_query)

    with _no_http():
        job = _run(agent, NEW_MESSAGE)

    run_query.assert_called_once_with(
        SELECT,
        principal="3412345",
        source="agent",
        job_id="agent-job-1",
        source_ip="198.51.100.7",
    )
    assert job["status"] == "complete"


def test_query_tool_returns_results_and_records_same_steps(agent, monkeypatch):
    long_text = '{"results":{"bindings":[' + ",".join(["{}"] * 400) + "]}}"
    returned = []
    _use_agent(
        agent,
        monkeypatch,
        lambda tools: returned.append(tools["query_neptune"](SELECT)) or "answer",
    )
    monkeypatch.setattr(
        agent,
        "run_query",
        MagicMock(return_value=NeptuneResult(long_text, "x", 200, 1.0)),
    )

    with _no_http():
        job = _run(agent, NEW_MESSAGE)

    assert returned == [long_text]
    assert job["steps"] == [
        {"type": "tool_call", "tool": "query_neptune", "sparql": SELECT},
        {"type": "tool_result", "tool": "query_neptune", "preview": long_text[:500]},
    ]
    assert job["status"] == "complete"
    assert job["answer"] == "answer"


def test_get_schema_goes_through_run_query(agent, monkeypatch):
    _use_agent(agent, monkeypatch, lambda tools: tools["get_schema"]("nf-osi"))
    run_query = MagicMock(return_value=RESULT)
    monkeypatch.setattr(agent, "run_query", run_query)

    job = _run(agent, NEW_MESSAGE)

    (sparql,), kwargs = run_query.call_args
    assert 'STRSTARTS(STR(?term), "http://nf-osi.github.com/terms#")' in sparql
    assert kwargs["source"] == "agent"
    assert job["steps"][0] == {
        "type": "tool_call",
        "tool": "query_neptune",
        "sparql": sparql,
    }


def test_non_select_is_rejected_before_run_query(agent, monkeypatch):
    errors = []

    def script(tools):
        try:
            tools["query_neptune"]("DELETE WHERE { ?s ?p ?o }")
        except ValueError as e:
            errors.append(str(e))
        return "answer"

    _use_agent(agent, monkeypatch, script)
    run_query = MagicMock()
    monkeypatch.setattr(agent, "run_query", run_query)

    job = _run(agent, NEW_MESSAGE)

    assert errors and "Only SPARQL SELECT" in errors[0]
    run_query.assert_not_called()
    assert job["steps"] == []


def test_neptune_failure_is_an_error_step_and_raises_to_the_model(agent, monkeypatch):
    raised = []

    def script(tools):
        try:
            tools["query_neptune"](SELECT)
        except RuntimeError as e:
            raised.append(str(e))
        return "answer"

    _use_agent(agent, monkeypatch, script)
    monkeypatch.setattr(
        agent, "run_query", MagicMock(side_effect=requests.Timeout("read timed out"))
    )

    job = _run(agent, NEW_MESSAGE)

    assert raised == ["SPARQL query failed: read timed out"]
    assert job["steps"][-1] == {
        "type": "tool_result",
        "tool": "query_neptune",
        "error": "read timed out",
    }


# ---------------------------------------------------------------------------
# T122 — over-length / rate-limited queries become error steps; the job continues
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc, message",
    [
        (
            QueryRejected("Query exceeds maximum length of 8000 characters"),
            "Query exceeds maximum length of 8000 characters",
        ),
        (
            RateLimited("principal", 0.2),
            "Rate limit exceeded; retry after 0.2s",
        ),
        (
            RateLimited("global", 1.5),
            "Rate limit exceeded; retry after 1.5s",
        ),
    ],
)
def test_rejection_is_tool_result_error_and_job_continues(
    agent, monkeypatch, exc, message
):
    returned = []

    def script(tools):
        returned.append(tools["query_neptune"](SELECT))
        returned.append(tools["query_neptune"](SELECT))  # the model tries again
        return "answer"

    _use_agent(agent, monkeypatch, script)
    monkeypatch.setattr(agent, "run_query", MagicMock(side_effect=[exc, RESULT]))

    job = _run(agent, NEW_MESSAGE)  # handler did not raise: the SQS message succeeds

    assert returned == [f"Error: {message}", RESULT.text]
    assert job["steps"] == [
        {"type": "tool_call", "tool": "query_neptune", "sparql": SELECT},
        {"type": "tool_result", "tool": "query_neptune", "error": message},
        {"type": "tool_call", "tool": "query_neptune", "sparql": SELECT},
        {"type": "tool_result", "tool": "query_neptune", "preview": RESULT.text},
    ]
    assert job["status"] == "complete"
    assert job["answer"] == "answer"


# ---------------------------------------------------------------------------
# T123 — both message shapes during cutover; the credential is never logged/stored
# ---------------------------------------------------------------------------


def test_legacy_message_runs_as_legacy_principal(agent, monkeypatch, capsys):
    _use_agent(agent, monkeypatch, lambda tools: tools["query_neptune"](SELECT))
    run_query = MagicMock(return_value=RESULT)
    monkeypatch.setattr(agent, "run_query", run_query)

    with _no_http():
        job = _run(agent, LEGACY_MESSAGE)

    assert run_query.call_args.kwargs["principal"] == "legacy-apigw"
    assert run_query.call_args.kwargs["job_id"] == "agent-job-2"
    assert job["status"] == "complete"
    assert LEGACY_TOKEN not in json.dumps(agent.jobs)
    assert "legacy-secret-token" not in capsys.readouterr().out


def test_user_id_wins_when_both_shapes_present(agent, monkeypatch):
    _use_agent(agent, monkeypatch, lambda tools: tools["query_neptune"](SELECT))
    run_query = MagicMock(return_value=RESULT)
    monkeypatch.setattr(agent, "run_query", run_query)

    _run(agent, {**NEW_MESSAGE, "authorization": LEGACY_TOKEN})

    assert run_query.call_args.kwargs["principal"] == "3412345"


def test_invocation_log_carries_principal_and_never_the_token(
    agent, monkeypatch, capsys
):
    _use_agent(agent, monkeypatch, lambda tools: "answer")

    _run(agent, NEW_MESSAGE)
    _run(agent, LEGACY_MESSAGE)

    out = capsys.readouterr().out
    events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    invocations = [e for e in events if e["event"] == "agent_invocation"]
    assert [e["principal"] for e in invocations] == ["3412345", "legacy-apigw"]
    assert "legacy-secret-token" not in out


def test_error_log_never_carries_the_token(agent, monkeypatch, capsys):
    def boom(tools):
        raise RuntimeError("bedrock exploded")

    _use_agent(agent, monkeypatch, boom)

    with pytest.raises(RuntimeError):
        _run(agent, LEGACY_MESSAGE)

    assert agent.jobs["agent-job-2"]["status"] == "error"
    assert "legacy-secret-token" not in capsys.readouterr().out
    assert LEGACY_TOKEN not in json.dumps(agent.jobs)


def test_message_without_identity_fails_the_job_without_running(agent, monkeypatch):
    scripted = _use_agent(agent, monkeypatch, lambda tools: "answer")
    ScriptedAgent.instances.clear()

    _run(agent, {"job_id": "agent-job-3", "question": "q?"})

    assert agent.jobs["agent-job-3"]["status"] == "error"
    assert "user_id" in agent.jobs["agent-job-3"]["error"]
    assert scripted not in ScriptedAgent.instances


# ---------------------------------------------------------------------------
# Warm-instance isolation: nothing from one job leaks into the next
# ---------------------------------------------------------------------------


def test_no_state_leaks_between_jobs_on_a_warm_instance(agent, monkeypatch):
    captured_tools = []

    def script(tools):
        captured_tools.append(tools)
        tools["query_neptune"](SELECT)
        return "answer"

    _use_agent(agent, monkeypatch, script)
    run_query = MagicMock(return_value=RESULT)
    monkeypatch.setattr(agent, "run_query", run_query)

    _run(agent, NEW_MESSAGE)
    _run(agent, LEGACY_MESSAGE)

    first, second = run_query.call_args_list
    assert first.kwargs["principal"] == "3412345"
    assert first.kwargs["source_ip"] == "198.51.100.7"
    assert second.kwargs["principal"] == "legacy-apigw"
    assert second.kwargs["source_ip"] != "198.51.100.7"
    assert second.kwargs["job_id"] == "agent-job-2"
    # Each job's trace holds only its own steps.
    assert len(agent.jobs["agent-job-1"]["steps"]) == 2
    assert len(agent.jobs["agent-job-2"]["steps"]) == 2
    # A tool kept from the first job still runs as the first job's caller.
    captured_tools[0]["query_neptune"](SELECT)
    assert run_query.call_args.kwargs["principal"] == "3412345"
    assert len(agent.jobs["agent-job-2"]["steps"]) == 2


def test_no_module_level_caller_state(agent):
    for name in ("_auth_header", "_current_job_id", "_steps"):
        assert not hasattr(agent, name)


def test_retry_attempt_starts_a_clean_trace(agent, monkeypatch):
    calls = []

    def script(tools):
        calls.append(1)
        tools["query_neptune"](SELECT)
        if len(calls) == 1:
            raise RuntimeError("ServiceUnavailableException: try later")
        return "answer"

    _use_agent(agent, monkeypatch, script)
    monkeypatch.setattr(agent, "run_query", MagicMock(return_value=RESULT))
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)

    job = _run(agent, NEW_MESSAGE)

    assert len(calls) == 2
    assert len(job["steps"]) == 2
    assert job["status"] == "complete"


# ---------------------------------------------------------------------------
# Parity rows 1, 2, 12a, 16 — real sagebrain_core on the agent path (Neptune patched)
# ---------------------------------------------------------------------------


@pytest.fixture
def limiter(monkeypatch):
    now = [1000.0]
    fresh = ratelimit.LocalLimiter(clock=lambda: now[0])
    monkeypatch.setattr(ratelimit, "_default_limiter", fresh)
    return fresh


@pytest.fixture
def neptune(monkeypatch):
    monkeypatch.setenv("NEPTUNE_ENDPOINT", "reader.example")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    resp = MagicMock(status_code=200, text=RESULT.text, headers={})
    with patch(
        "sagebrain_core.neptune.requests.post", return_value=resp
    ) as post, patch("sagebrain_core.neptune.SigV4Auth"), patch(
        "sagebrain_core.neptune.botocore.session.Session"
    ):
        yield post


def _queries(n):
    return lambda tools: [tools["query_neptune"](SELECT) for _ in range(n)] and "a"


def test_agent_queries_drain_the_callers_query_buckets(
    agent, monkeypatch, limiter, neptune
):
    _use_agent(agent, monkeypatch, _queries(limits.USER_BURST + 1))

    job = _run(agent, NEW_MESSAGE)

    assert neptune.call_count == limits.USER_BURST
    results = [s for s in job["steps"] if s["type"] == "tool_result"]
    assert all("preview" in s for s in results[:-1])
    assert results[-1]["error"].startswith("Rate limit exceeded")
    assert job["status"] == "complete"
    assert set(limiter.keys()) == {"{rl:query}:global", "{rl:query}:u:3412345"}
    # The same bucket POST /query charges for this caller is now empty...
    with pytest.raises(RateLimited) as exc:
        ratelimit.admit_principal("query", "3412345", limiter)
    assert exc.value.scope == "principal"
    # ...while another caller's is untouched.
    ratelimit.admit_principal("query", "999", limiter)


def test_agent_path_sparql_audit_event(agent, monkeypatch, limiter, neptune, capsys):
    _use_agent(agent, monkeypatch, _queries(1))

    _run(agent, NEW_MESSAGE)

    events = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    (event,) = [e for e in events if e["event"] == "sparql_query"]
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
    assert event["job_id"] == "agent-job-1"
    assert event["source_ip"] == "198.51.100.7"
    assert event["query"] == SELECT
    assert event["status_code"] == 200


def test_over_length_agent_query_never_reaches_neptune(
    agent, monkeypatch, limiter, neptune
):
    over = "SELECT ?s WHERE { ?s ?p ?o }" + " " * limits.QUERY_MAX_CHARS + "LIMIT 1"
    returned = []
    _use_agent(
        agent,
        monkeypatch,
        lambda tools: returned.append(tools["query_neptune"](over)) or "answer",
    )

    job = _run(agent, NEW_MESSAGE)

    neptune.assert_not_called()
    message = f"Query exceeds maximum length of {limits.QUERY_MAX_CHARS} characters"
    assert returned == [f"Error: {message}"]
    assert job["steps"][-1] == {
        "type": "tool_result",
        "tool": "query_neptune",
        "error": message,
    }
    assert job["status"] == "complete"
    # Rejected before admission: no bucket was charged.
    assert limiter.keys() == []


def test_legacy_principal_is_the_one_the_limiter_gives_machine_limits(agent):
    assert agent.LEGACY_PRINCIPAL == ratelimit.LEGACY_AGENT_PRINCIPAL


@pytest.fixture
def shared_limiter(monkeypatch, rate_limit_table):
    """The worker builds its limiter from RATE_LIMIT_TABLE_NAME, as in Lambda (moto table)."""
    from tests.conftest import RATE_LIMIT_TABLE

    monkeypatch.setenv("RATE_LIMIT_TABLE_NAME", RATE_LIMIT_TABLE)
    monkeypatch.setattr(ratelimit, "_default_limiter", None)
    built = ratelimit.default_limiter()
    assert isinstance(built, ratelimit.DynamoLimiter)
    assert built.table_name == RATE_LIMIT_TABLE
    # Same backend, frozen clock, so buckets don't refill while the test runs.
    clock = lambda: 1000.0  # noqa: E731
    monkeypatch.setattr(
        ratelimit,
        "_default_limiter",
        ratelimit.DynamoLimiter(RATE_LIMIT_TABLE, clock=clock),
    )
    # Another process on the same table: the app task that serves POST /query.
    return ratelimit.DynamoLimiter(RATE_LIMIT_TABLE, clock=clock)


def test_parity_row2_agent_drains_the_callers_shared_bucket(
    agent, monkeypatch, shared_limiter, neptune
):
    _use_agent(agent, monkeypatch, _queries(limits.USER_BURST + 1))

    job = _run(agent, NEW_MESSAGE)

    assert neptune.call_count == limits.USER_BURST
    results = [s for s in job["steps"] if s["type"] == "tool_result"]
    assert results[-1]["error"].startswith("Rate limit exceeded")
    assert job["status"] == "complete"
    # The caller's bucket is empty for every other instance too (e.g. the app's POST /query)...
    with pytest.raises(RateLimited) as exc:
        ratelimit.admit_principal("query", "3412345", shared_limiter)
    assert exc.value.scope == "principal"
    # ...while another caller's is untouched.
    ratelimit.admit_principal("query", "999", shared_limiter)


def test_legacy_jobs_share_the_machine_bucket_across_instances(
    agent, monkeypatch, shared_limiter, neptune
):
    """Open question from 1c: legacy jobs get machine limits, not a 5 rps / 20 burst user bucket."""
    _use_agent(agent, monkeypatch, _queries(limits.USER_BURST + 1))

    job = _run(agent, LEGACY_MESSAGE)

    assert neptune.call_count == limits.USER_BURST + 1
    assert job["status"] == "complete"
    for _ in range(limits.MACHINE_BURST - limits.USER_BURST - 1):
        ratelimit.admit_principal("query", "legacy-apigw", shared_limiter)
    with pytest.raises(RateLimited):
        ratelimit.admit_principal("query", "legacy-apigw", shared_limiter)
