import json
import os
import random
import time
from decimal import Decimal

import boto3
import requests
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

NEPTUNE_QUERY_URL = os.environ["NEPTUNE_QUERY_URL"]
# Base URL for polling: GET {NEPTUNE_QUERY_STATUS_URL}/{job_id}
# Derived from NEPTUNE_QUERY_URL if not explicitly set (same API, different path)
NEPTUNE_QUERY_STATUS_URL = os.environ.get("NEPTUNE_QUERY_STATUS_URL", NEPTUNE_QUERY_URL)
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
REGION = os.environ.get("AWS_REGION", "us-east-1")
DYNAMODB_TABLE = os.environ["JOB_TABLE_NAME"]

QUERY_POLL_INTERVAL = 3  # seconds between polls
QUERY_POLL_TIMEOUT = (
    70  # seconds before giving up; Neptune query worker has 75s timeout
)

_dynamodb = boto3.resource("dynamodb")

SYSTEM_PROMPT = """You are a biomedical knowledge graph assistant for the Sage Brain project.
You have access to a Neptune RDF graph containing biomedical data about genes, diseases,
pathways, and their relationships. The primary ontology namespace is
<http://nf-osi.github.com/terms#> (prefix: nf:).

When a user asks a question:
1. Call get_schema to discover available classes and properties if you are unsure of the graph structure.
   Pass a namespace_prefix to scope results to a specific ontology.
2. Formulate a SPARQL SELECT query to answer the question.
3. Call the query_neptune tool with that query.
4. Interpret the results and answer in plain language.
5. If the first query returns no results or needs refinement, try an alternative query.

Always explain what you found and how confident you are in the answer.
"""

# Module-level state reset per invocation.
_steps: list = []
_current_job_id: str = ""


def _flush_steps():
    """Write current steps to DynamoDB so the polling client sees live progress."""
    # TODO: steps grow with each tool call; a long-running agent can accumulate enough
    # steps to push the item over DynamoDB's 400KB limit before the job even completes.
    if _current_job_id:
        _update_job(_current_job_id, steps=_steps)


@tool
def query_neptune(sparql: str) -> str:
    """Execute a SPARQL SELECT query against the Neptune biomedical knowledge graph.
    Returns results as a JSON string with 'results.bindings' containing the rows.
    Use standard SPARQL 1.1 syntax with PREFIX declarations."""
    _steps.append({"type": "tool_call", "tool": "query_neptune", "sparql": sparql})
    _flush_steps()
    if _current_job_id:
        _update_job(
            _current_job_id,
            status_detail=f"Executing SPARQL query (step {len(_steps)})...",
        )

    # Submit job
    submit_response = requests.post(
        NEPTUNE_QUERY_URL,
        json={"query": sparql},
        headers={"X-Source": "agent"},
        timeout=10,
    )
    submit_response.raise_for_status()
    job_id = submit_response.json()["job_id"]

    # Poll until complete or timeout
    deadline = time.time() + QUERY_POLL_TIMEOUT
    while time.time() < deadline:
        time.sleep(QUERY_POLL_INTERVAL)
        poll = requests.get(
            f"{NEPTUNE_QUERY_STATUS_URL}/{job_id}",
            timeout=10,
        )
        poll.raise_for_status()
        data = poll.json()
        status = data["status"]

        if status == "complete":
            result_text = data.get("results", "")
            _steps.append(
                {
                    "type": "tool_result",
                    "tool": "query_neptune",
                    "preview": result_text[:500],
                }
            )
            _flush_steps()
            return result_text

        if status == "error":
            error_msg = data.get("error", "Unknown query error")
            _steps.append(
                {"type": "tool_result", "tool": "query_neptune", "error": error_msg}
            )
            _flush_steps()
            raise RuntimeError(f"SPARQL query failed: {error_msg}")

    timeout_msg = (
        f"SPARQL query job {job_id} did not complete within {QUERY_POLL_TIMEOUT}s"
    )
    _steps.append(
        {"type": "tool_result", "tool": "query_neptune", "error": timeout_msg}
    )
    _flush_steps()
    raise TimeoutError(timeout_msg)


_SCHEMA_SPARQL_BASE = """\
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT DISTINCT ?term ?kind ?label ?comment ?domain ?range WHERE {{
    {{
        ?term a owl:Class .
        BIND("Class" AS ?kind)
    }} UNION {{
        ?term a rdfs:Class .
        BIND("Class" AS ?kind)
    }} UNION {{
        ?term a owl:ObjectProperty .
        BIND("ObjectProperty" AS ?kind)
    }} UNION {{
        ?term a owl:DatatypeProperty .
        BIND("DatatypeProperty" AS ?kind)
    }} UNION {{
        ?term a rdf:Property .
        BIND("Property" AS ?kind)
    }}
    FILTER(isIRI(?term))
    OPTIONAL {{ ?term rdfs:label ?label }}
    OPTIONAL {{ ?term rdfs:comment ?comment }}
    OPTIONAL {{ ?term rdfs:domain ?domain }}
    OPTIONAL {{ ?term rdfs:range ?range }}
    {filter}
}} ORDER BY ?kind ?term"""


