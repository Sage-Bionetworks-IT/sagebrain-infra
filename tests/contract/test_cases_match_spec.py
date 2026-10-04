"""Keep api/openapi.yaml and tests/contract/cases.yaml in agreement.

Every expected response body in cases.yaml must validate against the schema the spec
declares for that operation + status code, and every request body the spec should accept
(202 cases) must validate against its request schema. If you change one file, this test
tells you the other needs to change too.
"""

from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from tests.contract.cases import load_cases

SPEC_PATH = Path(__file__).parents[2] / "api" / "openapi.yaml"
SPEC = yaml.safe_load(SPEC_PATH.read_text())
SPEC_URI = "urn:sagebrain:openapi"
REGISTRY = Registry().with_resource(SPEC_URI, Resource(SPEC, DRAFT202012))


def _validator(schema: dict) -> Draft202012Validator:
    # Rewrite local "#/components/..." refs to point at the registered spec document.
    return Draft202012Validator(_absolute_refs(schema), registry=REGISTRY)


def _absolute_refs(node):
    if isinstance(node, dict):
        return {
            k: (
                SPEC_URI + v if k == "$ref" and v.startswith("#") else _absolute_refs(v)
            )
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_absolute_refs(v) for v in node]
    return node


def _resolve(node: dict) -> dict:
    while "$ref" in node:
        target = SPEC
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target[part]
        node = target
    return node


def _operation(case) -> dict:
    method = case["request"]["method"].lower()
    return SPEC["paths"][_spec_path(case["request"]["path"])][method]


def _spec_path(path: str) -> str:
    if path in SPEC["paths"]:
        return path
    return path.rsplit("/", 1)[0] + "/{job_id}"


DOCUMENTED = [c for c in load_cases() if not c.get("undocumented")]
CASES = [c for c in DOCUMENTED if "body" in c["expect"] or "body_subset" in c["expect"]]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_expected_body_conforms_to_spec(case):
    responses = _operation(case)["responses"]
    status = str(case["expect"]["status"])
    assert status in responses, f"{case['id']}: spec has no {status} response"

    response = _resolve(responses[status])
    schema = response["content"]["application/json"]["schema"]
    body = case["expect"].get("body")
    if body is not None:
        _validator(schema).validate(body)

    legacy = case.get("legacy") or {}
    if "body" in legacy and str(legacy["status"]) == status:
        # A legacy body at the same status must still fit the declared schema (e.g. D-10).
        _validator(schema).validate(legacy["body"])


SUBMIT_OK = [
    c
    for c in load_cases(layer="handler")
    if c["request"]["method"] == "POST" and c["expect"]["status"] == 202
]


@pytest.mark.parametrize("case", SUBMIT_OK, ids=[c["id"] for c in SUBMIT_OK])
def test_accepted_request_bodies_conform_to_spec(case):
    schema = _operation(case)["requestBody"]["content"]["application/json"]["schema"]
    body = {
        k: v.strip() if isinstance(v, str) else v
        for k, v in case["request"]["json"].items()
    }
    _validator(schema).validate(body)


def test_every_status_code_in_cases_is_declared():
    for case in DOCUMENTED:
        responses = _operation(case)["responses"]
        assert str(case["expect"]["status"]) in responses, case["id"]


def test_every_spec_operation_has_a_case():
    """Contract-first: an operation added to the spec must come with at least one vector."""
    covered = {
        (_spec_path(c["request"]["path"]), c["request"]["method"].lower())
        for c in DOCUMENTED
    }
    declared = {
        (path, method)
        for path, ops in SPEC["paths"].items()
        for method in ops
        if method != "parameters"
    }
    missing = declared - covered - {("/healthz", "get")}
    assert not missing, f"spec operations without cases.yaml vectors: {sorted(missing)}"


def test_public_operations_are_exactly_health_and_docs():
    public = {
        (path, method)
        for path, ops in SPEC["paths"].items()
        for method, op in ops.items()
        if op.get("security") == []
    }
    assert public == {
        ("/healthz", "get"),
        ("/api", "get"),
        ("/api/openapi.json", "get"),
        ("/api/openapi.yaml", "get"),
    }


def test_limits_extension_matches_schema_limits():
    limits = SPEC["info"]["x-sagebrain-limits"]
    schemas = SPEC["components"]["schemas"]
    assert (
        schemas["QueryRequest"]["properties"]["query"]["maxLength"]
        == limits["query_max_chars"]
    )
    assert (
        schemas["AskRequest"]["properties"]["question"]["maxLength"]
        == limits["question_max_chars"]
    )
