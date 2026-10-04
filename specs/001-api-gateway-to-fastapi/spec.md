# 001 — Replace API Gateway with a FastAPI service

**Status:** Accepted (Phase 0 implemented)
**Contract:** [`api/openapi.yaml`](../../api/openapi.yaml) — new; describes the existing API plus the deviations below.

## Problem
`POST /query`, `GET /query/{job_id}`, `POST /ask` and `GET /ask/{job_id}` are built from two
API Gateway REST APIs, a Lambda authorizer, and four submit/status Lambdas. The contract exists
only in prose (README, CLAUDE.md, `docs/neptune.md`), the docs contradict each other and the code
(team ID, auth caching, sync vs async `/query`), and the submit/status handlers had no tests.
Limits like 50 rps / 100 burst and the 10 s timeout live in CDK settings, so nothing in the app
tests them.

## Goals
1. A committed OpenAPI 3.1 contract that code is generated from and tested against.
2. One FastAPI service on ECS Fargate behind an ALB + WAF that serves both APIs.
3. Every limit API Gateway enforces today is enforced explicitly and has a test (see
   [parity-matrix.md](parity-matrix.md)).
4. All query limits are enforced **server-side** in one shared query service
   (`sagebrain_core`). The HTTP route, the query worker and the agent each call that service
   directly. The agent does **not** loop back through `POST /query` over HTTP, so no limit
   may live only in HTTP middleware, the ALB or WAF.
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
- As a **machine client**, I authenticate with `x-api-key` alone. I no longer need the dummy
  `Authorization: ApiKey` header.
- As an **operator**, I see per-API request counts, errors, latency, throttles and WAF blocks,
  and get alarms when the service is unhealthy.
- As a **developer**, I change the API by editing `api/openapi.yaml` first, and CI tells me if
  code, generated models, test vectors or clients drift from it.

## Functional requirements
- **FR-1 Submit query.** JSON body `{"query": string}`. The query is trimmed, must be non-empty,
  and may be at most 8000 characters after trimming. On success, the service stores the DynamoDB
  item `{job_id, status: pending, created_at, ttl: created_at + 86400}`, sends the SQS message
  `{job_id, query, source, source_ip, user_agent}`, and returns `202 {job_id, status}`.
  `source` comes from `X-Source`, defaults to `"direct"`, and may be at most 64 characters
  (D-11). The service logs `query_submitted`.
  *Tests:* `cases.yaml` `query_submit_*`, `test_query_job_item_and_message_shape`.
- **FR-2 Query status.** 404 `{"error":"Job not found"}` for unknown ids. 200 returns
  `{job_id, status}`. A `complete` job adds `results` (default `""`) and `content_type`
  (default `application/sparql-results+json`). An `error` job adds `error`
  (default `"Unknown error"`). *Tests:* `query_status_*`.
- **FR-3 Submit question.** Same as FR-1, but the field is `question`, the maximum is 2000, and
  the stored item also contains `question`. The SQS message is
  `{job_id, question, user_id, source_ip}`, where `user_id` is the authenticated principal.
  **No credential is enqueued** (D-8). The service logs `question_submitted`.
  *Tests:* `ask_submit_*`, `test_ask_job_item_and_message_shape`.
- **FR-4 Ask status.** Same as FR-2, plus `status_detail` when present and `steps` when
  non-empty (at any status). A `complete` job adds `answer` (default `""`). *Tests:* `ask_status_*`.
- **FR-5 Auth.** This is a port of `src/lambda_authorizer/authorizer.py`:
  1. If `x-api-key` is present, it must equal the machine key (constant-time compare). The
     principal is `machine`. A wrong key is rejected even if a valid bearer token is also sent.
  2. Otherwise `Authorization: Bearer <token>` is checked: `GET /repo/v1/userProfile` gives
     `ownerId`. If that returns 403, the service falls back to `GET /auth/v1/oauth2/userinfo`
     and uses the `userid` claim, never `sub`. It then calls `membershipStatus` for the
     configured team; a 400 or 404 response means the caller is not a member.
  3. Each Synapse call times out after 5 s, and the whole auth step after 10 s.
  4. Only successful results are cached, for 300 s, per task.
  5. A denial returns 401 `{"message":"Unauthorized"}`.

  The log events `auth_allow`, `auth_deny` and `oidc_userinfo` are kept.
- **FR-6 Edge behaviour.**
  - CORS allows all origins, the methods `POST, GET, OPTIONS`, and the headers `Content-Type,
    Authorization, X-Source, x-api-key`. Every response, including errors, carries
    `Access-Control-Allow-Origin: *`.
  - Throttling and size limits are covered in the parity matrix.
- **FR-7 Shared query service (`sagebrain_core`).** This is the single server-side
  enforcement point for SPARQL. It has two entry points:
  - `validate_query(raw) -> str`. It trims the input, then checks type, emptiness and the
    8000-character limit, using the FR-1 error messages.
  - `run_query(query, *, principal, source, source_ip, user_agent, job_id=None)`. It calls
    `validate_query`, then charges the global `query` bucket and the principal's per-user
    bucket (both shared with `POST /query`). It then runs the query on the Neptune **reader**
    endpoint with SigV4, the read-only IAM actions and a 60 s timeout, and emits the
    `sparql_query` audit event with the same fields as today, now including `principal`.

  Callers:
  - `POST /query` calls `validate_query` plus the rate check, then enqueues the job.
  - The query worker calls `run_query`.
  - The agent tool calls `run_query(source="agent", principal=<caller user_id>)`
    synchronously, after its own SELECT-only check (`_safe_sparql`).

  Limit values come from one module (`sagebrain_core.limits`). A test asserts they equal the
  spec's `x-sagebrain-limits`.
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
  "Try it out" works with **Authorize** (Synapse PAT or machine key). FastAPI's default
  `/docs`, `/redoc` and `/openapi.json` are disabled so that only the committed contract is
  published. *Tests:* `cases.yaml` `docs_*`; `test_public_operations_are_exactly_health_and_docs`.

