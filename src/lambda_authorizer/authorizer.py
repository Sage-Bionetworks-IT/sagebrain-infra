import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger()
log.setLevel(logging.INFO)

SYNAPSE_API = "https://repo-prod.prod.sagebase.org/repo/v1"
TEAM_ID = os.getenv("SYNAPSE_TEAM_ID")
if not TEAM_ID:
    raise RuntimeError("Missing required environment variable: SYNAPSE_TEAM_ID")


class _AuthDenied(Exception):
    """Expected auth failure — token invalid or user not in team.
    Never include the token value in the message; it ends up in CloudWatch logs."""


def handler(event, context):
    raw_token = event.get("authorizationToken", "")
    method_arn = event["methodArn"]

    try:
        if not raw_token.lower().startswith("bearer "):
            raise _AuthDenied("missing or malformed Authorization header")
        token = raw_token[7:]  # strip "Bearer " (case-insensitive, always 7 chars)
        user_id = _validate_token(token)
        _check_team_membership(token, user_id)
        log.info(json.dumps({"event": "auth_allow", "user_id": user_id}))
        return _policy(user_id, "Allow", method_arn)
    except _AuthDenied as e:
        log.warning(json.dumps({"event": "auth_deny", "reason": str(e)}))
        return _policy("anonymous", "Deny", method_arn)
    # Transient errors (Synapse timeout / 5xx / network) propagate uncaught.
    # API Gateway treats a Lambda error as Unauthorized (401) and does NOT
    # cache the result, so valid users aren't locked out for the cache TTL.


def _validate_token(token):
    req = urllib.request.Request(
        f"{SYNAPSE_API}/userProfile",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise _AuthDenied("invalid token") from e
        raise  # 5xx or unexpected — let it propagate
    # Synapse returns 200 + Anonymous profile for missing/invalid tokens
    if data.get("userName") == "anonymous":
        raise _AuthDenied("unauthenticated")
    return str(data["ownerId"])


def _check_team_membership(token, user_id):
    req = urllib.request.Request(
        f"{SYNAPSE_API}/team/{TEAM_ID}/member/{user_id}/membershipStatus",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise _AuthDenied("invalid token on membership check") from e
        raise
    if not data.get("isMember"):
        raise _AuthDenied("not a team member")


def _policy(principal_id, effect, method_arn):
    # Wildcard across all methods/stages so the cached policy covers both
    # POST /query and GET /query/{job_id} without a separate authorizer call.
    parts = method_arn.split(":")
    region, account = parts[3], parts[4]
    api_id = parts[5].split("/")[0]
    resource = f"arn:aws:execute-api:{region}:{account}:{api_id}/*/*"

    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource,
                }
            ],
        },
        # Passed to downstream Lambdas as event["requestContext"]["authorizer"].
        # Never include the token here — context is visible in X-Ray traces.
        "context": {"user_id": principal_id},
    }
