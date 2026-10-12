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
- **CDK Docker bundling mounts the raw source directory**, so `exclude=` doesn't filter what the
  bundling command sees. Clean up inside the command (see `src/core_layer.py`).
- **`pre-commit run --all-files` only checks tracked files.** `git add` new files first.
- **`datamodel-code-generator` can't share a venv with aws-cdk-lib**: it needs `typeguard>=4`, and
  jsii pins `typeguard==2.13.3`. Check drift with `pre-commit run openapi-models --all-files`; to
  regenerate, run `python tools/gen_models.py` in a venv with the hook's pinned versions.
- **FastAPI 0.142 wraps included routers in `app.routes`.** Walk `.original_router.routes` to list
  the real routes (see `tests/unit/app/test_routes_match_spec.py`).
- **The `swagger-ui-bundle` wheel ships Swagger UI 4.15.5, which can't render OpenAPI 3.1.** The
  image takes 5.x from the npm `swagger-ui-dist` tarball, pinned by sha256 in `app/Dockerfile`.
- **Copilot coding agent fails to start on issues** with "insufficient GitHub AI Credits". It's a
  billing problem, not a bug in the issue.
