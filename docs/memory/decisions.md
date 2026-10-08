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
- **`DefaultNamedGraph` was legacy data and was removed (2026-10-08, #60).** `tools/load_kg.py`
  doesn't set `namedGraphUri`, so anything it loads lands there and silently joins the union. Prefer
  the pipeline. If you must use `load_kg.py`, expect to clean up afterwards.
- **Load errors are captured per file on failure** (#49, issue #46): the loader fetches
  `?errors=TRUE&details=TRUE`, so you rarely need direct Neptune access to see what broke.

## Infrastructure
- **Neptune is serverless-only** with per-env min/max capacity (#45). Big Turtle files can OOM the
  loader on small capacity (#10).
- **Removed: the SageMaker stack** (#56). It was the old way into the VPC; direct access is now the
  temporary bastion ([runbook](runbooks/direct-neptune-access.md)). **Reverted: cost anomaly
  alerts** (#55 → #57).
- **Graph Explorer queries Neptune directly** (#36), so they bypass the `/query` audit log. Its
  access control is the ALB's IP allow-list.

## API / auth
- **Both APIs are async (submit + poll)**, because SPARQL takes 4–40s and API Gateway caps at 29s (#18).
- **Synapse team-gated authorizer** (#31, #40). Dev and prod use different teams (#63, issue #62).
- **Spec-driven development + FastAPI on Fargate** (spec 001, #64). Under it, `api/openapi.yaml` is
  the contract, and `sagebrain_core` is the single in-process query chokepoint for limits and audit
  logging. Until #64 merges, `dev` has none of this.

## Governance
- **Access requirements first, then ACLs** (spec 002, #66; it builds on #64). It replaces earlier
  explorations: #54 (ReBAC concept + AVP) and #58 (policy engine + query rewriter). Issue #16 is
  the original question.
