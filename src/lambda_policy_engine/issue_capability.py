"""
Policy Engine - Issue Capability Tokens

Policy-as-Code Architecture Component:
    User Evidence → Policy Engine → Capability Token → Asset Guardian

This Lambda evaluates user evidence (GA4GH Passport claims, DUO terms, etc.)
against machine-readable governance policies and issues signed, time-limited
capability tokens if authorized.

Flow:
    1. User submits evidence + requested resources
    2. Query governance graph for resource policies
    3. Evaluate evidence against DUO/Cedar policies
    4. Issue signed JWT capability token if authorized
    5. User presents capability token to Asset Guardian (query API)

See: Policy-as-Code_sketches and pitches.pdf (Page 4)
"""

import json
import os
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import botocore.session
import jwt
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

NEPTUNE_ENDPOINT = os.environ["NEPTUNE_ENDPOINT"]
REGION = os.environ["AWS_REGION"]
# Signing key for JWTs (in production, use KMS or Secrets Manager)
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-in-production")
JWT_ALGORITHM = "HS256"
CAPABILITY_TTL_SECONDS = int(os.environ.get("CAPABILITY_TTL_SECONDS", "3600"))  # 1 hour

CORS_HEADERS = {"Access-Control-Allow-Origin": "*"}


def _json_response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": json.dumps(body),
    }


def _query_governance_policies(resource_ids: list[str]) -> dict:
    """
    Query Neptune governance graph for DUO terms and access requirements.

    Returns dict mapping resource_id -> policies:
        {
            "syn123": {
                "duo_terms": ["DUO:0000007"],  # disease-specific research
                "access_requirements": ["AR001"],
                "data_use_permission": "DS",  # disease-specific
            }
        }
    """
    # Build SPARQL query to fetch governance policies for resources
    resource_uris = [f"<https://www.synapse.org/Synapse:{rid}>" for rid in resource_ids]
    values_clause = " ".join(resource_uris)

    query = f"""
        PREFIX gov: <https://sagebionetworks.org/governance/>
        PREFIX duo: <http://purl.obolibrary.org/obo/>

        SELECT ?resource ?duoTerm ?accessRequirement ?dataUsePermission WHERE {{
          VALUES ?resource {{ {values_clause} }}

          OPTIONAL {{
            ?resource gov:hasDataUseCondition ?duoTerm .
          }}

          OPTIONAL {{
            ?resource gov:hasAccessRequirement ?accessRequirement .
          }}

          OPTIONAL {{
            ?resource gov:dataUsePermission ?dataUsePermission .
          }}
        }}
    """

    url = f"https://{NEPTUNE_ENDPOINT}:8182/sparql"
    body = urlencode({"query": query})
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/sparql-results+json",
    }

    session = botocore.session.Session()
    credentials = session.get_credentials()
    aws_request = AWSRequest(method="POST", url=url, data=body, headers=headers)
    SigV4Auth(credentials, "neptune-db", REGION).add_auth(aws_request)

    try:
        response = requests.post(
            url,
            data=body,
            headers=dict(aws_request.headers),
            timeout=20,
        )
        response.raise_for_status()
        results = response.json()
    except Exception as e:
        print(f"Governance query failed: {e}")
        return {}

    # Parse results into policy dict
    policies = {}
    for binding in results.get("results", {}).get("bindings", []):
        resource_uri = binding.get("resource", {}).get("value", "")
        resource_id = resource_uri.split(":")[-1]  # Extract syn123 from URI

        if resource_id not in policies:
            policies[resource_id] = {
                "duo_terms": [],
                "access_requirements": [],
                "data_use_permission": None,
            }

        duo_term = binding.get("duoTerm", {}).get("value")
        if duo_term:
            policies[resource_id]["duo_terms"].append(duo_term)

        ar = binding.get("accessRequirement", {}).get("value")
        if ar:
            policies[resource_id]["access_requirements"].append(ar)

        dup = binding.get("dataUsePermission", {}).get("value")
        if dup:
            policies[resource_id]["data_use_permission"] = dup

    return policies


