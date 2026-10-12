# 001 — Replace API Gateway with a FastAPI service

**Status:** Proposed
**Contract:** the first deliverable after this spec is a hand-written OpenAPI 3.1 contract,
`api/openapi.yaml` ([R-0](research.md)). It describes the existing API plus the deviations below.

## Problem
`POST /query`, `GET /query/{job_id}`, `POST /ask` and `GET /ask/{job_id}` are built from two
API Gateway REST APIs, a Lambda authorizer, and four submit/status Lambdas. The contract exists
only in prose (README, CLAUDE.md, `docs/neptune.md`), the docs contradict each other and the code
(auth caching, sync vs async `/query`), and the submit/status handlers have no tests.
Limits like 50 rps / 100 burst and the 10 s timeout live in CDK settings, so nothing in the app
tests them.

## Goals
1. A committed OpenAPI 3.1 contract that code is generated from and tested against.
2. One FastAPI service on ECS Fargate behind an ALB + WAF that serves both APIs.
3. Every limit API Gateway enforces today is enforced explicitly and has a test (see
   [parity-matrix.md](parity-matrix.md)).
4. All query limits are enforced **server-side** in one shared query service. The HTTP route,
   the query worker and the agent each call that service directly. The agent does **not** loop
   back through `POST /query` over HTTP, so no limit may live only in HTTP middleware, the ALB
   or WAF.
5. The SQS + Lambda workers stay: job tables, queues, DLQs, retries and the agent's
   concurrency cap of 10 are unchanged. The only worker change is that the agent's
   `query_neptune` tool calls the shared service in-process, replacing its HTTP submit/poll
   against `/query`.

## User stories
- As a **researcher with a Synapse PAT**, I submit SPARQL or a question and poll for the result,
  exactly as today, at a new hostname.
- As the **agent worker**, I run SPARQL by calling the shared query service directly with the
  original caller's `user_id` and `source="agent"`. My queries get the same limits and the same
  `sparql_query` audit log as `POST /query`, without an HTTP round trip, a 3 s poll loop, or
  holding the caller's bearer token.
- As an **operator**, I see per-API request counts, errors, latency, throttles and WAF blocks,
  and get alarms when the service is unhealthy.
- As a **developer**, I change the API by editing `api/openapi.yaml` first, and CI tells me if
  code, generated models, test vectors or clients drift from it.

## Functional requirements
- **FR-1 Submit query.** JSON body `{"query": string}`. The query is trimmed, must be non-empty,
  and may be at most 8000 characters after trimming. On success, the service stores the job
  item `{job_id, status: pending, created_at, ttl: created_at + 86400}`, sends the SQS message
  `{job_id, query, source, source_ip, user_agent}`, and returns `202 {job_id, status}`.
  `source` comes from `X-Source`, defaults to `"direct"`, and may be at most 64 characters
  (D-11). The service logs `query_submitted`.
- **FR-2 Query status.** 404 `{"error":"Job not found"}` for unknown ids. 200 returns
  `{job_id, status}`. A `complete` job adds `results` (default `""`) and `content_type`
  (default `application/sparql-results+json`). An `error` job adds `error`
  (default `"Unknown error"`).
- **FR-3 Submit question.** Same as FR-1, but the field is `question`, the maximum is 2000, and
  the stored item also contains `question`. The SQS message is
  `{job_id, question, user_id, source_ip}`, where `user_id` is the authenticated principal.
  **No credential is enqueued** (D-8). The service logs `question_submitted`.
- **FR-4 Ask status.** Same as FR-2, plus `status_detail` when present and `steps` when
  non-empty (at any status). A `complete` job adds `answer` (default `""`).
