"""Job persistence: the DynamoDB item + SQS message the workers consume (data-model.md).

Shapes must not change — the query and agent worker Lambdas depend on them.
"""

import json
import time
import uuid

import boto3
from botocore.config import Config

from sagebrain_core import limits

# Well inside the 10 s request budget (parity matrix row 4).
AWS_CLIENT_CONFIG = Config(connect_timeout=2, read_timeout=5)


class JobStore:
    """One API's jobs table and queue. `table` / `sqs` are boto3 objects (or test fakes)."""

    def __init__(self, table, sqs, queue_url: str):
        self._table = table
        self._sqs = sqs
        self._queue_url = queue_url

    @classmethod
    def from_aws(cls, table_name: str, queue_url: str) -> "JobStore":
        dynamodb = boto3.resource("dynamodb", config=AWS_CLIENT_CONFIG)
        sqs = boto3.client("sqs", config=AWS_CLIENT_CONFIG)
        return cls(dynamodb.Table(table_name), sqs, queue_url)

    def submit(self, item: dict, message: dict) -> str:
        """Store a `pending` job (plus `item` fields), enqueue `message`, return the job id."""
        job_id = str(uuid.uuid4())
        now = int(time.time())
        self._table.put_item(
            Item={
                "job_id": job_id,
                "status": "pending",
                **item,
                "created_at": now,
                "ttl": now + limits.JOB_TTL_SECONDS,
            }
        )
        self._sqs.send_message(
            QueueUrl=self._queue_url,
            MessageBody=json.dumps({"job_id": job_id, **message}),
        )
        return job_id

    def get(self, job_id: str) -> dict | None:
        return self._table.get_item(Key={"job_id": job_id}).get("Item")
