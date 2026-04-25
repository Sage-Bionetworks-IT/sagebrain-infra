import json
import os
import urllib.error
import urllib.request

SYNAPSE_API = "https://repo-prod.prod.sagebase.org/repo/v1"
TEAM_ID = os.environ["SYNAPSE_TEAM_ID"]


def handler(event, context):
    raw_token = event.get("authorizationToken", "")
    method_arn = event["methodArn"]

    token = raw_token.removeprefix("Bearer ").removeprefix("bearer ")

    try:
        user_id = _validate_token(token)
        _check_team_membership(token, user_id)
        return _policy(user_id, "Allow", method_arn)
    except Exception:
        return _policy("anonymous", "Deny", method_arn)


def _validate_token(token):
    req = urllib.request.Request(
        f"{SYNAPSE_API}/userProfile",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read())
    # Synapse returns 200 + Anonymous profile for missing/invalid tokens
    if data.get("userName") == "anonymous":
        raise ValueError("unauthenticated")
    return str(data["ownerId"])


def _check_team_membership(token, user_id):
    """Raises on non-200 (e.g. 404 = not a team member, 401 = bad token)."""
    req = urllib.request.Request(
        f"{SYNAPSE_API}/teamMember/{TEAM_ID}/{user_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read()


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
    }
