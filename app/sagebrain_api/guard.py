from fastapi import Request

from sagebrain_core.ratelimit import admit_global, admit_principal

from .auth import Principal


class ApiGuard:
    """Route dependency for job endpoints: global bucket -> auth -> per-principal bucket.

    The global bucket is charged before auth so unauthenticated floods are throttled without
    reaching Synapse (parity matrix row 1). Health and docs routes carry no guard.
    """

    def __init__(self, api: str):
        self.api = api

    async def __call__(self, request: Request) -> Principal:
        state = request.app.state
        admit_global(self.api, state.limiter)
        principal = await state.authenticator.authenticate(request.headers)
        request.state.principal = principal  # read by the access log
        admit_principal(self.api, principal.id, state.limiter)
        return principal
