# Sage Brain Constitution

Non-negotiable rules for every change to this repo. Specs, plans and reviews are checked
against these articles; a deviation must be called out explicitly in the feature's `spec.md`.

## I. The contract comes first
`api/openapi.yaml` is the source of truth for every HTTP endpoint. A behaviour change starts
as a spec change (in the same PR, ahead of the code). Pydantic models are generated from the
spec, never hand-edited. Breaking changes need the `spec-breaking` label and a `specs/NNN-*`
entry explaining the migration.

## II. Parity before improvement
When replacing an implementation, first reproduce today's behaviour exactly (characterization
tests driven by `tests/contract/cases.yaml`), then change it deliberately. Every intentional
difference is listed as a numbered deviation in the feature spec, with a `legacy` entry in
`cases.yaml` describing the old behaviour.

## III. Test first; every threshold has a test
Limits (lengths, rates, bursts, sizes, timeouts, TTLs, retention) are declared once in config
or the spec's `x-sagebrain-limits`, enforced in exactly one server-side place, and covered by a
test that fails if the limit moves. No threshold exists only in documentation or only in
infrastructure settings.

## IV. Least privilege, no secrets in config
IAM grants name exact actions on exact resources. Credentials live in Secrets Manager and
reach containers as ECS secrets — never in YAML, environment literals, logs, or error bodies.

## V. One server-side query service is the chokepoint
All SPARQL, whether from `POST /query` or from the agent, runs through the shared
`sagebrain_core` query service. That service enforces every query limit (length, rate
buckets, Neptune timeout) and emits the structured `sparql_query` audit log. Internal callers
such as the agent call the service function directly rather than looping back over HTTP, so
limits must never live only in HTTP middleware, the ALB or WAF. Those layers are extra
defence, not the enforcement point. A new query path must go through the service.

## VI. Every PR is deployable and reversible on its own
Phases are independently mergeable. Infrastructure that replaces something runs in parallel
behind a config flag until cutover; removal of the old path happens in its own PR(s), after
cross-stack exports are released.

## VII. Observability parity before cutover
A replacement must emit at least the logs, metrics and alarms of what it replaces before it
takes traffic.

## Workflow
`specs/NNN-short-name/` per feature: `spec.md` (what & why, requirements, deviations,
success criteria) → `plan.md` (how, files, phases) → `tasks.md` (ordered, test-first
checklist). Copy `specs/_template/` to start.
