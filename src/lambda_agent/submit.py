import json
import os
import time
import uuid

import boto3

DYNAMODB_TABLE = os.environ["JOB_TABLE_NAME"]
SQS_QUEUE_URL = os.environ["JOB_QUEUE_URL"]

_dynamodb = boto3.resource("dynamodb")
_sqs = boto3.client("sqs")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
}

MAX_QUESTION_LENGTH = 2000
JOB_TTL_SECONDS = 86400  # 24 hours


def handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
        question = body.get("question", "").strip()
    except json.JSONDecodeError:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps({"error": "Request body must be valid JSON"}),
        }

    if not question:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps({"error": "Missing 'question' field"}),
        }

    if len(question) > MAX_QUESTION_LENGTH:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps(
                {
                    "error": f"Question exceeds maximum length of {MAX_QUESTION_LENGTH} characters"
                }
            ),
        }

    job_id = str(uuid.uuid4())
    now = time.time()

    _dynamodb.Table(DYNAMODB_TABLE).put_item(
        Item={
            "job_id": job_id,
            "status": "pending",
            "question": question,
            "created_at": int(now),
            "ttl": int(now) + JOB_TTL_SECONDS,
        }
    )

    _sqs.send_message(
        QueueUrl=SQS_QUEUE_URL,
        MessageBody=json.dumps({"job_id": job_id, "question": question}),
    )

    return {
        "statusCode": 202,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": json.dumps({"job_id": job_id, "status": "pending"}),
    }
