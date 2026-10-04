import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal

import boto3
from sagebrain_core.query_service import run_query
from sagebrain_core.ratelimit import LEGACY_AGENT_PRINCIPAL
from sagebrain_core.errors import QueryRejected, RateLimited
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

# Set by CDK from config AGENT.bedrock_model_id; the fallback only serves local runs and tests.
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-5-5")
REGION = os.environ.get("AWS_REGION", "us-east-1")
DYNAMODB_TABLE = os.environ["JOB_TABLE_NAME"]

# Legacy /ask Lambda messages carry the caller's raw token instead of a user_id (data-model.md).
# The old authorizer already authenticated them; their queries are charged to this principal,
# which sagebrain_core gives the machine bucket (spec 001 D-12).
LEGACY_PRINCIPAL = LEGACY_AGENT_PRINCIPAL
UNKNOWN_SOURCE_IP = "unknown"

# After stripping PREFIX declarations, the query must begin with SELECT.
_PREFIX_STRIP_RE = re.compile(
    r"^\s*(PREFIX\s+\S+\s*<[^>]*>\s*(#[^\n]*)?\s*)*",
    re.IGNORECASE,
)


def _safe_sparql(sparql: str) -> str:
    """Reject non-SELECT queries."""
    clean = re.sub(
        r"(?m)^\s*#[^\n]*\n?", "", sparql
    )  # strip full-line comments only (# inside IRIs must survive)
    body = _PREFIX_STRIP_RE.sub("", clean).lstrip()
    if not body.upper().startswith("SELECT"):
        first = body.split()[0] if body.split() else "(empty)"
        raise ValueError(f"Only SPARQL SELECT queries are permitted; got {first!r}.")
    return sparql


_dynamodb = boto3.resource("dynamodb")

SYSTEM_PROMPT = """You are a biomedical knowledge graph assistant for the Sage Brain project.
You have access to a Neptune RDF graph containing biomedical data about genes, diseases,
pathways, and their relationships. The primary ontology namespace is
<http://nf-osi.github.com/terms#> (prefix: nf:).

When a user asks a question:
1. Call get_schema to discover available classes and properties if you are unsure of the graph structure.
   Pass a namespace — one of "nf-osi", "obo", "efo", or "edam".
2. Formulate a SPARQL SELECT query to answer the question.
3. Call the query_neptune tool with that query.
4. Interpret the results and answer in plain language.
5. If the first query returns no results or needs refinement, try an alternative query.

Always explain what you found and how confident you are in the answer.
"""


@dataclass
class _Job:
    """One agent job's caller context and step trace. Built per SQS message, never shared."""

    job_id: str
    principal: str
    source_ip: str
    steps: list = field(default_factory=list)

    def record(self, step: dict):
        """Append a step and write the trace so the polling client sees live progress."""
        # TODO: steps grow with each tool call; a long-running agent can accumulate enough
        # steps to push the item over DynamoDB's 400KB limit before the job even completes.
        self.steps.append(step)
        _update_job(self.job_id, steps=self.steps)


def _rejection_message(e: Exception) -> str:
    if isinstance(e, RateLimited):
        return f"Rate limit exceeded; retry after {e.retry_after:.1f}s"
    return e.message


def _query_neptune(job: _Job, sparql: str) -> str:
    sparql = _safe_sparql(sparql)
    job.record({"type": "tool_call", "tool": "query_neptune", "sparql": sparql})
    _update_job(
        job.job_id,
        status_detail=f"Executing SPARQL query (step {len(job.steps)})...",
    )

    # Validation, rate admission and the sparql_query audit log all live in run_query.
    try:
        result = run_query(
            sparql,
            principal=job.principal,
            source="agent",
            job_id=job.job_id,
            source_ip=job.source_ip,
        )
    except (QueryRejected, RateLimited) as e:
        # A rejected query is the model's to fix (shorten it, slow down), not a job failure.
        error_msg = _rejection_message(e)
        job.record({"type": "tool_result", "tool": "query_neptune", "error": error_msg})
        return f"Error: {error_msg}"
    except Exception as e:
        job.record({"type": "tool_result", "tool": "query_neptune", "error": str(e)})
        raise RuntimeError(f"SPARQL query failed: {e}") from e

    job.record(
        {"type": "tool_result", "tool": "query_neptune", "preview": result.text[:500]}
    )
    return result.text


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

# Allowed namespace prefixes surfaced to the agent as discrete choices.
KNOWN_NAMESPACES = {
    "nf-osi": "http://nf-osi.github.com/terms#",
    "obo": "http://purl.obolibrary.org/obo/",
    "efo": "http://www.ebi.ac.uk/efo/",
    "edam": "http://edamontology.org/",
}


def _schema_sparql(namespace: str) -> str:
    if namespace not in KNOWN_NAMESPACES:
        raise ValueError(
            f"Unknown namespace {namespace!r}. "
            f"Must be one of: {', '.join(sorted(KNOWN_NAMESPACES))}."
        )
    prefix_iri = KNOWN_NAMESPACES[namespace]
    filter_clause = f'FILTER(STRSTARTS(STR(?term), "{prefix_iri}"))'
    return _SCHEMA_SPARQL_BASE.format(filter=filter_clause)


