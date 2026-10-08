# Gotchas

Symptom → cause → fix. Add one when something cost you more than 15 minutes.

## AWS access
- **Laptop → Neptune times out on :8182.** The cluster is in private subnets only, and there's no
  bastion or SageMaker anymore. Use the [temporary bastion](runbooks/direct-neptune-access.md).
- **`iam:CreateRole` AccessDenied** on the SSO `Developer` role. Developers can't create IAM roles
  or instance profiles by hand, but CloudFormation can (via the CDK bootstrap exec role), so put any
  IAM you need in a stack.
- **zsh doesn't word-split `$VAR`**: `P="--profile x"; aws $P …` passes one argument and the CLI
  rejects it. Write flags out, or use `AWS_PROFILE=…`.

## Neptune
- **`DROP GRAPH` on ~2M triples → `TimeLimitExceededException` after 120s** (the default
  `neptune_query_timeout`). The update rolls back atomically, so nothing is deleted. Delete in
  `LIMIT 300000` batches instead (≈50s each on prod serverless). `bastion.sh drop` does this.
- **Never `DROP DEFAULT` / `CLEAR DEFAULT`** to remove "default graph" data. In Neptune the default
  graph is the union of every graph. Name `<http://aws.amazon.com/neptune/vocab/v01/DefaultNamedGraph>`
  explicitly.
- **Plain `curl` → `AccessDeniedException`**: IAM auth is on. Use `curl --aws-sigv4
  'aws:amz:us-east-1:neptune-db'` plus `x-amz-security-token` (curl ≥ 7.75, present on AL2023).
- **Counting a whole named-graph inventory takes ~50s** on prod (≈70M triples). Fine for a one-off,
  but it would time out a synchronous API Gateway call.

## Tooling
- **`tools/load_kg.py` (and `delete_graph.py`, added in #64) → `ModuleNotFoundError: aws_requests_auth`** in the
  `neptune` conda env: `pip install aws-requests-auth`. Those tools still need VPC access, though.
- **Copilot coding agent fails to start on issues** with "insufficient GitHub AI Credits". It's a
  billing problem, not a bug in the issue.
