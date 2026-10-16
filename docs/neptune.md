# Neptune Graph Database

Amazon Neptune cluster for the Sage Brain knowledge graph.

## Architecture

- **Neptune Cluster**: Managed graph database in private subnets, IAM auth enabled
- **Security Groups**: No broad ingress — each consumer stack adds a targeted SG-to-SG rule on port 8182
- **Graph Explorer** (`app-dev-neptune-viz`): Visual graph browsing interface running on Fargate behind an IP-restricted ALB (VPN-only access). Open-source Neptune Graph Explorer for non-technical users to explore the knowledge graph visually.
- **SPARQL API** (`app-dev-api`, jobs in `app-dev-neptune-api`): Async read-only SPARQL interface, Synapse team-gated. `POST /query` submits a job and returns a `job_id`. A worker Lambda executes the SPARQL against Neptune and writes results to DynamoDB. Callers poll `GET /query/{job_id}`. All queries logged to CloudWatch with source, principal, IP, duration, and query text.
- **AI Agent API** (`app-dev-api`, jobs in `app-dev-neptune-agent`): Async natural-language interface. `POST /ask` enqueues a job and returns a `job_id` immediately. A worker Lambda (Bedrock Strands, model from `AGENT.bedrock_model_id`) processes the job via SQS, writes the result to DynamoDB, and the caller polls `GET /ask/{job_id}` for the answer.
- **Backup & Monitoring**: Automated backups and CloudWatch audit/slow-query logging

## Accessing Neptune

### Via Graph Explorer (visual interface)

Get the URL from CloudFormation (only accessible from Sage VPN):

```bash
aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune-viz \
  --query "Stacks[0].Outputs[?OutputKey=='GraphExplorerUrl'].OutputValue" \
  --output text
```

Open the URL in a browser — the connection to Neptune is pre-configured. Graph Explorer provides a visual interface for exploring the knowledge graph, browsing entities, and running SPARQL queries interactively.

**Access Control**: Graph Explorer is fronted by an ALB with security group rules that only allow Sage's network egress IP (`52.44.61.21/32`). It has no authentication of its own — the IP allowlist is the access control.

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

### From the SPARQL API (read-only, Synapse team-gated)

Every request needs a Synapse PAT from a member of the environment's team (see the README's
"Using the API"; full reference at `<ApiBaseUrl>/api`). SPARQL queries use the same async job
pattern as `/ask`:

```bash
# 1. Submit query — returns immediately with a job_id
curl -X POST <ApiBaseUrl>/query \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <synapse-pat>" \
  -d '{"query": "SELECT (COUNT(*) AS ?c) WHERE { ?s ?p ?o }"}'
# → {"job_id": "abc-123", "status": "pending"}

# 2. Poll until status is "complete" or "error"
curl -H "Authorization: Bearer <synapse-pat>" <ApiBaseUrl>/query/abc-123
# → {"job_id": "abc-123", "status": "complete", "results": "{...sparql-results+json...}", "content_type": "..."}
```

Complex queries on the 2.27M-triple graph can take 25–40s — the async pattern means they complete successfully rather than hitting a 504.

### From the AI Agent API (natural language)

Ask questions in plain English using the async job pattern:

```bash
# 1. Submit a question — returns immediately with a job_id
curl -X POST <ApiBaseUrl>/ask \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <synapse-pat>" \
  -d '{"question": "What genes are linked to NF1?"}'
# → {"job_id": "abc-123", "status": "pending"}

# 2. Poll until status is "complete" or "error"
curl -H "Authorization: Bearer <synapse-pat>" <ApiBaseUrl>/ask/abc-123
# → {"job_id": "abc-123", "status": "complete", "answer": "...", "steps": [...]}
```

The agent runs its SPARQL in-process through the shared query service (`sagebrain_core.run_query`),
so every agent-generated query gets the same limits and appears in the audit log with
`"source": "agent"` and the asking user as `principal`.

## Query Audit Logging

Every SPARQL query, from the `/query` worker or the agent, is logged to CloudWatch as structured JSON
(by `sagebrain_core.neptune`):

```json
{
  "event": "sparql_query",
  "job_id": "abc-123",
  "query": "SELECT (COUNT(*) AS ?c) WHERE { ?s ?p ?o }",
  "query_length": 42,
  "source": "direct",
  "principal": "3421234",
  "source_ip": "1.2.3.4",
  "user_agent": "curl/8.7.1",
  "status_code": 200,
  "duration_ms": 719.0,
  "timestamp": 1774890587.4
}
```

`source` is `"agent"` when called by the Bedrock agent, and otherwise the caller's `X-Source` header
(default `"direct"`). `principal` is the Synapse user id of the caller. `/query` lines are in the
query worker's log group; the agent's are in the agent worker's.

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

