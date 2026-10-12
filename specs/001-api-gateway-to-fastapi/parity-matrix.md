# 001 — API Gateway → FastAPI threshold parity matrix

Every row needs a server-side enforcement point and a test before cutover (constitution,
articles III and V). The tests themselves land with the code.

**Scope** says which callers the limit covers:
- **all**: the shared query service enforces it, so it covers `POST /query`, the query worker
  **and** the agent's in-process calls.
- **http**: the FastAPI layer enforces it, for external HTTP callers only. Agent-originated
  SPARQL never passes through these.
- **edge**: WAF/ALB enforce it. This is defence in depth only and never the sole enforcement
  point.

| # | API Gateway today | Value | Scope | New enforcement |
|---|---|---|---|---|
| 1 | Stage throttle, per API | 50 rps steady, 100 burst | all | Global `query` and `ask` buckets in the shared rate-limit store. `POST /query` **and** the agent's queries charge `query`; `POST /ask` charges `ask`. HTTP charges happen before auth |
| 1e | (new) per-IP flood control | per-env config | edge | WAF rate-based rule |
| 2 | (new) per-principal limit | user 5 rps / 20 burst; `legacy-apigw` 25 / 50 (D-12) | all | Per-principal bucket per API in the shared store. Agent queries are charged to the original caller |
| 3 | (new) limiter outage | — | all | Fail open to a per-process bucket at rate ÷ instances, log `ratelimit_degraded`; never a 500 |
| 4 | Lambda integration timeout | 10 s | http | Request timeout returns 504 `{"message":"Endpoint request timed out"}`; AWS client timeouts below it |
| 4q | Neptune query timeout (query worker 60 s; agent poll 70 s) | 60 s | all | The shared service's Neptune call, for every caller |
| 5 | Authorizer Lambda timeout; Synapse calls | 10 s; 5 s each | http | Auth step timeout; per-call Synapse timeout |
| 6 | Authorizer caching (off in API Gateway; in-process cache in the Lambda) | 300 s, successes only | http | Per-task cache keyed by a hash of the token |
| 7 | `Authorization` identity source (missing gives 401 before the authorizer runs) | — | http | Auth dependency; `x-api-key` is not a credential (D-4) |
| 8 | `ACCESS_DENIED` remapped to 401, `UNAUTHORIZED` with CORS `*` | 401 `{"message":"Unauthorized"}` | http | Auth dependency + CORS middleware (D-10) |
| 9 | Transient authorizer error | 500 (F3) | http | Configurable status: 500 until decommission, then 503 (D-13) |
| 10 | CORS preflight | `*`; POST/GET/OPTIONS; Content-Type, Authorization, X-Source, x-api-key | http | CORS middleware; every error response carries ACAO |
| 11 | Payload limit | 10 MB → 256 KiB (D-5) | http | Body limit returns 413, with Content-Length and chunked bodies |
| 12 | Access logs | caller, httpMethod, ip, protocol, requestTime, resourcePath, responseLength, status, user | http | JSON access-log line with the same fields plus requestId, api, durationMs |
| 12a | SPARQL audit log | `sparql_query` per query | all | The shared service emits it for every caller; `source` is `direct` or `agent`, plus `principal` |
| 13 | Access log retention | 1 month | — | Container log group retention |
| 14 | Execution logs | ERROR | http | Unhandled errors log at ERROR with the stack trace and return 500 `{"message":"Internal server error"}` |
| 15 | Stage metrics | Count, 4XX, 5XX, Latency per API | http | ALB per-target-group metrics, embedded metrics, Container Insights |
| 16 | Query length | 8000 after trim | all | Shared service validation, on the HTTP path and the agent path |
| 16r | Read-only queries | Neptune IAM read actions only; the agent also requires SELECT-only | all | Reader endpoint and read-only IAM for every caller; the agent keeps its SELECT-only check |
| 17 | Question length | 2000 after trim | http | `/ask` validation |
| 17s | (new, D-11) `X-Source` length | 64 | http | `POST /query` validation; the agent passes `source="agent"` in-process |
| 18 | Job item and SQS message shapes; job TTL | TTL 86400 s | — | Unchanged shapes (spec, Constraints) |
| 19 | Workers: DLQ max receive 2, visibility 90 / 360 s, agent reserved concurrency 10, timeouts 75 / 300 s | — | — | Unchanged stacks |
| 20 | (new) TLS | — | edge | ALB 443 + ACM, 80 → 443 redirect (hostname: plan, Open decisions) |

**Single source of truth for values:** the contract's `info.x-sagebrain-limits`. A test asserts
the shared service's limit values equal it, so the spec, the code and the infra config can't
disagree.
