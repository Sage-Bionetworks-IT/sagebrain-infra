import json
import os

import requests
from aws_requests_auth.boto_utils import BotoAWSRequestsAuth

NEPTUNE_ENDPOINT = os.environ["NEPTUNE_ENDPOINT"]
REGION = os.environ["AWS_REGION"]


def handler(event, context):
    params = event.get("queryStringParameters") or {}
    query = params.get("query", "SELECT * WHERE { ?s ?p ?o } LIMIT 10")

    auth = BotoAWSRequestsAuth(
        aws_host=NEPTUNE_ENDPOINT,
        aws_region=REGION,
        aws_service="neptune-db",
    )

    try:
        response = requests.post(
            f"https://{NEPTUNE_ENDPOINT}:8182/sparql",
            auth=auth,
            data={"query": query},
            headers={"Accept": "application/sparql-results+json"},
            timeout=30,
        )
        response.raise_for_status()
        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
            },
            "body": response.text,
        }
    except requests.exceptions.HTTPError as e:
        return {
            "statusCode": response.status_code,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": str(e)}),
        }
    except Exception as e:
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": str(e)}),
        }