Data is loaded by the append-only ingestion pipeline (`app-dev-neptune-pipeline`) using the
Neptune S3 bulk loader. Each data release is a dated snapshot per portal, so S3 keeps a
historical data lake and Neptune keeps every snapshot in its own named graph
(`urn:sagebrain:{portal}:{date}`).

### S3 Layout

```
s3://<NeptuneDataBucketName>/
  {portal}/YYYY-MM-DD/
    data/          ← Turtle only; this whole prefix is bulk loaded
    other/         ← optional: anything that is not Turtle
    manifest.ttl   ← written LAST; its arrival triggers the load
```

Get the bucket name from CloudFormation:

```bash
aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune \
  --query "Stacks[0].Outputs[?OutputKey=='NeptuneDataBucketName'].OutputValue" \
  --output text
```

> [!IMPORTANT]
> Only Turtle may live under `data/`. The bulk loader parses every object under the prefix, and
> with `failOnError=TRUE` one non-RDF file (e.g. a `README.md`) fails the whole snapshot.

### Uploading a snapshot

```bash
export NEPTUNE_BUCKET=$(aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune \
  --query "Stacks[0].Outputs[?OutputKey=='NeptuneDataBucketName'].OutputValue" \
  --output text)

aws s3 cp ./data/ s3://$NEPTUNE_BUCKET/nf/2026-02-20/data/ --recursive
aws s3 cp ./manifest.ttl s3://$NEPTUNE_BUCKET/nf/2026-02-20/manifest.ttl   # last: starts the load
```

The `manifest.ttl` upload triggers EventBridge → Step Functions → the bulk loader. Progress and
lineage are in the Step Functions executions and the `app-dev-neptune-pipeline-loads` DynamoDB
table; see the "Automated Ingestion Pipeline" section of CLAUDE.md.

## Adding Graph Explorer Users

To grant more users access to Graph Explorer, add their egress CIDR to `config/<env>.yaml`:

```yaml
NEPTUNE_VIZ:
  enabled: true
  allowed_cidrs:
    - "52.44.61.21/32"  # Sage network
    - "1.2.3.4/32"      # Additional user
```

Then open a PR; merging it deploys the change through CI.

## Configuration

Neptune settings live in `config/base.yaml` (merged with environment overrides):

| Setting | Default | Notes |
|---|---|---|
| `engine_version` | `1.3.2.1` | |
| `serverless_min_capacity` | `1.0` | Required (stack is serverless-only) |
| `serverless_max_capacity` | `8.0` | Required (stack is serverless-only) |
| `instance_count` | `1` | Number of Neptune instances in the cluster |
| `instance_class` | `db.serverless` | Not configurable (stack always uses `db.serverless`) |
| `backup_retention_days` | `7` | |
| `iam_auth_enabled` | `true` | All requests must be SigV4-signed |
| `storage_encrypted` | `true` | |
| `deletion_protection` | `true` | Set to `false` in dev |

## Serverless Capacity by Environment

| Environment | Min NCU | Max NCU | Notes |
|---|---|---|---|
| Dev | `1.0` | `4.0` | Lower cost default |
| Stage | `1.0` | `8.0` | Mirrors baseline prod behavior |
| Prod | `2.0` | `16.0` | Higher ceiling for production load |

## Monitoring

CloudWatch logs enabled for:
- **Audit logs**: database access and operations
- **Slow query logs**: performance bottlenecks

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Graph Explorer won't load | Not connected to Sage VPN, or your IP not in `allowed_cidrs` — check ALB security group rules |
| `AccessDeniedException` on Neptune | Request not SigV4-signed, or IAM role missing Neptune permissions |
| `TimeoutError` on port 8182 | Security group rule missing, or VPC routing issue |
| `POST /query` or `/ask` returns 5XX | Should not happen — the submit Lambda only enqueues. Check the submit Lambda in `app-{env}-api` and the API's access log; a 503 means the authorizer couldn't reach Synapse |
| `GET /ask/{job_id}` returns `{"status": "pending"}` indefinitely | Worker Lambda may have errored — check the worker log group (`NeptuneAgentWorkerFunction`). Failed jobs land in the DLQ after 2 retries. |
| `GET /ask/{job_id}` returns `{"status": "error"}` | Agent error (Bedrock timeout, bad SPARQL, etc.) — `"error"` field has the message; `"steps"` shows how far it got. |
| `AccessDeniedException` on Bedrock | Model access for `AGENT.bedrock_model_id` not enabled — go to **AWS Console → Bedrock → Model access** and accept it |
| Agent answers but no query logs | The agent's `sparql_query` lines are in the agent worker's log group, not the query worker's |
