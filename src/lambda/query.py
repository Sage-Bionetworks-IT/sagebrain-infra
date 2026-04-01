import json
import os
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

_dynamodb = boto3.resource("dynamodb")


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
    job_id: str, query: str, source: str, source_ip: str, user_agent: str
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
        _log_query(
            job_id, query, source, source_ip, user_agent, response.status_code, duration
        )
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
        )