def _evaluate_evidence(evidence: dict, resource_policies: dict) -> dict:
    """
    Evaluate user evidence against resource governance policies.

    Evidence structure (GA4GH Passport-like):
        {
            "user_id": "9000001",
            "research_purpose": "DUO:0000007",  # disease-specific research
            "disease": "MONDO:0004975",  # Alzheimer's
            "approved_access_requirements": ["AR001", "AR002"],
            "institution": "Stanford University",
        }

    Returns:
        {
            "decision": "ALLOW" | "DENY",
            "authorized_resources": ["syn123"],
            "denied_resources": ["syn456"],
            "reasons": {
                "syn456": "Research purpose mismatch: requires DUO:0000006"
            }
        }
    """
    authorized = []
    denied = []
    reasons = {}

    user_research_purpose = evidence.get("research_purpose")
    user_disease = evidence.get("disease")
    user_approved_ars = set(evidence.get("approved_access_requirements", []))

    for resource_id, policies in resource_policies.items():
        # Rule 1: Check DUO terms match
        required_duo_terms = policies.get("duo_terms", [])
        if required_duo_terms and user_research_purpose not in required_duo_terms:
            denied.append(resource_id)
            reasons[resource_id] = (
                f"Research purpose mismatch: requires {', '.join(required_duo_terms)}"
            )
            continue

        # Rule 2: Check disease-specific match (if applicable)
        if policies.get("data_use_permission") == "DS":
            # Disease-specific data - check disease matches
            # In real implementation, would check MONDO hierarchy
            if not user_disease:
                denied.append(resource_id)
                reasons[resource_id] = (
                    "Disease-specific data requires disease declaration"
                )
                continue

        # Rule 3: Check access requirements satisfied
        required_ars = set(policies.get("access_requirements", []))
        if required_ars and not required_ars.issubset(user_approved_ars):
            missing = required_ars - user_approved_ars
            denied.append(resource_id)
            reasons[resource_id] = (
                f"Access requirements not met: missing {', '.join(missing)}"
            )
            continue

        # All checks passed
        authorized.append(resource_id)

    decision = "ALLOW" if authorized else "DENY"
    return {
        "decision": decision,
        "authorized_resources": authorized,
        "denied_resources": denied,
        "reasons": reasons,
    }


def _issue_capability_token(
    user_id: str,
    authorized_resources: list[str],
    evidence: dict,
    ttl_seconds: int = CAPABILITY_TTL_SECONDS,
) -> str:
    """
    Issue a signed JWT capability token.

    Token structure:
        {
            "iss": "sage-policy-engine",
            "sub": user_id,
            "aud": "neptune-query-api",
            "exp": <timestamp>,
            "iat": <timestamp>,
            "authorized_resources": ["syn123", "syn456"],
            "research_purpose": "DUO:0000007",
            "disease": "MONDO:0004975"
        }
    """
    now = datetime.utcnow()
    exp = now + timedelta(seconds=ttl_seconds)

    payload = {
        "iss": "sage-policy-engine",
        "sub": user_id,
        "aud": "neptune-query-api",
        "exp": int(exp.timestamp()),
        "iat": int(now.timestamp()),
        "authorized_resources": authorized_resources,
        "research_purpose": evidence.get("research_purpose"),
        "disease": evidence.get("disease"),
    }

    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token


def handler(event, context):
    """
    POST /policy/issue-capability

    Request:
        {
            "user_id": "9000001",
            "resource_ids": ["syn123", "syn456"],
            "evidence": {
                "research_purpose": "DUO:0000007",
                "disease": "MONDO:0004975",
                "approved_access_requirements": ["AR001"],
                "institution": "Stanford"
            }
        }

    Response (200):
        {
            "decision": "ALLOW",
            "capability_token": "eyJ...",
            "expires_at": "2026-09-03T12:00:00Z",
            "authorized_resources": ["syn123"],
            "denied_resources": ["syn456"],
            "reasons": {
                "syn456": "Research purpose mismatch"
            }
        }

    Response (403):
        {
            "decision": "DENY",
            "authorized_resources": [],
            "denied_resources": ["syn123", "syn456"],
            "reasons": {...}
        }
    """
    try:
        body = json.loads(event.get("body", "{}"))
    except json.JSONDecodeError:
        return _json_response(400, {"error": "Invalid JSON body"})

    user_id = body.get("user_id")
    resource_ids = body.get("resource_ids", [])
    evidence = body.get("evidence", {})

    if not user_id or not resource_ids:
        return _json_response(
            400, {"error": "Missing required fields: user_id, resource_ids"}
        )

    print(
        json.dumps(
            {
                "event": "capability_request",
                "user_id": user_id,
                "resource_ids": resource_ids,
                "research_purpose": evidence.get("research_purpose"),
                "timestamp": time.time(),
            }
        )
    )

    # Step 1: Query governance graph for resource policies
    resource_policies = _query_governance_policies(resource_ids)

    # Step 2: Evaluate evidence against policies
    evaluation = _evaluate_evidence(evidence, resource_policies)

    # Step 3: Issue capability token if any resources authorized
    capability_token = None
    expires_at = None

    if evaluation["authorized_resources"]:
        capability_token = _issue_capability_token(
            user_id, evaluation["authorized_resources"], evidence
        )
        expires_at = (
            datetime.utcnow() + timedelta(seconds=CAPABILITY_TTL_SECONDS)
        ).isoformat() + "Z"

        print(
            json.dumps(
                {
                    "event": "capability_issued",
                    "user_id": user_id,
                    "authorized_resources": evaluation["authorized_resources"],
                    "ttl_seconds": CAPABILITY_TTL_SECONDS,
                    "timestamp": time.time(),
                }
            )
        )

    response_body = {
        "decision": evaluation["decision"],
        "authorized_resources": evaluation["authorized_resources"],
        "denied_resources": evaluation["denied_resources"],
        "reasons": evaluation["reasons"],
    }

    if capability_token:
        response_body["capability_token"] = capability_token
        response_body["expires_at"] = expires_at

    status_code = 200 if evaluation["authorized_resources"] else 403
    return _json_response(status_code, response_body)
