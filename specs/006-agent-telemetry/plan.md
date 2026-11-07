# 006 — Plan

What gets built, not how: components, phases and rollback. File-level detail belongs in
`tasks.md` and the code PRs.

## Approach
One shared telemetry module in `sagebrain_core` provides the tracer provider, the allowlist
exporter, SQS propagation and a handler decorator. The workers adopt it one at a time. Every
phase is behind `TELEMETRY.enabled`, and until Phase 7 it's on in dev only. Nothing about the API
contract, alarms or CloudWatch logging changes, except `trace_id` in the audit events.

## Components
- **`core/sagebrain_core/telemetry.py`** (new):
  - The provider, with resource attributes from env vars.
  - The allowlist exporter (FR-1).
  - `inject_sqs` / `extract_sqs` (W3C `traceparent` in MessageAttributes).
  - `traced_handler(kind)`: starts the consumer span, records outcome and error class, and runs a
    bounded `force_flush` in `finally`.
  - A no-op path when disabled.
  - The secret, read once per cold start.
- **`core/sagebrain_core/telemetry_schema.py`** (new): the allowed and denied keys (FR-2).
- **Core layer** (`src/core_layer.py`): gains a pip install of the pinned OTel packages (Q-5).
- **`src/telemetry_stack.py`** → `app-{env}-telemetry` (new): only the ingestion-key secret,
  created with a placeholder. Consumers reference it by name, so there's no cross-stack export
  and no deploy-ordering constraint.
- **`config/` `TELEMETRY:`** (new): `enabled`, `ingest_host`, `export_timeout_seconds`,
  `sample_ratio`, `max_queue_size`.
- **Changes to existing code:**
  - `jobs.py`: start the context and send `traceparent` on submit.
  - `query.py`, `agent.py`, `loader.py`: `traced_handler`.
  - `neptune.py`: the `neptune.query` span and `trace_id` in `sparql_query`.
  - The agent runs Strands under our global provider (Strands' "existing provider" mode).
  - The pipeline state machine passes the execution id to the loader.
  - Stacks: env vars and `GetSecretValue` for each traced Lambda.
- **Docs:**
  - `docs/memory/runbooks/agent-triage.md`;
  - `CLAUDE.md` (a section on telemetry and the stack row);
  - `decisions.md` (SigNoz, the allowlist, the rejected alternatives);
  - `open-work.md` (prod enablement, edge spans).

## Phases (each independently mergeable)
| Phase | Delivers | Runtime change |
|---|---|---|
| 1: Spec | `spec.md`, `plan.md`, CLAUDE.md Draft line | none |
| 2: Telemetry core | telemetry module, schema and its tests, layer deps, `app-{env}-telemetry`, `TELEMETRY` config | none (nothing wired yet) |
| 3: Query path (dev) | submit propagation, query worker span, `neptune.query`, `trace_id` in `query_submitted` and `sparql_query` and the job item | dev query jobs traced |
| 4: Agent worker (dev) | Strands under our provider, tool and model spans, retries attribute, `trace_id` in `question_submitted` and `agent_invocation` | dev `/ask` jobs traced |
| 5: Loader (dev) | the execution id passed in, a span per action, load counts, `trace_id` in `kg_load` | dev loads traced |
| 6: Agent enablement | the triage runbook, SigNoz saved views, an ingest budget for the scaling path, and the metric stream if Q-4 says yes | none, or the metric stream |
| 7: Prod | `TELEMETRY.enabled` in prod after a decisions.md entry, cross-referenced from spec 005 | prod traced |

The open decisions settle before the phase that needs them: Q-1, Q-2 and Q-5 before Phase 2
(they shape the schema and the layer), Q-4 before Phase 6, Q-3 any time (it's out of scope here).

## Ordering constraints
- The secret must hold the real key before traces arrive. Phase 2 creates it with a placeholder,
  and the runbook step puts the key in before Phase 3 merges. A placeholder only causes rejected
  exports, which fail open (FR-7).
- The agent bundles its own OTel SDK through Strands, and that copy shadows the layer's
  (`/var/task` comes before `/opt/python`). Phase 2 pins the layer to the same versions, and a
  test fails if the two drift.

## Risks / rollback
| Risk | Mitigation / rollback |
|---|---|
| A content-bearing attribute slips through (a new Strands version adds a key) | The allowlist drops unknown keys by construction; a Strands upgrade can't add exported content. A test runs the real Strands tracer through the exporter. |
| SigNoz is slow or down | Bounded flush (2 s) and fail open. Turning `TELEMETRY.enabled` off reverts to today's behaviour with one config PR. |
| The flush adds latency to jobs | About 100 ms on jobs that take 4–40 s. The edge path isn't traced (Q-3). |
| Layer size or architecture problems from protobuf | Q-5 option B, the JSON exporter, avoids protobuf entirely. |
| A shadowed SDK version breaks the agent | The version-pin test (Ordering constraints). Phase 4 is reverted on its own. |
| A shared SigNoz tenant exposes our data to other teams | Only allowlisted, non-content fields are exported (spec 005 note). |
| Cost grows with volume | The scaling path in the spec: an ingest budget signal, then `sample_ratio`. |

## Open decisions
See [spec.md](spec.md#open-decisions), Q-1 to Q-5.
