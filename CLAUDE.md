# sage-brain-infra

AWS CDK (Python) infrastructure for the Sage Brain project. Deploys an Amazon Neptune graph database with a public read-only API (API Gateway + Lambda), a Bedrock Strands AI agent API, and SageMaker Studio for team data loading.

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
| NeptuneAgentStack | `app-dev-neptune-agent` | Bedrock Strands AI agent — NL-to-SPARQL via `POST /ask` |

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

## Lambda: src/lambda/query.py

Handles SPARQL queries against Neptune via `POST /query` with a JSON body `{"query": "<SPARQL>"}`.

- Accepts POST only (no GET) — avoids URL length limits for complex queries
- Query length capped at 8000 characters (DoS/cost guard)
- Signs requests with SigV4 (`neptune-db` service)
- Forwards Neptune's `Content-Type` header (typically `application/sparql-results+json`)
- CORS headers (`Access-Control-Allow-Origin: *`) on all responses including errors
- Only has `ReadDataViaQuery`, `GetEngineStatus`, `GetQueryStatus` IAM permissions
- **Logs every query** to CloudWatch as structured JSON including `source` (`"direct"` or `"agent"`), IP, user agent, duration, and status code

Get the live endpoint from CloudFormation:
```bash
aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune-api \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
  --output text
```

Test with curl:
```bash
curl -X POST <ApiUrl> \
  -H "Content-Type: application/json" \
  -d '{"query": "SELECT * WHERE { ?s ?p ?o } LIMIT 5"}'
```

## Lambda: src/lambda_agent/agent.py

Handles natural-language questions via `POST /ask` with a JSON body `{"question": "<question>"}`.

- Uses [Strands Agents SDK](https://strandsagents.com) with a `query_neptune` tool
- Model: `us.anthropic.claude-sonnet-4-6` (cross-region inference profile)
- Calls `POST /query` internally — does NOT sign directly to Neptune. This keeps all query traffic through the single audit-logged chokepoint.
- Returns `{"answer": "...", "steps": [...]}` — `steps` contains each tool call and result for UI transparency
- **Logs every invocation** to CloudWatch: question, status, step count, duration
- Requires **Bedrock model access** enabled in AWS Console → Bedrock → Model access (one-time account setup)

Get the live endpoint from CloudFormation:
```bash
aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune-agent \
  --query "Stacks[0].Outputs[?OutputKey=='AgentApiUrl'].OutputValue" \
  --output text
```

Test with curl:
```bash
curl -X POST <AgentApiUrl> \
  -H "Content-Type: application/json" \
  -d '{"question": "What types of biological entities are in this knowledge graph?"}'
```

## API Gateway

### app-dev-neptune-api (`POST /query`)
- **Throttling**: 50 RPS steady-state, 100 burst
- **Access logs**: CloudWatch log group with 1-month retention (every request, structured JSON)
- **Execution logs**: ERROR level only
- **CloudWatch role**: set via `cloud_watch_role=True` on `RestApi`
- No authentication — intentionally public read-only; throttling + IAM read-only scope are the mitigations

### app-dev-neptune-agent (`POST /ask`)
- **Throttling**: 10 RPS steady-state, 20 burst (agent calls are slower and more expensive)
- **Timeout**: 29s hard limit (API Gateway constraint) — typical agent responses complete in 10-20s
- **Access logs**: CloudWatch log group with 1-month retention
- No authentication — same public posture as `/query`

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
- **No auth planned** — the API is intentionally public; throttling and read-only IAM are the mitigations.
- **S3 bulk loader** is used for all data loading — not SPARQL INSERT batches. Neptune assumes `NeptuneLoadRole` (trusted by `rds.amazonaws.com`) to read from S3.
- **Date-partitioned S3 layout** (`YYYY-MM-DD/schema/` and `YYYY-MM-DD/data/rdf/`) preserves a historical data lake. Each load is a full reset + reload from a chosen prefix.
- **SageMaker Studio** runs in `VpcOnly` mode so notebook kernels can reach Neptune. Requires VPC interface endpoints for `sagemaker.api` and `sts` (in NetworkStack).
- **Agent routes through `/query`** rather than calling Neptune directly. This keeps `/query` as the single access control chokepoint — when node/edge-level filtering or caller-based ACLs are added, the agent inherits them for free.
- **Agent Lambda is ARM_64** — bundled with `platform=linux/arm64` to match compiled dependencies on Apple Silicon dev machines.
