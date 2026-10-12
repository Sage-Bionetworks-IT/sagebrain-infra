# 001 — Plan

What gets built for [spec 001](spec.md): the components, the phases and how to roll back.
Implementation detail lives in the code PRs.

## Architecture
```
Client ─HTTPS→ WAF ─→ ALB :443 ─→ FastAPI service on ECS Fargate (ARM64, private subnets)
                                   CORS → access log → body limit → timeout → auth → routes
                                   ├ POST /query → validate + rate check → SQS
                                   ├ POST /ask   → validate + rate check → SQS {job_id, question, user_id, source_ip}
                                   ├ GET status  → job tables (DynamoDB)
                                   ├ GET /api, /api/openapi.{json,yaml} → committed contract (public)
                                   └ Synapse REST (auth)

SQS → query worker Lambda → shared query service → Neptune reader
SQS → agent worker Lambda (concurrency 10) → Strands agent → query_neptune tool
                                           → shared query service → Neptune reader   (no HTTP loopback)

Shared query service (one package, used by the service, the query worker and the agent worker):
  limit values (equal to the contract's x-sagebrain-limits), query validation,
  rate buckets in a shared store, the 60 s SigV4 Neptune call, the sparql_query audit log
```

## Components
- **Contract** (R-0). The first deliverable after this plan, before any code: a hand-written
  OpenAPI 3.1 file, `api/openapi.yaml`, describing today's API plus the deviations, with every
  threshold in `info.x-sagebrain-limits`. Synapse bearer is its only security scheme (D-4), and
  `servers` stays a placeholder until the hostname is decided. CI lints it and fails a PR that
  makes a breaking change. The service's request/response models are generated from it.
  It lands in its own PR, right after this plan.
- **Shared query service.** The single enforcement point for every query limit and the audit
  log (constitution V). It's bundled into the service image and both worker Lambdas.
- **Shared rate-limit store.** One DynamoDB table in its own stack, so the buckets hold across
  every ECS task and Lambda instance. Requirements:
  - one store for every caller, so the agent's queries drain the same buckets as `POST /query`;
  - if the store is unreachable, fail open to a per-process bucket at `rate / instances` and log
    `ratelimit_degraded` (at most once a minute per process); never a 500;
  - its data is ephemeral (no backups) and idle buckets expire on their own.

  Accepted risk: during an outage the shared `query` global bucket is over-admitted
  (2 tasks × 25 + 10 agent workers × 5 = 100 rps against 50). Fail-open beats failing every
  request.
- **FastAPI service.** Replaces the RestApis, the authorizer and the four submit/status Lambdas.
  It runs **2 fixed tasks with no autoscaling**, so the rate-limit fallback divisor is 2.
  If autoscaling is added later, the divisor becomes the max task count.
- **Edge.** WAF (AWS managed common rule set, a per-IP rate rule), ALB with TLS on 443 and an
  80 → 443 redirect.
- **Workers.** Unchanged, except that the agent tool calls the shared service (D-8/D-9), and the
  agent worker accepts both message shapes until the `/ask` cutover.

## Phases (each independently mergeable)
| Phase | Scope | Infra change |
|---|---|---|
| 0 | This spec and plan; then the contract with CI lint and breaking-change checks; characterization tests of today's submit/status Lambdas; a parity probe that runs the same cases against both front doors | none |
| 1a | Shared query service: limits, validation, Neptune call, audit log, in-process rate buckets. The query worker moves onto it with behaviour unchanged | Lambda code only |
| 1b | FastAPI service, runnable locally: generated models, routes, auth port, middleware, docs routes, container image, contract-conformance tests | none |
| 1c | The agent tool calls the shared service in-process (D-8/D-9); the agent worker gets read-only Neptune IAM and 8182 ingress, and accepts both message shapes | agent Lambda + IAM/SG |
| 1d | Shared rate-limit store (DynamoDB), used by the agent worker now and the service in Phase 2 | new rate-limit stack + agent worker IAM/env |
| 2 | Service stack behind a config flag, running in parallel with API Gateway in dev | additive |
| 3 | Monitoring and alarms for the ALB, ECS, WAF and the rate-limit store, including an alarm on `ratelimit_degraded` | additive |
| 4 | Cutover dev → stage → prod (clients move host) | config |
| 5a / 5b | Release the cross-stack exports, then delete the RestApis, the authorizer and the submit/status Lambdas. Then switch the transient-auth status to 503 + `Retry-After` (D-13) and retire the legacy agent message shape and its bucket (D-12) | removal |
| 6 | CLAUDE.md, README and docs | none |

## Open decisions
- **Hostname and TLS (needed before Phase 2).** Which DNS zone each environment uses, who owns
  it (a Route 53 zone in each account, or managed by IT), and whether dev and prod get separate
  names. The contract keeps a placeholder `servers` entry until this is settled.

## Rollback
- Up to Phase 4: point clients back to the API Gateway URLs. API Gateway and its `/ask`
  Lambda stay untouched until 5b, and the agent worker accepts both message shapes, so
  either front door works.
- After 5b: redeploy from the tag taken before 5b.
