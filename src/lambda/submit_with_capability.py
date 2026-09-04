"""
Asset Guardian - Submit Lambda with Capability Token Validation

Hybrid Authentication:
    Mode 1: Synapse PAT (current) → ReBAC post-filter
    Mode 2: Capability Token (future) → Query rewrite

This lambda acts as the "Asset Guardian" from Policy-as-Code architecture.
It validates capability tokens and rewrites queries with governance filters.

Usage:
    # With Synapse PAT (current)
    POST /query
    Authorization: Bearer SYNAPSE_PAT
    {"query": "..."}

    # With Capability Token (future)
    POST /query
    Authorization: Bearer JWT_CAPABILITY_TOKEN
    {"query": "..."}
"""

import json
import logging
import os
import time
import uuid

import boto3
import jwt
from jwt.exceptions import DecodeError, ExpiredSignatureError, InvalidTokenError

log = logging.getLogger()
log.setLevel(logging.INFO)

DYNAMODB_TABLE = os.environ["JOB_TABLE_NAME"]
SQS_QUEUE_URL = os.environ["JOB_QUEUE_URL"]
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-in-production")
JWT_ALGORITHM = "HS256"

_dynamodb = boto3.resource("dynamodb")
_sqs = boto3.client("sqs")

CORS_HEADERS = {"Access-Control-Allow-Origin": "*"}
MAX_QUERY_LENGTH = 8000
JOB_TTL_SECONDS = 86400


def _validate_capability_token(token: str) -> dict:
    """
    Validate JWT capability token issued by Policy Engine.

    Returns decoded payload if valid, raises exception if invalid.

    Token payload:
        {
            "iss": "sage-policy-engine",
            "sub": "9000001",
            "aud": "neptune-query-api",
            "exp": 1725372000,
            "iat": 1725368400,
            "authorized_policies": ["disease_specific:alzheimers"],
            "duo_term": "DUO:0000007",
            "disease": "MONDO:0004975"
        }
    """
    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            audience="neptune-query-api",
            issuer="sage-policy-engine",
        )
        return payload
    except ExpiredSignatureError:
        raise PermissionError("Capability token expired. Please request a new token.")
    except DecodeError:
        raise PermissionError("Invalid capability token format.")
    except InvalidTokenError as e:
        raise PermissionError(f"Invalid capability token: {e}")


def _rewrite_query_with_capability(query: str, capability: dict) -> str:
    """
    Rewrite SPARQL query to inject governance filters from capability token.

    Capability token contains authorized policies (e.g., "disease_specific:alzheimers").
    We translate this to SPARQL filters that Neptune understands.

    Example:
        Original:
            SELECT ?file WHERE { ?file a :File }

        Rewritten (if user has disease_specific:alzheimers capability):
            SELECT ?file WHERE {
              ?file a :File .
              ?file gov:hasDataUseCondition duo:DUO0000007 ;
                    gov:diseaseContext mondo:MONDO0004975 .
            }
    """
    # Query rewriter not used in this fallback implementation
    # (inject_governance_filter would be imported if we use the full rewriter)

    authorized_policies = capability.get("authorized_policies", [])
    duo_term = capability.get("duo_term")
    disease = capability.get("disease")

    if not authorized_policies:
        raise PermissionError("Capability token contains no authorized policies")

    # Build governance filter based on capability
    # For now, inject disease-specific filter if present
    if duo_term and disease:
        # Find WHERE clause
        import re

        where_match = re.search(r"\bWHERE\s*\{", query, re.IGNORECASE | re.DOTALL)
        if not where_match:
            return query  # Can't rewrite, return original

        start = where_match.end()
        # Find closing brace
        brace_count = 1
        pos = start
        while pos < len(query) and brace_count > 0:
            if query[pos] == "{":
                brace_count += 1
            elif query[pos] == "}":
                brace_count -= 1
            pos += 1

        if brace_count != 0:
            return query  # Malformed, return original

        # Inject governance filter
        governance_filter = f"""
  # Governance filter from capability token
  # Policy: {', '.join(authorized_policies)}
  ?s gov:hasDataUseCondition <{duo_term}> .
  ?s gov:diseaseContext <{disease}> .
"""

        # Insert before closing brace
        rewritten = query[: pos - 1] + governance_filter + "\n" + query[pos - 1 :]

        log.info(
            json.dumps(
                {
                    "event": "query_rewritten_from_capability",
                    "user_id": capability.get("sub"),
                    "policies": authorized_policies,
                    "original_length": len(query),
                    "rewritten_length": len(rewritten),
                }
            )
        )

        return rewritten

    # No rewriting needed or possible
    return query