def _make_tools(job: _Job) -> list:
    """The agent's tools, bound to one job so a warm instance can't mix callers."""

    @tool
    def query_neptune(sparql: str) -> str:
        """Execute a SPARQL SELECT query against the Neptune biomedical knowledge graph.
        Returns results as a JSON string with 'results.bindings' containing the rows.
        Use standard SPARQL 1.1 syntax with PREFIX declarations."""
        return _query_neptune(job, sparql)

    @tool
    def get_schema(namespace: str) -> str:
        """Return all classes and properties defined in the knowledge graph ontology
        for a specific namespace.

        Use this to discover the graph structure (available types and predicates)
        before writing SPARQL queries.

        Args:
            namespace: The ontology namespace to inspect. Must be one of:
                - "nf-osi" → http://nf-osi.github.com/terms#
                - "obo"    → http://purl.obolibrary.org/obo/
                - "efo"    → http://www.ebi.ac.uk/efo/
                - "edam"   → http://edamontology.org/

        Returns a JSON string with 'results.bindings' rows containing term, kind,
        label, comment, domain, and range fields.
        """
        return _query_neptune(job, _schema_sparql(namespace))

    return [query_neptune, get_schema]


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


def _invoke_agent_with_retry(agent, question: str, job: _Job):
    """
    Call the Strands agent, retrying on transient Bedrock capacity errors.
    Wait 30s between attempts; 2 retries fit within the 300s Lambda budget.
    """
    MAX_ATTEMPTS = 3
    RETRY_WAIT = 30

    for attempt in range(MAX_ATTEMPTS):
        # Reset steps so a retry shows a clean trace
        job.steps = []
        _update_job(job.job_id, steps=job.steps)
        _update_job(
            job.job_id,
            status_detail=f"Generating SPARQL query (attempt {attempt + 1}/{MAX_ATTEMPTS})...",
        )
        try:
            return agent(question)
        except Exception as e:
            is_transient = "ServiceUnavailableException" in str(e)
            if is_transient and attempt < MAX_ATTEMPTS - 1:
                _update_job(
                    job.job_id,
                    status_detail=f"Model temporarily unavailable, retrying ({attempt + 2}/{MAX_ATTEMPTS})...",
                )
                jitter = random.uniform(0, 10)
                wait = RETRY_WAIT + jitter
                print(
                    json.dumps(
                        {
                            "event": "bedrock_retry",
                            "job_id": job.job_id,
                            "attempt": attempt + 1,
                            "wait_s": round(wait, 1),
                            "error": str(e)[:200],
                        }
                    )
                )
                time.sleep(wait)
            else:
                raise


def _process_job(job: _Job, question: str):
    start = time.time()
    job_id = job.job_id

    _update_job(job_id, status="running")

    model = BedrockModel(model_id=BEDROCK_MODEL_ID, region_name=REGION)
    agent = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=_make_tools(job),
    )

    try:
        result = _invoke_agent_with_retry(agent, question, job)
        duration = (time.time() - start) * 1000
        # TODO: answer + steps are stored in a single DynamoDB item; a verbose agent
        # response with many steps can exceed the 400KB item size limit.
        # Consider truncating steps or offloading large payloads to S3.
        _update_job(
            job_id,
            status="complete",
            answer=str(result),
            steps=job.steps,
            duration_ms=round(duration, 2),
        )
        print(
            json.dumps(
                {
                    "event": "agent_invocation",
                    "job_id": job_id,
                    "principal": job.principal,
                    "question": question,
                    "status": "success",
                    "step_count": len(job.steps),
                    "duration_ms": round(duration, 2),
                    "timestamp": time.time(),
                }
            )
        )
    except Exception as e:
        duration = (time.time() - start) * 1000
        # TODO: steps written on error path can also exceed 400KB if the agent ran many iterations.
        _update_job(job_id, status="error", error=str(e), steps=job.steps)
        print(
            json.dumps(
                {
                    "event": "agent_invocation",
                    "job_id": job_id,
                    "principal": job.principal,
                    "question": question,
                    "status": "error",
                    "error": str(e),
                    "step_count": len(job.steps),
                    "duration_ms": round(duration, 2),
                    "timestamp": time.time(),
                }
            )
        )
        raise  # re-raise so SQS can retry via DLQ


def _job_from_message(body: dict) -> _Job | None:
    """Accept both agent-queue shapes (data-model.md). The `authorization` value is never kept."""
    if body.get("user_id"):
        return _Job(
            job_id=body["job_id"],
            principal=str(body["user_id"]),
            source_ip=body.get("source_ip") or UNKNOWN_SOURCE_IP,
        )
    if "authorization" in body:
        return _Job(
            job_id=body["job_id"],
            principal=LEGACY_PRINCIPAL,
            source_ip=UNKNOWN_SOURCE_IP,
        )
    return None


def handler(event, context):
    """SQS-triggered worker. Each record is one job."""
    for record in event["Records"]:
        body = json.loads(record["body"])
        job = _job_from_message(body)
        if job is None:
            # Retrying can't fix a message with no caller; fail the job instead of the batch.
            _update_job(
                body["job_id"],
                status="error",
                error="Agent job message has neither 'user_id' nor 'authorization'",
            )
            continue
        _process_job(job, body["question"])