@tool
def get_schema(namespace_prefix: str = "") -> str:
    """Return all classes and properties defined in the knowledge graph ontology.

    Use this to discover the graph structure (available types and predicates)
    before writing SPARQL queries.

    Args:
        namespace_prefix: Optional IRI prefix to filter results to a specific
            ontology (e.g. "http://nf-osi.github.com/terms#"). Leave empty
            to return terms from all loaded ontologies.

    Returns a JSON string with 'results.bindings' rows containing term, kind,
    label, comment, domain, and range fields.
    """
    if namespace_prefix:
        if not namespace_prefix.startswith(("http://", "https://")):
            raise ValueError(
                f"namespace_prefix must be an HTTP(S) IRI, got: {namespace_prefix!r}"
            )
        safe_prefix = namespace_prefix.replace("\\", "").replace('"', "")
        filter_clause = f'FILTER(STRSTARTS(STR(?term), "{safe_prefix}"))'
    else:
        filter_clause = ""
    sparql = _SCHEMA_SPARQL_BASE.format(filter=filter_clause)
    return query_neptune(sparql)


def _update_job(job_id: str, **fields):
    table = _dynamodb.Table(DYNAMODB_TABLE)
    update_expr = "SET " + ", ".join(f"#{k} = :{k}" for k in fields)
    expr_names = {f"#{k}": k for k in fields}
    # DynamoDB doesn't support float — convert to Decimal
    expr_values = {
        f":{k}": Decimal(str(v)) if isinstance(v, float) else v
        for k, v in fields.items()
    }
    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


def _invoke_agent_with_retry(agent, question: str, job_id: str):
    """
    Call the Strands agent, retrying on transient Bedrock capacity errors.
    Wait 30s between attempts; 2 retries fit within the 300s Lambda budget.
    """
    MAX_ATTEMPTS = 3
    RETRY_WAIT = 30

    for attempt in range(MAX_ATTEMPTS):
        # Reset steps so a retry shows a clean trace
        global _steps
        _steps = []
        _flush_steps()
        _update_job(
            job_id,
            status_detail=f"Generating SPARQL query (attempt {attempt + 1}/{MAX_ATTEMPTS})...",
        )
        try:
            return agent(question)
        except Exception as e:
            is_transient = "ServiceUnavailableException" in str(e)
            if is_transient and attempt < MAX_ATTEMPTS - 1:
                _update_job(
                    job_id,
                    status_detail=f"Model temporarily unavailable, retrying ({attempt + 2}/{MAX_ATTEMPTS})...",
                )
                jitter = random.uniform(0, 10)
                wait = RETRY_WAIT + jitter
                print(
                    json.dumps(
                        {
                            "event": "bedrock_retry",
                            "job_id": job_id,
                            "attempt": attempt + 1,
                            "wait_s": round(wait, 1),
                            "error": str(e)[:200],
                        }
                    )
                )
                time.sleep(wait)
            else:
                raise


def _process_job(job_id: str, question: str):
    global _steps, _current_job_id
    _steps = []
    _current_job_id = job_id
    start = time.time()

    _update_job(job_id, status="running")

    model = BedrockModel(model_id=BEDROCK_MODEL_ID, region_name=REGION)
    agent = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[query_neptune, get_schema],
    )

    try:
        result = _invoke_agent_with_retry(agent, question, job_id)
        duration = (time.time() - start) * 1000
        # TODO: answer + steps are stored in a single DynamoDB item; a verbose agent
        # response with many steps can exceed the 400KB item size limit.
        # Consider truncating steps or offloading large payloads to S3.
        _update_job(
            job_id,
            status="complete",
            answer=str(result),
            steps=_steps,
            duration_ms=round(duration, 2),
        )
        print(
            json.dumps(
                {
                    "event": "agent_invocation",
                    "job_id": job_id,
                    "question": question,
                    "status": "success",
                    "step_count": len(_steps),
                    "duration_ms": round(duration, 2),
                    "timestamp": time.time(),
                }
            )
        )
    except Exception as e:
        duration = (time.time() - start) * 1000
        # TODO: steps written on error path can also exceed 400KB if the agent ran many iterations.
        _update_job(job_id, status="error", error=str(e), steps=_steps)
        print(
            json.dumps(
                {
                    "event": "agent_invocation",
                    "job_id": job_id,
                    "question": question,
                    "status": "error",
                    "error": str(e),
                    "step_count": len(_steps),
                    "duration_ms": round(duration, 2),
                    "timestamp": time.time(),
                }
            )
        )
        raise  # re-raise so SQS can retry via DLQ


def handler(event, context):
    """SQS-triggered worker. Each record is one job."""
    for record in event["Records"]:
        body = json.loads(record["body"])
        job_id = body["job_id"]
        question = body["question"]
        _process_job(job_id, question)
