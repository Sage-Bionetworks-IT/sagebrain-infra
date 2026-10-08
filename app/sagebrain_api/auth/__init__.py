"""Caller authentication: machine key or Synapse bearer token (FR-5, port of src/lambda_authorizer)."""

from .cache import TokenCache  # noqa: F401
from .synapse import (  # noqa: F401
    AuthDenied,
    Authenticator,
    AuthUnavailable,
    Principal,
)
