"""Error bodies (api/openapi.yaml): `{"error"}` for application errors, `{"message"}` for
edge/gateway errors (auth, throttling, size, timeout), matching API Gateway's defaults.
"""

import math
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from sagebrain_core.errors import QueryRejected, RateLimited

from .auth import AuthDenied, AuthUnavailable


class ClientError(Exception):
    """An HTTP-layer client error with a contract `{"error": ...}` body (bad JSON, job id, 404)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def gateway_error(status: int, message: str, headers=None) -> JSONResponse:
    return JSONResponse({"message": message}, status_code=status, headers=headers)


def install_exception_handlers(app: FastAPI, auth_transient_status: int) -> None:
    if auth_transient_status == 500:
        transient_message = "Internal server error"
    else:
        transient_message = HTTPStatus(auth_transient_status).phrase

    @app.exception_handler(ClientError)
    @app.exception_handler(QueryRejected)
    async def client_error(request: Request, exc):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.exception_handler(AuthDenied)
    async def auth_denied(request: Request, exc: AuthDenied):
        # D-10: one body for every auth failure.
        return gateway_error(401, "Unauthorized")

    @app.exception_handler(AuthUnavailable)
    async def auth_unavailable(request: Request, exc: AuthUnavailable):
        return gateway_error(auth_transient_status, transient_message)

    @app.exception_handler(RateLimited)
    async def rate_limited(request: Request, exc: RateLimited):
        retry_after = max(1, math.ceil(exc.retry_after))
        return gateway_error(
            429, "Too Many Requests", headers={"Retry-After": str(retry_after)}
        )

    @app.exception_handler(HTTPException)
    async def http_exception(request: Request, exc: HTTPException):
        # Routing errors (404 unknown path, 405) use the gateway body, not FastAPI's {"detail"}.
        return gateway_error(exc.status_code, exc.detail, headers=exc.headers)
