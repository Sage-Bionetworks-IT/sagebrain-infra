import json
import os
import time
from urllib.parse import urlencode

import boto3
import botocore.session
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import BotoCoreError, ClientError

NEPTUNE_ENDPOINT = os.environ["NEPTUNE_ENDPOINT"]
REGION = os.environ["AWS_REGION"]
POLICY_STORE_ID = os.environ["AVP_POLICY_STORE_ID"]
AVP_NAMESPACE = os.environ.get("AVP_NAMESPACE", "SageBrain")
DEFAULT_INFERRED_EDGE_MODE = os.environ.get("INFERRED_EDGE_MODE", "intersection")

_verified_permissions = boto3.client("verifiedpermissions")

CORS_HEADERS = {"Access-Control-Allow-Origin": "*"}
VALID_INFERRED_EDGE_MODES = {"intersection", "union"}
_SYNAPSE_PREFIX = "https://www.synapse.org/Synapse:"


def _json_response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": json.dumps(body),
    }


def _resource_iri(resource_id: str) -> str:
    if resource_id.startswith("http://") or resource_id.startswith("https://"):
        return resource_id
    if resource_id.startswith("syn:"):
        return f"{_SYNAPSE_PREFIX}{resource_id[4:]}"
    if resource_id.startswith("syn"):
        return f"{_SYNAPSE_PREFIX}{resource_id}"
    return resource_id


def _local_name(uri: str) -> str:
    if "#" in uri:
        return uri.rsplit("#", 1)[1]
    if "/" in uri:
        return uri.rsplit("/", 1)[1]
    if ":" in uri:
        return uri.rsplit(":", 1)[1]
    return uri


def _principal_matches(grant_principal: str, principal_id: str) -> bool:
    return grant_principal == principal_id or grant_principal.endswith(
        f"principal-{principal_id}"
    )


def _governance_query(resource_iri: str) -> str:
    return f"""
PREFIX gov: <https://sagebionetworks.org/governance/>
SELECT ?grant ?grantPrincipal ?permission ?bindingType ?accessRequirement
WHERE {{
  VALUES ?resource {{ <{resource_iri}> }}
  OPTIONAL {{
    ?resource gov:hasAccessRequirement ?accessRequirement .
  }}
  OPTIONAL {{
    ?resource gov:hasACL ?grant .
    ?grant a gov:AccessGrant ;
           gov:principal ?grantPrincipal ;
           gov:permission ?permission ;
           gov:bindingType ?bindingType .
  }}
}}
"""


def _query_governance(resource_iri: str) -> list[dict]:
    url = f"https://{NEPTUNE_ENDPOINT}:8182/sparql"
    body = urlencode({"query": _governance_query(resource_iri)})
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/sparql-results+json",
    }

    session = botocore.session.Session()
    credentials = session.get_credentials()
    aws_request = AWSRequest(method="POST", url=url, data=body, headers=headers)
    SigV4Auth(credentials, "neptune-db", REGION).add_auth(aws_request)

    response = requests.post(
        url,
        data=body,
        headers=dict(aws_request.headers),
        timeout=20,
    )
    response.raise_for_status()

    payload = response.json()
    return payload.get("results", {}).get("bindings", [])


def _extract_governance(bindings: list[dict]) -> tuple[list[dict], list[str]]:
    grants = []
    access_requirements = set()
    for row in bindings:
        ar = row.get("accessRequirement", {}).get("value")
        if ar:
            access_requirements.add(ar)

        grant = row.get("grant", {}).get("value")
        principal = row.get("grantPrincipal", {}).get("value")
        permission = row.get("permission", {}).get("value")
        binding_type = row.get("bindingType", {}).get("value")
        if not (grant and principal and permission and binding_type):
            continue

        grants.append(
            {
                "grant": grant,
                "principal": principal,
                "permission": _local_name(permission),
                "binding_type": _local_name(binding_type),
            }
        )
    return grants, sorted(access_requirements)


def _avp_entity_type(name: str) -> str:
    return f"{AVP_NAMESPACE}::{name}"


