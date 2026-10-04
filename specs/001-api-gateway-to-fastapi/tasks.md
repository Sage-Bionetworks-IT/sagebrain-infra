# 001 — Tasks

## Phase 0 — contract & characterization (no infra change)
- [x] T001 Constitution: `.specify/memory/constitution.md`
- [x] T002 Spec templates: `specs/_template/`
- [x] T003 `specs/001-*/spec.md`, `plan.md`, `research.md`, `data-model.md`, `parity-matrix.md`
- [x] T004 Hand-write `api/openapi.yaml` and `api/.redocly.yaml`; lint is clean
- [x] T005 `tests/contract/cases.yaml` and the loader `tests/contract/cases.py`
- [x] T006 Characterization tests for the current submit/status Lambdas, `tests/unit/test_job_api_handlers.py`
- [x] T007 Spec ↔ cases agreement test, `tests/contract/test_cases_match_spec.py`
- [x] T008 `tools/parity_probe.py` (with `--legacy` mode)
- [x] T009 `API_APP` allowlist deep merge in `src/utils.py`, with tests
- [x] T010 CI `contract` job (redocly lint and contract tests)
- [x] T011 Run the parity probe against dev API Gateway (`--profile sagebrain-dev`) and record the results in `research.md`

## Phase 1a — shared server-side query service (`core/sagebrain_core`)
- [x] T101 Test: `sagebrain_core.limits` equals the spec's `x-sagebrain-limits` → add `limits.py`
- [x] T102 Test: `validate_query` passes every `query_submit_*` vector → implement it
- [x] T103 Test: `run_query` sends a SigV4 POST to the reader with a 60 s timeout and emits `sparql_query` with `principal` and `source` → move the logic out of `src/lambda/query.py`
- [x] T104 Test: `run_query` charges the global + per-principal `query` buckets and rejects when they're empty → `ratelimit.py` with LocalLimiter (DynamoDB in Phase 1d)
- [x] T105 Refactor the query worker onto `execute_query`. It doesn't use `run_query`, because the job was already validated and admitted at submit time, and each limit is charged exactly once. All 12 existing `tests/unit/test_query_handler.py` tests pass with **unchanged assertions**; only their `@patch` targets moved to `sagebrain_core.neptune`.
- [x] T106 Bundle `sagebrain_core` into the Lambda assets (shared layer); CDK test checks that the layer is attached

## Phase 1b — FastAPI app (local)
- [x] T111 `app/` skeleton, pinned requirements (`app/requirements.txt`), pydantic-settings; depends on `sagebrain_core`
- [x] T112 Codegen `models/_generated.py` from the spec (`tools/gen_models.py`); CI and pre-commit (`openapi-models` hook) fail on drift
- [x] T113 Test: drift between `app.routes` and the spec → implement the routers
- [x] T114 Test: every `cases.yaml` vector (`expect`, not `legacy`) → implement the handlers and `jobs.py` (`/ask` enqueues `user_id`, D-8)
- [x] T115 Test: authorizer cases ported from `tests/unit/test_authorizer.py` → implement `auth/`
- [x] T116 Tests: CORS, body limit (413), timeout (504), 500 handler, access log fields → implement the middleware
- [x] T117 Schemathesis conformance
- [x] T117a Test: `/api` returns Swagger UI HTML with no external asset hosts; `/api/openapi.json` equals the committed YAML with `servers` set to `settings.public_base_url`; `/api/openapi.yaml` is served; `/docs`, `/redoc` and `/openapi.json` return 404 → implement the docs router (FR-9)
- [x] T118 Dockerfile (ARM64, non-root), docker compose, CI image build. Re-verified 2026-10-04 after the Valkey removal: the image builds, compose runs the API only, `/healthz` and `/api` serve, and a `/query` job completes through the dev worker

## Phase 1c — agent calls the service in-process
- [x] T121 Test: the `query_neptune` tool calls `run_query(source="agent", principal=user_id)`, makes no HTTP calls, and records the same steps → rewrite the tool
- [x] T122 Test: an over-length or rate-limited agent query becomes a `tool_result` error step and the job continues → implement it
- [x] T123 Test: the worker accepts both the legacy `{authorization}` and new `{user_id}` messages → implement it
- [x] T124 CDK: agent role gets read-only `neptune-db`, 8182 ingress into the Neptune SG; remove `NEPTUNE_QUERY_URL`; stack tests

