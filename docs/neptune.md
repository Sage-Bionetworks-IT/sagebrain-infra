# Neptune Graph Database

Amazon Neptune cluster for the Sage Brain knowledge graph.

## Architecture

- **Neptune Cluster**: Managed graph database in private subnets, IAM auth enabled
- **Security Groups**: No broad ingress — each consumer stack adds a targeted SG-to-SG rule on port 8182
- **SageMaker Studio**: Team access for loading and querying data via JupyterLab (VpcOnly mode, routes through VPC)
- **Public SPARQL API** (`app-dev-neptune-api`): Async read-only SPARQL interface. `POST /query` submits a job and returns a `job_id`. A worker Lambda executes the SPARQL against Neptune and writes results to DynamoDB. Callers poll `GET /query/{job_id}`. All queries logged to CloudWatch with source, IP, duration, and query text.
- **AI Agent API** (`app-dev-neptune-agent`): Async natural-language interface. `POST /ask` enqueues a job and returns a `job_id` immediately. A worker Lambda (Bedrock Strands / Claude Sonnet 4.6) processes the job via SQS, writes the result to DynamoDB, and the caller polls `GET /ask/{job_id}` for the answer.
- **Backup & Monitoring**: Automated backups and CloudWatch audit/slow-query logging

## Accessing Neptune

### From SageMaker Studio (team access)

Go to **AWS Console → SageMaker → Studio**, open your user profile, and launch a JupyterLab space.

The execution role has full Neptune permissions (read, write, delete) scoped to this cluster. Authentication is handled automatically via instance credentials — no keys needed.

**Cluster endpoints** (from CloudFormation outputs):

```bash
# Writer endpoint (read + write)
aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune \
  --query "Stacks[0].Outputs[?OutputKey=='NeptuneClusterEndpoint'].OutputValue" \
  --output text

# Reader endpoint (read only)
aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune \
  --query "Stacks[0].Outputs[?OutputKey=='NeptuneClusterReadEndpoint'].OutputValue" \
  --output text
```

**Verify connectivity from a notebook:**

```python
import socket
socket.create_connection(("YOUR_NEPTUNE_ENDPOINT", 8182), timeout=5)
print("connected")
```

**Query Neptune from a notebook:**

```python
import requests
from aws_requests_auth.boto_utils import BotoAWSRequestsAuth

ENDPOINT = "<NeptuneClusterEndpoint>"  # from app-dev-neptune CloudFormation outputs
auth = BotoAWSRequestsAuth(
    aws_host=f"{ENDPOINT}:8182",
    aws_region="us-east-1",
    aws_service="neptune-db",
)

def sparql(query: str) -> list[dict]:
    resp = requests.post(
        f"https://{ENDPOINT}:8182/sparql",
        data={"query": query},
        auth=auth,
        headers={"Accept": "application/sparql-results+json"},
        timeout=60,
    )
    resp.raise_for_status()
    bindings = resp.json()["results"]["bindings"]
    return [{k: v["value"] for k, v in row.items()} for row in bindings]

# Check triple counts per named graph
import pandas as pd
rows = sparql("""
    SELECT ?graph (COUNT(*) AS ?triples)
    WHERE { GRAPH ?graph { ?s ?p ?o } }
    GROUP BY ?graph
    ORDER BY DESC(?triples)
""")
pd.DataFrame(rows)
```

### From the Public SPARQL API (read-only, no auth)

SPARQL queries use the same async job pattern as `/ask`:

```bash
# 1. Submit query — returns immediately with a job_id
curl -X POST <ApiUrl> \
  -H "Content-Type: application/json" \
  -d '{"query": "SELECT (COUNT(*) AS ?c) WHERE { ?s ?p ?o }"}'
# → {"job_id": "abc-123", "status": "pending"}

# 2. Poll until status is "complete" or "error"
curl <ApiUrl>/abc-123
# → {"job_id": "abc-123", "status": "complete", "results": "{...sparql-results+json...}", "content_type": "..."}
```

Complex queries on the 2.27M-triple graph can take 25–40s — the async pattern means they complete successfully rather than hitting a 504.

### From the AI Agent API (natural language)

Ask questions in plain English using the async job pattern:

```bash
# 1. Submit a question — returns immediately with a job_id
curl -X POST <AgentApiUrl> \
  -H "Content-Type: application/json" \
  -d '{"question": "What genes are linked to NF1?"}'
# → {"job_id": "abc-123", "status": "pending"}

# 2. Poll until status is "complete" or "error"
curl <AgentApiUrl>/abc-123
# → {"job_id": "abc-123", "status": "complete", "answer": "...", "steps": [...]}
```

The agent routes all SPARQL through `/query`, so every agent-generated query appears in the query audit log with `"source": "agent"`.

## Query Audit Logging

Every SPARQL query through `/query` is logged to CloudWatch as structured JSON:

```json
{
  "event": "sparql_query",
  "query": "SELECT (COUNT(*) AS ?c) WHERE { ?s ?p ?o }",
  "query_length": 42,
  "source": "direct",
  "source_ip": "1.2.3.4",
  "user_agent": "curl/8.7.1",
  "status_code": 200,
  "duration_ms": 719.0,
  "timestamp": 1774890587.4
}
```

`source` is `"agent"` when called by the Bedrock agent, `"direct"` for all other callers.

**Query logs in CloudWatch Insights:**

```
fields @timestamp, source, query, duration_ms, status_code
| filter event = "sparql_query"
| sort @timestamp desc
```

**Filter to agent queries only:**

```
fields @timestamp, query, duration_ms
| filter event = "sparql_query" and source = "agent"
| sort @timestamp desc
```

Agent invocations (question asked, answer status, total duration, step count) are logged separately in the agent Lambda log group:

