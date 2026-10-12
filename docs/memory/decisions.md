# Decisions

Newest first within each theme. Each entry: the decision → why → where it was made.

## Data model / ingestion
- **One named graph per snapshot, append-only** (`urn:sagebrain:{portal}:{date}`); Neptune does no
  diffs or upserts. Chosen over a full reset + reload (API downtime during loads) and over
  blue/green clusters (cost). Discussed in #12, built in #39 (SKG-149).
- **Neptune's default graph is the union of all named graphs.** Unscoped SPARQL reads every
  snapshot at once. "Query the latest snapshot per portal" is the required follow-up, and it is
  still not built.
- **Only `{portal}/{date}/data/` is loaded.** The bulk loader parses every object under its prefix
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
  into a stack: spec and plan #74, contract #73, core #71. The rules it introduced (contract first,
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
  shelves #71's DynamoDB rate limiter until one user actually starves others (spec 001 R-5).
- **Constitution I and IV amended for Lambda (2026-10-12, #74).** Article I no longer requires
  generated Pydantic models (pydantic's compiled deps complicate the x86 Lambdas); routes and
  responses are tested against the contract instead. Article IV no longer says "ECS secrets".
- **Synapse bearer tokens are the only API credential (2026-10-12, #74).** The authorizer's
  `x-api-key` machine-key path was configured in no environment, and the agent no longer needs a
  machine credential once it calls the query service in-process.

## Governance
- **Access requirements first, then ACLs** (spec 002, #66; it builds on spec 001: #74, #73, #71, #72). It replaces earlier
  explorations: #54 (ReBAC concept + AVP) and #58 (policy engine + query rewriter). Issue #16 is
  the original question.
