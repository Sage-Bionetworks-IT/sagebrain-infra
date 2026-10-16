# 004 — Tasks

Ordered, test-first. Phases are the ones in [plan.md](plan.md); each is independently mergeable.

## Phase 2 — release-keyed tags, arrival order, sequencer dedupe (#61)
- [x] T201 Test: `parse_snapshot` accepts `YYYY-MM-DD` and `v…` tags, rejects any other tag and
  `_` portals (FR-1, D-4) → tag regex in `loader.py`
- [x] T202 `graph_uri_template` placeholder `{date}` → `{tag}` in `config/base.yaml`, the stack
  default and the loader default (FR-2)
- [x] T203 Test: the StartLoad payload carries `released_at` = `$$.Execution.StartTime`; the loader
  stores it and the event's `sequencer` on the tracking row (FR-4)
- [x] T204 Test: a duplicate delivery (same sequencer) is skipped; an identical re-upload (same
  etag, new sequencer) reloads, and the old etag behaviour is recorded in the test (FR-5, D-5)

## Phase 3 — live registry, Prepare / Publish / DropLoop (#51)
- [x] T301 Test: `prepare` classifies new / duplicate / in progress / published / failed and claims
  the row with a conditional write (FR-5, FR-7, FR-8, FR-9)
- [x] T302 Test: `publish` writes `_live/<portal>` only if absent or older than this `released_at`;
  a loss is logged and the execution still succeeds (FR-6)
- [x] T303 Test: `drop` deletes one batch of `drop_batch_size` from the tag's own graph and reports
  whether any triples remain (FR-8)
- [x] T304 Test: a failure after the row is claimed marks it `error` before the execution fails, so
  the tag can be retried; a failed load is never published (FR-10)
- [x] T305 State machine: Prepare → Route → [DropLoop] → StartLoad → Wait/Check → RecordSuccess →
  Publish; stack tests for the states
- [x] T306 Stack: loader timeout 150 s, `ReadDataViaQuery` + `DeleteDataViaQuery` on the cluster,
  `DROP_BATCH_SIZE` from `NEPTUNE_PIPELINE.drop_batch_size` (dev 100000, prod 300000)
- [x] T307 `tools/backfill_live_registry.py` (`--dry-run`, `--portal`, idempotent) with unit tests
- [ ] T308 Operator: run the dev backfill after deploy (`--portal nf`; leave `test` unregistered)

## Phase 4 — scoping in dev
- [x] T401 Contract first: `/query` and `/ask` descriptions, the `SERVICE` 400 example,
  `x-sagebrain-limits.live_graphs_cache_seconds: 60`; a `SERVICE` vector in
  `tests/contract/cases.yaml` with a `legacy:` entry (D-3)
- [x] T402 Test: `validate_query` rejects `SERVICE` outside comments, strings and IRIs (FR-15) →
  `validation.py`
- [x] T403 Test: `live_graphs()` reads `_live`, caches for `live_graphs_cache_seconds`, returns the
  sentinel when empty and raises on a read error (FR-13, FR-14) → `graphs.py`
- [x] T404 Test: with scoping on, `execute_query` sends each graph as both `default-graph-uri` and
  `named-graph-uri` and logs `graphs`; off, the request and log are unchanged (FR-11, FR-16, D-8)
- [x] T405 The query worker rejects `SERVICE` in messages from the old submit Lambda, which
  doesn't use `validate_query`
- [x] T406 Stacks: query and agent workers get `dynamodb:Query` on the pipeline table, limited to the
  `_live` partition, and `LOAD_TABLE_NAME` / `GRAPH_SCOPING`; `NEPTUNE_API.graph_scoping` on in dev
- [x] T407 Agent system prompt (FR-17); test that a `SERVICE` query becomes a `tool_result` error step
- [ ] T408 Operator: on dev, check the success criteria in [spec.md](spec.md)

## Phase 5 — prod
- [ ] T501 Operator: compare the prod tracking table with `bastion.sh sagebrain graphs`, then run the
  prod backfill
- [ ] T502 `graph_scoping: true` in `config/prod.yaml`
