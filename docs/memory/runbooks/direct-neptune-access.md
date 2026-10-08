# Direct Neptune access (temporary bastion)

Use this when you need to read from or delete in Neptune **outside the audited `/query` path**:
dropping a graph, cleaning up a failed load, or checking data the APIs can't return. It is the
only write path besides the bulk loader. Last used 2026-10-08 to drop the stale
`DefaultNamedGraph` from prod (#60).

## Why it looks like this
- Neptune has no public endpoint, and the SageMaker stack that used to provide VPC access was
  removed (#56). The query API is read-only by design.
- The SSO `Developer` role can't create IAM roles, so the bastion's role comes from a CDK stack
  (`tools/neptune_bastion/app.py`, stack `sagebrain-neptune-bastion`). It isn't part of `app.py`
  and should never stay deployed.
- No SSH or port-forwarding. Commands run on the box through SSM `send-command`, and `curl
  --aws-sigv4` signs them with the instance role. That avoids the TLS hostname and SigV4-host
  problems you get when forwarding `localhost:8182`.
- The role is limited to `ReadDataViaQuery`, `DeleteDataViaQuery`, `GetQueryStatus` and
  `CancelQuery` on the cluster. To INSERT or run the loader, add actions in `app.py` deliberately.
- These calls don't produce a `sparql_query` audit log. Neptune's own `audit` CloudWatch log
  still records them.

## Steps
```bash
B=tools/neptune_bastion/bastion.sh
aws --profile sagebrain sso login   # or sagebrain-dev
$B sagebrain up                     # ~3 min: SG + 8182 ingress on Neptune SG, IAM role, t3.micro
$B sagebrain graphs                 # inventory first (~50s on prod)
$B sagebrain count  urn:sagebrain:nf:2026-09-14
$B sagebrain query  'SELECT * WHERE { GRAPH <urn:sagebrain:nf:2026-09-14> { ?s ?p ?o } } LIMIT 5'
$B sagebrain drop   urn:sagebrain:nf:2026-09-14     # prompts for the IRI, deletes in 300k batches
$B sagebrain down                   # ALWAYS — removes the box and the Neptune SG ingress rule
```
`up` needs the repo `.venv` (aws-cdk-lib + boto3). It works in any account with exactly one
Neptune cluster, because it derives the VPC, subnet, SG and resource ID from that cluster.

## Before deleting in prod
- Run `graphs` and confirm the exact IRI and triple count with the data owner.
- Recovery means restoring automated backups (7-day retention), which creates a new cluster. Treat
  deletes as irreversible.
- Don't run the drop while a pipeline load is writing to the same graph.

## If you need this often
The next step is an in-VPC admin Lambda (`list_graphs` / `count_graph` / `drop_graph`, with a confirm
field) invoked with `aws lambda invoke`. That was scoped on 2026-10-08 and deferred as
over-engineering for a one-off. Build it once direct access is needed regularly, or if
non-admins need it.
