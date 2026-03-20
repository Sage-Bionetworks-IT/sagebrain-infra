
# sage-brain-infra

AWS CDK infrastructure for the Sage Brain project, deploying an Amazon Neptune graph database with a secure bastion host for development access.

## Features

- **VPC Networking**: Isolated network environment with public and private subnets
- **Amazon Neptune**: Managed graph database for knowledge graphs
- **Bastion Host**: Secure remote access for Neptune development via SSM

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
AWS_PROFILE=sagebrain cdk deploy app-dev-neptune-bastion --context env=dev
```

## Connecting to the Neptune Bastion Host

### 1. Connect via SSM

No SSH key required — the bastion has the SSM agent installed.

```console
aws --profile sagebrain ssm start-session --target <BASTION_INSTANCE_ID>
```

> [!NOTE]
> Get the latest instance ID from CDK outputs if the instance has been replaced:
> ```console
> aws --profile sagebrain cloudformation describe-stacks \
>   --stack-name app-dev-neptune-bastion \
>   --query "Stacks[0].Outputs[?OutputKey=='BastionInstanceId'].OutputValue" \
>   --output text
> ```

### 2. Query Neptune from the Bastion

Once connected via SSM, activate the Neptune environment (`awscurl` is pre-installed):

```console
cd
source ~/.bashrc
conda activate neptune
```

Before calling Neptune, set an environment variable for the cluster endpoint. For example, using the CloudFormation output:

```console
export NEPTUNE_ENDPOINT=$(aws cloudformation describe-stacks \
  --stack-name <YOUR_STACK_NAME> \
  --query "Stacks[0].Outputs[?OutputKey=='NeptuneClusterEndpoint'].OutputValue" \
  --output text)
```

Replace `<YOUR_STACK_NAME>` with the name of the deployed CDK stack. Alternatively, set `NEPTUNE_ENDPOINT` manually to your cluster's writer endpoint (without protocol or port).

Use `awscurl` to interact with Neptune. Requests are automatically signed using the EC2 instance's IAM role.

**Check cluster status:**

```console
awscurl --service neptune-db --region us-east-1 \
  "https://$NEPTUNE_ENDPOINT:8182/status"
```

**Insert RDF data (SPARQL UPDATE):**

```console
awscurl --service neptune-db --region us-east-1 \
  -X POST \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data "update=PREFIX+ex%3A+%3Chttp%3A%2F%2Fexample.org%2F%3E+INSERT+DATA+%7B+ex%3AAlice+a+ex%3APerson+%3B+ex%3Aname+%22Alice%22+%3B+ex%3Aknows+ex%3ABob+.+ex%3ABob+a+ex%3APerson+%3B+ex%3Aname+%22Bob%22+.+%7D" \
  "https://$NEPTUNE_ENDPOINT:8182/sparql"
```

**Query data (SPARQL SELECT):**

```console
awscurl --service neptune-db --region us-east-1 \
  -X POST \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -H "Accept: application/sparql-results+json" \
  --data "query=SELECT+%2A+WHERE+%7B+%3Fs+%3Fp+%3Fo+%7D+LIMIT+10" \
  "https://$NEPTUNE_ENDPOINT:8182/sparql"
```

**Delete all data:**

```console
awscurl --service neptune-db --region us-east-1 \
  -X POST \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data "update=CLEAR+ALL" \
  "https://$NEPTUNE_ENDPOINT:8182/sparql"
```

> [!WARNING]
> `CLEAR ALL` permanently deletes every triple in the database. To delete a specific named graph only: `CLEAR GRAPH <http://example.org/>`.

> [!NOTE]
> Neptune has IAM auth enabled. All requests must be signed — plain `curl` will return `AccessDeniedException`. Use `awscurl` or the Python `aws-requests-auth` library.

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
