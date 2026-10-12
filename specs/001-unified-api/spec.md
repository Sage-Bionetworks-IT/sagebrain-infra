# 001 — One API Gateway endpoint for `/query` and `/ask`

**Status:** Proposed
**Contract:** the first deliverable after this spec is a hand-written OpenAPI 3.1 contract,
`api/openapi.yaml` ([R-0](research.md)). It describes the existing API plus the deviations below.

## Problem
`POST /query`, `GET /query/{job_id}`, `POST /ask` and `GET /ask/{job_id}` are built from two
API Gateway REST APIs, each with its own hostname and `/prod` stage prefix, two copies of the
Lambda authorizer, and four submit/status Lambdas. The contract exists only in prose (README,
CLAUDE.md, `docs/neptune.md`), the docs contradict each other and the code (auth caching, sync
vs async `/query`), and the submit/status handlers have no tests. Limits like 50 rps / 100
burst and the 10 s timeout live in CDK settings, so nothing tests them.

The agent's `query_neptune` tool reaches Neptune by calling `POST /query` over HTTPS with the
caller's bearer token, which `/ask` puts in the SQS message (F8), then polls every 3 s.

## Goals
1. A committed OpenAPI 3.1 contract that code is tested against, published at `/api`.
2. **One** API Gateway REST API, on one hostname, serving both `/query` and `/ask`.
3. Every limit API Gateway enforces today is enforced explicitly and has a test (see
   [parity-matrix.md](parity-matrix.md)).
4. All SPARQL runs through one shared query service, called **in-process** by the `/query`
   submit Lambda, the query worker and the agent worker. The agent does **not** loop back
   through `POST /query` over HTTP, and no bearer token is put in SQS.
5. The SQS + Lambda workers stay: job tables, queues, DLQs, retries and the agent's
   concurrency cap of 10 are unchanged. The only worker change is the agent's `query_neptune`
   tool calling the shared service in-process.

Not a goal: moving off API Gateway. FastAPI on Fargate was considered and deferred
([R-1](research.md)).

## Design target
**100 concurrent users**, each with one job in flight, polling status every 2 s (the
`Retry-After` hint, D-15). That is about 50 status requests per second at the worst case.
Today the prod team gate has 9 members and dev has 101. Beyond this target, see the scaling
path in [plan.md](plan.md); thousands of users is a future spec, after spec 002 opens access
beyond the team.

## User stories
- As a **researcher with a Synapse PAT**, I submit SPARQL or a question and poll for the
  result, exactly as today, at one base URL for both.
- As a **researcher or developer**, I open `/api` in a browser to read the contract and try
  requests with my PAT.
- As the **agent worker**, I run SPARQL by calling the shared query service directly with the
  original caller's `user_id` and `source="agent"`. My queries get the same validation, timeout
  and `sparql_query` audit log as `POST /query`, without an HTTP round trip, a 3 s poll loop,
  or holding the caller's bearer token.
- As an **operator**, I see request counts, errors, latency and throttles for `/query` and
  `/ask` separately, even though they share one API.
- As a **developer**, I change the API by editing `api/openapi.yaml` first, and CI tells me if
  the routes, handlers or test vectors drift from it.

## Functional requirements
- **FR-1 Submit query.** JSON body `{"query": string}`. The submit Lambda validates it with
  the shared query service (FR-7): trimmed, non-empty, at most 8000 characters after trimming.
  On success it stores the job item `{job_id, status: pending, created_at, ttl: created_at +
  86400}`, sends the SQS message `{job_id, query, source, source_ip, user_agent}`, and returns
  `202 {job_id, status}`. `source` comes from `X-Source`, defaults to `"direct"`, and may be at
  most 64 characters (D-11). It logs `query_submitted`.
- **FR-2 Query status.** 404 `{"error":"Job not found"}` for unknown ids. 200 returns
  `{job_id, status}`. A `complete` job adds `results` (default `""`) and `content_type`
  (default `application/sparql-results+json`). An `error` job adds `error`
  (default `"Unknown error"`). A `pending` or `running` job's response carries
  `Retry-After: 2`, the minimum poll interval (D-15).
- **FR-3 Submit question.** Same as FR-1, but the field is `question`, the maximum is 2000, and
  the stored item also contains `question`. The SQS message is
  `{job_id, question, user_id, source_ip}`, where `user_id` is the authorizer's
  `context.user_id`. **No credential is enqueued** (D-8). It logs `question_submitted`.
- **FR-4 Ask status.** Same as FR-2, plus `status_detail` when present and `steps` when
  non-empty (at any status). A `complete` job adds `answer` (default `""`).
