
# sage-brain-infra

AWS CDK infrastructure for the Sage Brain project, deploying an Amazon Neptune graph database with a public read-only SPARQL API and a secure bastion host for development access.

## Features

- **VPC Networking**: Isolated network environment with public and private subnets
- **Amazon Neptune**: Managed graph database for knowledge graphs
- **Public SPARQL API**: Read-only API Gateway + Lambda endpoint for querying Neptune over HTTPS
- **SageMaker Studio**: Team JupyterLab environment for loading and querying the knowledge graph

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
AWS_PROFILE=sagebrain cdk deploy app-dev-neptune-sagemaker --context env=dev
```

## Querying Neptune via the Public API

A read-only SPARQL endpoint is available over HTTPS — no authentication required.

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
  -d '{"query": "SELECT * WHERE { ?s ?p ?o } LIMIT 10"}'
```

The endpoint returns `application/sparql-results+json`. Queries are limited to 8000 characters and throttled to 50 requests/second (burst: 100).

## Accessing and Loading Data via SageMaker Studio

Team members can load and query the knowledge graph directly from JupyterLab in the AWS Console — no SSH or EC2 required.

### 1. Open Studio

Go to **AWS Console → SageMaker → Studio**, select your user profile, and launch a JupyterLab space.

### 2. Upload data to S3

Data is stored in a date-partitioned layout that preserves a historical data lake. Each contribution goes under its own date prefix:

```
s3://app-dev-neptune-neptunedatabucketb8719d9a-7a8slykvpqaf/
  YYYY-MM-DD/
    schema/       ← ontology / schema TTL files
    data/
      rdf/        ← data TTL files
```

Upload via the AWS Console S3 UI, or from the Studio terminal:

```console
aws s3 cp ./schema/ s3://app-dev-neptune-neptunedatabucketb8719d9a-7a8slykvpqaf/2026-02-20/schema/ --recursive
aws s3 cp ./data/rdf/ s3://app-dev-neptune-neptunedatabucketb8719d9a-7a8slykvpqaf/2026-02-20/data/rdf/ --recursive
```

### 3. Load into Neptune

Run `tools/load_kg.py` from a Studio terminal. It resets Neptune, loads schema first, then data:

```console
pip install requests aws-requests-auth

export NEPTUNE_ENDPOINT=neptunedbcluster-mwltugp7vgl4.cluster-cwbs4mqme6zz.us-east-1.neptune.amazonaws.com
export NEPTUNE_BUCKET=app-dev-neptune-neptunedatabucketb8719d9a-7a8slykvpqaf
export NEPTUNE_LOAD_ROLE=arn:aws:iam::620117233256:role/app-dev-neptune-NeptuneLoadRole6C006CFE-Q9xhaXQNFYGw

python tools/load_kg.py --prefix 2026-02-20 --stats
```

> [!NOTE]
> Neptune has IAM auth enabled. All requests must be SigV4-signed — plain `curl` will return `AccessDeniedException`. Use `aws-requests-auth` or `awscurl`.

> [!NOTE]
> For detailed usage including SPARQL querying, see [docs/neptune.md](docs/neptune.md).

## Secrets

Secrets can be manually created in the
[AWS Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/create_secret.html).
When naming your secret make sure that the secret does not end in a pattern that matches
`-??????`, this will cause issues with how AWS CDK looks up secrets.

To pass secrets to a container set the secrets manager `container_secrets`
In this repository, the infrastructure does not define application-specific helpers such as `ServiceProps` or `ServiceSecret`. Instead, it assumes that:

- Sensitive values (for example, Neptune credentials or application API keys) are stored in **AWS Secrets Manager** or **SSM Parameter Store**.
- Client applications that connect to Neptune (typically through the bastion host or from other trusted workloads in the VPC) are responsible for retrieving those secrets and exposing them to their own runtime (for example, as environment variables or in their own configuration layer).

A typical pattern is:

1. Store connection details (host, port, user, password, etc.) in a secret in AWS Secrets Manager.
2. Grant IAM permissions for that secret to:
   - Developers or automation that need to connect via the bastion host, and/or
   - Application workloads that will access Neptune from within the VPC.
3. Have those clients retrieve the secret at runtime and use it to construct the Neptune endpoint/connection string.

The exact mechanism for loading and using secrets (for example, via environment variables, configuration files, or direct SDK calls to AWS Secrets Manager) is left to the consuming application or tooling and is not implemented in this CDK stack.

> [!NOTE]
> Retrieving secrets requires access to the AWS Secrets Manager (and/or SSM Parameter Store) and appropriate IAM permissions.
