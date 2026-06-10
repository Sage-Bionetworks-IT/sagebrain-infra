# sage-brain-infra

AWS CDK (Python) infrastructure for the Sage Brain project. Deploys an Amazon Neptune graph database with a Synapse-authenticated read-only API (API Gateway + Lambda), a Bedrock Strands AI agent API, and SageMaker Studio for team data loading.

## AWS Profile

All AWS CLI and CDK commands require `--profile sagebrain`. Login first:

```bash
aws --profile sagebrain sso login
```

## Stacks

| Stack | Name | Purpose |
|---|---|---|
| NetworkStack | `app-dev-network` | VPC, subnets, VPC endpoints |
| NeptuneStack | `app-dev-neptune` | Neptune cluster + S3 data bucket + load role |
| NeptuneSageMakerStack | `app-dev-neptune-sagemaker` | SageMaker Studio for team data loading |
| NeptuneApiStack | `app-dev-neptune-api` | API Gateway + Lambda read-only SPARQL API |
| NeptuneAgentStack | `app-dev-neptune-agent` | Bedrock Strands AI agent — async NL-to-SPARQL via `POST /ask` + `GET /ask/{job_id}` |
| NeptuneVizStack | `app-dev-neptune-viz` | Open-source Graph Explorer on Fargate behind an IP-restricted ALB (VPN-only) |

## S3 Data Bucket

Data is stored in a date-partitioned (Hive-style) layout — each contribution is a snapshot under its own date prefix, preserving a historical data lake:

```
s3://<NeptuneDataBucketName>/
  YYYY-MM-DD/
    schema/       ← ontology / schema TTL files
    data/
      rdf/        ← data TTL files
```

Any principal authenticated to the AWS account can upload to the bucket. Bucket name, cluster endpoint, and load role ARN are all in the `app-dev-neptune` CloudFormation outputs (`NeptuneDataBucketName`, `NeptuneClusterEndpoint`, `NeptuneLoadRoleArn`).

## Loading Data

Run `tools/load_kg.py` from a SageMaker Studio terminal. Read values from CloudFormation outputs:

```bash
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

python tools/load_kg.py --prefix 2026-02-20 --stats
```

## Deployment

```bash
# Full deploy (all stacks)
cdk deploy --all --profile sagebrain

# Lambda code only (fast, skips CloudFormation for unchanged infra)
cdk deploy app-dev-neptune-api --hotswap --profile sagebrain
cdk deploy app-dev-neptune-agent --hotswap --profile sagebrain

# API Gateway or IAM changes (requires full deploy + approval bypass)
cdk deploy app-dev-neptune-api --profile sagebrain --require-approval never
cdk deploy app-dev-neptune-agent --profile sagebrain --require-approval never
```

Use `--hotswap` only for Lambda code changes. API Gateway method/stage/IAM changes need a full deploy — hotswap will silently skip them.

## Query API: src/lambda/ (async job pattern)

Three Lambdas + SQS + DynamoDB — mirrors the agent's async pattern.

### submit.py — POST /query
- Validates query (max 8000 chars), writes `status=pending` to DynamoDB, enqueues to SQS
- Returns `202 {"job_id": "...", "status": "pending"}` immediately
- Captures caller metadata (IP, user agent, X-Source) and passes it in the SQS message for audit logging

### status.py — GET /query/{job_id}
- Reads job from DynamoDB
- `complete` → `results` (raw SPARQL JSON string) + `content_type`; `error` → `error` message

### query.py — SQS worker
- SQS-triggered (batch size 1), runs in VPC to reach Neptune
- Signs requests with SigV4 (`neptune-db` service)
- **60s Neptune timeout** — handles 40s+ complex queries on the 2.27M-triple graph
- Only has `ReadDataViaQuery`, `GetEngineStatus`, `GetQueryStatus` IAM permissions
- **Logs every query** to CloudWatch as structured JSON: `job_id`, `source`, IP, user agent, duration, status

Get endpoints from CloudFormation:
```bash
aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune-api \
  --query "Stacks[0].Outputs" --output table
```

Test with curl:
```bash
# Submit
JOB=$(curl -s -X POST <ApiUrl> \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <synapse-pat>" \
  -d '{"query": "SELECT * WHERE { ?s ?p ?o } LIMIT 5"}')
JOB_ID=$(echo $JOB | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

# Poll
curl -H "Authorization: Bearer <synapse-pat>" <ApiUrl>/$JOB_ID
```

## Agent API: src/lambda_agent/ (async job pattern)