- **FR-5 Auth.** Today's Lambda REQUEST authorizer, attached to every `/query` and `/ask`
  method, Synapse bearer tokens only (D-4):
  1. `Authorization: Bearer <token>` is checked: `GET /repo/v1/userProfile` gives `ownerId`.
     If that returns 403, it falls back to `GET /auth/v1/oauth2/userinfo` and uses the
     `userid` claim, never `sub`. It then calls `membershipStatus` for the configured team; a
     400 or 404 response means the caller is not a member.
  2. Each Synapse call times out after 5 s; the authorizer Lambda times out after 10 s.
  3. API Gateway's authorizer cache stays off (TTL 0). Only successful results are cached,
     for 300 s, inside the authorizer Lambda.
  4. Every denial returns 401 `{"message":"Unauthorized"}` (D-10). An authorizer failure
     returns 503 with `Retry-After` (D-13).

  The log events `auth_allow`, `auth_deny` and `oidc_userinfo` are kept.
- **FR-6 Edge behaviour.**
  - CORS allows all origins, the methods `POST, GET, OPTIONS`, and the headers `Content-Type,
    Authorization, X-Source, x-api-key`. Preflight stays a 204 from API Gateway. Every
    response, including gateway-generated errors, carries `Access-Control-Allow-Origin: *`.
  - `source_ip` is API Gateway's `requestContext.identity.sourceIp`, never a client-supplied
    header.
  - Unknown paths and methods return 404 `{"message":"Not Found"}` (D-3).
  - Bodies over 256 KiB return 413 `{"message":"Request Too Long"}` (D-5).
  - Throttling is covered in the parity matrix.
- **FR-7 Shared query service.** The single server-side enforcement point for SPARQL. It does
  two things:
  - **Validate** a query: trim it, then check type, emptiness and the 8000-character limit,
    with the FR-1 error messages.
  - **Run** a query for a principal and source: validate it, run it on the Neptune **reader**
    endpoint with SigV4, read-only IAM and a 60 s timeout, and emit the `sparql_query` audit
    event. The event keeps today's fields (`job_id, query, query_length, source, source_ip,
    user_agent, status_code, duration_ms, timestamp`) and adds `principal`.

  A result larger than 350 KB (350000 bytes, `max_result_bytes`) is not stored: the job is
  marked `error` with `"Result exceeds 350 KB; add LIMIT or narrow the query"` (D-16).

  Callers:
  - The `POST /query` submit Lambda validates, then enqueues the job.
  - The query worker runs the already-admitted job, at most 20 at once (D-14).
  - The agent tool runs its queries synchronously with `source="agent"` and the caller's
    `user_id`, after its existing SELECT-only check.

  Rate limits are not in the service (R-5): HTTP callers are throttled by API Gateway, query
  jobs are bounded by the query worker's reserved concurrency of 20 (D-14), and agent queries
  by the agent worker's reserved concurrency of 10. Every query also has the 60 s timeout. Limit values are defined once and a test asserts they equal the contract's
  `x-sagebrain-limits`.
- **FR-8 API docs at `/api`.** Public, no authorizer, served by a small docs Lambda:
  - `GET /api` serves Swagger UI. Its assets are bundled in the Lambda, not loaded from a CDN.
  - `GET /api/openapi.json` serves the committed `api/openapi.yaml` as JSON, except that
    `servers` is set to this deployment's stage URL
    (`https://<id>.execute-api.<region>.amazonaws.com/prod`), so "Try it out" and generated
    clients hit the right host.
  - `GET /api/openapi.yaml` serves the same document as YAML.

  These routes get the same stage throttle as every other method. "Try it out" works with
  **Authorize** (Synapse PAT).
- **FR-9 One API.** One REST API, stage `prod`, in its own edge stack, with the routes
  `POST /query`, `GET /query/{job_id}`, `POST /ask`, `GET /ask/{job_id}`, `GET /api`,
  `GET /api/openapi.json` and `GET /api/openapi.yaml`. Method-level metrics are on, so
  `/query` and `/ask` are separable in CloudWatch.

## Constraints
- The job table items and the query SQS message keep today's shapes; the workers depend on them.
- Until the old `/ask` API is deleted, the agent worker accepts both the legacy message
  `{job_id, question, authorization}` and the new `{job_id, question, user_id, source_ip}`.
  A legacy job runs as principal `legacy-apigw`; the `authorization` value is never used.

## Thresholds
Every threshold is declared once, in the contract's `info.x-sagebrain-limits`. Where each one is
enforced is in [parity-matrix.md](parity-matrix.md).

## Deviations from current behaviour
Numbers match earlier drafts of this spec; D-6 and D-12 (per-principal rate buckets) were
retired with the move away from FastAPI ([R-5](research.md)).

