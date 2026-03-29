import json
import os
from urllib.parse import urlencode

import botocore.session
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

NEPTUNE_ENDPOINT = os.environ["NEPTUNE_ENDPOINT"]
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
REGION = os.environ.get("AWS_REGION", "us-east-1")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
}

SYSTEM_PROMPT = """You are a biomedical knowledge graph assistant for the Sage Brain project.
You have access to a Neptune RDF graph containing biomedical data about genes, diseases,
pathways, and their relationships, described using standard ontologies (e.g. RDF/OWL, SPARQL).

When a user asks a question:
1. Formulate a SPARQL SELECT query to answer it.
2. Call the query_neptune tool with that query.
3. Interpret the results and answer in plain language.
4. If the first query returns no results or needs refinement, try an alternative query.

Always explain what you found and how confident you are in the answer.
"""

# Module-level list reset per invocation to capture tool calls for the response.
_steps: list = []


@tool
def query_neptune(sparql: str) -> str:
    """Execute a SPARQL SELECT query against the Neptune biomedical knowledge graph.
    Returns results as a JSON string with 'results.bindings' containing the rows.
    Use standard SPARQL 1.1 syntax with PREFIX declarations."""
    _steps.append({"type": "tool_call", "tool": "query_neptune", "sparql": sparql})

    url = f"https://{NEPTUNE_ENDPOINT}:8182/sparql"
    body = urlencode({"query": sparql})
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
        timeout=25,
    )
    response.raise_for_status()

    result_text = response.text
    _steps.append(
        {
            "type": "tool_result",
            "tool": "query_neptune",
            "preview": result_text[:500],
        }
    )
    return result_text


def handler(event, context):
    global _steps
    _steps = []

    try:
        body = json.loads(event.get("body") or "{}")
        question = body.get("question", "").strip()
    except json.JSONDecodeError:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps({"error": "Request body must be valid JSON"}),
        }

    if not question:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps({"error": "Missing 'question' field"}),
        }

    model = BedrockModel(model_id=BEDROCK_MODEL_ID, region_name=REGION)
    agent = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[query_neptune],
    )

    try:
        result = agent(question)
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps(
                {
                    "answer": str(result),
                    "steps": _steps,
                }
            ),
        }
    except Exception as e:
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps({"error": str(e), "steps": _steps}),
        }
