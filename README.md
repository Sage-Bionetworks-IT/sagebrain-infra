
# sage-brain-infra

AWS CDK infrastructure for the Sage Brain project, deploying an Amazon Neptune graph database with a public read-only SPARQL API and an AI agent interface.

## Features

- **VPC Networking**: Isolated network environment with public and private subnets
- **Amazon Neptune**: Managed graph database for knowledge graphs
- **SPARQL API**: Read-only API Gateway + Lambda endpoint for querying Neptune over HTTPS (Synapse team-gated)
- **AI Agent API**: Natural-language query interface powered by Bedrock Strands (Claude Sonnet 4.6) — converts plain-language questions to SPARQL and returns answers with full reasoning trace
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
 * `cdk deploy`      deploy this stack to your default AWS account/region
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

## Login with the AWS CLI

> [!NOTE]
> Requires AWS CLI v2. Check your version with `aws --version`. If needed, [install AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html).

Configure SSO interactively (first time only):

```console
aws configure sso
```

When prompted, select the `org-sagebase-sagebrain-prod` account and set the profile name to `sagebrain`.

Login:

```console
aws --profile sagebrain sso login
```

## Deploy

```console
AWS_PROFILE=sagebrain cdk deploy --context env=dev --all
```

To deploy a specific stack:

```console
AWS_PROFILE=sagebrain cdk deploy app-dev-neptune-api --context env=dev
```

## Querying Neptune via the Public API

### Direct SPARQL (`POST /query`)

A read-only SPARQL endpoint is available over HTTPS. Requests must include a Synapse Personal Access Token (PAT) and the caller must be a member of Synapse team (dev: [273957](https://www.synapse.org/Team:273957), prod: [3605470](https://www.synapse.org/Team:3605470)) (configured as `AUTH.synapse_team_id` in `config/base.yaml`).

Get the API URL from CDK outputs after deployment:

```console
aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune-api \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
  --output text
```

Send a `POST` request with a JSON body containing your SPARQL query:

```console
curl -X POST <API_URL> \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-synapse-pat>" \
  -d '{"query": "SELECT * WHERE { ?s ?p ?o } LIMIT 10"}'
```

The endpoint returns `application/sparql-results+json`. Queries are limited to 8000 characters and throttled to 50 requests/second (burst: 100).

### Natural Language Agent (`POST /ask`)

Ask questions in plain English. The agent (Bedrock Strands + Claude Sonnet 4.6) translates your question into SPARQL, queries Neptune, and returns a plain-language answer with the full reasoning trace.

Get the agent URL from CDK outputs:

```console
aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune-agent \
  --query "Stacks[0].Outputs[?OutputKey=='AgentApiUrl'].OutputValue" \
  --output text
```

Send a `POST` request with a `question` field:

```console
curl -X POST <AGENT_URL> \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-synapse-pat>" \
  -d '{"question": "What types of biological entities are in this knowledge graph?"}'
```

Response includes the answer and the SPARQL steps taken:

```json
{
  "answer": "The graph contains 408,644 files, 1,332 cell lines, 734 donors...",
  "steps": [
    {"type": "tool_call", "tool": "query_neptune", "sparql": "SELECT ?type (COUNT(?e) AS ?count) ..."},
    {"type": "tool_result", "tool": "query_neptune", "preview": "..."}
  ]
}
```

> [!NOTE]
> Requires Claude Sonnet 4.6 model access to be enabled in **AWS Console → Bedrock → Model access**. This is a one-time account-level setup.

## Loading Data

Data loads automatically through the ingestion pipeline (`app-dev-neptune-pipeline`). Upload a
snapshot per portal (keyed by a date or a `v…` release tag), and write `manifest.ttl` **last**: its arrival starts a Neptune bulk
load of that snapshot's `data/` prefix into the named graph `urn:sagebrain:{portal}:{tag}`.

```
s3://<NeptuneDataBucketName>/
  {portal}/{tag}/   ← tag: YYYY-MM-DD, or a release such as v97
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

Secrets can be manually created in the
[AWS Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/create_secret.html).
When naming your secret make sure that the secret does not end in a pattern that matches
`-??????`, this will cause issues with how AWS CDK looks up secrets.

To pass secrets to a container set the secrets manager `container_secrets`
In this repository, the infrastructure does not define application-specific helpers such as `ServiceProps` or `ServiceSecret`. Instead, it assumes that:

- Sensitive values (for example, Neptune credentials or application API keys) are stored in **AWS Secrets Manager** or **SSM Parameter Store**.
- Client applications that connect to Neptune (from trusted workloads in the VPC) are responsible for retrieving those secrets and exposing them to their own runtime (for example, as environment variables or in their own configuration layer).

A typical pattern is:

1. Store connection details (host, port, user, password, etc.) in a secret in AWS Secrets Manager.
2. Grant IAM permissions for that secret to application workloads that will access Neptune from within the VPC.
3. Have those clients retrieve the secret at runtime and use it to construct the Neptune endpoint/connection string.

The exact mechanism for loading and using secrets (for example, via environment variables, configuration files, or direct SDK calls to AWS Secrets Manager) is left to the consuming application or tooling and is not implemented in this CDK stack.

> [!NOTE]
> Retrieving secrets requires access to the AWS Secrets Manager (and/or SSM Parameter Store) and appropriate IAM permissions.
