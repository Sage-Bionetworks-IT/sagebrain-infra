"""Loader for tests/contract/cases.yaml — shared by handler tests, app tests and tools/parity_probe.py."""

import json
import re
import uuid
from pathlib import Path

import yaml

CASES_PATH = Path(__file__).with_name("cases.yaml")

_REPEAT = re.compile(r"\$\{repeat:(.):(\d+)\}")


def _expand(value):
    if isinstance(value, str):
        return _REPEAT.sub(lambda m: m.group(1) * int(m.group(2)), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def load_cases(layer=None, api=None):
    cases = yaml.safe_load(CASES_PATH.read_text())["cases"]
    ids = [c["id"] for c in cases]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate case ids in {CASES_PATH}: {sorted(duplicates)}")
    return [
        _expand(c)
        for c in cases
        if (layer is None or c["layer"] == layer) and (api is None or c["api"] == api)
    ]


def request_body(case) -> str:
    """The raw request body string for a case ('' when none)."""
    req = case["request"]
    if "json" in req:
        return json.dumps(req["json"])
    return req.get("body", "")


def job_id_for(case) -> str:
    item = (case.get("given") or {}).get("item")
    return item["job_id"] if item else str(uuid.uuid4())


def request_path(case, job_id: str) -> str:
    return case["request"]["path"].replace("{job_id}", job_id)


def check_response(expect: dict, status: int, body, headers=None) -> None:
    """Assert an HTTP-level expectation. `body` is the decoded JSON (or None)."""
    assert status == expect["status"], f"status {status} != {expect['status']}: {body}"
    if "body" in expect:
        assert body == expect["body"], f"body {body!r} != {expect['body']!r}"
    if "body_subset" in expect:
        assert_subset(expect["body_subset"], body, "body")
    if "body_keys" in expect:
        assert sorted(body) == sorted(expect["body_keys"]), f"keys {sorted(body)}"
    for name, value in (expect.get("headers") or {}).items():
        got = {k.lower(): v for k, v in (headers or {}).items()}.get(name.lower())
        assert got == value, f"header {name}: {got!r} != {value!r}"


def assert_subset(expected: dict, actual: dict, what: str) -> None:
    for key, value in expected.items():
        assert key in actual, f"{what} missing {key!r}: {actual!r}"
        assert actual[key] == value, f"{what}[{key!r}] {actual[key]!r} != {value!r}"
