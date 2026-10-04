# 001 — API Gateway → FastAPI threshold parity matrix

This is a living document. Every row needs a server-side enforcement point and a test before
cutover (constitution, articles III and V). Fill in the Status column as phases land.

**Scope** says which callers the limit covers:
- **all**: `sagebrain_core` enforces it, so it covers `POST /query`, the query worker **and** the
  agent's in-process calls.
- **http**: the FastAPI layer enforces it, for external HTTP callers only. Agent-originated
  SPARQL never passes through these.
- **edge**: WAF/ALB enforce it. This is defence in depth only and never the sole enforcement
  point.

| # | API Gateway today (source) | Value | Scope | New enforcement | Test | Status |
|---|---|---|---|---|---|---|
| 1 | Stage throttle (`deploy_options` in both API stacks) | 50 rps steady, 100 burst, per API | all | `sagebrain_core` rate limiter: Valkey token bucket `{rl:query}:global`, charged by `POST /query` **and** by `run_query` for agent calls; `{rl:ask}:global` charged by `POST /ask`. HTTP charges happen before auth. | fakeredis frozen clock: first 100 pass, the 101st is rejected; refill at 50/s; `query` and `ask` buckets are independent; agent calls drain the `query` bucket; `tools/throttle_test.py` | Phase 2 (Valkey). Bucket logic, keys and admission done in 1a with `LocalLimiter`: `tests/unit/core/test_ratelimit.py` |
| 1e | (new) per-IP flood control | config `waf_ip_rate_limit_5min` | edge | WAF rate-based rule | CDK assertion | Phase 3 |
| 2 | (new) per-principal limit | config `rate_limits.user_*`, `machine_*` | all | `{rl:<api>}:u:<principal>`. Agent calls are charged to the original caller's `user_id`. | Users are isolated from each other; an agent job's queries count against its caller; machine limits are separate | Bucket logic done in 1a (`test_admit_per_principal_bucket`, `test_admit_machine_principal_uses_machine_limits`); Valkey in Phase 2 |
| 3 | (new) limiter outage | — | all | Fail open to a per-process local bucket (rate ÷ instances), log `ratelimit_degraded` | Redis-down test | Phase 2 |
| 4 | Lambda integration timeout | 10 s | http | `RequestTimeoutMiddleware` returns 504 `{"message":"Endpoint request timed out"}`; boto3 connect 2 s / read 5 s | A slow fake store gives 504 with CORS | Phase 1 |
| 4q | Neptune query timeout (`query.py` `timeout=60`; agent poll timeout 70 s) | 60 s | all | `run_query`'s HTTP timeout to Neptune, from `sagebrain_core.limits` | Fake Neptune that hangs → timeout error, `sparql_query` logged with an error status | **Done (1a)**: `tests/unit/core/test_neptune.py::test_uses_spec_timeout` |
| 5 | Authorizer Lambda timeout; Synapse calls | 10 s; 5 s each | http | Auth step `asyncio.timeout(10)`; httpx timeout 5 s | A Synapse timeout gives the transient status and is not cached | Phase 1 |
| 6 | Authorizer caching (`results_cache_ttl=0` on API Gateway; in-process cache in the Lambda) | 300 s, successes only | http | Per-task TTL cache keyed by sha256(token) | Ported from `tests/unit/test_authorizer.py` | Phase 1 |
| 7 | Identity source `Authorization` header (missing gives 401 before the authorizer runs) | — | http | Auth dependency (D-4: `x-api-key` alone is now enough) | No credentials gives 401 + ACAO | Phase 1 |
| 8 | `ACCESS_DENIED` remapped to 401, `UNAUTHORIZED` with CORS `*` | 401 `{"message":…}` | http | Auth dependency + CORS middleware | Non-member / bad key with a valid bearer gives 401 + ACAO; `cases.yaml` gateway cases | Phase 1 |
| 9 | Transient authorizer error | ~500 (verify, F3) | http | `AUTH_TRANSIENT_STATUS` setting | A unit test asserts the configured status | Phase 0 probe → Phase 1 |
| 10 | CORS preflight (`default_cors_preflight_options`) | `*`; POST/GET/OPTIONS; Content-Type, Authorization, X-Source, x-api-key | http | `CORSMiddleware` | Preflight test; every error response carries ACAO | Phase 1 |
| 11 | Payload limit | 10 MB (API Gateway hard limit) | http | `MAX_BODY_BYTES` = 256 KiB returns 413 (D-5) | Tests with Content-Length and with a chunked body | Phase 1 |
| 12 | Access logs (`json_with_standard_fields`) | caller, httpMethod, ip, protocol, requestTime, resourcePath, responseLength, status, user | http | `AccessLogMiddleware` JSON line with the same fields plus requestId, api, durationMs | caplog test checks every key | Phase 1 |
| 12a | SPARQL audit log (`query.py` `_log_query`) | `sparql_query` event per query | all | `run_query` emits it for every caller; `source` is `direct` or `agent`, and `principal` is added | Fields test, run for both the HTTP path and the agent path | **Done (1a)**: `test_audit_event_fields` (the HTTP path via the query worker; the agent path lands in 1c) |
| 13 | Access log retention | 1 month | — | ECS container log group `RetentionDays.ONE_MONTH` | CDK `RetentionInDays: 30` | Phase 3 |
| 14 | Execution logs | ERROR | http | Unhandled exception handler logs ERROR with the stack trace and returns 500 `{"message":"Internal server error"}` | 500 path test | Phase 1 |
| 15 | Stage metrics | Count, 4XX, 5XX, Latency per API | http | ALB per-target-group metrics, EMF `SageBrain/Api`, Container Insights | Dashboard CDK test; EMF test | Phase 4 |
| 16 | Query length | 8000 after trim | all | `sagebrain_core.validate_query`, called by `POST /query` and by `run_query` | `cases.yaml` `query_submit_*`; an agent-path over-length test | **Pinned (Phase 0)** for HTTP; `validate_query` done (1a): `tests/unit/core/test_validation.py` |
| 16r | Read-only queries | Neptune IAM read actions only; the agent also requires SELECT-only (`_safe_sparql`) | all | `run_query` uses only the reader endpoint and read-only IAM; the agent tool keeps `_safe_sparql` | `tests/unit/test_safe_sparql.py` + CDK IAM assertions | Existing |
| 17 | Question length | 2000 after trim | http | Router + spec `maxLength` | `cases.yaml` `ask_submit_*` | **Pinned (Phase 0)** |
| 18 | Job item and SQS message shapes; job TTL | TTL 86400 s | — | `jobs.py` | `test_*_job_item_and_message_shape`; `ask_submit_ok` (D-8) | **Pinned (Phase 0)** |
| 19 | Workers: DLQ max receive 2, visibility 90 / 360 s, agent reserved concurrency 10, timeouts 75 / 300 s | — | — | Unchanged stacks | Existing stack tests + `test_logical_ids_unchanged` | Phase 3 |
| 20 | (new) TLS | — | edge | ALB 443 + ACM, `RECOMMENDED_TLS`, 80→443 redirect | CDK assertions | Phase 3 |

**Pinned** means the current Lambda behaviour is locked by `tests/unit/test_job_api_handlers.py`,
and the FastAPI implementation must pass the same `cases.yaml` vectors.

**Single source of truth for values:** `sagebrain_core.limits`. A contract test (Phase 1) asserts
it equals `api/openapi.yaml` `info.x-sagebrain-limits`, so the spec, the code and the infra
config can't disagree.
