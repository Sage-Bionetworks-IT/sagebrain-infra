import json
import os
import re
import time
from decimal import Decimal
from urllib.parse import urlencode

import boto3
import botocore.session
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

NEPTUNE_ENDPOINT = os.environ["NEPTUNE_ENDPOINT"]
REGION = os.environ["AWS_REGION"]
DYNAMODB_TABLE = os.environ["JOB_TABLE_NAME"]
REBAC_AUTHORIZE_FUNCTION_NAME = os.environ.get(
    "REBAC_AUTHORIZE_FUNCTION_NAME", ""
).strip()

_dynamodb = boto3.resource("dynamodb")
_lambda = boto3.client("lambda")


def _update_job(job_id: str, **fields):
    table = _dynamodb.Table(DYNAMODB_TABLE)
    update_expr = "SET " + ", ".join(f"#{k} = :{k}" for k in fields)
    expr_names = {f"#{k}": k for k in fields}
    # DynamoDB doesn't support float — convert to Decimal
    expr_values = {
        f":{k}": Decimal(str(v)) if isinstance(v, float) else v
        for k, v in fields.items()
    }
    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


def _extract_resource_ids_from_results(results_json: str) -> set[str]:
    """Extract Synapse resource IDs from SPARQL query results."""
    try:
        results = json.loads(results_json)
    except json.JSONDecodeError:
        return set()

    resource_ids = set()
    # SPARQL JSON format: results.bindings is an array of binding objects
    bindings = results.get("results", {}).get("bindings", [])

    # Pattern to match Synapse IDs in URIs or literal values
    patterns = [
        re.compile(r"https?://[^\s<>\"']+/Synapse:(?:syn)?(?P<id>\d+)", re.IGNORECASE),
        re.compile(r"(?:syn:|syn)(?P<id>\d+)", re.IGNORECASE),
    ]

    for binding in bindings:
        for var_binding in binding.values():
            value = var_binding.get("value", "")
            for pattern in patterns:
                for match in pattern.finditer(str(value)):
                    resource_id = f"syn{match.group('id')}"
                    resource_ids.add(resource_id)

    return resource_ids


def _authorize_results(user_id: str, resource_ids: list[str]) -> dict | None:
    """
    Check ReBAC authorization for resource IDs found in results.
    Returns error dict if access denied, None if authorized or ReBAC disabled.
    """
    if not REBAC_AUTHORIZE_FUNCTION_NAME or not resource_ids:
        return None

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
        return {
            "error": f"Authorization check unavailable: {exc}",
            "denied_resources": resource_ids,
        }

    if response.get("FunctionError"):
        return {
            "error": f"Authorization check failed: {response['FunctionError']}",
            "denied_resources": resource_ids,
        }

    payload = response.get("Payload")
    if payload is None:
        return {
            "error": "Authorization check returned no payload.",
            "denied_resources": resource_ids,
        }

    try:
        payload_bytes = payload.read() if hasattr(payload, "read") else payload
        payload_obj = json.loads(
            payload_bytes.decode("utf-8")
            if isinstance(payload_bytes, (bytes, bytearray))
            else payload_bytes
        )
    except (TypeError, ValueError):
        return {
            "error": "Authorization check returned invalid JSON.",
            "denied_resources": resource_ids,
        }

    if isinstance(payload_obj, dict) and "statusCode" in payload_obj:
        body = (
            json.loads(payload_obj.get("body") or "{}")
            if payload_obj.get("body")
            else {}
        )
    else:
        body = payload_obj

    authorized = body.get("authorized_resource_ids", [])
    denied = body.get("denied_resource_ids", [])

    # All-or-nothing access: if ANY resource is denied, deny entire query
    if denied:
        denied_list = ", ".join(denied)
        error_msg = (
            f"Access denied. You don't have permission to access: {denied_list}. "
            "Please request access to these resources."
        )
        return {
            "error": error_msg,
            "denied_resources": denied,
            "authorized_resources": authorized,
        }

    return None


def _log_query(
    job_id: str,
    query: str,
    source: str,
    source_ip: str,
    user_agent: str,
    status_code: int,
    duration_ms: float,
):
    print(
        json.dumps(
            {
                "event": "sparql_query",
                "job_id": job_id,
                "query": query,
                "query_length": len(query),
                "source": source,
                "source_ip": source_ip,
                "user_agent": user_agent,
                "status_code": status_code,
                "duration_ms": round(duration_ms, 2),
                "timestamp": time.time(),
            }
        )
    )


def _execute_query(
    job_id: str, query: str, source: str, source_ip: str, user_agent: str, user_id: str
):
    start = time.time()
    _update_job(job_id, status="running")

    url = f"https://{NEPTUNE_ENDPOINT}:8182/sparql"
    body = urlencode({"query": query})
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/sparql-results+json",
    }

    session = botocore.session.Session()
    credentials = session.get_credentials()
    aws_request = AWSRequest(method="POST", url=url, data=body, headers=headers)
    SigV4Auth(credentials, "neptune-db", REGION).add_auth(aws_request)

    try:
        response = requests.post(
            url,
            data=body,
            headers=dict(aws_request.headers),
            timeout=60,  # Neptune can be slow on complex queries
        )
        response.raise_for_status()
        content_type = response.headers.get(
            "Content-Type", "application/sparql-results+json"
        )
        duration = (time.time() - start) * 1000
        _log_query(job_id, query, source, source_ip, user_agent, 200, duration)

        # Post-query ReBAC authorization: extract resource IDs and check access
        resource_ids = _extract_resource_ids_from_results(response.text)
        if resource_ids:
            auth_error = _authorize_results(user_id, sorted(resource_ids))
            if auth_error:
                # All-or-nothing: if any node is denied, deny entire query
                print(
                    json.dumps(
                        {
                            "event": "sparql_access_denied",
                            "job_id": job_id,
                            "user_id": user_id,
                            "denied_resources": auth_error.get("denied_resources", []),
                            "timestamp": time.time(),
                        }
                    )
                )
                _update_job(
                    job_id,
                    status="error",
                    error=auth_error["error"],
                    denied_resources=auth_error.get("denied_resources", []),
                    duration_ms=round(duration, 2),
                )
                return

        # TODO: response.text is stored raw in DynamoDB; large result sets can exceed
        # the 400KB item size limit, causing this update to fail even though Neptune
        # succeeded. Consider truncating at ~300KB or offloading results to S3.
        _update_job(
            job_id,
            status="complete",
            results=response.text,
            content_type=content_type,
            duration_ms=round(duration, 2),
        )
    except requests.exceptions.HTTPError as e:
        duration = (time.time() - start) * 1000
        status_code = e.response.status_code if e.response else 500
        _log_query(job_id, query, source, source_ip, user_agent, status_code, duration)
        _update_job(job_id, status="error", error=str(e))
        raise
    except Exception as e:
        duration = (time.time() - start) * 1000
        _log_query(job_id, query, source, source_ip, user_agent, 500, duration)
        _update_job(job_id, status="error", error=str(e))
        raise


def handler(event, context):
    """SQS-triggered worker. Each record is one SPARQL query job."""
    for record in event["Records"]:
        body = json.loads(record["body"])
        _execute_query(
            job_id=body["job_id"],
            query=body["query"],
            source=body.get("source", "direct"),
            source_ip=body.get("source_ip", "unknown"),
            user_agent=body.get("user_agent", "unknown"),
            user_id=body.get("user_id", "unknown"),
        )
