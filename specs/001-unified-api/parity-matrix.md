# 001 — Old APIs → unified API threshold parity matrix

Every row needs an enforcement point and a test before cutover (constitution, articles III
and V). The tests themselves land with the code.

**Scope** says which callers the limit covers:
- **all**: the shared query service enforces it, so it covers `POST /query`, the query worker
  **and** the agent's in-process calls.
- **http**: API Gateway or the edge-stack handlers enforce it, for external HTTP callers only.
  Agent-originated SPARQL never passes through these.
- **agent**: the agent worker's own bounds.

For infrastructure settings, the test is a CDK assertion on the synthesized template that
reads the value from `x-sagebrain-limits`.

| # | Old APIs today | Value | Scope | New enforcement |
|---|---|---|---|---|
| 1 | Stage throttle, per API | 50 rps steady, 100 burst | http | Stage throttle on the one API. It applies per method, so each of `POST /query`, `POST /ask` and the status/docs routes keeps 50 / 100. CDK assertion + burst probe |
| 1a | (no equivalent: agent queries went through `POST /query`) | 10 concurrent, 60 s each | agent | Agent worker reserved concurrency (unchanged) + the shared service's Neptune timeout (row 4q) |
| 4 | Lambda integration timeout | 10 s | http | Integration timeout on every method; AWS client timeouts in the handlers below it. CDK assertion |
| 4q | Neptune query timeout (query worker 60 s; agent poll 70 s) | 60 s | all | The shared service's Neptune call, for every caller |
| 5 | Authorizer Lambda timeout; Synapse calls | 10 s; 5 s each | http | Authorizer Lambda timeout (CDK assertion); per-call Synapse timeout (unit test) |
| 6 | Authorizer caching (off in API Gateway; in-process cache in the Lambda) | 300 s, successes only | http | Unchanged: API Gateway TTL 0, in-Lambda cache keyed by a hash of the token |
| 7 | `Authorization` identity source (missing gives 401 before the authorizer runs) | — | http | Unchanged; `x-api-key` is not a credential (D-4) |
| 8 | `ACCESS_DENIED` remapped to 401, `UNAUTHORIZED` with CORS `*` | 401 `{"message":"Unauthorized"}` | http | Gateway responses with a fixed body template (D-10) |
| 9 | Transient authorizer error | 500 (F3) | http | `AUTHORIZER_FAILURE` gateway response → 503 + `Retry-After` (D-13) |
| 10 | CORS preflight | `*`; POST/GET/OPTIONS; Content-Type, Authorization, X-Source, x-api-key | http | Unchanged preflight; `DEFAULT_4XX` / `DEFAULT_5XX` gateway responses carry ACAO; handlers set ACAO |
| 11 | Payload limit | 10 MB → 256 KiB (D-5) | http | Handler check returns 413; API Gateway's 10 MB cap stays above it |
| 12 | Access logs | caller, httpMethod, ip, protocol, requestTime, resourcePath, responseLength, status, user | http | One access-log group for the one stage, same fields plus requestId; `resourcePath` separates `/query` from `/ask` |
| 12a | SPARQL audit log | `sparql_query` per query | all | The shared service emits it for every caller; `source` is `direct` or `agent`, plus `principal` |
| 13 | Access log retention | 1 month | — | Unchanged |
| 14 | Execution logs | ERROR | http | Unchanged |
| 15 | Stage metrics | Count, 4XX, 5XX, Latency per API | http | Method-level metrics on the one stage, so `/query` and `/ask` stay separable |
| 16 | Query length | 8000 after trim | all | Shared service validation, on the HTTP path and the agent path |
| 16r | Read-only queries | Neptune IAM read actions only; the agent also requires SELECT-only | all | Reader endpoint and read-only IAM for every caller; the agent keeps its SELECT-only check |
| 17 | Question length | 2000 after trim | http | `/ask` submit validation |
| 17s | (new, D-11) `X-Source` length | 64 | http | `POST /query` submit validation; the agent passes `source="agent"` in-process |
| 18 | Job item and SQS message shapes; job TTL | TTL 86400 s | — | Unchanged shapes (spec, Constraints) |
| 19 | Workers: DLQ max receive 2, visibility 90 / 360 s, agent reserved concurrency 10, timeouts 75 / 300 s | — | — | Unchanged stacks |

Rows 2 and 3 of the earlier draft (per-principal buckets, rate-limiter outage) and 1e and 20
(WAF per-IP rule, ALB TLS) were retired with [R-1](research.md), [R-5](research.md) and
[R-9](research.md). TLS comes from API Gateway.

**Single source of truth for values:** the contract's `info.x-sagebrain-limits`. A test asserts
the shared service's limit values and the CDK settings equal it, so the spec, the code and the
infra config can't disagree.
