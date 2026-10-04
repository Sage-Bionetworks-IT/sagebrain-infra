# 001 — Plan

See the full design: target architecture, the CDK resources for `src/neptune_app_stack.py`,
the middleware order, and the WAF rules. This file is the authoritative copy, so keep it
up to date.

## Architecture
```
Client ─HTTPS→ WAF ─→ ALB :443 ─ /query* → TG-query, /ask* → TG-ask, /api* + /healthz → TG-query
                        └→ ECS Fargate (ARM64, private subnets)
                             CORS → access log → body limit → timeout
                             → auth → router
                             ├ POST /query → sagebrain_core.validate_query + rate check → SQS
                             ├ POST /ask   → validate question + rate check → SQS {job_id, question, user_id}
                             ├ GET status  → DynamoDB
                             ├ GET /api, /api/openapi.{json,yaml} → committed contract (public)
                             └ Synapse REST (auth)

SQS → query worker Lambda → sagebrain_core.run_query(source="direct") → Neptune reader
SQS → agent worker Lambda (rc=10) → Strands agent → query_neptune tool
                                    → sagebrain_core.run_query(source="agent", principal=user_id)
                                    → Neptune reader            (no HTTP loopback)

sagebrain_core (shared by the app image, the query worker and the agent worker):
  limits.py         single source of the threshold values (== spec x-sagebrain-limits)
  query_service.py  validate_query, run_query (rate buckets, 60 s Neptune timeout, SigV4, audit log)
  ratelimit.py      Valkey token buckets (+ local fail-open fallback)
```

## Layout
- `api/openapi.yaml` (contract)
- `core/sagebrain_core/` (the shared, server-side query service and limits)
- `app/` (Docker build context, `sagebrain_api` package; depends on `sagebrain_core`)
- `src/neptune_app_stack.py`
- `tests/contract/`, `tests/unit/core/`, `tests/unit/app/`
- `tools/parity_probe.py`

## Phases (each independently mergeable)
| Phase | Scope | Infra change |
|---|---|---|
| 0 | Contract, constitution, specs, `cases.yaml`, Lambda characterization tests, parity probe, `API_APP` deep merge, CI lint | none |
| 1a | `sagebrain_core`: limits, `validate_query`, `run_query` (moved out of `src/lambda/query.py`), LocalLimiter. The query worker is refactored onto it with behaviour unchanged (the existing `test_query_handler.py` must still pass) | Lambda code only |
| 1b | FastAPI app (local): codegen, routers, auth port, middleware, Dockerfile, drift and Schemathesis tests | none |
| 1c | Agent tool calls `run_query` in-process (D-8/D-9); agent stack gets Neptune read IAM and 8182 ingress; the worker accepts both message shapes | agent Lambda + IAM/SG |
| 2 | Valkey token bucket (spec 002) | none |
| 3 | `NeptuneAppStack` behind `API_APP.enabled`; runs in parallel with API Gateway in dev | additive |
| 4 | Monitoring for the ALB, ECS, WAF and Valkey | additive |
| 5 | Cutover dev → stage → prod (clients move host) (spec 003) | config |
| 6a / 6b | Release the cross-stack exports, then delete the RestApi, authorizer and submit/status Lambdas | removal |
| 7 | CLAUDE.md, README and docs | none |

## Rollback
- Up to Phase 5: point clients back to the API Gateway URLs. API Gateway and its `/ask`
  Lambda stay untouched until 6b, and the agent worker accepts both message shapes, so
  either front door works.
- After 6b: redeploy from the tag taken before 6b.
