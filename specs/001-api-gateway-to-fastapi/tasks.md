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
- [x] T104 Test: `run_query` charges the global + per-principal `query` buckets and rejects when they're empty → `ratelimit.py` with LocalLimiter (Valkey in Phase 2)
- [x] T105 Refactor the query worker onto `execute_query`. It doesn't use `run_query`, because the job was already validated and admitted at submit time, and each limit is charged exactly once. All 12 existing `tests/unit/test_query_handler.py` tests pass with **unchanged assertions**; only their `@patch` targets moved to `sagebrain_core.neptune`.
- [x] T106 Bundle `sagebrain_core` into the Lambda assets (shared layer); CDK test checks that the layer is attached

## Phase 1b — FastAPI app (local)
- [ ] T111 `app/` skeleton, pinned requirements, pydantic-settings; depends on `sagebrain_core`
- [ ] T112 Codegen `models/_generated.py` from the spec; CI and pre-commit fail on drift
- [ ] T113 Test: drift between `app.routes` and the spec → implement the routers
- [ ] T114 Test: every `cases.yaml` vector (`expect`, not `legacy`) → implement the handlers and `jobs.py` (`/ask` enqueues `user_id`, D-8)
- [ ] T115 Test: authorizer cases ported from `tests/unit/test_authorizer.py` → implement `auth/`
- [ ] T116 Tests: CORS, body limit (413), timeout (504), 500 handler, access log fields → implement the middleware
- [ ] T117 Schemathesis conformance
- [ ] T117a Test: `/api` returns Swagger UI HTML with no external asset hosts; `/api/openapi.json` equals the committed YAML with `servers` set to `settings.public_base_url`; `/api/openapi.yaml` is served; `/docs`, `/redoc` and `/openapi.json` return 404 → implement the docs router (FR-9)
- [ ] T118 Dockerfile (ARM64, non-root), docker compose with Valkey, CI image build

## Phase 1c — agent calls the service in-process
- [ ] T121 Test: the `query_neptune` tool calls `run_query(source="agent", principal=user_id)`, makes no HTTP calls, and records the same steps → rewrite the tool
- [ ] T122 Test: an over-length or rate-limited agent query becomes a `tool_result` error step and the job continues → implement it
- [ ] T123 Test: the worker accepts both the legacy `{authorization}` and new `{user_id}` messages → implement it
- [ ] T124 CDK: agent role gets read-only `neptune-db`, 8182 ingress into the Neptune SG; remove `NEPTUNE_QUERY_URL`; stack tests

## Phase 2+
Phase 2 and later are tracked in their own specs: `002-redis-rate-limiting` and
`003-cutover-and-decommission`. Infra (Phases 3 and 4) is tracked in this file once Phase 1
lands.

---

## Handoff: current state (updated 2026-10-04, end of Phase 1a)
**Branch:** `spec-driven-fastapi-phase0`. Phases 0 and 1a are complete. **Next: Phase 1b (T111–T118).**

Read these first, in this order:
1. `.specify/memory/constitution.md`: the rules. Contract first, limits server-side,
   every threshold tested.
2. `specs/001-api-gateway-to-fastapi/spec.md`: requirements FR-1..9, deviations D-1..10.
3. `parity-matrix.md` and `research.md`: where each limit is enforced, and the live
   API Gateway behaviour observed in dev.
4. `api/openapi.yaml`: the contract. `tests/contract/cases.yaml`: the vectors FastAPI must pass.
5. `core/sagebrain_core/`: the shared service the app must **reuse**, not duplicate:
   - `validate_query` / `validate_question`
   - `admit("query"|"ask", principal)`
   - `limits.*`

Verify the starting state before writing anything:
```bash
python -m pytest tests/ -q                       # expect all green (301 at handoff)
npx --yes @redocly/cli@2 lint api/openapi.yaml --config api/.redocly.yaml
pre-commit run --all-files
```

Decisions already made. Do not re-litigate them:
- FastAPI on ECS Fargate behind ALB + WAF.
- The SQS + Lambda workers stay.
- The agent calls `sagebrain_core.run_query` in-process (Phase 1c), never over HTTP to `/query`.
- `/ask` enqueues `{job_id, question, user_id, source_ip}` with no token (D-8).
- The docs live at `/api` (Swagger UI, self-hosted assets), `/api/openapi.json` and
  `/api/openapi.yaml`. They are public, and FastAPI's `/docs`, `/redoc` and `/openapi.json`
  are disabled.
- Rate limiting is global then per-principal via `sagebrain_core.ratelimit.admit`. HTTP charges
  the global bucket before auth; Valkey arrives in spec 002.
- Valid-JSON-but-bad-field errors come from `sagebrain_core` (`QueryRejected.message`). Body
  parsing errors ("must be valid JSON" / "must be a JSON object") belong to the HTTP layer.

Phase 1b notes:
- FastAPI tests must run every `cases.yaml` vector against `expect`, never `legacy`. Reuse
  `tests/contract/cases.py` (`check_response`, `request_body`, …).
- Deviation D-4: `x-api-key` works alone, without the `Authorization` header. D-10: every auth
  failure returns 401 `{"message":"Unauthorized"}`.
- `AUTH_TRANSIENT_STATUS` defaults to 500 (research.md, F3).
- Port the auth cases from `tests/unit/test_authorizer.py`.
- The app depends on `core/` (pip-install it in the image). Tests already get `core/` through
  `tests/conftest.py`.

Gotchas:
- AWS profiles: `sagebrain-dev` for dev, `sagebrain` for prod (only `app-prod-*` stacks).
  `$SYNAPSE_AUTH_TOKEN` holds a team-member PAT for live probes. Rerun the dev probe with the
  command in `research.md`.
- CDK Docker bundling mounts the **raw** source directory, so `exclude=` doesn't filter what the
  bundling command copies. Clean up inside the command instead (see `src/core_layer.py`).
- CDK tests need Docker running only for real synth. The unit templates disable bundling.
- Pre-commit's black reformats files, so re-read a file before making exact-match edits to it.
- `config/*.yaml` merges shallowly at the top level. Only `API_APP` deep-merges (`src/utils.py`).