```json
{
  "event": "agent_invocation",
  "question": "What genes are linked to NF1?",
  "status": "success",
  "step_count": 2,
  "duration_ms": 7224.65,
  "source_ip": "1.2.3.4",
  "timestamp": 1774890237.1
}
```

## Loading Data

Data is loaded via the Neptune S3 bulk loader. Each data release is uploaded to S3 under a date-partitioned prefix, creating a historical data lake of past snapshots.

### S3 Layout (Hive-style partitioning)

```
s3://<NeptuneDataBucketName>/
  YYYY-MM-DD/
    schema/       ← ontology / schema TTL files
    data/
      rdf/        ← data TTL files (one or more .ttl files)
```

Get the bucket name from CloudFormation:

```bash
aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune \
  --query "Stacks[0].Outputs[?OutputKey=='NeptuneDataBucketName'].OutputValue" \
  --output text
```

Each date prefix is a self-contained snapshot. Old prefixes are retained in S3 — they are not loaded into Neptune but are available for reference or rollback.

> [!NOTE]
> Non-RDF files (e.g. `README.md`) in the S3 prefix are harmlessly skipped by the bulk loader.

### Uploading data to S3

From a Studio terminal or any machine with the `sagebrain` AWS profile:

```bash
export NEPTUNE_BUCKET=$(aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune \
  --query "Stacks[0].Outputs[?OutputKey=='NeptuneDataBucketName'].OutputValue" \
  --output text)

aws s3 cp ./schema/ s3://$NEPTUNE_BUCKET/2026-02-20/schema/ --recursive
aws s3 cp ./data/rdf/ s3://$NEPTUNE_BUCKET/2026-02-20/data/rdf/ --recursive
```

### Running the loader

Run `tools/load_kg.py` from a JupyterLab terminal in Studio. It performs a full reset (wipes Neptune) then loads schema followed by data:

```bash
pip install requests aws-requests-auth

export NEPTUNE_ENDPOINT=$(aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune \
  --query "Stacks[0].Outputs[?OutputKey=='NeptuneClusterEndpoint'].OutputValue" \
  --output text)
export NEPTUNE_BUCKET=$(aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune \
  --query "Stacks[0].Outputs[?OutputKey=='NeptuneDataBucketName'].OutputValue" \
  --output text)
export NEPTUNE_LOAD_ROLE=$(aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune \
  --query "Stacks[0].Outputs[?OutputKey=='NeptuneLoadRoleArn'].OutputValue" \
  --output text)

# Full reset + load
python tools/load_kg.py --prefix 2026-02-20 --stats

# Append only (no reset)
python tools/load_kg.py --prefix 2026-02-20 --no-reset --stats

# Dry-run (print what would happen, no changes)
python tools/load_kg.py --prefix 2026-02-20 --dry-run
```

The loader:
1. Resets Neptune via the system reset endpoint (faster than `DROP ALL`)
2. Waits for Neptune to restart (~15–30s)
3. Bulk loads `YYYY-MM-DD/schema/` first
4. Bulk loads `YYYY-MM-DD/data/rdf/`
5. Polls until each job completes and prints record counts

## Adding Team Members

User profiles are managed in CDK. Add a new username to `config/dev.yaml`:

```yaml
NEPTUNE_SAGEMAKER:
  user_profiles:
    - thomas-yu
    - new-user   # alphanumeric + hyphens only, no dots
```

Then deploy:

```bash
AWS_PROFILE=sagebrain cdk deploy app-dev-neptune-sagemaker --context env=dev
```

The user can then log into the AWS Console and launch Studio from their profile. No `iam:PassRole` permission is required on their SSO role — CloudFormation handles it.

New users automatically inherit access to the Neptune S3 data bucket and can run `tools/load_kg.py` from their Studio space.

## Configuration

Neptune settings live in `config/base.yaml` (merged with environment overrides):

| Setting | Default | Notes |
|---|---|---|
| `engine_version` | `1.3.2.1` | |
| `backup_retention_days` | `7` | |
| `iam_auth_enabled` | `true` | All requests must be SigV4-signed |
| `storage_encrypted` | `true` | |
| `deletion_protection` | `true` | Set to `false` in dev |

## Instance Classes

| Environment | Instance | Notes |
|---|---|---|
| Dev | `db.t3.medium` | Burstable, deletion protection off |
| Prod | `db.r5.large`+ | Memory-optimized for graph workloads |

## Monitoring

CloudWatch logs enabled for:
- **Audit logs**: database access and operations
- **Slow query logs**: performance bottlenecks

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `ConnectTimeout` from Studio | Space wasn't restarted after SG/network change — stop and restart the space |
| `AccessDeniedException` on Neptune | Request not SigV4-signed, or IAM role missing Neptune permissions |
| `TimeoutError` on port 8182 | Security group rule missing, or VPC routing issue |
| `POST /ask` returns 504 | Should not happen — submit Lambda only enqueues. Check if the stack was redeployed with the new async stack. |
| `GET /ask/{job_id}` returns `{"status": "pending"}` indefinitely | Worker Lambda may have errored — check the worker log group (`NeptuneAgentWorkerFunction`). Failed jobs land in the DLQ after 2 retries. |
| `GET /ask/{job_id}` returns `{"status": "error"}` | Agent error (Bedrock timeout, bad SPARQL, etc.) — `"error"` field has the message; `"steps"` shows how far it got. |
| `AccessDeniedException` on Bedrock | Claude Sonnet 4.6 model access not enabled — go to **AWS Console → Bedrock → Model access** and request access |
| Agent answers but no query logs | Agent is calling `/query` correctly — check the query Lambda log group, not the agent log group |
