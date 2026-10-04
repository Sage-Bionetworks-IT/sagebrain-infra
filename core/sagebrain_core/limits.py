"""Threshold values. Must equal api/openapi.yaml info.x-sagebrain-limits (tests/unit/core/test_limits.py)."""

QUERY_MAX_CHARS = 8000
QUESTION_MAX_CHARS = 2000
NEPTUNE_QUERY_TIMEOUT_SECONDS = 60

# Token buckets: rate = refill per second, burst = bucket capacity.
GLOBAL_RATE_PER_API_RPS = 50
GLOBAL_BURST_PER_API = 100
USER_RATE_RPS = 5
USER_BURST = 20
MACHINE_RATE_RPS = 25
MACHINE_BURST = 50

MAX_BODY_BYTES = 262144
REQUEST_TIMEOUT_SECONDS = 10
AUTH_TIMEOUT_SECONDS = 10
SYNAPSE_CALL_TIMEOUT_SECONDS = 5
AUTH_CACHE_TTL_SECONDS = 300
JOB_TTL_SECONDS = 86400


def as_spec_dict() -> dict:
    """The limits keyed the way the spec's x-sagebrain-limits names them."""
    return {
        name.lower(): value
        for name, value in globals().items()
        if name.isupper() and isinstance(value, int)
    }
