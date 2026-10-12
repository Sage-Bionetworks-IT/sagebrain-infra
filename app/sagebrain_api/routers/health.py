from fastapi import APIRouter

from ..models import Health

router = APIRouter()


@router.get("/healthz")
async def healthz():
    """FR-8: no auth, not rate limited."""
    return Health(status="ok").model_dump()
