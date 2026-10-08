"""T117 — Schemathesis: generated requests against the app; every response conforms to the spec.

The schema is loaded from the app's own /api/openapi.json, so this also checks the published
document. Excluded checks:
  - positive_data_acceptance: `x-strip-before-validate` fields make "   " schema-valid but a
    correct 400 (FR-1/FR-3), which JSON Schema can't express.
  - negative_data_rejection / missing_required_header: the legacy behaviour accepts any
    X-Source and a missing/empty body maps to "Missing 'query' field"; covered by cases.yaml.
  - stateful checks (use_after_free, ensure_resource_availability): no delete operations.
"""

import httpx
import schemathesis
from hypothesis import HealthCheck, settings
from schemathesis.checks import not_a_server_error
from schemathesis.specs.openapi.checks import (
    allow_header_conformance,
    content_type_conformance,
    response_headers_conformance,
    response_schema_conformance,
    status_code_conformance,
    unsupported_method,
)

from sagebrain_api.jobs import JobStore
from sagebrain_api.main import create_app
from sagebrain_api.settings import Settings
from sagebrain_core.ratelimit import Decision

from .conftest import AUTH_API, MACHINE_KEY, REPO_API, TEAM_ID, FakeSqs, FakeSynapse

STEPS = [
    {"type": "tool_call", "tool": "query_neptune", "sparql": "SELECT 1"},
    {"type": "tool_result", "tool": "query_neptune", "preview": "{}"},
]


class EveryJobExists:
    """Every id is a job, cycling through the states so 200 bodies are exercised too."""

    def __init__(self, extra):
        self.extra = extra

    def get_item(self, Key):
        job_id = Key["job_id"]
        status = ("pending", "running", "complete", "error")[len(job_id) % 4]
        if len(job_id) % 5 == 0:
            return {}  # some 404s
        return {"Item": {"job_id": job_id, "status": status, **self.extra}}

    def put_item(self, Item):
        pass


class AllowAll:
    def acquire(self, key, rate, burst):
        return Decision(True)


def _app(tmp_dir):
    app_settings = Settings(
        synapse_team_id=TEAM_ID,
        machine_api_key=MACHINE_KEY,
        query_job_table_name="q",
        query_job_queue_url="q",
        ask_job_table_name="a",
        ask_job_queue_url="a",
        synapse_repo_api=REPO_API,
        synapse_auth_api=AUTH_API,
        swagger_ui_dir=tmp_dir,
    )
    return create_app(
        app_settings,
        query_jobs=JobStore(EveryJobExists({"results": "{}"}), FakeSqs(), "q"),
        ask_jobs=JobStore(
            EveryJobExists({"steps": STEPS, "status_detail": "working"}), FakeSqs(), "a"
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(FakeSynapse())),
        limiter=AllowAll(),
    )


APP = _app("/nonexistent")
schema = schemathesis.openapi.from_asgi("/api/openapi.json", APP)

CHECKS = [
    not_a_server_error,
    status_code_conformance,
    content_type_conformance,
    response_schema_conformance,
    response_headers_conformance,
    unsupported_method,
    allow_header_conformance,
]


@schema.parametrize()
@settings(
    max_examples=40,
    deadline=None,
    database=None,
    suppress_health_check=list(HealthCheck),
)
def test_responses_conform_to_spec(case):
    case.call_and_validate(headers={"x-api-key": MACHINE_KEY}, checks=CHECKS)
