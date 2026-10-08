from fastapi import Request
from starlette.concurrency import run_in_threadpool

from sagebrain_core.ratelimit import admit_global, admit_principal

from .auth import Principal


class ApiGuard:
    """Route dependency for job endpoints: global bucket -> auth -> per-principal bucket.

    The global bucket is charged before auth so unauthenticated floods are throttled without
    reaching Synapse (parity matrix row 1). Health and docs routes carry no guard. The
    limiter may be a DynamoDB round trip, so it runs in the threadpool like the job calls.
    """

    def __init__(self, api: str):
        self.api = api

    async def __call__(self, request: Request) -> Principal:
        state = request.app.state
        await run_in_threadpool(admit_global, self.api, state.limiter)
        principal = await state.authenticator.authenticate(request.headers)
        request.state.principal = principal  # read by the access log
        await run_in_threadpool(admit_principal, self.api, principal.id, state.limiter)
        return principal
