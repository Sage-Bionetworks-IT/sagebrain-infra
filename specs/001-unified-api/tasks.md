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

## Phase 2 onward
Tasks for the edge stack (Phase 2), alarms (Phase 3), cutover (Phase 4) and removal (Phase 5) are
added here when their PRs start.