## Thresholds
All thresholds are declared in `api/openapi.yaml` `info.x-sagebrain-limits`. The full list of
where each one is enforced and which test covers it is in [parity-matrix.md](parity-matrix.md).

## Deviations from current behaviour
| # | Today | New | Why | Case(s) |
|---|---|---|---|---|
| D-1 (F4) | `X-Source` and `User-Agent` are read case-sensitively, so `x-source: agent` is logged as `direct` | Header names are case-insensitive (HTTP semantics) | Correct audit attribution | `query_submit_lowercase_source_header` |
| D-2 (F5) | A non-string field or a non-object body crashes the handler (API Gateway 502) | 400 `'<field>' must be a string` / `Request body must be a JSON object` | A client error should not be reported as a server error | `*_non_string`, `*_non_object` |
| D-3 (F6) | A blank `job_id` can't reach the handler (API Gateway answers first) | Blank or over 128 chars returns 400 `Missing job_id`; no `/query/` route (404) | Explicit input bounds | spec `JobId` parameter |
| D-4 | `x-api-key` clients must also send `Authorization: ApiKey` | `x-api-key` alone is enough; `Authorization: ApiKey` is still accepted and ignored | The extra header was only there to satisfy API Gateway's identity source | Phase 1 auth tests |
| D-5 | API Gateway accepts bodies up to 10 MB | 256 KiB returns 413 `{"message":"Request Too Long"}` | The largest valid body is about 8 KB; this narrows the abuse surface | Phase 1 body-limit tests |
| D-6 | Throttle is per API only | Plus a per-principal bucket and a WAF per-IP rule | Stops one user starving everyone else | Phase 1d (DynamoDB) |
| D-7 | URLs carry a `/prod` stage prefix on two hostnames | One hostname, no stage prefix | One service | spec 003 (optional temporary `/prod/*` alias) |
| D-8 | `/ask` puts the caller's raw `Authorization` header in SQS; the agent re-presents it to `POST /query` over HTTPS and polls every 3 s | `/ask` enqueues `{job_id, question, user_id, source_ip}`; the agent calls `run_query` in-process, attributed to `user_id` with `source: agent` | Limits live server-side; no bearer tokens in queues; no HTTP loopback through the NAT, WAF or rate limiter; fixes F8 | `ask_submit_ok` |
| D-9 | Agent queries go through the query SQS queue and worker (two hops), with a 70 s poll timeout | They run synchronously in the agent worker against the Neptune reader with a 60 s timeout. The agent worker gains read-only Neptune IAM and port 8182 ingress, and loses `NEPTUNE_QUERY_URL` | Fewer moving parts and lower latency | Phase 1 agent tool tests |
| D-10 | Bad or non-member bearer returns 401 `{"message":"User is not authorized to access this resource with an explicit deny in an identity-based policy"}` (observed in dev) | Every auth failure returns 401 `{"message":"Unauthorized"}` | One body for clients; no IAM wording leaked | `query_bad_bearer` |
| D-11 | `X-Source` of any length is accepted and written verbatim into the SQS message and the `sparql_query` audit log; the spec's `maxLength: 64` was unenforced | Over 64 characters returns 400 `'X-Source' exceeds maximum length of 64 characters` (`sagebrain_core.validate_source`) | A threshold must be enforced, not only documented (constitution III); bounds what callers can write into audit logs | `query_submit_source_too_long`, `query_submit_source_max_length` |

## Known issues
- **F8 (resolved by D-8):** today, for a machine-key `/ask` call, only `Authorization: ApiKey`
  reaches the worker, so the agent's `/query` calls are denied. This is pinned for the legacy
  Lambda by `test_ask_does_not_forward_api_key`. It goes away once the agent calls the service
  with `principal="machine"`.
- **F3:** today, when Synapse is down, the authorizer raises an exception. API Gateway probably
  returns 500 for this (`AUTHORIZER_FAILURE`), not 401. The status code is configurable as
  `AUTH_TRANSIENT_STATUS`; see [research.md](research.md).

## Out of scope
- Changing the worker Lambdas beyond the agent tool change in D-8/D-9, the job item size limits
  (the 400 KB TODOs), or Bedrock access.
- A query-side "latest snapshot per portal" default (this belongs to the pipeline follow-up).

## Success criteria
- `pytest tests/` passes, including `tests/contract/` and the characterization tests.
- `tools/parity_probe.py --target apigw … --target app … --legacy apigw` shows 0 failures and
  0 DIFFs in dev, stage and prod before cutover.
- `tools/throttle_test.py --url <app>/query`: a burst of 300 gets about 100 accepted and the
  rest 429.
- In each environment, the API Gateway `Count` stays at about 0 for 7 days before
  decommissioning.
