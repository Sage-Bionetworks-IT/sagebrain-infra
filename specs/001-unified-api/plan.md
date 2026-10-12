# 001 — Plan

What gets built for [spec 001](spec.md): the components, the phases and how to roll back.
Implementation detail lives in the code PRs.

## Architecture
```
Client ─HTTPS→ API Gateway REST API, stage prod (edge stack, one hostname)
               throttle 50 rps / 100 burst per method · gateway responses (401/404/503 + CORS)
               ├ POST /query        → authorizer → query submit Lambda  → shared service: validate → SQS
               ├ GET  /query/{id}   → authorizer → query status Lambda  → query job table
               ├ POST /ask          → authorizer → ask submit Lambda    → SQS {job_id, question, user_id, source_ip}
               ├ GET  /ask/{id}     → authorizer → ask status Lambda    → ask job table
               └ GET  /api, /api/openapi.{json,yaml} → docs Lambda (no authorizer)

SQS → query worker Lambda → shared query service → Neptune reader
SQS → agent worker Lambda (concurrency 10) → Strands agent → query_neptune tool
                                           → shared query service → Neptune reader   (no HTTP loopback)

Shared query service (one package, a Lambda layer used by the query submit Lambda, the query
worker and the agent worker): limit values (equal to the contract's x-sagebrain-limits),
query validation, the 60 s SigV4 Neptune call, the sparql_query audit log
```

## Components
- **Contract** (R-0). The first deliverable after this plan, before any code: a hand-written
  OpenAPI 3.1 file, `api/openapi.yaml`, describing today's API plus the deviations, with every
  threshold in `info.x-sagebrain-limits`. Synapse bearer is its only security scheme (D-4).
  `servers` is a placeholder that the docs Lambda fills in per deployment. CI lints it and
  fails a PR that makes a breaking change. It carries no API Gateway extensions (R-14).
- **Shared query service.** The single enforcement point for query validation, the Neptune
  timeout and the audit log (constitution V). It ships as one Lambda layer.
- **Edge stack** (`app-{env}-api`). One `RestApi` with:
  - routes built by hand in CDK, with a test that the synthesized routes equal the contract's
    paths and methods (R-14);
  - the Synapse authorizer (moved here from the two API stacks) on every `/query` and `/ask`
    method;
  - the four submit/status Lambdas, reading the existing tables and queues from the API and
    agent stacks;
  - the docs Lambda for `/api` (R-15);
  - gateway responses: `ACCESS_DENIED` and `UNAUTHORIZED` → 401, `MISSING_AUTHENTICATION_TOKEN`
    → 404, `AUTHORIZER_FAILURE` → 503 + `Retry-After`, `DEFAULT_4XX` / `DEFAULT_5XX` with CORS;
  - the stage throttle (50 / 100, applied per method), method-level metrics, and an access-log
    group with 1-month retention.

  It sits behind `API_EDGE.enabled`, so it runs alongside the two old APIs until cutover.
- **Workers.** Unchanged, except that the agent tool calls the shared service (D-8/D-9), and
  the agent worker accepts both message shapes until the old `/ask` API is deleted.

## Phases (each independently mergeable)
| Phase | Scope | Infra change |
|---|---|---|
| 0 | This spec and plan; then the contract with CI lint and breaking-change checks; characterization tests of today's submit/status Lambdas; a parity probe that runs the same cases against old and new base URLs | none |
| 1a | Shared query service: limits, validation, Neptune call, audit log. The query worker moves onto it with behaviour unchanged | Lambda code + layer |
| 1b | The agent tool calls the shared service in-process (D-8/D-9); the agent worker gets read-only Neptune IAM and 8182 ingress, and accepts both message shapes | agent Lambda + IAM/SG |
| 2 | Edge stack behind `API_EDGE.enabled`, deployed in parallel with the old APIs in dev: authorizer, submit/status Lambdas (with D-1, D-2, D-5, D-11), docs Lambda, gateway responses, route-vs-contract test | additive |
| 3 | Alarms and a dashboard for the edge stack (5XX, latency, 429s per method), at least matching what the old APIs have (constitution VII) | additive |
| 4 | Cutover dev → stage → prod: clients move to the new base URL | config |
| 5 | Release the cross-stack exports, then delete the two old RestApis, their authorizers and the old submit/status Lambdas; retire the legacy agent message shape | removal |
| 6 | CLAUDE.md, README and docs | none |

## Open decisions
- **Custom domain (deferred).** Not needed to ship: API Gateway's `execute-api` URL is HTTPS.
  Adding one later means a certificate, a DNS record and one more client URL change.

## Rollback
- Up to Phase 5: point clients back to the old API URLs. The old APIs are untouched until
  then, and the agent worker accepts both message shapes, so either endpoint works.
- After Phase 5: redeploy from the tag taken before it.
