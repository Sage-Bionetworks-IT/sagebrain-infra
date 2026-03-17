
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
aws --profile sagebrain ssm start-session --target i-052ceee0a9de50b47
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

Install `awscurl` on the bastion:

```console
pip3 install awscurl
```

Use `awscurl` to interact with Neptune. Requests are automatically signed using the EC2 instance's IAM role.

**Check cluster status:**

```console
awscurl --service neptune-db --region us-east-1 \
  "https://neptunedbcluster-mwltugp7vgl4.cluster-cwbs4mqme6zz.us-east-1.neptune.amazonaws.com:8182/status"
```

**Insert RDF data (SPARQL UPDATE):**

```console
awscurl --service neptune-db --region us-east-1 \
  -X POST \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode 'update=
    PREFIX ex: <http://example.org/>
    INSERT DATA {
      ex:Alice a ex:Person ;
               ex:name "Alice" ;
               ex:knows ex:Bob .
      ex:Bob   a ex:Person ;
               ex:name "Bob" .
    }
  ' \
  "https://neptunedbcluster-mwltugp7vgl4.cluster-cwbs4mqme6zz.us-east-1.neptune.amazonaws.com:8182/sparql"
```

**Query data (SPARQL SELECT):**

```console
awscurl --service neptune-db --region us-east-1 \
  -X POST \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -H "Accept: application/sparql-results+json" \
  --data-urlencode "query=SELECT * WHERE { ?s ?p ?o } LIMIT 10" \
  "https://neptunedbcluster-mwltugp7vgl4.cluster-cwbs4mqme6zz.us-east-1.neptune.amazonaws.com:8182/sparql"
```

> [!NOTE]
> Neptune has IAM auth enabled. All requests must be signed — plain `curl` will return `AccessDeniedException`. Use `awscurl` or the Python `aws-requests-auth` library.

## Secrets

Secrets can be manually created in the
[AWS Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/create_secret.html).
When naming your secret make sure that the secret does not end in a pattern that matches
`-??????`, this will cause issues with how AWS CDK looks up secrets.

To pass secrets to a container set the secrets manager `container_secrets`
when creating a `ServiceProp` object. You'll be creating a list of `ServiceSecret` objects:
```python
from src.service_props import ServiceProps, ServiceSecret

app_service_props = ServiceProps(
    ecs_task_cpu=256,
    ecs_task_memory=512,
    container_name="app",
    container_port=443,
    container_location="ghcr.io/sage-bionetworks/app:v1.0",
    container_secrets=[
        ServiceSecret(
            secret_name="app/dev/DATABASE",
            environment_key="NAME_OF_ENVIRONMENT_VARIABLE_SET_FOR_CONTAINER",
        ),
        ServiceSecret(
            secret_name="app/dev/PASSWORD",
            environment_key="SINGLE_VALUE_SECRET",
        )
    ]
)
```

For example, the KVs for `app/dev/DATABASE` could be:
```json
{
    "DATABASE_USER": "maria",
    "DATABASE_PASSWORD": "password"
}
```

And the value for `app/dev/PASSWORD` could be: `password`

In the application (Python) code the secrets may be loaded using:

```python
import json
import os

all_secrets_dict = json.loads(os.environ["NAME_OF_ENVIRONMENT_VARIABLE_SET_FOR_CONTAINER"])
my_secret = os.environ.get("SINGLE_VALUE_SECRET", None)
```

> [!NOTE]
> Retrieving secrets requires access to the AWS Secrets Manager