def handler(event, context):
    """
    POST /query - Submit SPARQL query with either Synapse PAT or Capability Token

    Two authentication modes:
        1. Synapse PAT (header: Authorization: Bearer SYNAPSE_PAT)
           → User ID from authorizer context
           → Post-filter mode (current)

        2. Capability Token (header: Authorization: Bearer JWT_TOKEN)
           → Validate token, extract policies
           → Query rewrite mode (future)
    """
    # Get authorization header
    headers = event.get("headers") or {}
    auth_header = headers.get("Authorization") or headers.get("authorization", "")

    if not auth_header or not auth_header.startswith("Bearer "):
        return {
            "statusCode": 401,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps({"error": "Missing Authorization header"}),
        }

    token = auth_header[7:]  # Remove "Bearer "

    # Determine token type: Capability Token (JWT) or Synapse PAT
    capability_token = None
    user_id = None
    authentication_mode = None

    try:
        # Try to decode as JWT capability token
        capability_token = _validate_capability_token(token)
        user_id = capability_token["sub"]
        authentication_mode = "capability_token"

        log.info(
            json.dumps(
                {
                    "event": "capability_token_validated",
                    "user_id": user_id,
                    "policies": capability_token.get("authorized_policies"),
                    "timestamp": time.time(),
                }
            )
        )

    except PermissionError as e:
        # JWT validation failed - return error
        return {
            "statusCode": 401,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps({"error": str(e)}),
        }
    except Exception:
        # Not a JWT - assume Synapse PAT (legacy mode)
        # User ID comes from API Gateway authorizer context
        user_id = (
            event.get("requestContext", {})
            .get("authorizer", {})
            .get("user_id", "unknown")
        )
        authentication_mode = "synapse_pat"

    # Parse request body
    try:
        body = json.loads(event.get("body") or "{}")
        query = body.get("query", "").strip()
    except json.JSONDecodeError:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps({"error": "Request body must be valid JSON"}),
        }

    if not query:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps({"error": "Missing 'query' field"}),
        }

    if len(query) > MAX_QUERY_LENGTH:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps(
                {
                    "error": f"Query exceeds maximum length of {MAX_QUERY_LENGTH} characters"
                }
            ),
        }

    # Capability Token Mode: Rewrite query with governance filters
    if authentication_mode == "capability_token":
        try:
            query = _rewrite_query_with_capability(
                query, capability_token
            )  # noqa: F811
        except PermissionError as e:
            return {
                "statusCode": 403,
                "headers": {"Content-Type": "application/json", **CORS_HEADERS},
                "body": json.dumps({"error": str(e)}),
            }

    # Create job
    source_ip = (
        event.get("requestContext", {}).get("identity", {}).get("sourceIp", "unknown")
    )
    job_id = str(uuid.uuid4())
    now = time.time()

    _dynamodb.Table(DYNAMODB_TABLE).put_item(
        Item={
            "job_id": job_id,
            "status": "pending",
            "created_at": int(now),
            "ttl": int(now) + JOB_TTL_SECONDS,
            "authentication_mode": authentication_mode,
        }
    )

    # Enqueue job
    _sqs.send_message(
        QueueUrl=SQS_QUEUE_URL,
        MessageBody=json.dumps(
            {
                "job_id": job_id,
                "query": query,
                "source": headers.get("X-Source", "direct"),
                "source_ip": source_ip,
                "user_agent": headers.get("User-Agent", "unknown"),
                "user_id": user_id,
                "authentication_mode": authentication_mode,
            }
        ),
    )

    log.info(
        json.dumps(
            {
                "event": "query_submitted",
                "job_id": job_id,
                "user_id": user_id,
                "authentication_mode": authentication_mode,
                "source_ip": source_ip,
            }
        )
    )

    return {
        "statusCode": 202,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": json.dumps(
            {
                "job_id": job_id,
                "status": "pending",
                "authentication_mode": authentication_mode,
            }
        ),
    }
