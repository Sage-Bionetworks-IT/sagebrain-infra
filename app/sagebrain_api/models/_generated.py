# GENERATED from api/openapi.yaml by tools/gen_models.py. Do not edit by hand:
# change the spec, then run `pre-commit run openapi-models --all-files`.

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class QueryRequest(BaseModel):
    query: Annotated[
        str,
        Field(
            description="SPARQL query. Leading/trailing whitespace is stripped; must be non-empty and at most 8000 characters after stripping.",
            max_length=8000,
            min_length=1,
        ),
    ]


class AskRequest(BaseModel):
    question: Annotated[
        str,
        Field(
            description="Natural-language question. Leading/trailing whitespace is stripped; must be non-empty and at most 2000 characters after stripping.",
            max_length=2000,
            min_length=1,
        ),
    ]


class JobAccepted(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    job_id: UUID
    status: Literal["pending"]


class QueryJobStatus(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    job_id: str
    status: Literal["pending", "running", "complete", "error"]
    results: Annotated[
        str | None,
        Field(
            description="Raw Neptune response body (present when status is complete)."
        ),
    ] = None
    content_type: Annotated[
        str | None,
        Field(
            description="Content type of `results` (present when status is complete)."
        ),
    ] = "application/sparql-results+json"
    error: Annotated[
        str | None, Field(description="Error message (present when status is error).")
    ] = None


class AgentStep(BaseModel):
    type: Literal["tool_call", "tool_result"]
    tool: str | None = None
    sparql: str | None = None
    preview: Annotated[
        str | None, Field(description="First 500 characters of the tool result.")
    ] = None
    error: str | None = None


class AskJobStatus(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    job_id: str
    status: Literal["pending", "running", "complete", "error"]
    status_detail: Annotated[
        str | None, Field(description="Human-readable progress message.")
    ] = None
    steps: list[AgentStep] | None = None
    answer: Annotated[
        str | None, Field(description="Agent answer (present when status is complete).")
    ] = None
    error: Annotated[
        str | None, Field(description="Error message (present when status is error).")
    ] = None


class ErrorBody(BaseModel):
    error: str


class GatewayError(BaseModel):
    message: str


class OpenApiDocument(BaseModel):
    openapi: str
    info: dict[str, Any]
    paths: dict[str, Any]


class Health(BaseModel):
    status: Literal["ok"]