- **FR-5 Auth.** A port of today's Lambda authorizer, Synapse bearer tokens only (D-4):
  1. `Authorization: Bearer <token>` is checked: `GET /repo/v1/userProfile` gives `ownerId`.
     If that returns 403, the service falls back to `GET /auth/v1/oauth2/userinfo` and uses the
     `userid` claim, never `sub`. It then calls `membershipStatus` for the configured team; a
     400 or 404 response means the caller is not a member.
  2. Each Synapse call times out after 5 s, and the whole auth step after 10 s.
  3. Only successful results are cached, for 300 s, per task.
  4. A denial returns 401 `{"message":"Unauthorized"}`.

  The log events `auth_allow`, `auth_deny` and `oidc_userinfo` are kept.
- **FR-6 Edge behaviour.**
  - CORS allows all origins, the methods `POST, GET, OPTIONS`, and the headers `Content-Type,
    Authorization, X-Source, x-api-key`. Every response, including errors, carries
    `Access-Control-Allow-Origin: *`.
  - `source_ip` is the **rightmost** `X-Forwarded-For` entry (the one the ALB appends), never
    the client-supplied leftmost entry.
  - Throttling and size limits are covered in the parity matrix.
- **FR-7 Shared query service.** The single server-side enforcement point for SPARQL. It does
  two things:
  - **Validate** a query: trim it, then check type, emptiness and the 8000-character limit,
    with the FR-1 error messages.
  - **Run** a query for a principal and source: validate it, charge the global `query` bucket
    and the principal's bucket (both shared with `POST /query`), run it on the Neptune
    **reader** endpoint with SigV4, read-only IAM and a 60 s timeout, and emit the
    `sparql_query` audit event. The event keeps today's fields (`job_id, query, query_length,
    source, source_ip, user_agent, status_code, duration_ms, timestamp`) and adds `principal`.

  Callers:
  - `POST /query` validates and charges the buckets, then enqueues the job.
  - The query worker runs the already-admitted job, so no limit is charged twice.
  - The agent tool runs its queries synchronously with `source="agent"` and the caller's
    `user_id`, after its existing SELECT-only check.

  Limit values are defined once and a test asserts they equal the contract's
  `x-sagebrain-limits`. The buckets are shared by every instance of every caller, so the
  limits hold across ECS tasks and Lambda instances. If the shared store is unreachable, each
  process fails open to its own buckets at `rate / instances` and logs `ratelimit_degraded`;
  a rate check never turns into a 500.
- **FR-8 Health.** `GET /healthz` returns 200 `{"status":"ok"}`. It needs no auth and is not
  rate limited.
- **FR-9 API docs at `/api`.** The service publishes its own contract, publicly with no auth:
  - `GET /api` serves Swagger UI. Its assets are bundled in the image and served by the
    service, not a CDN.
  - `GET /api/openapi.json` serves the committed `api/openapi.yaml` verbatim, except that
    `servers` is set to this deployment's public base URL, so "Try it out" and generated
    clients hit the right host.
  - `GET /api/openapi.yaml` serves the same document as YAML.

  These routes are not charged to the app rate buckets, but WAF per-IP limits still apply.
  "Try it out" works with **Authorize** (Synapse PAT). FastAPI's default `/docs`, `/redoc` and
  `/openapi.json` are disabled so that only the committed contract is published.

## Constraints
- The job table items and the query SQS message keep today's shapes; the workers depend on them.
- Until the `/ask` cutover, the agent worker accepts both the legacy message
  `{job_id, question, authorization}` (from the API Gateway `/ask` Lambda) and the new
  `{job_id, question, user_id, source_ip}`. A legacy job runs as principal `legacy-apigw`
  (D-12); the `authorization` value is never used.

## Thresholds
Every threshold is declared once, in the contract's `info.x-sagebrain-limits`. Where each one is
enforced is in [parity-matrix.md](parity-matrix.md).

## Deviations from current behaviour
| # | Today | New | Why |
|---|---|---|---|
| D-1 | `X-Source` and `User-Agent` are read case-sensitively, so `x-source: agent` is logged as `direct` | Header names are case-insensitive (HTTP semantics) | Correct audit attribution |
| D-2 | A non-string field or a non-object body crashes the handler (API Gateway 502) | 400 `'<field>' must be a string` / `Request body must be a JSON object` | A client error should not be reported as a server error |
| D-3 | A blank `job_id` can't reach the handler (API Gateway answers first) | Blank or over 128 chars returns 400 `Missing job_id`; no `/query/` route (404) | Explicit input bounds |
| D-4 | The authorizer has an `x-api-key` machine-key path (unconfigured in every environment) | Synapse bearer tokens only; `x-api-key` is not a credential | Nothing uses it; the agent no longer needs a machine credential (D-8) |
| D-5 | API Gateway accepts bodies up to 10 MB | 256 KiB returns 413 `{"message":"Request Too Long"}` | The largest valid body is about 8 KB; this narrows the abuse surface |
| D-6 | Throttle is per API only | Plus a per-principal bucket and a WAF per-IP rule | Stops one user starving everyone else |
| D-7 | URLs carry a `/prod` stage prefix on two hostnames | One hostname, no stage prefix | One service |
| D-8 | `/ask` puts the caller's raw `Authorization` header in SQS; the agent re-presents it to `POST /query` over HTTPS and polls every 3 s | `/ask` enqueues `{job_id, question, user_id, source_ip}`; the agent runs its queries in-process through the shared service, attributed to `user_id` with `source: agent` | Limits live server-side; no bearer tokens in queues; no HTTP loopback through the NAT, WAF or rate limiter |
| D-9 | Agent queries go through the query SQS queue and worker (two hops), with a 70 s poll timeout | They run synchronously in the agent worker against the Neptune reader with a 60 s timeout. The agent worker gains read-only Neptune IAM and port 8182 ingress, and loses `NEPTUNE_QUERY_URL` | Fewer moving parts and lower latency |
| D-10 | Bad or non-member bearer returns 401 `{"message":"User is not authorized to access this resource with an explicit deny in an identity-based policy"}` (observed in dev) | Every auth failure returns 401 `{"message":"Unauthorized"}` | One body for clients; no IAM wording leaked |
| D-11 | `X-Source` of any length is accepted and written verbatim into the SQS message and the `sparql_query` audit log | Over 64 characters returns 400 `'X-Source' exceeds maximum length of 64 characters` | A threshold must be enforced, not only documented (constitution III); bounds what callers can write into audit logs |
| D-12 | Agent queries go through `POST /query` with the caller's token, so only the per-API stage throttle applies | Legacy agent jobs (no caller id) are all charged to one principal, `legacy-apigw`, with its own shared bucket (`legacy_agent_*`: 25 rps / 50 burst). New jobs are charged to the caller's user bucket (5 / 20) | A shared 5 / 20 bucket would throttle every legacy agent job together (10 concurrent workers); this keeps the old headroom until the `/ask` cutover retires the legacy shape |
| D-13 | A transient authorizer error (Synapse down) returns 500 | 500 until API Gateway is decommissioned, then 503 with `Retry-After` | Parity during cutover; afterwards a Synapse outage shouldn't look like a service bug |

## Known issues
- **F8 (resolved by D-8):** today, the agent re-presents the caller's `Authorization` header to
  `/query`, so it depends on that credential still being valid and on the authorizer accepting it.
- **F3:** today, when Synapse is down, the authorizer raises an exception. API Gateway documents
  this as a 500 (`AUTHORIZER_FAILURE`), not 401. See D-13 and [research.md](research.md).

## Out of scope
- Changing the worker Lambdas beyond the agent tool change in D-8/D-9, the job item size limits,
  or Bedrock access.
- A query-side "latest snapshot per portal" default (this belongs to the pipeline follow-up).

## Success criteria
- The unit, contract and characterization tests pass.
- A parity probe run against API Gateway and the new service shows 0 failures and 0
  unexplained differences in dev, stage and prod before cutover.
- A burst of 300 requests to `/query` gets about 100 accepted and the rest 429.
- In each environment, the API Gateway `Count` stays at about 0 for 7 days before
  decommissioning.
