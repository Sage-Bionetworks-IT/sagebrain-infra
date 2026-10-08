"""Pure-ASGI middleware (plan.md order, outermost first):

    AllowOriginEverywhere -> CORSMiddleware -> AccessLog -> UnhandledError -> BodyLimit -> RequestTimeout

Everything inside CORS, so every response — 413, 500 and 504 included — carries
`Access-Control-Allow-Origin: *` (FR-6).
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from starlette.datastructures import Headers, MutableHeaders

from .errors import gateway_error

access_log = logging.getLogger("sagebrain_api.access")
log = logging.getLogger(__name__)

_API_BY_PREFIX = {"query": "query", "ask": "ask", "api": "docs", "healthz": "health"}


def source_ip(headers: Headers, client) -> str:
    """The rightmost X-Forwarded-For entry (appended by the ALB), never the client-supplied leftmost."""
    forwarded = [ip.strip() for ip in headers.get("x-forwarded-for", "").split(",")]
    if forwarded[-1]:
        return forwarded[-1]
    return client[0] if client else "unknown"


def _on_response_start(send, callback):
    async def wrapped(message):
        if message["type"] == "http.response.start":
            callback(message)
        await send(message)

    return wrapped


class AllowOriginEverywhere:
    """Starlette's CORSMiddleware only answers requests that send `Origin`; API Gateway's
    gateway responses carried ACAO unconditionally, so add it to every response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        def add_header(message):
            MutableHeaders(scope=message).setdefault("access-control-allow-origin", "*")

        await self.app(scope, receive, _on_response_start(send, add_header))


class AccessLogMiddleware:
    """One JSON line per request: API Gateway's json_with_standard_fields + requestId, api, durationMs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        start = time.perf_counter()
        request_time = datetime.now(timezone.utc)
        state = scope.setdefault("state", {})  # shared with request.state
        status, length = 500, 0

        async def counting_send(message):
            nonlocal status, length
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                length += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, counting_send)
        finally:
            principal = state.get("principal")
            route = scope.get("route")
            access_log.info(
                json.dumps(
                    {
                        "requestId": str(uuid.uuid4()),
                        "api": _API_BY_PREFIX.get(scope["path"].split("/")[1]),
                        "caller": principal.method if principal else None,
                        "user": principal.id if principal else None,
                        "httpMethod": scope["method"],
                        "ip": source_ip(Headers(scope=scope), scope.get("client")),
                        "protocol": f"HTTP/{scope.get('http_version', '1.1')}",
                        "requestTime": request_time.strftime("%d/%b/%Y:%H:%M:%S +0000"),
                        "resourcePath": getattr(route, "path", None) or scope["path"],
                        "responseLength": length,
                        "status": status,
                        "durationMs": round((time.perf_counter() - start) * 1000, 2),
                    }
                )
            )


class UnhandledErrorMiddleware:
    """Unhandled exception -> ERROR log with stack trace + 500 {"message": "Internal server error"}.

    Starlette's own ServerErrorMiddleware sits outside all user middleware, so its 500 would
    skip CORS; this one sits inside.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        started = False

        def mark_started(message):
            nonlocal started
            started = True

        try:
            await self.app(scope, receive, _on_response_start(send, mark_started))
        except Exception:
            log.exception(
                json.dumps(
                    {
                        "event": "unhandled_error",
                        "httpMethod": scope["method"],
                        "path": scope["path"],
                    }
                )
            )
            if started:
                raise
            await gateway_error(500, "Internal server error")(scope, receive, send)


class _BodyTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """413 {"message": "Request Too Long"} above `max_bytes` — by Content-Length up front, or
    while a chunked body is being read (D-5)."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        too_large = gateway_error(413, "Request Too Long")
        content_length = Headers(scope=scope).get("content-length", "")
        if content_length.isdigit() and int(content_length) > self.max_bytes:
            return await too_large(scope, receive, send)

        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge
            return message

        def mark_started(message):
            nonlocal started
            started = True

        try:
            await self.app(
                scope, limited_receive, _on_response_start(send, mark_started)
            )
        except _BodyTooLarge:
            if started:
                raise
            await too_large(scope, receive, send)


class RequestTimeoutMiddleware:
    """504 {"message": "Endpoint request timed out"} after `seconds` (parity row 4).

    The handler runs as its own task so the 504 goes out on time even when the handler is
    stuck in a worker thread (boto3) that can't be cancelled; anything it sends later is dropped.
    """

    def __init__(self, app, seconds: float):
        self.app = app
        self.seconds = seconds

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        started = timed_out = False

        async def guarded_send(message):
            nonlocal started
            if timed_out:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        task = asyncio.ensure_future(self.app(scope, receive, guarded_send))
        done, _ = await asyncio.wait({task}, timeout=self.seconds)
        if task in done:
            return task.result()

        timed_out = True
        task.cancel()
        task.add_done_callback(_consume_result)
        log.warning(json.dumps({"event": "request_timeout", "path": scope["path"]}))
        if not started:
            await gateway_error(504, "Endpoint request timed out")(scope, receive, send)


def _consume_result(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()
