import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from sagebrain_core.validation import validate_question

from ..auth import Principal
from ..guard import ApiGuard
from ..models import AskJobStatus, JobAccepted
from ._common import caller_ip, load_job, read_json_object, submit_job

log = logging.getLogger(__name__)
router = APIRouter()
guard = ApiGuard("ask")


@router.post("/ask", status_code=202)
async def submit_question(request: Request, principal: Principal = Depends(guard)):
    """FR-3. D-8: enqueue the caller's user_id — never a credential."""
    body = await read_json_object(request)
    question = validate_question(body.get("question"))
    ip = caller_ip(request)

    job_id = await submit_job(
        request.app.state.ask_jobs,
        {"question": question},
        {"question": question, "user_id": principal.id, "source_ip": ip},
    )
    log.info(
        json.dumps(
            {
                "event": "question_submitted",
                "job_id": job_id,
                "user_id": principal.id,
                "source_ip": ip,
            }
        )
    )
    accepted = JobAccepted(job_id=job_id, status="pending")
    return JSONResponse(accepted.model_dump(mode="json"), status_code=202)


@router.get("/ask/{job_id}")
async def get_ask_job(job_id: str, request: Request, _: Principal = Depends(guard)):
    """FR-4. `steps` at every status so callers can watch in-progress SPARQL."""
    job_id, item = await load_job(request.app.state.ask_jobs, job_id)
    result = {"job_id": job_id, "status": item["status"]}
    if "status_detail" in item:
        result["status_detail"] = item["status_detail"]
    if item.get("steps"):
        result["steps"] = item["steps"]
    if item["status"] == "complete":
        result["answer"] = item.get("answer", "")
    elif item["status"] == "error":
        result["error"] = item.get("error", "Unknown error")
    status = AskJobStatus.model_validate(result)
    return JSONResponse(status.model_dump(mode="json", exclude_unset=True))
