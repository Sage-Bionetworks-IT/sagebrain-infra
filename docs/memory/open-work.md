# Open work, by theme

Durable context only. Check current status with `gh issue view N`. Delete an entry in the PR that
resolves it.

## Ingestion pipeline
- **Failed or partial loads stay live in the union** until someone re-publishes that date. Options
  are drop-before-load (self-healing on re-publish) vs drop-on-failure (covers the gap), and a
  staging cluster would make the question moot (#51). That depends on a staging → prod promotion
  strategy (#52).
- **Release-keyed sources** (e.g. Reactome `v97`) are rejected because the snapshot token must be
  a date; the proposal is to widen it to `v…` tags (#61).
- **Bulk loader mode**: `NEW` vs the inherited `AUTO` (#44). Move the loader to the boto3
  `neptunedata` client (#50). Note: `neptunedata` has **no SPARQL execute API**, only loader,
  openCypher and Gremlin.
- **Turtle file size vs serverless capacity** (#10): aim for 100 MB–1 GB per file.

## Query path / agent
- **Scope queries to the latest snapshot per portal** (see decisions.md). This is the main
  correctness gap for `/query`, `/ask` and Graph Explorer.
- Agent hardening: sanitize `/ask` errors (#22, PR #53); avoid logging full questions (#21);
  CORS / abuse protections (#23); `get_schema` can be unbounded (#30).
- Normalize header keys in the query Lambda's logs (#20).
- **Status polling cost** (spec 001 plan, scaling path): each poll is billed per request; at a
  sustained ~1M requests/day a fixed-cost front door is cheaper. The request-volume alarm
  reopens spec 001 R-1. Related levers to evaluate then: API Gateway's authorizer cache (R-18).
- **Results over 350 KB fail** (spec 001 D-16) because results live in a DynamoDB item.
  Offloading them to S3 needs its own spec.
- **Custom domain for the unified API** (spec 001 R-16): needs a certificate and DNS from IT, and
  one more client URL change. Deferred until someone needs a stable vanity URL. Before it lands,
  escape `<` in the `/api` page's spec URL (`src/lambda_docs/docs.py`): a mapping changes what
  `requestContext.path` can contain (security review 2026-10-13).

## Infra hygiene
- Narrow Neptune SG egress to the S3 prefix list (#15). Make the endpoint configurable per env (#14).
- Extract the Synapse authorizer into a reusable construct (#32). Stop reading env at import time (#8).
- #4 and #13 refer to the removed SageMaker stack and are likely obsolete.
