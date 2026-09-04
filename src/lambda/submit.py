import json
import logging
import os
import re
import time
import uuid

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

DYNAMODB_TABLE = os.environ["JOB_TABLE_NAME"]
SQS_QUEUE_URL = os.environ["JOB_QUEUE_URL"]
REBAC_AUTHORIZE_FUNCTION_NAME = os.environ.get(
    "REBAC_AUTHORIZE_FUNCTION_NAME", ""
).strip()

_dynamodb = boto3.resource("dynamodb")
_sqs = boto3.client("sqs")
_lambda = boto3.client("lambda")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
}

MAX_QUERY_LENGTH = 8000
JOB_TTL_SECONDS = 86400  # 24 hours


def _extract_resource_ids(query: str) -> list[str]:
    """Extract explicit Synapse resource IDs from a SPARQL query."""
    resource_ids = set()
    patterns = [
        re.compile(r"https?://[^\s<>\"']+/Synapse:(?:syn)?(?P<id>\d+)", re.IGNORECASE),
        re.compile(r"(?:syn:|syn)(?P<id>\d+)", re.IGNORECASE),
    ]
    for pattern in patterns:
        for match in pattern.finditer(query):
            resource_id = f"syn{match.group('id')}"
            resource_ids.add(resource_id)
    return sorted(resource_ids)


def _authorize_query(event: dict, user_id: str, query: str) -> None:
    if not REBAC_AUTHORIZE_FUNCTION_NAME:
        return

    resource_ids = _extract_resource_ids(query)
    if not resource_ids:
        raise PermissionError(
            "ReBAC enforcement is enabled but the query does not reference explicit Synapse resources."
        )

    try:
        response = _lambda.invoke(
            FunctionName=REBAC_AUTHORIZE_FUNCTION_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps(
                {
                    "principal_id": str(user_id),
                    "action": "ACCESS",
                    "resource_ids": resource_ids,
                }
            ),
        )
    except Exception as exc:
        raise PermissionError(f"ReBAC authorization unavailable: {exc}") from exc

    if response.get("FunctionError"):
        raise PermissionError(
            f"ReBAC authorization failed: {response['FunctionError']}"
        )

    payload = response.get("Payload")
    if payload is None:
        raise PermissionError("ReBAC authorization returned no payload.")
    try:
        payload_bytes = payload.read() if hasattr(payload, "read") else payload
        payload_obj = json.loads(
            payload_bytes.decode("utf-8")
            if isinstance(payload_bytes, (bytes, bytearray))
            else payload_bytes
        )
    except (TypeError, ValueError) as exc:
        raise PermissionError("ReBAC authorization returned invalid JSON.") from exc

    if isinstance(payload_obj, dict) and "statusCode" in payload_obj:
        body = (
            json.loads(payload_obj.get("body") or "{}")
            if payload_obj.get("body")
            else {}
        )
    else:
        body = payload_obj

    if body.get("decision") != "ALLOW":
        raise PermissionError(body.get("reason") or "ReBAC policy denied this query.")

    authorized = body.get("authorized_resource_ids") or []
    if not authorized:
        raise PermissionError("No authorized Synapse resources matched this query.")


def handler(event, context):
    user_id = (
        event.get("requestContext", {}).get("authorizer", {}).get("user_id", "unknown")
    )

    try:
        body = json.loads(event.get("body") or "{}")
        query = body.get("query", "").strip()
    except json.JSONDecodeError:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps({"error": "Request body must be valid JSON"}),
        }

    if not query:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps({"error": "Missing 'query' field"}),
        }

    if len(query) > MAX_QUERY_LENGTH:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps(
                {
                    "error": f"Query exceeds maximum length of {MAX_QUERY_LENGTH} characters"
                }
            ),
        }

    # ReBAC authorization moved to post-query filtering in query.py worker
    # Synapse team authorization still enforced by API Gateway authorizer

    # Capture caller metadata for audit logging by the worker
    headers = event.get("headers") or {}
    source_ip = (
        event.get("requestContext", {}).get("identity", {}).get("sourceIp", "unknown")
    )

    job_id = str(uuid.uuid4())
    now = time.time()

    _dynamodb.Table(DYNAMODB_TABLE).put_item(
        Item={
            "job_id": job_id,
            "status": "pending",
            "created_at": int(now),
            "ttl": int(now) + JOB_TTL_SECONDS,
        }
    )

    _sqs.send_message(
        QueueUrl=SQS_QUEUE_URL,
        MessageBody=json.dumps(
            {
                "job_id": job_id,
                "query": query,
                "source": headers.get("X-Source", "direct"),
                "source_ip": source_ip,
                "user_agent": headers.get("User-Agent", "unknown"),
                "user_id": user_id,
            }
        ),
    )

    log.info(
        json.dumps(
            {
                "event": "query_submitted",
                "job_id": job_id,
                "user_id": user_id,
                "source_ip": source_ip,
                "source": headers.get("X-Source", "direct"),
            }
        )
    )

    return {
        "statusCode": 202,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": json.dumps({"job_id": job_id, "status": "pending"}),
    }
