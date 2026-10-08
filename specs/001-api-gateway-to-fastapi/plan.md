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
                             ├ rate checks → DynamoDB rate-limit table (UpdateItem)
                             └ Synapse REST (auth)

SQS → query worker Lambda → sagebrain_core.run_query(source="direct") → Neptune reader
SQS → agent worker Lambda (rc=10) → Strands agent → query_neptune tool
                                    → sagebrain_core.run_query(source="agent", principal=user_id)
                                    → rate-limit table, then Neptune reader   (no HTTP loopback)

sagebrain_core (shared by the app image, the query worker and the agent worker):
  limits.py         single source of the threshold values (== spec x-sagebrain-limits)
  query_service.py  validate_query, run_query (rate buckets, 60 s Neptune timeout, SigV4, audit log)
  ratelimit.py      token buckets: DynamoLimiter (shared, app-{env}-rate-limits) when
                    RATE_LIMIT_TABLE_NAME is set, else LocalLimiter; fails open to a
                    local bucket at rate ÷ instances on a DynamoDB outage
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
| 1d | Shared DynamoDB token buckets (`DynamoLimiter`, design below); later also the cache for governance/ReBAC lookups | new `app-{env}-rate-limits` stack (one table) + agent worker IAM/env |
| 3 | `NeptuneAppStack` behind `API_APP.enabled`; runs in parallel with API Gateway in dev | additive |
| 4 | Monitoring for the ALB, ECS, WAF and the rate-limit table | additive |
| 5 | Cutover dev → stage → prod (clients move host) (spec 003) | config |
| 6a / 6b | Release the cross-stack exports, then delete the RestApi, authorizer and submit/status Lambdas | removal |
| 7 | CLAUDE.md, README and docs | none |

## Phase 1d — shared rate limiter (DynamoDB)
**Bucket encoding.** DynamoDB update expressions have `+` and `-` but no multiply or `min`, so
a classic `tokens` + `last_refill` bucket can't be refilled inside one UpdateItem. Each bucket
is instead one number, `tat`, the time it is full again (GCRA). It admits exactly what
`LocalLimiter` admits, and the same tests run against both. An admission is one conditional
UpdateItem, either *consume* (`tat BETWEEN now AND now + (burst-1)/rate` → `tat += 1/rate`) or
*refill* (`attribute_not_exists(tat) OR tat < now` → `tat = now + 1/rate`). A failed condition
returns the item (`ReturnValuesOnConditionCheckFailure=ALL_OLD`): either a rejection with
`retry_after = tat - limit`, or the other branch applies. A per-process hint tries the likely
branch first, so the steady state is one UpdateItem per call. An idle-to-busy transition
costs two, and nothing ever reads first. `now` is wall time from an injected clock (frozen in
tests), at 1 ns decimal resolution so DynamoDB's sums are exact.

**Outage.** A DynamoDB `ClientError` (throttling, missing table, access denied, …) or a botocore
error (connect/read timeout, endpoint unreachable) fails open to a per-process `LocalLimiter` at
`rate / instances` and `ceil(burst / instances)`, and logs `ratelimit_degraded` at most once a
minute per process. `ConditionalCheckFailed` is a normal rejection. The client is module-level
and lazy (one per warm Lambda or task), with connect 2 s / read 5 s like the app's other AWS
calls and **no retries**, so a throttled table degrades at once instead of retrying into the
throttle. The app's guard runs the rate checks in the threadpool, off the event loop.

**Settings.** `RATE_LIMIT_TABLE_NAME` (unset: `LocalLimiter`, for tests and the offline profile)
and `RATE_LIMIT_FALLBACK_INSTANCES` (default 2, the planned minimum task count). The app reads
them through `Settings` (`create_app` → `make_limiter`). The agent worker reads them from its
environment (`run_query` → `default_limiter()` → `limiter_from_env`). The worker's instance
count is its reserved concurrency (10), set by CDK. In an outage the `query` global bucket is
then over-admitted (2 tasks × 25 + 10 workers × 5 = 100 rps against 50). That's accepted:
fail-open beats failing every request. Neither knob is a threshold: the rates and bursts all
come from `sagebrain_core.limits`. The TTL horizon (3600 s after the bucket is full again) is
unobservable, because a missing item and a full bucket admit the same calls. So neither is in
`x-sagebrain-limits`.

**Stack.** The table lives in its own stack, `app-{env}-rate-limits`
(`src/rate_limit_stack.py`, one resource), not in the Neptune or agent stacks:
- It depends on nothing, so the agent stack and the Phase 3 app stack can both reference it
  without a cycle. The Neptune stack, which holds the cluster, never redeploys for it.
- Referencing it from the agent stack auto-exports the table name and ARN
  (`app-{env}-rate-limits:ExportsOutput…`), and CDK orders `app-{env}-rate-limits` before
  `app-{env}-neptune-agent`.
- The agent's grant is a single `dynamodb:UpdateItem` statement on the table ARN, in the
  worker role's existing DefaultPolicy, so no agent logical ID changes.

**Deploy order (1d).** Deploy `app-{env}-rate-limits` first, then `app-{env}-neptune-agent`.
`cdk deploy app-dev-neptune-agent` deploys the dependency first unless `--exclusively` is
given. It's a full deploy, not `--hotswap` (IAM and env change). This is independent of the
1c order (agent before the API stack's `export_value` removal), and both can go in one agent
deploy. The new exports change no existing stack. Deleting or renaming the table later needs
the usual two steps: drop the agent's reference, deploy the agent, then change the table.

**Phase 3 TODO.** The app task role gets the same single `dynamodb:UpdateItem` statement on
the table ARN, plus `RATE_LIMIT_TABLE_NAME` and `RATE_LIMIT_FALLBACK_INSTANCES` (= the
service's max task count) in the task definition. A stack test pins least privilege (no
Get/Put/Delete/Scan/Query).

## Rollback
- Up to Phase 5: point clients back to the API Gateway URLs. API Gateway and its `/ask`
  Lambda stay untouched until 6b, and the agent worker accepts both message shapes, so
  either front door works.
- After 6b: redeploy from the tag taken before 6b.
