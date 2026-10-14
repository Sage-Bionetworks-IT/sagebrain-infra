# 004 — Plan

## Architecture
```
S3 {portal}/{tag}/manifest.ttl ─→ EventBridge ─→ Step Functions (load pipeline)
                                                   │ Prepare → [DropLoop] → StartLoad → Wait/Check
                                                   │ → RecordSuccess → Publish
                                                   ↓
                                    tracking table  app-{env}-neptune-pipeline-loads
                                      portal/snapshot rows   (lineage, unchanged keys)
                                      "_live"/<portal> items (the registry)
                                                   ↑ Query, cached 60 s
/query worker, /ask worker ─→ sagebrain_core.execute_query ─→ Neptune /sparql
                               query=… & default-graph-uri=g1 & named-graph-uri=g1 & …
```
Neptune keeps every snapshot. Only the request's dataset decides what a query sees.

## Components

### Pipeline (`src/lambda_loader/loader.py`, `src/neptune_pipeline_stack.py`)
- `parse_snapshot`: tag regex (FR-1), `_` portals rejected, `date` → `tag`. Graph template
  `urn:sagebrain:{portal}:{tag}` in `config/base.yaml` and both defaults.
- New actions on the same Lambda, keeping one fast call per state:
  - `prepare` (dedupe on sequencer, classify new / failed / published / in progress)
  - `drop` (one batched `DELETE` + `ASK`, the form in `tools/neptune_bastion/bastion.sh`)
  - `publish` (conditional put on `_live/<portal>`)
  - `start`, `check` and `record` stay, with `released_at` and `sequencer` added to rows.
- State machine:
  ```
  Prepare ─ duplicate / in progress ──────────────────────────────→ Skip (Succeed)
     ├─ published tag ─────────────────────────────────────────────→ Publish → Succeed
     ├─ failed tag → DropLoop(own graph) ─┐
     └─ new tag ──────────────────────────┴→ StartLoad → Wait/Check
           ├─ LOAD_COMPLETED → RecordSuccess → Publish → Succeed
           └─ failed         → RecordFailure → Fail (alarm)
  ```
  `released_at` comes from `$$.Execution.StartTime` in the Prepare payload, so the EventBridge rule
  doesn't change.
- Loader Lambda: timeout 150 s. IAM adds `neptune-db:ReadDataViaQuery` and `DeleteDataViaQuery` on
  the cluster. Config adds `NEPTUNE_PIPELINE.drop_batch_size` (dev 100000, prod 300000).

### Query service (`core/sagebrain_core/`)
- `graphs.py` (new): `live_graphs()` queries `_live` on the pipeline table, with an in-memory cache of
  `limits.LIVE_GRAPHS_CACHE_SECONDS`. An empty result → `["urn:sagebrain:none"]`; an error raises.
- `neptune.execute_query`: when `graph_scoping` is on, append each graph as both
  `default-graph-uri` and `named-graph-uri`, and log `graphs`.
- `validation.validate_query`: reject `SERVICE` outside comments (FR-15), with the new contract error
  string. `limits.py` gains the cache constant.

### Stacks / config
- `app.py` passes the pipeline table name to `NeptuneApiStack` and `NeptuneAgentStack`. The query and
  agent workers get `dynamodb:Query` on that table's ARN (built from the deterministic name, so no
  new CloudFormation export), plus env vars `LOAD_TABLE_NAME` and `GRAPH_SCOPING`.
- `NEPTUNE_API.graph_scoping`: `false` in base, `true` in `config/dev.yaml`. Mind the shallow-merge
  gotcha: an env file's top-level key replaces the whole base block.

### Agent (`src/lambda_agent/agent.py`)
- System prompt (FR-17). Queries already go through `run_query`, so scoping and the `SERVICE` rule
  apply without other changes.

### Contract and tests
- `api/openapi.yaml`:
  - descriptions for `/query` and `/ask` (scoping)
  - the `SERVICE` 400 example
  - `x-sagebrain-limits.live_graphs_cache_seconds: 60`
- `tests/contract/cases.yaml`: a `SERVICE` vector, with a `legacy:` entry for today's behaviour.
- Unit tests:
  - `tests/unit/test_loader_handler.py`: tags, sequencer, each Prepare branch, publish win/lose,
    drop loop.
  - `tests/unit/test_neptune_pipeline_stack.py`: states, IAM, timeout, batch-size env.
  - `tests/unit/core/`: both lists sent, sentinel, error, cache TTL, `SERVICE`, `graphs` in the log.
  - Stack tests for the new grant and env vars.

### Migration
- `tools/backfill_live_registry.py` writes `_live/<portal>` = the newest `complete` row per portal.
  Operator-run, idempotent, `--dry-run`. Run it in an env **before** `graph_scoping` is turned on
  there.
- Dev: register `nf` → `nf:2026-07-28`. Leave `test:2026-07-28` unregistered (hidden).
- Prod: compare the tracking table with `bastion.sh sagebrain graphs` first. Graphs the tracking table
  doesn't know about become invisible once scoping is on.

### Docs (same PRs as the facts they record)
- CLAUDE.md: key contract (`{tag}`), pipeline flow, "Querying a snapshot" and "Not yet implemented"
  rewritten.
- `docs/memory/decisions.md`: supersede "union of all snapshots"; the constitution V exception.
- `gotchas.md`: Q4/Q5 behaviour; send both lists; `SERVICE` exhausts memory; Neptune's error body is
  dropped.
- `open-work.md`: remove #51, #61, "scope queries to latest"; add the merge-cost signal (Q6) and #88.

## Phases (each independently mergeable; merging deploys via CI/CD)
1. **Spec** — this directory only. The contract edits (`api/openapi.yaml`, `tests/contract/cases.yaml`)
   land in phase 4, first in that PR, alongside the code. `tests/unit/core/test_limits.py` and the
   `cases.yaml` vectors run against the code, so a contract change on its own fails CI.
2. **#61** — tag token, `released_at`, sequencer dedupe, `_` rejection. No behaviour change for
   existing dated keys.
3. **Registry** — Prepare/Publish/DropLoop, IAM, timeout, backfill tool. Readers are unaffected
   until phase 4. Run the dev backfill after deploy.
4. **Scoping in dev** — contract edits, then the query service change, `SERVICE` rejection, grants,
   agent prompt, flag on in dev.
5. **Prod** — backfill prod, then `graph_scoping: true` in `config/prod.yaml`.

`tasks.md` lands with phase 2's code (constitution workflow).

## Scaling path
- **Query cost** tracks portals listed, not snapshots stored (research Q6). Signal: summed execution
  time of scoped queries vs portal count. Lever: fewer, larger portals per query, or a per-portal
  opt-in parameter (a contract change).
- **Storage** grows by about 200 B/triple per snapshot (Q8). Lever: #88.
- **Registry reads**: one DynamoDB `Query` per Lambda per 60 s. Negligible.

## Open decisions
- None blocking. #88 settles retention N and the sweep trigger.

## Rollback
- Phase 4/5: set `graph_scoping: false` and redeploy → the union again. The registry and rows stay.
- Phase 3: revert the PR. Registry items are inert without phase 4.
- Phase 2: dated keys behave as before. Revert only blocks new `v…` tags (any already-loaded `v…`
  graphs stay in the union).
