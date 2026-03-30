import json
import os

import boto3

DYNAMODB_TABLE = os.environ["JOB_TABLE_NAME"]

_dynamodb = boto3.resource("dynamodb")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
}


def handler(event, context):
    job_id = (event.get("pathParameters") or {}).get("job_id", "").strip()

    if not job_id:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps({"error": "Missing job_id"}),
        }

    response = _dynamodb.Table(DYNAMODB_TABLE).get_item(Key={"job_id": job_id})
    item = response.get("Item")

    if not item:
        return {
            "statusCode": 404,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps({"error": "Job not found"}),
        }

    status = item["status"]
    result = {"job_id": job_id, "status": status}

    if status == "complete":
        result["results"] = item.get("results", {})
        result["content_type"] = item.get(
            "content_type", "application/sparql-results+json"
        )
    elif status == "error":
        result["error"] = item.get("error", "Unknown error")

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": json.dumps(result),
    }
