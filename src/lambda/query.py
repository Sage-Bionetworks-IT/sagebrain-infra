import json
import os
from decimal import Decimal

import boto3
from sagebrain_core.neptune import NeptuneConfig, execute_query  # Lambda layer

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


def _execute_job(job: dict):
    """Run one admitted query job. Validation and rate limiting happened at submit time."""
    job_id = job["job_id"]
    _update_job(job_id, status="running")
    try:
        # Signing, the Neptune timeout and the sparql_query audit log live in sagebrain_core.
        result = execute_query(
            job["query"],
            config=NeptuneConfig.from_env(),
            job_id=job_id,
            source=job.get("source", "direct"),
            principal=job.get("user_id", "unknown"),
            source_ip=job.get("source_ip", "unknown"),
            user_agent=job.get("user_agent", "unknown"),
        )
    except Exception as e:
        _update_job(job_id, status="error", error=str(e))
        raise
    # TODO: response.text is stored raw in DynamoDB; large result sets can exceed
    # the 400KB item size limit, causing this update to fail even though Neptune
    # succeeded. Consider truncating at ~300KB or offloading results to S3.
    _update_job(
        job_id,
        status="complete",
        results=result.text,
        content_type=result.content_type,
        duration_ms=round(result.duration_ms, 2),
    )


def handler(event, context):
    """SQS-triggered worker. Each record is one SPARQL query job."""
    for record in event["Records"]:
        _execute_job(json.loads(record["body"]))
