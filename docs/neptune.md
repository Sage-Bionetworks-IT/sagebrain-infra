# Neptune Graph Database

Amazon Neptune cluster for the Sage Brain knowledge graph.

## Architecture

- **Neptune Cluster**: Managed graph database in private subnets, IAM auth enabled
- **Security Groups**: No broad ingress — each consumer stack adds a targeted SG-to-SG rule on port 8182
- **SageMaker Studio**: Team access for loading and querying data via JupyterLab (VpcOnly mode, routes through VPC)
- **Public API**: Read-only SPARQL endpoint via API Gateway + Lambda (see [README](../README.md))
- **Backup & Monitoring**: Automated backups and CloudWatch audit/slow-query logging

## Accessing Neptune

### From SageMaker Studio (team access)

Go to **AWS Console → SageMaker → Studio**, open your user profile, and launch a JupyterLab space.

The execution role has full Neptune permissions (read, write, delete) scoped to this cluster. Authentication is handled automatically via instance credentials — no keys needed.

**Cluster endpoints** (from CloudFormation outputs):

```bash
# Writer endpoint (read + write)
aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune \
  --query "Stacks[0].Outputs[?OutputKey=='NeptuneClusterEndpoint'].OutputValue" \
  --output text

# Reader endpoint (read only)
aws --profile sagebrain cloudformation describe-stacks \
  --stack-name app-dev-neptune \
  --query "Stacks[0].Outputs[?OutputKey=='NeptuneClusterReadEndpoint'].OutputValue" \
  --output text
```

**Verify connectivity from a notebook:**

```python
import socket
socket.create_connection(("YOUR_NEPTUNE_ENDPOINT", 8182), timeout=5)
print("connected")
```

**Query Neptune from a notebook:**

```python
import requests
from aws_requests_auth.boto_utils import BotoAWSRequestsAuth

ENDPOINT = "YOUR_NEPTUNE_CLUSTER_ENDPOINT"
auth = BotoAWSRequestsAuth(
    aws_host=f"{ENDPOINT}:8182",
    aws_region="us-east-1",
    aws_service="neptune-db",
)

def sparql(query: str) -> list[dict]:
    resp = requests.post(
        f"https://{ENDPOINT}:8182/sparql",
        data={"query": query},
        auth=auth,
        headers={"Accept": "application/sparql-results+json"},
        timeout=60,
    )
    resp.raise_for_status()
    bindings = resp.json()["results"]["bindings"]
    return [{k: v["value"] for k, v in row.items()} for row in bindings]

# Check triple counts per named graph
import pandas as pd
rows = sparql("""
    SELECT ?graph (COUNT(*) AS ?triples)
    WHERE { GRAPH ?graph { ?s ?p ?o } }
    GROUP BY ?graph
    ORDER BY DESC(?triples)
""")
pd.DataFrame(rows)
```

### From the Public API (read-only, no auth)

A read-only SPARQL endpoint is available over HTTPS. See the [README](../README.md) for the URL and usage.

## Loading Data

Data is loaded via the Neptune S3 bulk loader. Each data release is uploaded to S3 under a date-partitioned prefix, creating a historical data lake of past snapshots.

### S3 Layout (Hive-style partitioning)

```
s3://app-dev-neptune-neptunedatabucketb8719d9a-7a8slykvpqaf/
  YYYY-MM-DD/
    schema/       ← ontology / schema TTL files
    data/
      rdf/        ← data TTL files (one or more .ttl files)
```

Each date prefix is a self-contained snapshot. Old prefixes are retained in S3 — they are not loaded into Neptune but are available for reference or rollback.

> [!NOTE]
> Non-RDF files (e.g. `README.md`) in the S3 prefix are harmlessly skipped by the bulk loader.

### Uploading data to S3

From a Studio terminal or any machine with the `sagebrain` AWS profile:

```bash
aws s3 cp ./schema/ s3://app-dev-neptune-neptunedatabucketb8719d9a-7a8slykvpqaf/2026-02-20/schema/ --recursive
aws s3 cp ./data/rdf/ s3://app-dev-neptune-neptunedatabucketb8719d9a-7a8slykvpqaf/2026-02-20/data/rdf/ --recursive
```

### Running the loader

Run `tools/load_kg.py` from a JupyterLab terminal in Studio. It performs a full reset (wipes Neptune) then loads schema followed by data:

```bash
pip install requests aws-requests-auth

export NEPTUNE_ENDPOINT=neptunedbcluster-mwltugp7vgl4.cluster-cwbs4mqme6zz.us-east-1.neptune.amazonaws.com
export NEPTUNE_BUCKET=app-dev-neptune-neptunedatabucketb8719d9a-7a8slykvpqaf
export NEPTUNE_LOAD_ROLE=arn:aws:iam::620117233256:role/app-dev-neptune-NeptuneLoadRole6C006CFE-Q9xhaXQNFYGw

# Full reset + load
python tools/load_kg.py --prefix 2026-02-20 --stats

# Append only (no reset)
python tools/load_kg.py --prefix 2026-02-20 --no-reset --stats

# Dry-run (print what would happen, no changes)
python tools/load_kg.py --prefix 2026-02-20 --dry-run
```

The loader:
1. Resets Neptune via the system reset endpoint (faster than `DROP ALL`)
2. Waits for Neptune to restart (~15–30s)
3. Bulk loads `YYYY-MM-DD/schema/` first
4. Bulk loads `YYYY-MM-DD/data/rdf/`
5. Polls until each job completes and prints record counts

## Adding Team Members

User profiles are managed in CDK. Add a new username to `config/dev.yaml`:

```yaml
NEPTUNE_SAGEMAKER:
  user_profiles:
    - thomas-yu
    - new-user   # alphanumeric + hyphens only, no dots
```

Then deploy:

```bash
AWS_PROFILE=sagebrain cdk deploy app-dev-neptune-sagemaker --context env=dev
```

The user can then log into the AWS Console and launch Studio from their profile. No `iam:PassRole` permission is required on their SSO role — CloudFormation handles it.

New users automatically inherit access to the Neptune S3 data bucket and can run `tools/load_kg.py` from their Studio space.

## Configuration

Neptune settings live in `config/base.yaml` (merged with environment overrides):

| Setting | Default | Notes |
|---|---|---|
| `engine_version` | `1.3.2.1` | |
| `backup_retention_days` | `7` | |
| `iam_auth_enabled` | `true` | All requests must be SigV4-signed |
| `storage_encrypted` | `true` | |
| `deletion_protection` | `true` | Set to `false` in dev |

## Instance Classes

| Environment | Instance | Notes |
|---|---|---|
| Dev | `db.t3.medium` | Burstable, deletion protection off |
| Prod | `db.r5.large`+ | Memory-optimized for graph workloads |

## Monitoring

CloudWatch logs enabled for:
- **Audit logs**: database access and operations
- **Slow query logs**: performance bottlenecks

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `ConnectTimeout` from Studio | Space wasn't restarted after SG/network change — stop and restart the space |
| `AccessDeniedException` | Request not SigV4-signed, or IAM role missing Neptune permissions |
| `TimeoutError` on port 8182 | Security group rule missing, or VPC routing issue |
