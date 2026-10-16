
# sage-brain-infra

AWS CDK infrastructure for the Sage Brain project, deploying an Amazon Neptune graph database with a Synapse team-gated, read-only SPARQL API and an AI agent interface, both served by one API Gateway REST API.

## Features

- **VPC Networking**: Isolated network environment with public and private subnets
- **Amazon Neptune**: Managed graph database for knowledge graphs
- **SPARQL API** (`/query`): Async, read-only SPARQL jobs against Neptune over HTTPS (Synapse team-gated)
- **AI Agent API** (`/ask`): Natural-language query interface powered by Bedrock Strands (the Claude model in `AGENT.bedrock_model_id`) — converts plain-language questions to SPARQL and returns answers with full reasoning trace
- **API docs** (`/api`): Swagger UI and the OpenAPI contract ([api/openapi.yaml](api/openapi.yaml)), public
- **Automated Ingestion Pipeline**: Append-only, event-driven loader — an S3 `manifest.ttl` upload triggers EventBridge → Step Functions → Neptune bulk load, one named graph per dated snapshot
- **Query Audit Logging**: All SPARQL queries logged to CloudWatch with source tracking (direct vs. agent)

## Prerequisites

AWS CDK projects require bootstrapping before synthesis or deployment.
Please review the [bootstrapping documentation](https://docs.aws.amazon.com/cdk/v2/guide/getting_started.html#getting_started_bootstrap).

> [!NOTE]
> Sage IT deploys this CDK bootstrap upon creation of every AWS account in our AWS Organization.

# Development

Activate the Python virtual environment:

```console
source .venv/bin/activate
```

Install dependencies:

```console
pip install -r requirements.txt
```

## Useful commands

 * `cdk ls`          list all stacks in the app
 * `cdk synth`       emits the synthesized CloudFormation template
 * `cdk diff`        compare deployed stack with current state
 * `cdk docs`        open CDK documentation

## Testing

### Static Analysis

Validate CDK json, yaml and python files with [pre-commit](https://pre-commit.com):

```console
pre-commit run --all-files
```

Verify CDK to CloudFormation conversion:

```console
cdk synth --context env=dev
```

### Unit Tests

```console
python -m pytest tests/ -s -v
```

## Environments

When running `cdk` commands, specify the environment via context variable:

```console
cdk synth --context env=dev
cdk synth --context env=prod
```

Environment-specific config files live in [config/](./config). A `base.yaml` is always loaded and merged with the environment-specific file, with environment values taking precedence.

> [!NOTE]
> Ensure that `VPC_CIDR` is unique within your AWS organization.
> Refer to our [guidance](https://sagebionetworks.jira.com/wiki/spaces/IT/pages/2850586648/Setup+AWS+VPC) on selecting a unique CIDR block.

# Deployment

Deploys run in CI: merging to `dev` deploys the dev account, merging to `prod` deploys prod
([.github/workflows/](.github/workflows)), each with `cdk deploy --all`. Don't deploy from a laptop.

## Login with the AWS CLI

> [!NOTE]
> Requires AWS CLI v2. Check your version with `aws --version`. If needed, [install AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html).

Each environment has its own account. Configure SSO interactively (first time only):

```console
aws configure sso
```

Name the dev account's profile `sagebrain-dev` (account `442650749796`) and the prod account's
`sagebrain` (`org-sagebase-sagebrain-prod`, `620117233256`). Then log in:

```console
aws --profile sagebrain-dev sso login   # dev
aws --profile sagebrain sso login       # prod
```

Compare your branch with what is deployed:

```console
cdk diff --context env=dev --profile sagebrain-dev
```

## Using the API

One REST API serves `/query`, `/ask` and the docs. Its full reference, with "Try it out", is the
Swagger UI at `<API_BASE>/api`; the contract is also at `<API_BASE>/api/openapi.json` and
`<API_BASE>/api/openapi.yaml`.

Get the base URL (it ends in `/dev` or `/prod`) from the `app-{env}-api` stack outputs:

```console
aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-prod-api \
  --query "Stacks[0].Outputs[?OutputKey=='ApiBaseUrl'].OutputValue" \
  --output text
```

Every `/query` and `/ask` request needs a Synapse personal access token (PAT) or OAuth token in
`Authorization: Bearer`, and its user must be a member of the environment's Synapse team (dev:
[273957](https://www.synapse.org/Team:273957), prod: [3605470](https://www.synapse.org/Team:3605470);
`AUTH.synapse_team_id` in `config/`). Any denial returns 401 `{"message": "Unauthorized"}`.

Both endpoints are asynchronous: the `POST` returns `202 {"job_id", "status": "pending"}`, and you
poll `GET .../{job_id}` until `status` is `complete` or `error`. While a job is `pending` or
`running`, wait the `Retry-After` header's seconds (2) between polls. Jobs expire after 24 hours.

### Direct SPARQL (`POST /query`)

```console
curl -X POST <API_BASE>/query \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-synapse-pat>" \
  -d '{"query": "SELECT * WHERE { ?s ?p ?o } LIMIT 10"}'
# → {"job_id": "abc-123", "status": "pending"}

curl -H "Authorization: Bearer <your-synapse-pat>" <API_BASE>/query/abc-123
# → {"job_id": "abc-123", "status": "complete", "results": "{...sparql-results+json...}", ...}
```

`results` is the SPARQL JSON result as a string. Queries are read-only, at most 8000 characters,
and time out after 60 s in Neptune; a result over 350 KB returns `status: error`.

### Natural Language Agent (`POST /ask`)

Ask questions in plain English. The agent (Bedrock Strands + Claude) translates your question into
SPARQL, queries Neptune, and returns a plain-language answer with the full reasoning trace.
Questions are at most 2000 characters.

```console
curl -X POST <API_BASE>/ask \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-synapse-pat>" \
  -d '{"question": "What types of biological entities are in this knowledge graph?"}'
# → {"job_id": "def-456", "status": "pending"}

curl -H "Authorization: Bearer <your-synapse-pat>" <API_BASE>/ask/def-456
```

The finished job has the answer and the SPARQL steps taken:

```json
{
  "job_id": "def-456",
  "status": "complete",
  "answer": "The graph contains 408,644 files, 1,332 cell lines, 734 donors...",
  "steps": [
    {"type": "tool_call", "tool": "query_neptune", "sparql": "SELECT ?type (COUNT(?e) AS ?count) ..."},
    {"type": "tool_result", "tool": "query_neptune", "preview": "..."}
  ]
}
```

### Limits and errors

`POST` requests are throttled to 50 requests/second (burst 100) and status polls to 100 (burst
200), shared by all callers; a throttled request gets 429 with `Retry-After: 1`. Request bodies
are capped at 256 KiB. Application errors (validation, unknown job) return `{"error": "..."}`;
errors from the API's front door (auth, throttling, size, unknown paths) return
`{"message": "..."}`. Every limit is listed in the contract's `info.x-sagebrain-limits`.

> [!NOTE]
> The agent's Bedrock model needs its model access (Marketplace agreement) accepted once per
> account in **AWS Console → Bedrock → Model access**.

## Loading Data

Data loads automatically through the ingestion pipeline (`app-dev-neptune-pipeline`). Upload a
dated snapshot per portal, and write `manifest.ttl` **last**: its arrival starts a Neptune bulk
load of that snapshot's `data/` prefix into the named graph `urn:sagebrain:{portal}:{date}`.

```
s3://<NeptuneDataBucketName>/
  {portal}/YYYY-MM-DD/
    data/          ← Turtle only; this whole prefix is bulk loaded
    other/         ← optional: anything that is not Turtle
    manifest.ttl   ← written LAST; its arrival triggers the load
```

Only Turtle may live under `data/`: the bulk loader parses every object there, and one non-RDF
file fails the whole snapshot.

Get the bucket name from CloudFormation and upload:

```console
export NEPTUNE_BUCKET=$(aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune \
  --query "Stacks[0].Outputs[?OutputKey=='NeptuneDataBucketName'].OutputValue" \
  --output text)

aws s3 cp ./data/ s3://$NEPTUNE_BUCKET/nf/2026-02-20/data/ --recursive
aws s3 cp ./manifest.ttl s3://$NEPTUNE_BUCKET/nf/2026-02-20/manifest.ttl
```

> [!NOTE]
> For the pipeline design, the manifest format and how to watch a load, see the "Automated
> Ingestion Pipeline" section of [CLAUDE.md](CLAUDE.md). For SPARQL querying, see
> [docs/neptune.md](docs/neptune.md).

## Secrets

There are none to manage. Neptune uses IAM authentication: every request is SigV4-signed by the
calling Lambda's or task's role, and the API checks callers' Synapse tokens on each request
without storing them.
