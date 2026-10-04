from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from sagebrain_core import limits
from sagebrain_core.ratelimit import Limiter, default_limiter

from .auth import Authenticator
from .errors import install_exception_handlers
from .jobs import JobStore
from .middleware import (
    AccessLogMiddleware,
    AllowOriginEverywhere,
    BodyLimitMiddleware,
    RequestTimeoutMiddleware,
    UnhandledErrorMiddleware,
)
from .routers import ask, docs, health, query
from .settings import Settings

CORS_ALLOW_METHODS = ["POST", "GET", "OPTIONS"]
CORS_ALLOW_HEADERS = ["Content-Type", "Authorization", "X-Source", "x-api-key"]
CORS_MAX_AGE_SECONDS = 600


def create_app(
    settings: Settings | None = None,
    *,
    query_jobs: JobStore | None = None,
    ask_jobs: JobStore | None = None,
    http_client: httpx.AsyncClient | None = None,
    limiter: Limiter | None = None,
) -> FastAPI:
    """Build the service. Tests inject fakes; in ECS everything comes from `Settings`."""
    settings = settings or Settings()
    http_client = http_client or httpx.AsyncClient()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await http_client.aclose()

    app = FastAPI(
        title="Sage Brain API",
        # Only the committed contract is published (FR-9), at /api.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        # D-3: `/query/` is a 404, not a redirect to `/query`.
        redirect_slashes=False,
        lifespan=lifespan,
        middleware=[
            Middleware(AllowOriginEverywhere),
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=CORS_ALLOW_METHODS,
                allow_headers=CORS_ALLOW_HEADERS,
                max_age=CORS_MAX_AGE_SECONDS,
            ),
            Middleware(AccessLogMiddleware),
            Middleware(UnhandledErrorMiddleware),
            Middleware(BodyLimitMiddleware, max_bytes=limits.MAX_BODY_BYTES),
            Middleware(
                RequestTimeoutMiddleware, seconds=limits.REQUEST_TIMEOUT_SECONDS
            ),
        ],
    )

    machine_key = settings.machine_api_key
    app.state.limiter = limiter or default_limiter()
    app.state.authenticator = Authenticator(
        client=http_client,
        team_id=settings.synapse_team_id,
        machine_api_key=machine_key.get_secret_value() if machine_key else None,
        repo_api=settings.synapse_repo_api,
        auth_api=settings.synapse_auth_api,
    )
    app.state.query_jobs = query_jobs or JobStore.from_aws(
        settings.query_job_table_name, settings.query_job_queue_url
    )
    app.state.ask_jobs = ask_jobs or JobStore.from_aws(
        settings.ask_job_table_name, settings.ask_job_queue_url
    )
    app.state.published_spec = docs.PublishedSpec(
        settings.openapi_spec_path, settings.public_base_url
    )

    install_exception_handlers(app, settings.auth_transient_status)
    for module in (query, ask, health, docs):
        app.include_router(module.router)
    app.mount(
        docs.STATIC_PREFIX,
        StaticFiles(directory=settings.swagger_ui_dir, check_dir=False),
        name="swagger-ui",
    )
    return app
