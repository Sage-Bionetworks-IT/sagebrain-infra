"""T102 — validate_query / validate_question satisfy the shared cases.yaml vectors."""

import json

import pytest

from sagebrain_core.errors import QueryRejected
from sagebrain_core import limits
from sagebrain_core.validation import (
    validate_query,
    validate_question,
    validate_source,
)
from tests.contract.cases import load_cases

VALIDATORS = {"query": validate_query, "ask": validate_question}
FIELDS = {"query": "query", "ask": "question"}


def _field_cases():
    """Submit vectors whose body is a JSON object — body parsing itself is the HTTP layer's job."""
    out = []
    for case in load_cases(layer="handler"):
        if case["request"]["method"] != "POST":
            continue
        req = case["request"]
        raw = json.dumps(req["json"]) if "json" in req else req.get("body", "")
        try:
            body = json.loads(raw or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(body, dict):
            out.append((case, body))
    return out


CASES = _field_cases()


@pytest.mark.parametrize("case,body", CASES, ids=[c["id"] for c, _ in CASES])
def test_validator_matches_contract(case, body):
    validate = VALIDATORS[case["api"]]
    value = body.get(FIELDS[case["api"]])
    headers = {k.lower(): v for k, v in (case["request"].get("headers") or {}).items()}
    expect = case["expect"]

    def run():
        if case["api"] == "query":
            validate_source(headers.get("x-source"))
        return validate(value)

    if expect["status"] == 202:
        assert run() == value.strip()
    else:
        with pytest.raises(QueryRejected) as exc:
            run()
        assert exc.value.message == expect["body"]["error"]
        assert exc.value.status == 400


def test_query_limit_boundary():
    assert len(validate_query("a" * 8000)) == 8000
    with pytest.raises(QueryRejected):
        validate_query("a" * 8001)


def test_question_limit_boundary():
    assert len(validate_question("q" * 2000)) == 2000
    with pytest.raises(QueryRejected):
        validate_question("q" * 2001)


def test_source_defaults_to_direct():
    assert validate_source(None) == "direct"


def test_source_limit_boundary():
    # Not stripped: the header value is recorded verbatim, as today.
    assert validate_source("s" * limits.SOURCE_MAX_CHARS) == "s" * 64
    with pytest.raises(QueryRejected) as exc:
        validate_source("s" * (limits.SOURCE_MAX_CHARS + 1))
    assert exc.value.message == "'X-Source' exceeds maximum length of 64 characters"
