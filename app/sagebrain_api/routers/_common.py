"""HTTP-layer request handling shared by the job routers.

Body *parsing* errors belong here; field validation is sagebrain_core's (QueryRejected).
"""

import json

from fastapi import Request
from starlette.concurrency import run_in_threadpool

from ..errors import ClientError
from ..jobs import JobStore
from ..middleware import source_ip

MAX_JOB_ID_CHARS = 128  # api/openapi.yaml JobId maxLength


async def read_json_object(request: Request) -> dict:
    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except ValueError:  # includes UnicodeDecodeError
        raise ClientError("Request body must be valid JSON")
    if not isinstance(body, dict):
        raise ClientError("Request body must be a JSON object")
    return body


def caller_ip(request: Request) -> str:
    return source_ip(request.headers, request.client)


async def submit_job(store: JobStore, item: dict, message: dict) -> str:
    return await run_in_threadpool(store.submit, item, message)


async def load_job(store: JobStore, job_id: str) -> tuple[str, dict]:
    """Return (stripped job id, item). Blank or over-long ids are 400 (D-3); unknown ids 404."""
    job_id = job_id.strip()
    if not job_id or len(job_id) > MAX_JOB_ID_CHARS:
        raise ClientError("Missing job_id")
    item = await run_in_threadpool(store.get, job_id)
    if not item:
        raise ClientError("Job not found", status=404)
    return job_id, item
