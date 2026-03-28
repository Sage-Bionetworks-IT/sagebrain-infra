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
| NeptuneBastionStack | `app-dev-neptune-bastion` | EC2 bastion for dev access |
| NeptuneApiStack | `app-dev-neptune-api` | API Gateway + Lambda read-only SPARQL API |

## Deployment

```bash
# Full deploy
cdk deploy --all --profile sagebrain

# Single stack (fast, code-only changes)
cdk deploy app-dev-neptune-api --hotswap --profile sagebrain
```

`--hotswap` updates Lambda code directly without a CloudFormation deployment — use it for Lambda-only changes.

## Lambda: src/lambda/query.py

Handles SPARQL queries against Neptune via API Gateway GET `/query?query=<SPARQL>`.

- Signs requests with SigV4 (`neptune-db` service)
- Forwards Neptune's `Content-Type` header (typically `application/sparql-results+json`) to callers
- Only has `ReadDataViaQuery`, `GetEngineStatus`, `GetQueryStatus` IAM permissions

Live endpoint: `https://ewdwpljfla.execute-api.us-east-1.amazonaws.com/prod/query`

## Environment / Config

Environments are selected via CDK context: `--context env=dev` (default: `dev`).
Config files live in `config/`. `base.yaml` is merged with the env-specific file.

## Testing

```bash
# Unit tests
python -m pytest tests/ -s -v

# Validate synthesis
cdk synth --context env=dev

# Pre-commit (lint/validate)
pre-commit run --all-files
```

## Key Design Decisions

- Neptune has **IAM auth enabled** — all requests must be SigV4-signed. Plain `curl` returns `AccessDeniedException`.
- Neptune security group has **no broad ingress rules**. Each consumer stack (Lambda, bastion) adds a targeted SG-to-SG ingress rule on port 8182.
- The API Lambda uses the **read endpoint** only, scoped to read-only IAM actions.
- `CfnSecurityGroupIngress` is used (not `SecurityGroup.add_ingress_rule`) to avoid cross-stack cyclic references.
