import json
import os
from decimal import Decimal

import boto3
from sagebrain_core import limits  # Lambda layer
from sagebrain_core.neptune import NeptuneConfig, execute_query
from sagebrain_core.query_service import LEGACY_PRINCIPAL

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


RESULT_TOO_LARGE = "Result exceeds 350 KB; add LIMIT or narrow the query"


def _execute_job(job: dict):
    """Run one admitted query job. Validation happened at submit time."""
    job_id = job["job_id"]
    _update_job(job_id, status="running")
    try:
        # Signing, the Neptune timeout and the sparql_query audit log live in sagebrain_core.
        result = execute_query(
            job["query"],
            config=NeptuneConfig.from_env(),
            job_id=job_id,
            source=job.get("source", "direct"),
            # Messages from the old /query submit Lambda carry no user_id (spec 001 D-19).
            principal=str(job.get("user_id") or LEGACY_PRINCIPAL),
            source_ip=job.get("source_ip", "unknown"),
            user_agent=job.get("user_agent", "unknown"),
        )
    except Exception as e:
        _update_job(job_id, status="error", error=str(e))
        raise
    # The result is stored in the job item, under DynamoDB's 400 KB item limit (spec 001 D-16).
    # Retrying returns the same result, so this fails the job, not the SQS message.
    if len(result.text.encode("utf-8")) > limits.MAX_RESULT_BYTES:
        _update_job(
            job_id,
            status="error",
            error=RESULT_TOO_LARGE,
            duration_ms=round(result.duration_ms, 2),
        )
        return
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
