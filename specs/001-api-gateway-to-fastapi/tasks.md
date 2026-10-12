# 001 — Tasks

## Phase 0 — contract & characterization (no infra change)
- [x] T001 Constitution: `docs/memory/constitution.md`
- [x] T002 Spec templates: `specs/_template/`
- [x] T003 `specs/001-*/spec.md`, `plan.md`, `research.md`, `parity-matrix.md` (#74)
- [x] T004 Hand-write `api/openapi.yaml` and `api/.redocly.yaml`; lint is clean (#73)
- [x] T005 `tests/contract/cases.yaml` and the loader `tests/contract/cases.py`
- [x] T006 Characterization tests for the current submit/status Lambdas, `tests/unit/test_job_api_handlers.py`
- [x] T007 Spec ↔ cases agreement test, `tests/contract/test_cases_match_spec.py`
- [x] T008 `tools/parity_probe.py` (with `--legacy` mode)
- [x] T009 `API_APP` allowlist deep merge in `src/utils.py`, with tests
- [x] T010 CI `contract` job: redocly lint and `oasdiff breaking` (#73), contract tests (this PR)
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
- [ ] T130 Docs: a "Running locally" runbook in `docs/memory/runbooks/` covering the steps above and the caveats. Local jobs share the dev queues/tables with API Gateway and show up in the dev audit logs under your principal. Unit tests stay fully offline (moto, respx). Running the **workers** locally isn't supported: Neptune is VPC-only and SigV4 signs the cluster host, so keep the workers deployed and use `cdk deploy … --hotswap`
- [ ] T131 (optional) Compose `offline` profile: LocalStack for DynamoDB and SQS via `AWS_ENDPOINT_URL`, for submit/status/auth/middleware work with no AWS credentials. No worker runs, so jobs stay `pending`

### Docs and auth cleanup
Swagger UI at `/api` renders the committed `api/openapi.yaml`, not FastAPI's generated schema
(FastAPI's `/docs` is disabled). So whatever it shows, including the docs paths and the
Authorize dialog, comes from the spec file.
- [x] T132 Remove machine-key auth (`x-api-key`). It isn't configured in any deployed authorizer, and after 1c the agent calls `run_query` in-process instead of needing a machine credential (D-4, R-11).
  - [x] Contract (#73): no `apiKeyAuth`; the machine limits are `legacy_agent_rate_rps` / `legacy_agent_burst` (D-12).
  - [x] Core (this PR): `MACHINE_PRINCIPAL` removed; `limits.LEGACY_AGENT_*`; `"machine"` is an ordinary user principal (`test_machine_is_an_ordinary_user_principal`).
  - [x] App (this PR): dropped `MACHINE_API_KEY` from `settings.py` and `app/compose.yaml`, the x-api-key path in `auth/`, and the x-api-key cases. Tests: x-api-key alone returns 401, and the Authorize dialog lists only `bearerAuth`.
  - The legacy Lambda authorizer keeps its unused key path until 5b.
- [ ] T133 Hide the docs routes from the docs. Mark `/api`, `/api/openapi.json` and `/api/openapi.yaml` with `x-internal: true` in the spec, and remove the `docs` tag. The docs router strips `x-internal` paths (and any tag left unused) from the published copy. The committed YAML keeps them, so the T113 route-drift test still passes. Update the T117a assertion to "equals the committed YAML except `servers` and `x-internal` paths". This only hides them from the *listing*: `/api` (Swagger UI), `/api/openapi.json` and `/api/openapi.yaml` stay live and public. Tests: the published JSON has no `/api*` paths and `/healthz` stays; `GET /api` still returns 200 with the Swagger UI, and the two spec URLs still return 200

## Phase 1d — DynamoDB rate limiter
Decision: shared token buckets live in DynamoDB. There is no Valkey/ElastiCache. At 50 RPS with
4–40 s queries, the 5–10 ms check doesn't matter. DynamoDB needs no VPC or security groups,
costs almost nothing on demand, and is already operated here. Later governance/ReBAC caching
(authz decisions, ListObjects results, Synapse token validation) uses the same table, plus S3
for SPARQL results. The limiter lives in this phase, not a separate spec.
- [x] T141 Spec: remove Valkey from the plan. `plan.md` (architecture diagram, `ratelimit.py` line, phase table), `research.md`, `parity-matrix.md` rows 1–3, D-6 in `spec.md`, and the "Later phases" note below. There is no separate rate-limiting spec 002
- [x] T142 Test (moto, frozen clock): `DynamoLimiter.acquire(key, rate, burst)` matches `LocalLimiter`. The first `burst` calls pass, the next is rejected with `retry_after`, refill happens at `rate`/s, and buckets are independent. The existing `tests/unit/core/test_ratelimit.py` cases run against both limiters → implement it in `core/sagebrain_core/ratelimit.py`. One conditional `UpdateItem` per call (refill from `last_refill`, then decrement if `tokens >= 1`), with `now` taken from the caller. **As built:** a refill can't be expressed in one UpdateItem (no multiply or `min`), so each bucket is a GCRA `tat`: one conditional UpdateItem per attempt (consume or refill), one per call in steady state, never a read (the `DynamoLimiter` docstring)
- [x] T143 Test: on DynamoDB errors or throttling, fall back to a per-process `LocalLimiter` at rate ÷ instances and log `ratelimit_degraded` (parity row 3) → implement it
- [x] T144 Bucket items carry `expires_at` so TTL removes idle per-principal buckets. Reads never trust TTL, because deletion is lazy
- [x] T145 Settings: `RATE_LIMIT_TABLE_NAME` (unset means `LocalLimiter`, for tests and the offline profile). `default_limiter()` picks the backend
- [x] T146 Compose and tests: remove the `valkey` service and `VALKEY_URL` from `app/compose.yaml`; `tests/unit/app/test_dockerfile.py::test_compose_runs_api_with_valkey` → `test_compose_runs_api_only`
- [x] T147 CDK: on-demand `rate-limits` table (PK `key`, TTL `expires_at`). Grant `UpdateItem` to the app task role (Phase 2) and the agent worker role (1c). Point-in-time recovery off, because the data is ephemeral. Stack tests. Done: `src/rate_limit_stack.py` (`app-{env}-rate-limits`) and the agent worker grant/env. **The app task role grant is a Phase 2 TODO** (the `DynamoLimiter` docstring)
- [x] T148 Live check: `tools/throttle_test.py` against the local app, pointed at the dev rate-limit table. The 101st request in a burst gets 429, and per-user 5/20 holds. **Ran 2026-10-04** against a two-instance ad-hoc compose (T128's `local-aws` profile doesn't exist yet), `RATE_LIMIT_TABLE_NAME=app-dev-rate-limits`. The load generator ran inside the compose network: from the host, Docker Desktop's port forwarding capped arrivals at about 41 rps. The tool gained `--token`/`--token-from-env`, `--mode status` (404 = admitted, no jobs), repeated `--url`, `--burst` and `--rps/--duration`. Results (expected in brackets):
  - per-user, 1 instance, 100 req at 10 rps: 65 admitted [20 + 5 × 9 = 65]
  - per-user, 2 instances, burst of 40: 22 admitted [20 + refill; two buckets would give ~40]
  - global, 2 instances, burst of 300 in 2.15 s: 140 admitted [100 + refill; two buckets ≥ 200]
  - global, 2 instances, 150 rps for 6 s: 366 of 900 admitted [100 + 50 × 5 = 350]
  - outage (nonexistent table): no 500s; one `ratelimit_degraded` per instance (`fallback_rate` 25, `fallback_burst` 50)
  - 429s carry `Retry-After: 1` (2 when deep in deficit)

## Later phases
The shared rate limiter is Phase 1d above. Cutover and decommission
(Phases 4 and 5) get their own spec when Phase 3 lands. Infra (Phases 2 and 3) is
tracked in this file once Phase 1 lands. Phase 2 runs 2 fixed tasks (R-12). Carried forward from 1d:
- Phase 2: the app task role gets one `dynamodb:UpdateItem` statement on the rate-limit table
  ARN, plus `RATE_LIMIT_TABLE_NAME` and `RATE_LIMIT_FALLBACK_INSTANCES` (= 2, the fixed task count),
  with a least-privilege stack test (the `DynamoLimiter` docstring).
- Phase 2, from T148: an outage still pays a DynamoDB round trip (up to the 2 s connect / 5 s
  read timeouts) on every request before falling back. Consider a short circuit-breaker (skip
  DynamoDB for a few seconds after a failure). Also raise botocore's `max_pool_connections`
  (default 10) to the threadpool size (40) for the limiter's client.
- Phase 3: alarm on `ratelimit_degraded` log lines (a metric filter on the agent and app log
  groups), and add the rate-limit table's throttles/errors to the dashboard.
