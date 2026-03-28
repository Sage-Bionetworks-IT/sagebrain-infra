# sage-brain-infra

AWS CDK (Python) infrastructure for the Sage Brain project. Deploys an Amazon Neptune graph database with a public read-only API (API Gateway + Lambda) and a bastion host for dev access.

## AWS Profile

All AWS CLI and CDK commands require `--profile sagebrain`. Login first:

```bash
aws --profile sagebrain sso login
```

## Stacks

| Stack | Name | Purpose |
|---|---|---|
| NetworkStack | `app-dev-network` | VPC, subnets |
| NeptuneStack | `app-dev-neptune` | Neptune cluster |
| NeptuneBastionStack | `app-dev-neptune-bastion` | EC2 bastion for dev access via SSM |
| NeptuneApiStack | `app-dev-neptune-api` | API Gateway + Lambda read-only SPARQL API |

## Deployment

```bash
# Full deploy (all stacks)
cdk deploy --all --profile sagebrain

# Lambda code only (fast, skips CloudFormation for unchanged infra)
cdk deploy app-dev-neptune-api --hotswap --profile sagebrain

# API Gateway or IAM changes (requires full deploy + approval bypass)
cdk deploy app-dev-neptune-api --profile sagebrain --require-approval never
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

Live endpoint: `https://ewdwpljfla.execute-api.us-east-1.amazonaws.com/prod/query`

Test with curl:
```bash
curl -X POST https://ewdwpljfla.execute-api.us-east-1.amazonaws.com/prod/query \
  -H "Content-Type: application/json" \
  -d '{"query": "SELECT * WHERE { ?s ?p ?o } LIMIT 5"}'
```

## API Gateway

- **Throttling**: 50 RPS steady-state, 100 burst
- **Access logs**: CloudWatch log group with 1-month retention (every request, structured JSON)
- **Execution logs**: ERROR level only (failures only, auto-named log group)
- **CloudWatch role**: set via `cloud_watch_role=True` on `RestApi` (CDK manages the account-level role)
- No authentication — intentionally public read-only API; throttling + IAM read-only scope are the mitigations

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
- Neptune security group has **no broad ingress rules**. Each consumer stack (Lambda, bastion) adds a targeted SG-to-SG `CfnSecurityGroupIngress` rule on port 8182 to avoid cross-stack cyclic references.
- The API Lambda uses the **read endpoint** only, scoped to read-only IAM actions.
- The API is **POST only** — GET was removed to avoid URL length limits for complex SPARQL queries.
- **No auth planned** — the API is intentionally public; throttling and read-only IAM are the mitigations.
- IMDSv2 is enforced on the bastion via a `LaunchTemplate` (CDK sets `require_imdsv2=True` this way, not directly on the instance).
