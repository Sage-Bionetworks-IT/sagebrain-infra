# Sage Brain Constitution

Non-negotiable rules for every change to this repo. Specs, plans and reviews are checked
against these articles; a deviation must be called out explicitly in the feature's `spec.md`.

This file holds the **rules**. The **why** — the decisions behind them, standing exceptions,
open work and gotchas — lives alongside it in this memory bank ([README.md](README.md)).
Adding, changing or excepting an article also adds an entry to [decisions.md](decisions.md) in
the same PR.

## I. The contract comes first
The committed OpenAPI contract (spec 001 creates it as `api/openapi.yaml`) is the source of truth
for every HTTP endpoint. A behaviour change starts as a spec change (in the same PR, ahead of the
code). Code is held to the spec by tests: deployed routes match its paths and methods, and
responses validate against its schemas. A breaking change (flagged by CI's breaking-change
check) needs a `specs/NNN-*` entry explaining the migration.

## II. Parity before improvement
When replacing an implementation, first reproduce today's behaviour exactly (characterization
tests that run against the old implementation), then change it deliberately. Every intentional
difference is listed as a numbered deviation in the feature spec, with a test that records the
old behaviour.

## III. Test first; every threshold has a test
Limits (lengths, rates, bursts, sizes, timeouts, TTLs, retention) are declared once in config
or the spec's `x-sagebrain-limits`, enforced in exactly one server-side place, and covered by a
test that fails if the limit moves. No threshold exists only in documentation or only in
infrastructure settings.

## IV. Least privilege, no secrets in config
IAM grants name exact actions on exact resources. Credentials live in Secrets Manager and are
read at runtime by the code that needs them — never in YAML, environment literals, logs, or
error bodies.

## V. One server-side query service is the chokepoint
All SPARQL, whether from `POST /query` or from the agent, runs through one shared
server-side query service. That service enforces every query limit (length, read-only,
Neptune timeout) and emits the structured `sparql_query` audit log. Internal callers such as
the agent call the service function directly rather than looping back over HTTP, so a query
limit must never live only in API Gateway. Request rate is the one limit outside the service:
API Gateway throttles HTTP callers, and an internal caller is bounded by its worker's reserved
concurrency and the service's timeout. Each bound has a test. A new query path must go through
the service and name its rate bound.

Standing exceptions (not user-facing query paths; each recorded in `decisions.md`):
Graph Explorer, which signs its own requests to Neptune behind the ALB IP allow-list, and the
temporary operator bastion
([runbook](runbooks/direct-neptune-access.md)), which is never left deployed, and the
ingestion pipeline's loader, which deletes a failed snapshot's graph before its retry (spec 004).
None of them emits a `sparql_query` audit log. A new exception needs its own decisions entry.

## VI. Every PR is deployable and reversible on its own
Phases are independently mergeable. Infrastructure that replaces something runs in parallel
behind a config flag until cutover; removal of the old path happens in its own PR(s), after
cross-stack exports are released.

## VII. Observability parity before cutover
A replacement must emit at least the logs, metrics and alarms of what it replaces before it
takes traffic.

## Workflow
`specs/NNN-short-name/` per feature: `spec.md` (what & why, requirements, deviations,
success criteria) → `plan.md` (what gets built: components, phases, rollback; no file-level
detail) → `tasks.md` (ordered, test-first checklist; it lands with the code, not with the
plan). Copy `specs/_template/` to start. Before writing a spec, read
`decisions.md` and `open-work.md` for prior decisions and known threads.