### Local run against dev AWS
The app never calls Neptune or Bedrock (only the workers do), and DynamoDB, SQS and Synapse
are all public endpoints. So the app runs on a laptop with no VPN or tunnel. Jobs go onto the
**dev** queues, the deployed dev workers run them, and local `GET` status reads the result.
These tasks build on the 1b pydantic-settings (T111) and docker compose (T118). Don't change those tasks.
- [ ] T125 Test: every AWS resource name, the rate-limit table name, the Synapse team ID and `public_base_url` come only from settings/env, with no hardcoded names; an optional `AWS_ENDPOINT_URL` is passed through to boto3 → audit and fix the settings
- [ ] T126 CDK: `app-dev-neptune-api` and `app-dev-neptune-agent` export the job table names and queue URLs as stack outputs; stack tests. Dev already has `QueryJobTableName` / `AgentJobTableName` and auto-generated exports with the queue *names*, so add clean `QueryJobQueueUrl` / `AgentJobQueueUrl` outputs
- [ ] T127 `tools/local_env.sh [--profile sagebrain-dev]`: reads those outputs and writes `.env.local`, then writes short-lived credentials to `.env.aws` with `aws configure export-credentials --format env-no-export`. Add both files to `.gitignore`
- [ ] T128 Compose `local-aws` profile: the app only (the rate limiter is DynamoDB after 1d), `env_file: [.env.local, .env.aws]`. Real Synapse auth, so test with `$SYNAPSE_AUTH_TOKEN`. There is **no** auth-bypass flag. If one is ever added, startup must fail unless `ENV=local`
- [ ] T129 Smoke test: `tools/parity_probe.py` against `http://localhost:<port>` (`--profile sagebrain-dev`). `POST /query` reaches `complete` through the dev worker, and `POST /ask` reaches `complete` through the dev agent worker (needs 1c's `{user_id}` message shape)
- [ ] T130 Docs: a "Running locally" section in `plan.md` covering the steps above and the caveats. Local jobs share the dev queues/tables with API Gateway and show up in the dev audit logs under your principal. Unit tests stay fully offline (moto, respx). Running the **workers** locally isn't supported: Neptune is VPC-only and SigV4 signs the cluster host, so keep the workers deployed and use `cdk deploy … --hotswap`
- [ ] T131 (optional) Compose `offline` profile: LocalStack for DynamoDB and SQS via `AWS_ENDPOINT_URL`, for submit/status/auth/middleware work with no AWS credentials. No worker runs, so jobs stay `pending`

### Docs and auth cleanup
Swagger UI at `/api` renders the committed `api/openapi.yaml`, not FastAPI's generated schema
(FastAPI's `/docs` is disabled). So whatever it shows, including the docs paths and the
Authorize dialog, comes from the spec file.
- [ ] T132 Remove machine-key auth (`apiKeyAuth` / `x-api-key`). It isn't configured in any deployed authorizer (dev and prod authorizers have only `SYNAPSE_TEAM_ID`), and after 1c the agent calls `run_query` in-process instead of needing a machine credential. Spec first: drop `apiKeyAuth` from `components.securitySchemes`, the top-level `security`, the `/api` description, and the x-api-key cases in `cases.yaml`. Then: drop `MACHINE_API_KEY` from `settings.py` and `app/compose.yaml`, the x-api-key path in `auth/`, `MACHINE_PRINCIPAL` and the machine bucket in `sagebrain_core.ratelimit`/`limits` (plus `x-sagebrain-limits`), and D-4 in `spec.md`. The legacy Lambda authorizer keeps its unused key path until 6b. Tests: x-api-key alone returns 401, and the Authorize dialog lists only `bearerAuth`. **Conflict from 1d (D-12):** `legacy-apigw` agent jobs use the machine bucket (`MACHINE_RATE_RPS`/`MACHINE_BURST`). Until the `/ask` cutover retires `{authorization}` messages, remove only `MACHINE_PRINCIPAL` and the x-api-key path; keep the two limits (renamed to `LEGACY_AGENT_*`, spec first) and `test_legacy_agent_principal_uses_machine_limits`
- [ ] T133 Hide the docs routes from the docs. Mark `/api`, `/api/openapi.json` and `/api/openapi.yaml` with `x-internal: true` in the spec, and remove the `docs` tag. The docs router strips `x-internal` paths (and any tag left unused) from the published copy. The committed YAML keeps them, so the T113 route-drift test still passes. Update the T117a assertion to "equals the committed YAML except `servers` and `x-internal` paths". This only hides them from the *listing*: `/api` (Swagger UI), `/api/openapi.json` and `/api/openapi.yaml` stay live and public. Tests: the published JSON has no `/api*` paths and `/healthz` stays; `GET /api` still returns 200 with the Swagger UI, and the two spec URLs still return 200

## Phase 1d — DynamoDB rate limiter
Decision: shared token buckets live in DynamoDB. There is no Valkey/ElastiCache. At 50 RPS with
4–40 s queries, the 5–10 ms check doesn't matter. DynamoDB needs no VPC or security groups,
costs almost nothing on demand, and is already operated here. Later governance/ReBAC caching
(authz decisions, ListObjects results, Synapse token validation) uses the same table, plus S3
for SPARQL results. The limiter lives in this phase, not a separate spec.
- [x] T141 Spec: remove Valkey from the plan. `plan.md` (architecture diagram, `ratelimit.py` line, phase table), `research.md`, `parity-matrix.md` rows 1–3, D-6 in `spec.md`, and the "Later phases" note below. There is no separate rate-limiting spec 002
- [x] T142 Test (moto, frozen clock): `DynamoLimiter.acquire(key, rate, burst)` matches `LocalLimiter`. The first `burst` calls pass, the next is rejected with `retry_after`, refill happens at `rate`/s, and buckets are independent. The existing `tests/unit/core/test_ratelimit.py` cases run against both limiters → implement it in `core/sagebrain_core/ratelimit.py`. One conditional `UpdateItem` per call (refill from `last_refill`, then decrement if `tokens >= 1`), with `now` taken from the caller. **As built:** a refill can't be expressed in one UpdateItem (no multiply or `min`), so each bucket is a GCRA `tat`: one conditional UpdateItem per attempt (consume or refill), one per call in steady state, never a read (plan.md, Phase 1d)
- [x] T143 Test: on DynamoDB errors or throttling, fall back to a per-process `LocalLimiter` at rate ÷ instances and log `ratelimit_degraded` (parity row 3) → implement it
- [x] T144 Bucket items carry `expires_at` so TTL removes idle per-principal buckets. Reads never trust TTL, because deletion is lazy
- [x] T145 Settings: `RATE_LIMIT_TABLE_NAME` (unset means `LocalLimiter`, for tests and the offline profile). `default_limiter()` picks the backend
- [x] T146 Compose and tests: remove the `valkey` service and `VALKEY_URL` from `app/compose.yaml`; `tests/unit/app/test_dockerfile.py::test_compose_runs_api_with_valkey` → `test_compose_runs_api_only`
- [x] T147 CDK: on-demand `rate-limits` table (PK `key`, TTL `expires_at`). Grant `UpdateItem` to the app task role (Phase 3) and the agent worker role (1c). Point-in-time recovery off, because the data is ephemeral. Stack tests. Done: `src/rate_limit_stack.py` (`app-{env}-rate-limits`) and the agent worker grant/env. **The app task role grant is a Phase 3 TODO** (plan.md, Phase 1d)
- [ ] T148 Live check: `tools/throttle_test.py` against the local app (`local-aws` profile), pointed at a dev rate-limit table. The 101st request in a burst gets 429, and per-user 5/20 holds. **Blocked:** needs T125–T128 (settings audit, stack outputs, `tools/local_env.sh`, the `local-aws` profile) and `app-dev-rate-limits` deployed. It also needs `throttle_test.py` to send `Authorization: Bearer $SYNAPSE_AUTH_TOKEN` (add a `--token` option); today it sends none, so against the app it would only see 401/429

## Later phases
The shared rate limiter is Phase 1d above (there is no Phase 2). Cutover and decommission
(Phases 5 and 6) are tracked in `003-cutover-and-decommission`. Infra (Phases 3 and 4) is
tracked in this file once Phase 1 lands. Carried forward from 1d:
- Phase 3: the app task role gets one `dynamodb:UpdateItem` statement on the rate-limit table
  ARN, plus `RATE_LIMIT_TABLE_NAME` and `RATE_LIMIT_FALLBACK_INSTANCES` (= max task count),
  with a least-privilege stack test (plan.md, Phase 1d).
- Phase 4: alarm on `ratelimit_degraded` log lines (a metric filter on the agent and app log
  groups), and add the rate-limit table's throttles/errors to the dashboard.

---

## Handoff: current state (updated 2026-10-04, end of Phase 1d code)
**Branch:** `spec-driven-fastapi-phase0`. 0, 1a and 1b–1c are committed (`7810dc9`, `21d4a63`).
**Phase 1d (T141–T147) is in the working tree, uncommitted.** Nothing is deployed or pushed.
Phases 0, 1a, 1c and 1d-code are complete. 1b is complete (T118 re-verified with a
real build + compose run). T148 is blocked (see its entry).
**Next: the local-run tasks (T125–T131)**, then deploy 1c + 1d to dev (order below), then
T148, then the docs/auth cleanup (T132–T133, with the D-12 conflict noted on T132).

Valkey re-checks from the removal: T104 (the `LocalLimiter` cases now run against both
limiters), T141 (no Valkey/Redis wording left anywhere in the repo) and T146 (compose runs
the API only; `test_compose_runs_api_only` passes) are verified and ticked. T118 is re-verified and ticked.

Read these first, in this order:
1. `.specify/memory/constitution.md`: the rules. Contract first, limits server-side,
   every threshold tested.
2. `specs/001-api-gateway-to-fastapi/spec.md`: requirements FR-1..9, deviations D-1..12.
   `plan.md` "Phase 1d" has the rate-limiter design, stack choice and deploy order.
3. `parity-matrix.md` and `research.md`: where each limit is enforced, and the live
   API Gateway behaviour observed in dev.
4. `api/openapi.yaml`: the contract. `tests/contract/cases.yaml`: the vectors.
5. `core/sagebrain_core/`: the shared service. `query_service.run_query` is what the agent tool calls.
6. `app/sagebrain_api/`: the FastAPI service (map below).

Verify the starting state before writing anything:
```bash
python -m pytest tests/ -q                       # expect all green (579 at handoff; needs moto, see requirements-dev.txt)
npx --yes @redocly/cli@2 lint api/openapi.yaml --config api/.redocly.yaml
pre-commit run --all-files                       # incl. the openapi-models drift hook
docker build --platform linux/arm64 -f app/Dockerfile -t sagebrain-api .   # optional
cdk synth --context env=dev --profile sagebrain-dev   # needs SSO; default creds can't DescribeAvailabilityZones
```

`app/sagebrain_api` map:
- `main.create_app(settings, *, query_jobs, ask_jobs, http_client, limiter)`: tests inject fakes.
  Middleware order (outermost first): AllowOriginEverywhere → CORS → AccessLog →
  UnhandledError → BodyLimit → RequestTimeout.
- `guard.ApiGuard(api)`: the route dependency. It charges `admit_global`, then authenticates,
  then charges `admit_principal`. Health and docs routes have no guard.
- `auth/`: Synapse/machine-key port (`Authenticator`, `TokenCache`).
- `jobs.JobStore`: the DynamoDB item + SQS message (data-model.md shapes).
- `routers/`: `query`, `ask`, `health` and `docs`. Bodies are built through the generated models.
- `settings.Settings`: env names `SYNAPSE_TEAM_ID`, `MACHINE_API_KEY`, `{QUERY,ASK}_JOB_TABLE_NAME`,
  `{QUERY,ASK}_JOB_QUEUE_URL`, `PUBLIC_BASE_URL`, `AUTH_TRANSIENT_STATUS`, `SWAGGER_UI_DIR`,
  `RATE_LIMIT_TABLE_NAME` (unset: `LocalLimiter`), `RATE_LIMIT_FALLBACK_INSTANCES` (default 2).
  `create_app` builds the limiter with `sagebrain_core.ratelimit.make_limiter` unless one is injected.

Decisions already made. Do not re-litigate them:
- FastAPI on ECS Fargate behind ALB + WAF. The SQS + Lambda workers stay.
- The agent calls `sagebrain_core.run_query` in-process (Phase 1c), never over HTTP to `/query`.
- `/ask` enqueues `{job_id, question, user_id, source_ip}` with no token (D-8). This is done
  on the app side; the worker must accept it in T123.
- Docs at `/api` (Swagger UI), `/api/openapi.json` and `/api/openapi.yaml`, public. FastAPI's
  `/docs`, `/redoc` and `/openapi.json` are disabled.
- Rate limiting is global then per-principal. `sagebrain_core.ratelimit.admit` = `admit_global` +
  `admit_principal`; HTTP calls them separately, around auth. Status polls charge the same
  buckets as submits (as the API Gateway stage throttle did).

Made in Phase 1b:
- **Codegen runs in its own env.** `datamodel-code-generator` → `inflect` needs `typeguard>=4`,
  while aws-cdk-lib/jsii pin `typeguard==2.13.3`. It is not in `requirements-dev.txt`. Regenerate
  with `pre-commit run openapi-models --all-files` (check mode); to write the file, run
  `python tools/gen_models.py` in a venv that has the hook's pinned versions.
- **Swagger UI 5.33.1** comes from the npm `swagger-ui-dist` tarball, pinned by sha256 in
  `app/Dockerfile`. The `swagger-ui-bundle` wheel ships 4.15.5, which can't render OpenAPI 3.1.
  Tests use stub assets.
- **Spec change:** both `/api/openapi.{json,yaml}` responses use a new `OpenApiDocument` schema.
  The YAML response used to be `type: string`, which Schemathesis flagged, because a YAML
  document is an object.
- The app tests use hand-written fakes (FakeTable/FakeSqs, `httpx.MockTransport` for Synapse),
  not moto/respx. Accepted by the user on 2026-10-04 ("ok for now").
- **D-11:** `X-Source` over 64 characters returns 400 (`sagebrain_core.validate_source`,
  `limits.SOURCE_MAX_CHARS`, spec `source_max_chars`). Decided by the user on 2026-10-04.
- AUTH_TRANSIENT_STATUS=500 returns `{"message":"Internal server error"}`. Any other status
  returns its HTTP reason phrase.
- `caller` in the access log is the auth method (`bearer`/`apiKey`); `user` is the principal.
- Schemathesis excludes `positive_data_acceptance` (`x-strip-before-validate` makes `"   "`
  schema-valid but a correct 400) and `ignored_auth` (bearer OR apiKey: it corrupts one scheme
  while the other is still valid).

Made in Phase 1c:
- **Agent worker (`src/lambda_agent/agent.py`).** No module-level caller state. Each SQS message
  becomes a `_Job(job_id, principal, source_ip, steps)`, and `_make_tools(job)` builds the
  `query_neptune` / `get_schema` tools as closures over it. So a warm instance can't mix callers,
  even through a tool object kept from an earlier job (`test_no_state_leaks_between_jobs_on_a_warm_instance`).
  `requests`, `NEPTUNE_QUERY_URL` and the 3 s / 70 s poll loop are gone.
- **Import path:** `from sagebrain_core.query_service import run_query`. The package `__init__`
  has no re-exports, and every other caller imports submodules too.
- **Error mapping in the tool:** `QueryRejected` → step `error` = the contract message;
  `RateLimited` → `"Rate limit exceeded; retry after N.Ns"`. Both **return** `"Error: <msg>"` to
  the model, so the job continues without depending on Strands' exception handling. A Neptune or
  network failure still records an `error` step and **raises** `RuntimeError("SPARQL query failed: …")`,
  as before. `_safe_sparql` still raises `ValueError` before any step is recorded (unchanged).
- **Messages:** `user_id` wins if both shapes are present. Legacy messages get `principal="legacy-apigw"`
  and `source_ip="unknown"`. The `authorization` value is never read past the shape check. A message
  with neither marks the job `error` and does **not** raise (retrying can't fix it).
  `agent_invocation` now also carries `principal`.
- **Tests run without Strands installed:** `tests/unit/test_agent_worker.py` stubs `strands` in
  `sys.modules` and uses a scripted fake `Agent` that calls the tools. The parity tests (rows 1, 2,
  12a, 16) use the real `run_query`/`execute_query` with only `requests.post`/SigV4 patched.
- **CDK:** the agent stack takes `neptune_read_endpoint`, `neptune_cluster_resource_id` and
  `neptune_security_group` (the same as the API stack), and no longer takes the query URLs. New
  resources: `SageBrainCoreLayer…` and `AgentToNeptuneIngress`. Every pre-existing logical ID is
  pinned by `test_logical_ids_unchanged`. **The agent Lambda did not have the layer before
  (the old handoff note was wrong).** It does now.
- **`AWS_REGION` is not set in the function env.** It's a reserved Lambda variable that the
  runtime always sets (CloudFormation rejects functions that set it), so `NeptuneConfig.from_env`
  reads the runtime's value. A stack test asserts it isn't set explicitly.

Made in Phase 1d:
- **No separate spec 002.** The limiter is spec 001 Phase 1d, as this file already said. Its
  decisions are in `plan.md` (Phase 1d), `spec.md` (FR-7, D-12) and `data-model.md` (the
  table item and the `ratelimit_degraded` event).
- **GCRA, not `tokens` + `last_refill`.** DynamoDB can't multiply or `min` in an update, so a
  bucket is one `tat` number. It's one conditional UpdateItem per attempt: consume, or refill
  if missing or full. `ALL_OLD` on failure gives a rejection's `retry_after` without a read.
  A per-process hint makes the steady state one call. `DynamoLimiter`'s clock is `time.time`
  (it must be shared across hosts); `LocalLimiter` keeps `time.monotonic`.
- **Outage:** any `ClientError` except `ConditionalCheckFailed`, and any `BotoCoreError`, fails
  open to a `LocalLimiter` at `rate / instances`, `ceil(burst / instances)`. `ratelimit_degraded`
  is logged at most once per 60 s per limiter, with a `suppressed` count. The botocore client is
  module-level and lazy, with connect 2 s / read 5 s and **no retries** (`total_max_attempts: 1`,
  standard mode).
- **The app guard now runs `admit_global` / `admit_principal` in the threadpool**
  (`test_limiter_runs_off_the_event_loop`), so a DynamoDB call can't block the event loop.
- **D-12 (decides the 1c open question):** `legacy-apigw` gets the machine bucket (25/50),
  shared across all instances. `sagebrain_core.ratelimit.LEGACY_AGENT_PRINCIPAL`; `agent.py`'s
  `LEGACY_PRINCIPAL` is that constant. This conflicts with T132 (noted there): keep the
  machine limits until the `/ask` cutover.
- **Not limits:** `RATE_LIMIT_FALLBACK_INSTANCES` (a deployment fact; the agent sets it to its
  reserved concurrency, 10) and `BUCKET_TTL_SECONDS` (3600, unobservable). The spec's
  `x-sagebrain-limits` is unchanged.
- **CDK:** a new stack `app-{env}-rate-limits` (`src/rate_limit_stack.py`, one table, depends on
  nothing). `NeptuneAgentStack` takes a required `rate_limit_table`. The worker gets one
  `dynamodb:UpdateItem` statement on the table ARN (in its existing DefaultPolicy), plus
  `RATE_LIMIT_TABLE_NAME` and `RATE_LIMIT_FALLBACK_INSTANCES`. `WORKER_RESERVED_CONCURRENCY = 10`
  is now a module constant (unchanged value). `test_logical_ids_unchanged`'s `added` set didn't
  change.
- **Tests:** `moto[dynamodb]==5.2.3` is in `requirements-dev.txt`. It doesn't pull typeguard, and
  `pip check` is clean with the CDK pin. The `rate_limit_table` fixture in `tests/conftest.py`
  sets fake credentials, resets the module client and yields a moto client. The bucket cases in
  `tests/unit/core/test_ratelimit.py` are parametrized `local`/`dynamo`. Outage, TTL and backend
  selection are in `tests/unit/core/test_ratelimit_dynamo.py`.

**Deploy order (1d), whenever someone deploys:** `app-dev-rate-limits` first (new, no deps; it
exports the table name and ARN), then `app-dev-neptune-agent` with a **full** deploy (IAM + env).
`cdk deploy app-dev-neptune-agent --profile sagebrain-dev` deploys the new stack first by itself,
unless `--exclusively` is given. The 1d change can ride in the same agent deploy as 1c below. After
deploying, an `/ask` should leave `{rl:query}:global` and `{rl:query}:u:<user>` (or
`…:u:legacy-apigw`) items in the table, and **no** `ratelimit_degraded` lines in the agent log group.

**Deploy order (when someone deploys 1c):** the deployed `app-dev-neptune-agent` still imports
two auto-generated exports from `app-dev-neptune-api` (`ExportsOutputRefNeptuneApi63B1DFC8…` and
`…DeploymentStageprod…`, its old `NEPTUNE_QUERY_URL`). `app.py` keeps them via `export_value`
(TODO comment). Deploy `app-dev-neptune-agent` first (`cdk deploy app-dev-neptune-agent`), then
delete those two `export_value` lines and deploy `app-dev-neptune-api`. Synth output for the API
stack is unchanged until then. The agent change is IAM/VPC/env, so it needs a **full** deploy, not
`--hotswap`. Smoke test afterwards: an `/ask` should log `sparql_query` with `source=agent` in the
**agent** log group, not the query worker's. Also, any `/ask` job still queued from the old submit
Lambda then runs as `legacy-apigw`.

Open questions:
- Starlette 1.7 warns that `TestClient` over `httpx` is deprecated in favour of `httpx2`.
  It's harmless for now.
- ~~1d: the `legacy-apigw` bucket~~: decided as D-12 (machine limits). **T132** removes
  `MACHINE_PRINCIPAL`, but must keep the machine limit values for `legacy-apigw` until the `/ask`
  cutover (noted on T132).
- In an outage the fallback over-admits the shared `query` global bucket (app tasks × 25 +
  10 agent workers × 5 = 100 rps against 50). This is accepted in plan.md. Revisit if Phase 3
  sizes the service differently.
- The DynamoDB limiter hasn't run against real DynamoDB yet (moto only). T148 is the first
  live check. moto honours `ReturnValuesOnConditionCheckFailure`, but confirm a real rejection
  returns `Item` too (the `retry_after` path depends on it; without it every rejection costs a
  second attempt and reports `retry_after = 1/rate`).
- The 1c agent path hasn't been run against real Strands/Bedrock yet (no local Strands, nothing
  deployed). The first end-to-end check is T129's `/ask` smoke test, which needs the 1c worker
  deployed to dev.

Next-task notes:
- T125–T131 (local run against dev AWS) are unblocked: the dev agent worker understands
  `{user_id}` messages **once 1c is deployed to dev**. Before that, local `/ask` jobs fail in the
  old worker (it reads `authorization`, gets `""`, and `/query` returns 401). So deploy 1c to dev
  (deploy order above) before T129's `/ask` leg.
- T126 adds `AgentJobQueueUrl` to the agent stack: keep `test_logical_ids_unchanged` green (outputs
  aren't resources, so it shouldn't change).
- T125 already has its rate-limit half: `RATE_LIMIT_TABLE_NAME` is a setting, and botocore
  honours `AWS_ENDPOINT_URL` for the limiter's client (`test_client_honours_aws_endpoint_url`).
  T126/T127 should also export and write `RateLimitTableName` (from `app-dev-rate-limits`) to
  `.env.local`.
- T148 (blocked): after T125–T128 and the 1d dev deploy, run the local app with
  `RATE_LIMIT_TABLE_NAME=app-dev-rate-limits`. Add `--token` to `tools/throttle_test.py` (it
  sends no `Authorization` today), then check that the 101st burst request is 429. Also check
  per-user 5/20: the global bucket is charged first, so one token's burst hits the user limit
  at 21.

Gotchas:
- AWS profiles: `sagebrain-dev` for dev, `sagebrain` for prod (only `app-prod-*` stacks).
  `$SYNAPSE_AUTH_TOKEN` holds a team-member PAT for live probes.
- `pre-commit run --all-files` only checks **tracked** files, so `git add` new files first, or
  run black and flake8 on them directly.
- FastAPI 0.142 keeps included routers as wrappers in `app.routes`. Walk `.original_router.routes`
  (see `tests/unit/app/test_routes_match_spec.py`).
- CDK Docker bundling mounts the **raw** source directory, so `exclude=` doesn't filter what the
  bundling command copies. Clean up inside the command instead (see `src/core_layer.py`).
- Pre-commit's black reformats files, so re-read a file before making exact-match edits to it.
- `config/*.yaml` merges shallowly at the top level. Only `API_APP` deep-merges (`src/utils.py`).
