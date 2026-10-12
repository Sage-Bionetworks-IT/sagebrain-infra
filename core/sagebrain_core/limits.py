"""Threshold values. Must equal api/openapi.yaml info.x-sagebrain-limits (tests/unit/core/test_limits.py)."""

QUERY_MAX_CHARS = 8000
QUESTION_MAX_CHARS = 2000
SOURCE_MAX_CHARS = 64  # X-Source header, recorded in the sparql_query audit log
NEPTUNE_QUERY_TIMEOUT_SECONDS = 60

# API Gateway throttle, applied per method; the status methods get their own override.
METHOD_RATE_RPS = 50
METHOD_BURST = 100
STATUS_RATE_RPS = 100
STATUS_BURST = 200
POLL_INTERVAL_SECONDS = 2  # Retry-After on pending/running status responses
THROTTLED_RETRY_AFTER_SECONDS = 1
AGENT_WORKER_CONCURRENCY = 10  # the agent's rate bound (constitution V)

MAX_RESULT_BYTES = 350000  # under DynamoDB's 400 KB item limit
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
