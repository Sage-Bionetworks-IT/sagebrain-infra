import json
import logging
import os
import time
import uuid

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

DYNAMODB_TABLE = os.environ["JOB_TABLE_NAME"]
SQS_QUEUE_URL = os.environ["JOB_QUEUE_URL"]

_dynamodb = boto3.resource("dynamodb")
_sqs = boto3.client("sqs")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
}

MAX_QUERY_LENGTH = 8000
JOB_TTL_SECONDS = 86400  # 24 hours


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