def _avp_is_allowed(
    principal_id: str,
    action: str,
    resource_id: str,
    grant: dict,
    access_requirements: list[str],
    inferred_edge_mode: str,
) -> tuple[bool, list[str]]:
    resource_entity = {
        "identifier": {
            "entityType": _avp_entity_type("SynapseEntity"),
            "entityId": resource_id,
        },
        "parents": [
            {
                "entityType": _avp_entity_type("AccessGrant"),
                "entityId": grant["grant"],
            }
        ],
        "attributes": {
            "accessRequirements": {
                "set": [{"string": req} for req in access_requirements]
            },
        },
    }
    grant_entity = {
        "identifier": {
            "entityType": _avp_entity_type("AccessGrant"),
            "entityId": grant["grant"],
        },
        "attributes": {
            "permission": {"string": grant["permission"]},
            "bindingType": {"string": grant["binding_type"]},
            "principal": {"string": grant["principal"]},
        },
    }

    result = _verified_permissions.is_authorized(
        policyStoreId=POLICY_STORE_ID,
        principal={
            "entityType": _avp_entity_type("User"),
            "entityId": principal_id,
        },
        action={
            "actionType": _avp_entity_type("Action"),
            "actionId": action,
        },
        resource={
            "entityType": _avp_entity_type("SynapseEntity"),
            "entityId": resource_id,
        },
        entities={"entityList": [resource_entity, grant_entity]},
        context={"contextMap": {"inferredEdgeMode": {"string": inferred_edge_mode}}},
    )
    policies = [
        p.get("policyId")
        for p in result.get("determiningPolicies", [])
        if p.get("policyId")
    ]
    return result.get("decision") == "ALLOW", policies


def _merge_decisions(evaluations: list[dict], inferred_edge_mode: str) -> bool:
    direct = [
        decision["allowed"]
        for decision in evaluations
        if decision["binding_type"].lower() == "direct"
    ]
    inferred = [
        decision["allowed"]
        for decision in evaluations
        if decision["binding_type"].lower() != "direct"
    ]
    if not inferred:
        return any(direct)
    if inferred_edge_mode == "union":
        return any(direct + inferred)
    return any(direct) and all(inferred)


def handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _json_response(400, {"error": "Request body must be valid JSON"})

    principal_id = str(body.get("principal_id", "")).strip()
    action = str(body.get("action", "")).strip().upper()
    resource_id = str(body.get("resource_id", "")).strip()
    inferred_edge_mode = str(
        body.get("inferred_edge_mode", DEFAULT_INFERRED_EDGE_MODE)
    ).strip()

    if not principal_id or not action or not resource_id:
        return _json_response(
            400,
            {
                "error": "Missing one or more required fields: principal_id, action, resource_id"
            },
        )
    if inferred_edge_mode not in VALID_INFERRED_EDGE_MODES:
        return _json_response(
            400,
            {
                "error": f"inferred_edge_mode must be one of: {', '.join(sorted(VALID_INFERRED_EDGE_MODES))}"
            },
        )

    lookup_start = time.time()
    resource_iri = _resource_iri(resource_id)
    try:
        bindings = _query_governance(resource_iri)
        grants, access_requirements = _extract_governance(bindings)
        matching_grants = [
            g
            for g in grants
            if g["permission"] == action
            and _principal_matches(g["principal"], principal_id)
        ]

        if not matching_grants:
            return _json_response(
                200,
                {
                    "decision": "DENY",
                    "reason": "no_matching_governance_grant",
                    "principal_id": principal_id,
                    "action": action,
                    "resource_id": resource_id,
                    "inferred_edge_mode": inferred_edge_mode,
                    "lookup_ms": round((time.time() - lookup_start) * 1000, 2),
                    "access_requirements": access_requirements,
                },
            )

        evaluations = []
        for grant in matching_grants:
            allowed, policy_ids = _avp_is_allowed(
                principal_id=principal_id,
                action=action,
                resource_id=resource_id,
                grant=grant,
                access_requirements=access_requirements,
                inferred_edge_mode=inferred_edge_mode,
            )
            evaluations.append(
                {
                    "grant": grant["grant"],
                    "binding_type": grant["binding_type"],
                    "allowed": allowed,
                    "determining_policies": policy_ids,
                }
            )

        allowed = _merge_decisions(evaluations, inferred_edge_mode)
        return _json_response(
            200,
            {
                "decision": "ALLOW" if allowed else "DENY",
                "reason": "authorized" if allowed else "rebac_policy_denied",
                "principal_id": principal_id,
                "action": action,
                "resource_id": resource_id,
                "inferred_edge_mode": inferred_edge_mode,
                "lookup_ms": round((time.time() - lookup_start) * 1000, 2),
                "access_requirements": access_requirements,
                "evaluated_grants": evaluations,
            },
        )
    except (
        requests.exceptions.RequestException,
        requests.exceptions.JSONDecodeError,
        ClientError,
        BotoCoreError,
    ) as e:
        print(json.dumps({"event": "rebac_auth_unavailable", "error": str(e)}))
        return _json_response(
            503,
            {
                "decision": "DENY",
                "reason": "authorization_unavailable",
                "error": str(e),
            },
        )
