
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

## Accessing Neptune via SageMaker Studio

Team members can load and query the knowledge graph directly from JupyterLab in the AWS Console — no SSH or EC2 required.

### 1. Open Studio

Go to **AWS Console → SageMaker → Studio**, select your user profile, and launch a JupyterLab space.

### 2. Get the Neptune endpoint

```console
aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune \
  --query "Stacks[0].Outputs[?OutputKey=='NeptuneClusterEndpoint'].OutputValue" \
  --output text
```

### 3. Query Neptune from a notebook

Authentication is handled automatically via the Studio execution role (SigV4 signed). Install dependencies once per space:

```console
pip install requests aws-requests-auth
```

```python
from aws_requests_auth.boto_utils import BotoAWSRequestsAuth
import requests

ENDPOINT = "<NEPTUNE_CLUSTER_ENDPOINT>"
auth = BotoAWSRequestsAuth(aws_host=f"{ENDPOINT}:8182", aws_region="us-east-1", aws_service="neptune-db")

resp = requests.get(f"https://{ENDPOINT}:8182/status", auth=auth, timeout=10)
print(resp.json())
```

> [!NOTE]
> Neptune has IAM auth enabled. All requests must be SigV4-signed — plain `curl` will return `AccessDeniedException`. Use `aws-requests-auth` or `awscurl`.

> [!NOTE]
> For detailed usage including bulk data loading, see [docs/neptune.md](docs/neptune.md).

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
