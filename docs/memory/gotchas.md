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
- **Bedrock `AccessDeniedException` mentioning Marketplace `ViewSubscriptions`/`Subscribe`**: the
  model's Marketplace agreement hasn't been accepted in this account. Open the model once in the
  Bedrock console (per model, per account). Don't grant `aws-marketplace:*` to the worker role.
- **`cdk synth` fails on `DescribeAvailabilityZones`** with default credentials. Log in with SSO and
  pass `--profile`.
- **A stack shows `UPDATE_ROLLBACK_COMPLETE` for months after one failed update.** A later deploy
  with no changes doesn't update the status. Check `cdk diff` before assuming it's broken:
  `app-dev-monitoring` has shown it since the 2026-09-04 cost-anomaly attempt (#55, reverted in
  #57) with no differences from the code.
- **Strands prints the streamed answer to stdout by default**, glued onto the next log line, so
  JSON log events stop parsing in Insights. Pass `callback_handler=None` to `Agent`.
- **An unknown API Gateway route returns 403 `IncompleteSignatureException` when the request has a
  Bearer header**, even with `MISSING_AUTHENTICATION_TOKEN` remapped to 404. API Gateway parses the
  header as SigV4, and that error has no gateway response type (it falls to `DEFAULT_4XX`, whose
  status applies to everything). Route the request instead: mock `ANY` methods plus a root
  `{proxy+}` (`src/api_edge_stack.py`). A mock can't parse a passed-through body, so give it
  request templates and `PassthroughBehavior.NEVER` (other types get 415, not 500).
- **Setting `AWS_REGION` in a Lambda's environment fails the deploy**: it's reserved, and the runtime
  always sets it. Read it; don't set it.

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
- **An env file's top-level key replaces base.yaml's whole block.** `load_context_config` merges
  shallowly (`{**base, **env}`), so `API_EDGE: {enabled: true}` in `dev.yaml` drops every other
  `API_EDGE` key from `base.yaml`. Put shared settings under their own top-level key
  (`API_EDGE_ALARMS`), or repeat them in the env file.
- **A CDK metric with a `label` makes an alarm use the `Metrics` (math) form**, with no top-level
  `MetricName`. Label the dashboard copy (`metric.with_(label=…)`), not the alarm's metric.
- **CDK Docker bundling mounts the raw source directory**, so `exclude=` doesn't filter what the
  bundling command sees. Clean up inside the command (see `src/core_layer.py`).
- **Stack tests still run Docker bundling.** Their `@aws-cdk/core:bundlingStacks` context key isn't
  the one CDK reads (`aws:cdk:bundling-stacks`), so `pytest tests/` needs Docker and network
  (pip, and npm for the docs Lambda's Swagger UI).
- **A bundling `volumes` mount isn't part of the asset hash.** The docs Lambda mounts
  `api/openapi.yaml` from outside its asset dir, so it sets a custom hash over both
  (`src/api_edge_stack.py`); without it a contract-only change wouldn't redeploy the docs.
- **PyPI `swagger-ui-bundle` ships Swagger UI 4**, which can't render OpenAPI 3.1. The docs Lambda
  fetches `swagger-ui-dist` 5 from npm at bundle time instead.
- **`pre-commit run --all-files` only checks tracked files.** `git add` new files first.
- **Copilot coding agent fails to start on issues** with "insufficient GitHub AI Credits". It's a
  billing problem, not a bug in the issue.
