# Decisions

Newest first within each theme. Each entry: the decision → why → where it was made.

## Data model / ingestion
- **One named graph per snapshot, append-only** (`urn:sagebrain:{portal}:{tag}`); Neptune does no
  diffs or upserts. Chosen over a full reset + reload (API downtime during loads) and over
  blue/green clusters (cost). Discussed in #12, built in #39 (SKG-149).
- **Snapshot tags are a date or a `v…` release, immutable once published (2026-10-15, spec 004,
  #61).** A correction goes under a new tag; there is no suffix for same-date corrections, so
  "latest" is simply the last arrival (`released_at`, the execution start time), not a sort of tags.
  Duplicate S3 deliveries are detected by `object.sequencer`: an identical re-upload has the same
  etag but is a new PUT.
- **Neptune's default graph is the union of all named graphs.** Unscoped SPARQL reads every
  snapshot at once. "Query the latest snapshot per portal" is the required follow-up, and it is
  still not built.
- **Only `{portal}/{tag}/data/` is loaded.** The bulk loader parses every object under its prefix
  as Turtle, and `failOnError=TRUE` means one stray README fails the whole snapshot (#42).
- **`DefaultNamedGraph` was legacy data and was removed (2026-10-08, #60).** The old manual
  loader, `tools/load_kg.py`, didn't set `namedGraphUri`, so what it loaded landed there and silently
  joined the union. It has been removed (#68): every load now goes through the pipeline.
- **Load errors are captured per file on failure** (#49, issue #46): the loader fetches
  `?errors=TRUE&details=TRUE`, so you rarely need direct Neptune access to see what broke.

## Infrastructure
- **Neptune is serverless-only** with per-env min/max capacity (#45). Big Turtle files can OOM the
  loader on small capacity (#10).
- **Removed: the SageMaker stack** (#56). It was the old way into the VPC; direct access is now the
  temporary bastion ([runbook](runbooks/direct-neptune-access.md)). **Reverted: cost anomaly
  alerts** (#55 → #57).
- **Graph Explorer queries Neptune directly** (#36), so they bypass the `/query` audit log. Its
  access control is the ALB's IP allow-list. A standing exception to constitution article V, as is
  the temporary bastion (#67): both are operator/VPN-only, not user-facing query paths.

## API / auth
- **Both APIs are async (submit + poll)**, because SPARQL takes 4–40s and API Gateway caps at 29s (#18).
- **Synapse team-gated authorizer** (#31, #40). Dev and prod use different teams (#63, issue #62).
- **Spec-driven development, one API Gateway endpoint** (spec 001). The contract comes first: a
  hand-written OpenAPI 3.1 file, right after the plan and before any code (spec 001 R-0). Split
  into a stack: spec and plan #74, contract #73, then the shared query service and the agent's in-process queries #71
  (Phases 1a and 1b). The rules it introduced (contract first,
  parity before improvement, every threshold tested, one query chokepoint) are the articles in
  [constitution.md](constitution.md); this file records why and any exceptions, not the rules themselves.
- **FastAPI on Fargate was dropped for one API Gateway REST API (2026-10-12, #74).** The 29 s
  ceiling doesn't bite (both APIs are async), traffic is far below API Gateway's limits, and every
  goal (contract, one endpoint, in-process agent queries, no token in SQS) works on API Gateway;
  Fargate would add an always-on ALB/WAF/task cost and its own TLS and patching. Reopen it for
  response streaming, long-lived connections, >29 s sync calls or >10 MB bodies (spec 001 R-1).
  This supersedes the FastAPI PR #72. No custom domain yet: the `execute-api` URL is HTTPS (R-16).
- **Rate limits stay at API Gateway; per-principal buckets deferred (2026-10-12, #74).** Agent
  queries skip API Gateway but are bounded by the worker's reserved concurrency of 10 and the 60 s
  timeout. This amended constitution article V (rate is no longer one of the service's limits) and
  removed #71's DynamoDB rate limiter; per-principal buckets wait until one user actually starves
  others (spec 001 R-5). Tracked in #75, which has the signals, the design (an in-service DynamoDB
  limiter, not Valkey: a 5–10 ms check doesn't matter at 4–40 s queries and needs no VPC), and the
  rejected alternatives. Needing per-user limits is not an argument for FastAPI: Fargate tasks
  would need the same shared store.
- **Design target: 100 concurrent users (2026-10-12, #74).** Prod's team gate has 9 members.
  Status polling dominates traffic and is billed per request; the break-even with a fixed-cost
  front door is ~1M requests/day, which is the alarm that reopens the hosting decision. The query
  worker gets a per-env safety cap (dev 5, prod 10): it was uncapped, so one script could push
  the single serverless Neptune instance to max NCU and drain the account's Lambda pool. Neptune
  Serverless scales up one instance, not out, so the cap is per env. Stage isn't deployed, so
  rollout is dev → prod; old URLs retire after 7 quiet days and 30 days past an announced sunset.
- **Constitution I and IV amended for Lambda (2026-10-12, #74).** Article I no longer requires
  generated Pydantic models (pydantic's compiled deps complicate the x86 Lambdas); routes and
  responses are tested against the contract instead. Article IV no longer says "ECS secrets".
- **Synapse bearer tokens are the only API credential (2026-10-12, #74).** The authorizer's
  `x-api-key` machine-key path was configured in no environment, and the agent no longer needs a
  machine credential once it calls the query service in-process. Removed in spec 001 Phase 2 rather
  than waiting for Phase 5: it was checked before the bearer, so any request carrying `x-api-key`
  was denied even with a valid token. `x-api-key` stays in the CORS allow-list (FR-6 parity).
- **The query chokepoint is a library, `sagebrain_core`, not an endpoint** (#71). The agent's
  in-process queries get the same query limits and `sparql_query` audit log as `POST /query`, with
  no HTTP loop-back and no bearer token in SQS. Jobs from the old APIs carry no caller id, so they
  are attributed to `legacy-apigw` until those APIs are deleted (spec 001 D-19).
- **`sagebrain_core` lives in `core/`, not under `src/` (2026-10-12, #71).** `src/` is the CDK app,
  imported as `src.*`. In Lambda the layer puts `sagebrain_core` at the top level, so a copy under
  `src/` would invite `from src.sagebrain_core import …`, which passes tests and fails in Lambda.
  It's a standalone package (`core/pyproject.toml`): the layer ships it now, and a future API
  service can `pip install ./core` without depending on CDK. A future service gets its own
  top-level directory next to `core/`, not a place under `src/`.
- **The edge stack's docs inline Swagger UI (2026-10-12, spec 001 Phase 2).** Its JS and CSS go into
  the `/api` page (~1.8 MB) rather than onto extra asset routes, so the deployed routes stay exactly
  the contract's paths (R-14) and nothing loads from a CDN (FR-8). Swagger UI is pinned with its npm
  sha512 and fetched at bundle time, not committed.
- **Both authorizer-failure gateway responses map to 503 (2026-10-12, spec 001 Phase 2).** Which
  of `AUTHORIZER_FAILURE` / `AUTHORIZER_CONFIGURATION_ERROR` API Gateway returns for a raising Lambda
  authorizer isn't clearly documented, so D-13 covers both, with `Retry-After: 300`
  (`auth_failure_retry_after_seconds`, new in the contract). The dev deploy confirms which one fires.
  300 s, not 5: Synapse's weekly Sunday maintenance takes it down for ~30 min, and a short hint
  would have every client retrying through the whole window.
- **The contract describes the API, not its implementation (2026-10-12, #73).** `api/openapi.yaml`'s
  descriptions are written for clients, because they feed Swagger UI and, later, generated
  server and client code. Notes on where a limit is enforced (shared service, API Gateway,
  worker concurrency) go in YAML comments and the spec's parity matrix. Values that differ per env
  (the query worker cap) live in `config/`, not in `x-sagebrain-limits`.
- **`/api` ships with the edge stack (Phase 2), not with the contract (2026-10-12, #73).** It's a
  route on the one combined API, and its `servers` URL is that API's stage URL. Building it earlier
  would put it on one of the two APIs that Phase 5 deletes. The contract describes the routes now.
- **The unified API's stage is named after the env (2026-10-13).** Dev's base URL ends in `/dev`,
  prod's in `/prod`, so a dev URL no longer reads as prod. Only the new API changed, before any
  client moved to it; the old APIs keep CDK's default `/prod` in every env until Phase 5 deletes them.
- **An oversized query result fails the job, not the SQS message (2026-10-12, #71).** Over
  350 KB the worker marks the job `error` and returns: retrying would get the same result, then
  land in the DLQ (spec 001 D-16).

## Governance
- **ACL and access requirements checked at query time, always enforced** (spec 002, #66; it builds
  on spec 001: #74, #73, #71, #72). It replaces earlier explorations: #54 (ReBAC concept + AVP) and
  #58 (policy engine + query rewriter). Issue #16 is the original question.
  - **Fail closed, no modes (2026-10-14).** Governance is part of the service: wherever it's
    deployed it's enforced, and missing data or a backend error returns nothing. `GOVERNANCE.enabled`
    says only whether it's deployed in an env; no `off`/`shadow`/`report`.
  - **DynamoDB from Snowflake, not a governance graph in Neptune (2026-10-14).** Cedar takes its
    entities in each request, so the store doesn't matter to the policies. Two flattened tables
    (entities, grants) exported from the Synapse warehouse replace a second Neptune cluster,
    governanceDUO's per-entity sync and its ACT credential. A graph store is a Stage 2 question.
  - **Grants are managed ACT only in the POC:** the latest accessor change on an Approved
    submission is `GRANT_ACCESS`/`RENEW_ACCESS`. Other AR types are denied to everyone for now.
    ARs inherit down the parent path; ACLs come from the benefactor.
  - **Non-Synapse nodes are allowed through**, flagged as open (spec 002 research Q-2). Graph
    Explorer stays a standing exception: no synth guard against it.
