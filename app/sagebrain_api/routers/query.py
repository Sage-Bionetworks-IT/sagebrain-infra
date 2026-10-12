import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from sagebrain_core.neptune import DEFAULT_CONTENT_TYPE
from sagebrain_core.validation import validate_query, validate_source

from ..auth import Principal
from ..guard import ApiGuard
from ..models import JobAccepted, QueryJobStatus
from ._common import caller_ip, load_job, read_json_object, submit_job

log = logging.getLogger(__name__)
router = APIRouter()
guard = ApiGuard("query")


@router.post("/query", status_code=202)
async def submit_query(request: Request, principal: Principal = Depends(guard)):
    """FR-1. The worker only executes: validation + admission happen once, here."""
    body = await read_json_object(request)
    query = validate_query(body.get("query"))
    source = validate_source(request.headers.get("x-source"))  # D-1, D-11
    ip = caller_ip(request)

    job_id = await submit_job(
        request.app.state.query_jobs,
        {},
        {
            "query": query,
            "source": source,
            "source_ip": ip,
            "user_agent": request.headers.get("user-agent", "unknown"),
        },
    )
    log.info(
        json.dumps(
            {
                "event": "query_submitted",
                "job_id": job_id,
                "user_id": principal.id,
                "source_ip": ip,
                "source": source,
            }
        )
    )
    accepted = JobAccepted(job_id=job_id, status="pending")
    return JSONResponse(accepted.model_dump(mode="json"), status_code=202)


@router.get("/query/{job_id}")
async def get_query_job(job_id: str, request: Request, _: Principal = Depends(guard)):
    """FR-2."""
    job_id, item = await load_job(request.app.state.query_jobs, job_id)
    result = {"job_id": job_id, "status": item["status"]}
    if item["status"] == "complete":
        result["results"] = item.get("results", "")
        result["content_type"] = item.get("content_type", DEFAULT_CONTENT_TYPE)
    elif item["status"] == "error":
        result["error"] = item.get("error", "Unknown error")
    status = QueryJobStatus.model_validate(result)
    return JSONResponse(status.model_dump(mode="json", exclude_unset=True))
