import json
import os
import time
from urllib.parse import urlencode

import botocore.session
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

NEPTUNE_ENDPOINT = os.environ["NEPTUNE_ENDPOINT"]
REGION = os.environ["AWS_REGION"]

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
}

MAX_QUERY_LENGTH = 8000


def _log_query(query: str, event: dict, status_code: int, duration_ms: float):
    source_ip = (
        event.get("requestContext", {}).get("identity", {}).get("sourceIp", "unknown")
    )
    user_agent = (event.get("headers") or {}).get("User-Agent", "unknown")
    print(
        json.dumps(
            {
                "event": "sparql_query",
                "query": query,
                "query_length": len(query),
                "source_ip": source_ip,
                "user_agent": user_agent,
                "status_code": status_code,
                "duration_ms": round(duration_ms, 2),
                "timestamp": time.time(),
            }
        )
    )


def handler(event, context):
    start = time.time()

    try:
        body = json.loads(event.get("body") or "{}")
        query = body.get("query", "SELECT * WHERE { ?s ?p ?o } LIMIT 10")
    except json.JSONDecodeError:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps({"error": "Request body must be valid JSON"}),
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
            timeout=30,
        )
        response.raise_for_status()
        content_type = response.headers.get(
            "Content-Type", "application/sparql-results+json"
        )
        _log_query(query, event, 200, (time.time() - start) * 1000)
        return {
            "statusCode": 200,
            "headers": {"Content-Type": content_type, **CORS_HEADERS},
            "body": response.text,
        }
    except requests.exceptions.HTTPError as e:
        _log_query(query, event, response.status_code, (time.time() - start) * 1000)
        return {
            "statusCode": response.status_code,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps({"error": str(e)}),
        }
    except Exception as e:
        _log_query(query, event, 500, (time.time() - start) * 1000)
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps({"error": str(e)}),
        }
