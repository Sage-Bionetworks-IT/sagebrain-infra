"""SigV4-signed SPARQL execution against Neptune, with the `sparql_query` audit log."""

import json
import os
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import botocore.session
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from . import limits

DEFAULT_CONTENT_TYPE = "application/sparql-results+json"


@dataclass(frozen=True)
class NeptuneConfig:
    endpoint: str  # reader endpoint — this service only ever reads
    region: str

    @classmethod
    def from_env(cls) -> "NeptuneConfig":
        return cls(os.environ["NEPTUNE_ENDPOINT"], os.environ["AWS_REGION"])


@dataclass(frozen=True)
class NeptuneResult:
    text: str
    content_type: str
    status_code: int
    duration_ms: float


def _log_query(
    job_id, query, source, principal, source_ip, user_agent, status_code, duration_ms
):
    print(
        json.dumps(
            {
                "event": "sparql_query",
                "job_id": job_id,
                "query": query,
                "query_length": len(query),
                "source": source,
                "principal": principal,
                "source_ip": source_ip,
                "user_agent": user_agent,
                "status_code": status_code,
                "duration_ms": round(duration_ms, 2),
                "timestamp": time.time(),
            }
        )
    )


def execute_query(
    query: str,
    *,
    config: NeptuneConfig,
    job_id: str,
    source: str,
    principal: str,
    source_ip: str,
    user_agent: str,
) -> NeptuneResult:
    """Run an already-validated, already-admitted query. Logs `sparql_query`; re-raises on failure."""
    start = time.time()
    url = f"https://{config.endpoint}:8182/sparql"
    body = urlencode({"query": query})
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": DEFAULT_CONTENT_TYPE,
    }

    credentials = botocore.session.Session().get_credentials()
    aws_request = AWSRequest(method="POST", url=url, data=body, headers=headers)
    SigV4Auth(credentials, "neptune-db", config.region).add_auth(aws_request)

    def log(status_code):
        duration_ms = (time.time() - start) * 1000
        _log_query(
            job_id,
            query,
            source,
            principal,
            source_ip,
            user_agent,
            status_code,
            duration_ms,
        )
        return duration_ms

    response = None
    try:
        response = requests.post(
            url,
            data=body,
            headers=dict(aws_request.headers),
            timeout=limits.NEPTUNE_QUERY_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.exceptions.HTTPError:
        log(response.status_code)
        raise
    except Exception:
        log(500)
        raise

    duration_ms = log(200)
    return NeptuneResult(
        text=response.text,
        content_type=response.headers.get("Content-Type", DEFAULT_CONTENT_TYPE),
        status_code=200,
        duration_ms=duration_ms,
    )