Three Lambdas + SQS + DynamoDB implement a fire-and-poll pattern that eliminates the 29s API Gateway timeout.

### Flow
```
POST /ask  →  submit.py  →  SQS  →  agent.py (worker)  →  DynamoDB
GET /ask/{job_id}  →  status.py  →  DynamoDB
```

### submit.py — POST /ask
- Generates a UUID `job_id`, writes `status=pending` to DynamoDB, enqueues to SQS
- Returns `202 {"job_id": "...", "status": "pending"}` immediately (no Bedrock/Neptune calls)

### status.py — GET /ask/{job_id}
- Reads job from DynamoDB and returns current state
- `pending` / `running` → poll again; `complete` → `answer` + `steps`; `error` → `error` + `steps`

### agent.py — SQS worker
- SQS-triggered (batch size 1). Uses [Strands Agents SDK](https://strandsagents.com) with a `query_neptune` tool
- Model: `us.anthropic.claude-sonnet-4-6` (cross-region inference profile)
- Calls `POST /query` internally — keeps all query traffic through the single audit-logged chokepoint
- Writes `status=complete` (with `answer`/`steps`) or `status=error` to DynamoDB when done
- **Logs every invocation** to CloudWatch: `job_id`, question, status, step count, duration
- Concurrency capped at 10 — prevents Bedrock/Neptune overload under burst traffic
- Failed jobs retry twice then land in the DLQ
- Requires **Bedrock model access** enabled in AWS Console → Bedrock → Model access (one-time account setup)

### DynamoDB job items
- TTL: 24 hours after creation
- `status`: `pending` → `running` → `complete` | `error`

Get endpoints from CloudFormation:
```bash
aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune-agent \
  --query "Stacks[0].Outputs" --output table
```

Test with curl:
```bash
# Submit
JOB=$(curl -s -X POST <AgentApiUrl> \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <synapse-pat>" \
  -d '{"question": "What types of biological entities are in this knowledge graph?"}')
JOB_ID=$(echo $JOB | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

# Poll
curl -H "Authorization: Bearer <synapse-pat>" <AgentApiUrl>/$JOB_ID
```

## Graph Explorer Visualization: src/neptune_viz_stack.py

Open-source [Graph Explorer](https://github.com/aws/graph-explorer) for visually browsing the knowledge graph — for non-technical users.

### Architecture
- Single **Fargate** task running `public.ecr.aws/neptune/graph-explorer:latest` (X86_64) in **private subnets**, no public IP.
- The task signs Neptune requests with **SigV4** (`IAM=true`, `SERVICE_TYPE=neptune-db`) using a least-privilege task role — read-only `neptune-db` actions only (same set as the query worker). `GRAPH_TYPE=sparql` (RDF triplestore). The proxy is locked to our cluster via `PROXY_SERVER_ALLOWED_DB_ORIGINS`.
- Fronted by an **internet-facing ALB on plain HTTP (port 80)** whose security group admits **only Sage's network egress IP** (`52.44.61.21/32`, set in `config/*.yaml` as `NEPTUNE_VIZ.allowed_cidrs`). Graph Explorer has no auth of its own — **the SG IP allow-list IS the access control**. Reach it over the Sage VPN.
- Gated by `NEPTUNE_VIZ.enabled` (default `false`; enabled in `dev`).

### Access
```bash
# Get the URL (only resolves/loads from the Sage VPN)
aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune-viz \
  --query "Stacks[0].Outputs[?OutputKey=='GraphExplorerUrl'].OutputValue" --output text
# → http://<alb-dns>/explorer
```
A default connection to Neptune is pre-configured, so the graph loads on first open. To grant more users, add their egress CIDR to `NEPTUNE_VIZ.allowed_cidrs` and redeploy.

### Caveats
- Queries go **directly** to Neptune via SigV4 — they do **not** pass through the audit-logged `/query` chokepoint, so Graph Explorer activity is not captured in the SPARQL audit log.
- HTTP only (no TLS): traffic between the VPN client and the ALB is unencrypted within the Sage network. To add TLS, supply an ACM cert + DNS and switch the listener to HTTPS.
- `latest` image tag — pin a digest if you need reproducible deploys.

## API Gateway

### app-dev-neptune-api (`POST /query`, `GET /query/{job_id}`)
- **Throttling**: 50 RPS steady-state, 100 burst (lightweight — no Neptune calls inline)
- **Timeout**: 10s integration timeout (submit/status are fast; SPARQL runs async in worker)
- **Access logs**: CloudWatch log group with 1-month retention (every request, structured JSON)
- **Execution logs**: ERROR level only
- **CloudWatch role**: set via `cloud_watch_role=True` on `RestApi`
- **Auth**: Synapse token authorizer (Lambda) — caller must present a valid Synapse PAT/OAuth token and be a member of Synapse team 273957. Result cached 5 min per token.

### app-dev-neptune-agent (`POST /ask`, `GET /ask/{job_id}`)
- **Throttling**: 50 RPS steady-state, 100 burst (lightweight — no Bedrock/Neptune calls inline)
- **Timeout**: 10s integration timeout (submit/status are fast; agent runs async in worker)
- **Access logs**: CloudWatch log group with 1-month retention
- **Auth**: same Synapse token authorizer as `/query`

## Query Audit Logging

All queries through `/query` are logged as `{"event": "sparql_query", ...}`. Agent invocations are logged as `{"event": "agent_invocation", ...}` in the agent Lambda log group. Use `"source": "agent"` vs `"source": "direct"` to distinguish callers.

CloudWatch Insights query:
```
fields @timestamp, source, query, duration_ms
| filter event = "sparql_query"
| sort @timestamp desc
```

## Bedrock / Agent: Known Gotchas

- Claude 4 models require **cross-region inference profile IDs** (prefix `us.`) — base model IDs like `anthropic.claude-sonnet-4-6` return `ValidationException`
- Model access must be enabled per-account in the Bedrock console before first use — the Lambda IAM role alone is not sufficient
- Claude 3.x models are marked Legacy in this account — use Claude 4+ only
- Lambda architecture must be `ARM_64` when bundling on Apple Silicon — `pydantic_core` and other compiled deps will fail on x86 otherwise

## Environment / Config

Environments are selected via CDK context: `--context env=dev` (default: `dev`).
Config files live in `config/`. `base.yaml` is merged with the env-specific file.

## Testing

```bash
# All unit tests
python -m pytest tests/ -s -v

# Validate synthesis
cdk synth --context env=dev

# Pre-commit (lint/validate)
pre-commit run --all-files
```

Tests live in `tests/unit/`. Lambda handler tests import from `src/lambda/` via a `sys.path` insert at the top of [test_query_handler.py](tests/unit/test_query_handler.py).

## Key Design Decisions

- Neptune has **IAM auth enabled** — all requests must be SigV4-signed. Plain `curl` returns `AccessDeniedException`.
- Neptune security group has **no broad ingress rules**. Each consumer stack (Lambda, SageMaker) adds a targeted SG-to-SG `CfnSecurityGroupIngress` rule on port 8182 to avoid cross-stack cyclic references.
- The API Lambda uses the **read endpoint** only, scoped to read-only IAM actions.
- The API is **POST only** — GET was removed to avoid URL length limits for complex SPARQL queries.
- **Synapse team-gated auth** — both APIs require a valid Synapse PAT/OAuth token and membership in team 273957. The Lambda authorizer validates via Synapse's `/userProfile` + `/team/{id}/member/{userId}/membershipStatus` endpoints; results are cached 5 min by API Gateway.
- **S3 bulk loader** is used for all data loading — not SPARQL INSERT batches. Neptune assumes `NeptuneLoadRole` (trusted by `rds.amazonaws.com`) to read from S3.
- **Date-partitioned S3 layout** (`YYYY-MM-DD/schema/` and `YYYY-MM-DD/data/rdf/`) preserves a historical data lake. Each load is a full reset + reload from a chosen prefix.
- **SageMaker Studio** runs in `VpcOnly` mode so notebook kernels can reach Neptune. Requires VPC interface endpoints for `sagemaker.api` and `sts` (in NetworkStack).
- **Both APIs are async (submit + poll)** — Neptune SPARQL on the 2.27M-triple graph takes 4–40s; synchronous API Gateway has a hard 29s limit. SQS + DynamoDB decouples HTTP from execution for both `/query` and `/ask`.
- **Agent routes through `/query`** rather than calling Neptune directly. The `query_neptune` tool submits to `/query` and polls `/query/{job_id}` — keeps all query traffic through the single audit-logged chokepoint, and the agent inherits future ACLs for free.
- **Worker concurrency is capped** — query worker uncapped (SPARQL is read-only); agent worker capped at 10 concurrent invocations to prevent Bedrock rate-limit errors under burst traffic.
- **Agent Lambda is ARM_64** — bundled with `platform=linux/arm64` to match compiled dependencies on Apple Silicon dev machines.
