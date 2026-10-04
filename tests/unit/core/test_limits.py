"""T101 — sagebrain_core.limits is the single source of threshold values and must equal the spec."""

from pathlib import Path

import yaml

from sagebrain_core import limits

SPEC = yaml.safe_load((Path(__file__).parents[3] / "api" / "openapi.yaml").read_text())
SPEC_LIMITS = SPEC["info"]["x-sagebrain-limits"]


def test_every_spec_limit_has_a_constant_with_the_same_value():
    assert limits.as_spec_dict() == SPEC_LIMITS


def test_constants_are_ints():
    for name, value in limits.as_spec_dict().items():
        assert isinstance(value, int) and value > 0, name
