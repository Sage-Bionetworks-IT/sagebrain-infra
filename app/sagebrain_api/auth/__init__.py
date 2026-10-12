"""Caller authentication: Synapse bearer token (FR-5, port of src/lambda_authorizer; D-4)."""

from .cache import TokenCache  # noqa: F401
from .synapse import (  # noqa: F401
    AuthDenied,
    Authenticator,
    AuthUnavailable,
    Principal,
)
