"""Submit and status handlers for the unified API (spec 001 edge stack, `app-{env}-api`).

Four Lambdas share this module, each with its handler set to one function here:
  submit_query    POST /query          → query job table + query queue
  query_status    GET  /query/{job_id} → query job table
  submit_question POST /ask            → ask job table + ask queue
  ask_status      GET  /ask/{job_id}   → ask job table

Validation and limit values come from the sagebrain_core layer, so these handlers and the
agent's in-process queries apply the same rules. Responses follow api/openapi.yaml; the
vectors in tests/contract/cases.yaml pin them.
"""

import base64
import functools
import json
import logging
import os
import time
import uuid

import boto3
from botocore.config import Config
from sagebrain_core import limits
from sagebrain_core.errors import QueryRejected
from sagebrain_core.validation import (
    validate_query,
    validate_question,
    validate_source,
)

log = logging.getLogger()
log.setLevel(logging.INFO)

CORS_HEADERS = {"Access-Control-Allow-Origin": "*"}

# Two attempts at 1 s connect + 3 s read each stay under the 10 s integration timeout, so a
# slow AWS call surfaces as this Lambda's error rather than an API Gateway 504.
_AWS_CONFIG = Config(
    connect_timeout=1,
    read_timeout=3,
    retries={"mode": "standard", "total_max_attempts": 2},
)


@functools.cache
def _dynamodb():
    return boto3.resource("dynamodb", config=_AWS_CONFIG)


@functools.cache
def _sqs():
    return boto3.client("sqs", config=_AWS_CONFIG)


class _Reply(Exception):
    """A client error, raised from anywhere in a handler and returned as the response."""

    def __init__(self, status: int, body: dict):
        super().__init__(body)
        self.response = _response(status, body)


def _response(status: int, body: dict, headers: dict | None = None) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            **CORS_HEADERS,
            **(headers or {}),
        },
        "body": json.dumps(body),
    }


def _client_errors(handler):
    @functools.wraps(handler)
    def wrapper(event, context):
        try:
            return handler(event, context)
        except QueryRejected as e:
            return _response(e.status, {"error": e.message})
        except _Reply as reply:
            return reply.response

    return wrapper


def _headers(event) -> dict:
    """Request headers with lowercase names (D-1: HTTP header names are case-insensitive)."""
    return {k.lower(): v for k, v in (event.get("headers") or {}).items()}


def _body_field(event, field: str):
    """The JSON body's `field` (None when absent). 413 over max_body_bytes (D-5), 400 if not an object (D-2)."""
    raw = event.get("body") or ""
    try:
        data = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()
    except ValueError:
        raise QueryRejected("Request body must be valid JSON")
    if len(data) > limits.MAX_BODY_BYTES:
        raise _Reply(413, {"message": "Request Too Long"})
    try:
        body = json.loads(data or b"{}")
    except ValueError:  # JSONDecodeError, or bytes that aren't UTF-8/16/32
        raise QueryRejected("Request body must be valid JSON")
    if not isinstance(body, dict):
        raise QueryRejected("Request body must be a JSON object")
    return body.get(field)


def _caller(event) -> tuple[str, str]:
    """(user_id from the authorizer, source IP from API Gateway — never a client header)."""
    request_context = event.get("requestContext") or {}
    user_id = (request_context.get("authorizer") or {}).get("user_id", "unknown")
    source_ip = (request_context.get("identity") or {}).get("sourceIp", "unknown")
    return user_id, source_ip


def _create_job(extra: dict, message: dict) -> str:
    """Store a pending job item and enqueue its message; returns the new job_id."""
    job_id = str(uuid.uuid4())
    now = int(time.time())
    _dynamodb().Table(os.environ["JOB_TABLE_NAME"]).put_item(
        Item={
            "job_id": job_id,
            "status": "pending",
            **extra,
            "created_at": now,
            "ttl": now + limits.JOB_TTL_SECONDS,
        }
    )
    _sqs().send_message(
        QueueUrl=os.environ["JOB_QUEUE_URL"],
        MessageBody=json.dumps({"job_id": job_id, **message}),
    )
    return job_id


def _load_job(event) -> tuple[str, dict, dict]:
    """(job_id, the job's item, the base response body). 400 for a bad id, 404 if unknown."""
    job_id = ((event.get("pathParameters") or {}).get("job_id") or "").strip()
    if not job_id or len(job_id) > limits.JOB_ID_MAX_CHARS:
        raise _Reply(400, {"error": "Missing job_id"})
    item = (
        _dynamodb()
        .Table(os.environ["JOB_TABLE_NAME"])
        .get_item(Key={"job_id": job_id})
        .get("Item")
    )
    if not item:
        raise _Reply(404, {"error": "Job not found"})
    return job_id, item, {"job_id": job_id, "status": item["status"]}


def _status_response(result: dict) -> dict:
    # D-15: tell pollers the minimum interval while the job is still in flight
    headers = (
        {"Retry-After": str(limits.POLL_INTERVAL_SECONDS)}
        if result["status"] in ("pending", "running")
        else None
    )
    return _response(200, result, headers)


@_client_errors
def submit_query(event, context):
    headers = _headers(event)
    query = validate_query(_body_field(event, "query"))
    source = validate_source(headers.get("x-source"))
    user_id, source_ip = _caller(event)

    job_id = _create_job(
        {},
        {
            "query": query,
            "source": source,
            "source_ip": source_ip,
            "user_agent": headers.get("user-agent", "unknown"),
            "user_id": user_id,  # D-19: the worker logs it as the query's principal
        },
    )
    log.info(
        json.dumps(
            {
                "event": "query_submitted",
                "job_id": job_id,
                "user_id": user_id,
                "source_ip": source_ip,
                "source": source,
            }
        )
    )
    return _response(202, {"job_id": job_id, "status": "pending"})


@_client_errors
def query_status(event, context):
    _, item, result = _load_job(event)
    if item["status"] == "complete":
        result["results"] = item.get("results", "")
        result["content_type"] = item.get(
            "content_type", "application/sparql-results+json"
        )
    elif item["status"] == "error":
        result["error"] = item.get("error", "Unknown error")
    return _status_response(result)


@_client_errors
def submit_question(event, context):
    question = validate_question(_body_field(event, "question"))
    user_id, source_ip = _caller(event)

    # D-8: no credential is enqueued; the agent runs its queries as user_id
    job_id = _create_job(
        {"question": question},
        {"question": question, "user_id": user_id, "source_ip": source_ip},
    )
    log.info(
        json.dumps(
            {
                "event": "question_submitted",
                "job_id": job_id,
                "user_id": user_id,
                "source_ip": source_ip,
            }
        )
    )
    return _response(202, {"job_id": job_id, "status": "pending"})


@_client_errors
def ask_status(event, context):
    _, item, result = _load_job(event)
    if "status_detail" in item:
        result["status_detail"] = item["status_detail"]
    # Steps at every status, so callers can watch the agent's SPARQL calls in flight
    if item.get("steps"):
        result["steps"] = item["steps"]
    if item["status"] == "complete":
        result["answer"] = item.get("answer", "")
    elif item["status"] == "error":
        result["error"] = item.get("error", "Unknown error")
    return _status_response(result)
