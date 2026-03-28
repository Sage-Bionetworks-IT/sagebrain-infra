import json
import os
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


def handler(event, context):
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

    # Sign the request with SigV4 using botocore directly
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
        return {
            "statusCode": 200,
            "headers": {"Content-Type": content_type, **CORS_HEADERS},
            "body": response.text,
        }
    except requests.exceptions.HTTPError as e:
        return {
            "statusCode": response.status_code,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps({"error": str(e)}),
        }
    except Exception as e:
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps({"error": str(e)}),
        }
