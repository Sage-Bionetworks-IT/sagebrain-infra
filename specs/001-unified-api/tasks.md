# 001 — Tasks

Ordered, test-first. Phases are the ones in [plan.md](plan.md); each is independently mergeable.

## Phase 0 — contract and characterization (no infra change)
- [x] T001 Constitution: `docs/memory/constitution.md`
- [x] T002 Spec templates: `specs/_template/`
- [x] T003 `spec.md`, `plan.md`, `research.md`, `parity-matrix.md` (#74)
- [x] T004 Hand-write `api/openapi.yaml` and `api/.redocly.yaml`; lint is clean (#73)
- [x] T005 `tests/contract/cases.yaml` and the loader `tests/contract/cases.py`
- [x] T006 Characterization tests for today's submit/status Lambdas, `tests/unit/test_job_api_handlers.py`
- [x] T007 Spec ↔ cases agreement test, `tests/contract/test_cases_match_spec.py`
- [x] T008 `tools/parity_probe.py` (with `--legacy` mode)
- [x] T010 CI `contract` job: redocly lint and `oasdiff breaking` (#73), contract tests (#71)
- [x] T011 Run the parity probe against dev API Gateway (`--profile sagebrain-dev`); results in `research.md`

## Phase 1a — shared query service (`core/sagebrain_core`) and the query worker
- [x] T101 Test: `sagebrain_core.limits` equals the contract's `x-sagebrain-limits` → `limits.py`
- [x] T102 Test: `validate_query` passes every `query_submit_*` vector → `validation.py`
- [x] T103 Test: `execute_query` sends a SigV4 POST to the reader with a 60 s timeout and emits
  `sparql_query` with `principal` and `source` → `neptune.py`, moved out of `src/lambda/query.py`
- [x] T104 `run_query` = validate + execute, for in-process callers. No rate limiting (R-5)
- [x] T105 The query worker calls `execute_query`. The existing `tests/unit/test_query_handler.py`
  tests pass with unchanged assertions; only their `@patch` targets moved to `sagebrain_core.neptune`
- [x] T106 `sagebrain_core` ships as one Lambda layer (`src/core_layer.py`); CDK test checks it's attached
- [x] T107 Test: a result over `max_result_bytes` (UTF-8) marks the job `error` with the D-16 message
  and doesn't raise (a retry returns the same result) → implement it in the worker
- [x] T108 Test: the audit event's `principal` is the message's `user_id`, or `legacy-apigw` when
  it has none (D-19) → implement it in the worker
- [x] T109 Test: the worker's reserved concurrency comes from `NEPTUNE_API.query_worker_concurrency`,
  dev 5 and prod 10 (D-14) → config + `NeptuneApiStack`

## Phase 1b — the agent calls the service in-process (D-8, D-9)
- [x] T121 Test: the `query_neptune` tool calls `run_query(source="agent", principal=user_id)`, makes
  no HTTP calls, and records the same steps → rewrite the tool
- [x] T122 Test: an over-length agent query becomes a `tool_result` error step and the job continues
- [x] T123 Test: the worker accepts both the legacy `{authorization}` and new `{user_id}` messages;
  a legacy job runs as `legacy-apigw` and the token is never used
- [x] T124 CDK: the agent role gets read-only `neptune-db` and 8182 ingress into the Neptune SG;
  `NEPTUNE_QUERY_URL` is removed; stack tests
- [x] T125 Test: the agent worker's reserved concurrency equals the contract's `agent_worker_concurrency`
- [ ] T126 Deploy order (prod): `app-prod-neptune-agent` first, then delete the two `export_value`
  lines in `app.py` and deploy `app-prod-neptune-api` (see `docs/memory/open-work.md`)

## Phase 2 — the edge stack (`app-{env}-api`), behind `API_EDGE.enabled`
- [x] T201 Contract: `job_id_max_chars` (128) and `auth_failure_retry_after_seconds` (5) join
  `x-sagebrain-limits`; `sagebrain_core.limits` and the schema-limit test follow
- [x] T202 Vectors in `cases.yaml`: the query message carries `user_id` (D-19), lowercase
  `user-agent` (D-1), a body over 256 KiB (D-5), a `job_id` over 128 chars (D-3), and
  `Retry-After: 2` on `pending` / `running` (D-15). Each has a `legacy` entry for today's Lambdas
- [x] T203 Test: the edge handlers pass every `handler` vector's `expect` → `src/lambda_edge/jobs.py`
  (D-1, D-2, D-5, D-11, D-15, D-19, FR-1–FR-4), with AWS client timeouts under the 10 s integration
  timeout
- [x] T204 Test: the docs Lambda serves Swagger UI with inlined assets, and the contract as JSON and
  YAML with `servers` set to the calling stage → `src/lambda_docs/` (FR-8). Swagger UI is fetched at
  bundle time, pinned and integrity-checked
- [x] T205 Test: the synthesized routes equal the contract's paths and methods, and only the docs
  routes skip the authorizer → `src/api_edge_stack.py` (R-14, FR-9)
- [x] T206 Test: stage throttle 50 / 100, status methods 100 / 200 (D-17), integration timeouts,
  authorizer TTL 0 and timeout, method metrics, access-log fields and retention — every value
  read from `x-sagebrain-limits`
- [x] T207 Test: gateway responses — `ACCESS_DENIED` / `UNAUTHORIZED` → 401 `Unauthorized` (D-10),
  `MISSING_AUTHENTICATION_TOKEN` → 404 plus mock `ANY` / `{proxy+}` 404 routes (D-3), `AUTHORIZER_FAILURE` and
  `AUTHORIZER_CONFIGURATION_ERROR` → 503 + `Retry-After` (D-13), `THROTTLED` → 429 + `Retry-After: 1`
  (D-18), `DEFAULT_4XX` / `DEFAULT_5XX` with ACAO
- [x] T208 Test: least-privilege grants on the existing tables and queues (submit: write + send;
  status: read) → wire into `app.py` behind `API_EDGE.enabled` (dev on, prod off)
- [x] T209 `tools/load_probe.py`: the design-target load probe and the 300-request burst probe
- [x] T210 Deploy `app-dev-api`; run the parity probe (old vs new) and the load probe in dev; results
  in `research.md`. The burst criterion moved to "429s carry D-18's headers": API Gateway's
  throttle is approximate

- [x] T211 Test: `x-api-key` alone is denied without calling Synapse, a valid bearer plus `x-api-key`
  is allowed, and `MACHINE_API_KEY` grants nothing → delete the key path from the shared authorizer
  and the unused `machine_api_key` plumbing (D-4). It was configured in no env, so the old stacks'
  templates are unchanged; their authorizers only get the code update

## Phase 3 — alarms and a dashboard for the edge stack (constitution VII)
- [x] T301 Test: alarm thresholds come from `API_EDGE_ALARMS` in config (a top-level key: the config
  merge is shallow) → `config/base.yaml`
- [x] T302 Test: 5XX alarm from the access log, excluding 503 (Sum / 5 min, 2 of 3 periods), and the
  request-volume alarm (Sum / 1 day, 1M, scaling path 1) → `src/api_edge_monitoring.py`
- [x] T303 Test: access-log metric filters count 429s per `resourcePath` (API Gateway has no 429
  metric) and 503s (the authorizer failed); a 429 alarm on each status method after 15 sustained
  minutes (scaling path 1), and a Synapse-unavailable alarm after 60 minutes of 503s, so Synapse's
  ~30 min Sunday maintenance doesn't email
- [x] T304 Test: an oldest-message alarm on the query and ask queues, 5 min (scaling path 2)
- [x] T305 Test: the dashboard graphs every contract route (requests, 4XX, 5XX, p99 latency, 429s),
  the edge Lambdas' errors and p99 duration, and queue age
- [ ] T306 After CI deploys `app-dev-api`: confirm the SNS email subscription, check the alarms
  reach `OK` and the dashboard renders
- [x] T307 Test: every alarm notifies one SNS topic with an email subscription from `ALARM_EMAIL`
  (dev `aws-sagebrain-dev@sagebase.org`, prod `aws-sagebrain-prod@sagebase.org`). The
  subscription needs a one-time confirmation from that mailbox

## Phase 4 onward
Tasks for cutover (Phase 4) and removal (Phase 5) are added here when their PRs start.
