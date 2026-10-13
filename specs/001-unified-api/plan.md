# 001 — Plan

What gets built for [spec 001](spec.md): the components, the phases and how to roll back.
Implementation detail lives in the code PRs.

## Architecture
```
Client ─HTTPS→ API Gateway REST API, stage prod (edge stack, one hostname)
               throttle per method: POSTs 50 / 100, status 100 / 200 · gateway responses (401/404/503 + CORS)
               ├ POST /query        → authorizer → query submit Lambda  → shared service: validate → SQS
               ├ GET  /query/{id}   → authorizer → query status Lambda  → query job table
               ├ POST /ask          → authorizer → ask submit Lambda    → SQS {job_id, question, user_id, source_ip}
               ├ GET  /ask/{id}     → authorizer → ask status Lambda    → ask job table
               └ GET  /api, /api/openapi.{json,yaml} → docs Lambda (no authorizer)

SQS → query worker Lambda (concurrency: dev 5, prod 10) → shared query service → Neptune reader
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
    → 404, `AUTHORIZER_FAILURE` → 503 + `Retry-After`, `THROTTLED` → 429 + `Retry-After: 1`
    (D-18), `DEFAULT_4XX` / `DEFAULT_5XX` with CORS;
  - the stage throttle (50 / 100, applied per method) with a 100 / 200 override on the two
    status methods (D-17), method-level metrics, and an access-log group with 1-month retention.

  It sits behind `API_EDGE.enabled`, so it runs alongside the two old APIs until cutover.
- **Workers.** Unchanged, except that the query worker gets a per-env reserved concurrency and the
  350 KB result check (D-14, D-16), the agent tool calls the shared service (D-8/D-9), and the
  agent worker accepts both message shapes until the old `/ask` API is deleted.

## Phases (each independently mergeable)
| Phase | Scope | Infra change |
|---|---|---|
| 0 | This spec and plan; then the contract with CI lint and breaking-change checks; characterization tests of today's submit/status Lambdas; a parity probe that runs the same cases against old and new base URLs | none |
| 1a | Shared query service: limits, validation, Neptune call, audit log. The query worker moves onto it, with per-env reserved concurrency (D-14), the result-size check (D-16), and `principal` from an optional `user_id` (D-19) | Lambda code + layer + concurrency |
| 1b | The agent tool calls the shared service in-process (D-8/D-9); the agent worker gets read-only Neptune IAM and 8182 ingress, and accepts both message shapes | agent Lambda + IAM/SG |
| 2 | Edge stack behind `API_EDGE.enabled`, deployed in parallel with the old APIs in dev: authorizer, submit/status Lambdas (with D-1, D-2, D-5, D-11, D-15, D-19), docs Lambda, gateway responses, status throttle override (D-17), route-vs-contract test, then the load probe | additive |
| 3 | Alarms and a dashboard for the edge stack (5XX, latency, 429s per method), at least matching what the old APIs have (constitution VII), plus the two scaling signals below: total requests above 1M/day, and the oldest message in the query or agent queue above 5 minutes | additive |
| 4 | Cutover dev → prod: clients move to the new base URL. At prod cutover, announce a sunset date for the old URLs to the Synapse team | config |
| 5 | Once the old APIs have had ~0 requests for 7 days **and** the sunset date is at least 30 days past: release the cross-stack exports, then delete the two old RestApis, their authorizers and the old submit/status Lambdas; retire the legacy agent message shape | removal |
| 6 | CLAUDE.md, README and docs | none |

## Scaling path
This lists what breaks first beyond the design target, and what it costs. It flags; it does
not choose fixes. Each fix gets decided, with real numbers, when its signal fires.

**1. Status polling is most of the traffic, and it is billed per request.** A job is one POST
plus a GET every 2 s until it finishes: about 10 GETs for a 20 s query and 30 for a 60 s agent
question. Each GET runs API Gateway, the authorizer Lambda, the status Lambda and a DynamoDB
read, about **$4.10 per million polls** (API Gateway about $3.50, Lambda about $0.60).

| Usage | Polls / month | Polling cost / month |
|---|---|---|
| 100 users polling around the clock (worst case) | ~130M | ~$540 |
| 100 users, 8 h on weekdays, a job in flight half the time | ~14M | ~$60 |
| Today (9 prod members) | < 1M | < $5 |

For comparison, a self-hosted front door (2 Fargate tasks, ALB, WAF) costs roughly
$100–150/month whatever the traffic. The break-even is about 25M polls/month, a sustained
~10 polls/s or **~1M requests/day**. That is the threshold of the request-volume alarm, and a
week above it reopens [R-1](research.md).

Signal: the request-volume alarm, or 429s on the status methods.

**2. Other limits, flagged in the order they are likely to bite:**
- **Neptune throughput.** Neptune Serverless scales *up*, not out: each env has one
  `db.serverless` instance that grows to `serverless_max_capacity` (dev 4, prod 16 NCU) and
  then queues queries, and that queue time counts toward the 60 s timeout. Prod's 10 query
  workers at ~20 s a query finish about 0.5 queries/s. Growing toward max NCU also raises the
  Neptune bill. The D-14 caps are safety bounds, not tuned values; tuning them needs a probe
  with real queries that measures the NCU reached. Signal: the queue-age alarm on the query
  queue, or the instance sitting at max NCU.
- **Bulk loads share the instance with queries.** With one instance, the reader and writer
  endpoints are the same machine, so a large snapshot load slows every query. Signal: query
  queue age rising while a pipeline load is running.
- **Agent throughput.** 10 agent workers at 30–120 s a question finish 5–20 answers a minute.
  Signal: the queue-age alarm on the agent queue, or Bedrock throttling errors.
- **Authorizer load.** With API Gateway's authorizer cache off, every poll invokes the
  authorizer, and cold instances call Synapse. Signal: authorizer duration or errors.
- **Fairness.** All callers share each method's budget, so one heavy user can take it.
  Signal: 429s concentrated on one principal in the access logs. Fix: #75.

## Open decisions
- **Custom domain (deferred).** Not needed to ship: API Gateway's `execute-api` URL is HTTPS.
  Adding one later means a certificate, a DNS record and one more client URL change.

## Rollback
- Up to Phase 5: point clients back to the old API URLs. The old APIs are untouched until
  then, and the agent worker accepts both message shapes, so either endpoint works.
- After Phase 5: redeploy from the tag taken before it.
