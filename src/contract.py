"""The committed API contract (api/openapi.yaml), read at synth time.

Infra settings that are contract thresholds (throttles, timeouts, retry hints) come from
`LIMITS`, so the spec and the deployed stack can't disagree (constitution III).
"""

from pathlib import Path

import yaml

SPEC_PATH = Path(__file__).parents[1] / "api" / "openapi.yaml"
SPEC = yaml.safe_load(SPEC_PATH.read_text())
LIMITS = SPEC["info"]["x-sagebrain-limits"]


def operations() -> set[tuple[str, str]]:
    """Every (path, METHOD) the contract declares."""
    return {
        (path, method.upper())
        for path, ops in SPEC["paths"].items()
        for method in ops
        if method != "parameters"
    }


def public_operations() -> set[tuple[str, str]]:
    """Operations with `security: []` — served without the authorizer."""
    return {
        (path, method.upper())
        for path, ops in SPEC["paths"].items()
        for method, op in ops.items()
        if method != "parameters" and op.get("security") == []
    }