| # | Today | New | Why |
|---|---|---|---|
| D-1 | `X-Source` and `User-Agent` are read case-sensitively, so `x-source: agent` is logged as `direct` | Header names are case-insensitive (HTTP semantics) | Correct audit attribution |
| D-2 | A non-string field or a non-object body crashes the handler (API Gateway 502) | 400 `'<field>' must be a string` / `Request body must be a JSON object` | A client error should not be reported as a server error |
| D-3 | Unknown paths return 403 with a SigV4 parse error and no CORS; a blank `job_id` can't reach the handler | Gateway response maps `MISSING_AUTHENTICATION_TOKEN` to 404 `{"message":"Not Found"}` + ACAO. A `job_id` over 128 chars returns 400 `Missing job_id` | Explicit input bounds; an unknown path is not an auth error |
| D-4 | The authorizer has an `x-api-key` machine-key path (unconfigured in every environment) | Synapse bearer tokens only; `x-api-key` is not a credential | Nothing uses it; the agent no longer needs a machine credential (D-8) |
| D-5 | Bodies up to 10 MB are accepted | Handlers return 413 `{"message":"Request Too Long"}` above 256 KiB. API Gateway's own 10 MB cap can't be lowered, so the check is in the handler | The largest valid body is about 8 KB; this narrows the abuse surface |
| D-7 | Two hostnames, one per API, each with a `/prod` stage prefix | One hostname for both. The `/prod` prefix stays until a custom domain is added (out of scope) | One endpoint |
| D-8 | `/ask` puts the caller's raw `Authorization` header in SQS; the agent re-presents it to `POST /query` over HTTPS and polls every 3 s | `/ask` enqueues `{job_id, question, user_id, source_ip}`; the agent runs its queries in-process through the shared service, attributed to `user_id` with `source: agent` | No bearer tokens in queues; no HTTP loopback |
| D-9 | Agent queries go through the query SQS queue and worker (two hops), with a 70 s poll timeout | They run synchronously in the agent worker against the Neptune reader with a 60 s timeout. The agent worker gains read-only Neptune IAM and port 8182 ingress, and loses `NEPTUNE_QUERY_URL` | Fewer moving parts and lower latency |
| D-10 | Bad or non-member bearer returns 401 `{"message":"User is not authorized to access this resource with an explicit deny in an identity-based policy"}` (observed in dev) | Every auth failure returns 401 `{"message":"Unauthorized"}` | One body for clients; no IAM wording leaked |
| D-11 | `X-Source` of any length is accepted and written verbatim into the SQS message and the `sparql_query` audit log | Over 64 characters returns 400 `'X-Source' exceeds maximum length of 64 characters` | A threshold must be enforced, not only documented (constitution III); bounds what callers can write into audit logs |
| D-13 | A transient authorizer error (Synapse down) returns 500 (`AUTHORIZER_FAILURE`) | Gateway response maps `AUTHORIZER_FAILURE` to 503 with `Retry-After` | A Synapse outage shouldn't look like a service bug |
| D-14 | The query worker has no concurrency cap | Reserved concurrency of 20; extra jobs wait in SQS | Bounds load on Neptune, and stops the worker from draining the account's Lambda concurrency, which the authorizer and API Lambdas share. Names the query path's rate bound (constitution V) |
| D-15 | Status responses don't say how often to poll | `pending` / `running` responses carry `Retry-After: 2` | Polling is most of the traffic; this sizes it at the design target |
| D-16 | A result over DynamoDB's 400 KB item limit fails the worker's write, though Neptune succeeded (`src/lambda/query.py`) | Results over 350 KB mark the job `error` with an actionable message | A limit that exists only as a DynamoDB error breaks constitution III. Offloading big results to S3 is a separate spec |
| D-17 | Status methods share the 50 / 100 stage throttle | `GET /query/{job_id}` and `GET /ask/{job_id}` get 100 rps / 200 burst; the POSTs stay 50 / 100 | At the design target, worst-case polling is 50 rps, exactly the old limit |

## Known issues
- **F8 (resolved by D-8):** today, the agent re-presents the caller's `Authorization` header to
  `/query`, so it depends on that credential still being valid and on the authorizer accepting it.
- **F3 (resolved by D-13):** today, when Synapse is down, the authorizer raises an exception,
  which API Gateway documents as a 500 (`AUTHORIZER_FAILURE`), not 401.

## Out of scope
- A custom domain and certificate. API Gateway's `execute-api` HTTPS URL is the endpoint for
  now; a domain is a follow-up (clients would move URL once more).
- Per-principal rate limits and a shared rate-limit store ([R-5](research.md)).
- WAF ([R-9](research.md)).
- FastAPI, Fargate, an ALB ([R-1](research.md)).
- Changing the worker Lambdas beyond the agent tool change in D-8/D-9, the job item size limits,
  or Bedrock access.
- A query-side "latest snapshot per portal" default (this belongs to the pipeline follow-up).

## Success criteria
- The unit, contract and characterization tests pass.
- A parity probe run against the old APIs and the new one shows 0 failures and 0 unexplained
  differences in dev, stage and prod before cutover.
- A burst of 300 requests to `POST /query` gets about 100 accepted and the rest 429.
- A load probe in dev at the design target passes: 100 simulated users each submit a cheap
  `LIMIT 1` query and poll every 2 s for 10 minutes. The status methods return 0 429s, and the
  query queue's oldest message stays under 5 minutes. The cheap query keeps the probe a test of
  the API, not of Neptune.
- `/api` loads in a browser, and "Try it out" with a PAT returns 202 for `POST /query`.
- In each environment, the old APIs' `Count` stays at about 0 for 7 days before they are
  deleted.
