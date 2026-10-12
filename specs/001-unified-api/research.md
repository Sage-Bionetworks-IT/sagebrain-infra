# 001 — Research & decisions

## Decisions
- **R-0 Write the contract first, as a hand-written OpenAPI 3.1 file.** Today the contract
  exists only in prose that contradicts itself and the code. The contract (`api/openapi.yaml`)
  merges right after this plan and before any code, so the characterization tests, the
  parity probe and the new routes are all written against it. We rejected generating it from
  code, because the contract would then follow the implementation instead of pinning today's
  behaviour ahead of it, and we rejected keeping prose docs.
- **R-1 Hosting: one API Gateway REST API** (decided 2026-10-12). An earlier draft of this
  spec moved the submit, status and authorizer Lambdas to FastAPI on ECS Fargate behind an
  ALB + WAF. We dropped that:
  - Its main argument, API Gateway's 29 s ceiling, doesn't apply: both APIs are async, and
    submit/status finish well inside a 10 s timeout.
  - Traffic is far below API Gateway's limits. The account default is 10,000 rps steady /
    5,000 burst per region; our own stage throttle is 50 / 100. The bottlenecks are Neptune
    (4–40 s queries) and Bedrock, which no front door changes.
  - Every other goal (contract, tests, one endpoint, in-process agent queries, no token in
    SQS) is reachable on API Gateway.
  - Fargate adds an always-on cost (2 tasks, ALB, WAF) and ownership of TLS, DNS, image
    patching, health checks, and CORS, body-limit and timeout code.

  **Reopen it if** we need response streaming (for example streamed agent answers),
  long-lived connections, synchronous calls over 29 s, or bodies over 10 MB, **or** the
  request-volume alarm stays above 1M requests/day for a week. That is the cost break-even:
  API Gateway's per-request cost (about $4.10 per million polls, Lambda included) then
  overtakes a fixed ~$100–150/month front door (see the plan's scaling path).

  Also rejected:
  - FastAPI inside Lambda (Mangum): not worth a framework for four routes, and pydantic's
    compiled dependencies complicate the x86 query Lambdas.
  - A custom domain with base-path mappings over the two existing APIs: base path `query`
    plus the existing `/query` resource gives `/query/query`, so the resources would need
    restructuring anyway, and there would still be two authorizers, stages and log groups.
  - An HTTP API (v2): it lacks the gateway responses we use for 401/404/503 bodies with CORS,
    so parity would get harder.
- **R-2 Workers stay as SQS + Lambda.** This keeps the DLQs, retries and the agent's reserved
  concurrency of 10. The only change inside a worker is the agent tool (R-4).
- **R-3 Tables, queues and workers stay in their current stacks with the same logical IDs.**
  Nothing is replaced, so no data is lost. The edge stack imports them.
- **R-4 The agent calls the shared query service in-process, not `POST /query`** (decided
  2026-10-03). The agent's `query_neptune` tool runs its queries through the shared service as
  the original caller with `source="agent"`, so it gets the same validation, timeout and audit
  log as the HTTP path. As a result:
  - The audit chokepoint becomes the service, not the endpoint (constitution V).
  - No bearer token rides in SQS (D-8), and F8 disappears.
  - The agent worker (already in the VPC) needs read-only `neptune-db` IAM and port 8182
    ingress into the Neptune SG. It no longer needs `NEPTUNE_QUERY_URL`, so the agent stack's
    dependency on the API stack's URL goes away.
  - We rejected two alternatives. An HTTP loopback through API Gateway keeps the token in SQS
    and the 3 s poll. Enqueueing to the query SQS queue and polling adds two hops and the same
    poll granularity.
- **R-5 Rate limits: API Gateway throttle for HTTP, worker concurrency for the agent**
  (decided 2026-10-12). Stage throttling applies per method, so merging both APIs into one
  stage keeps 50 / 100 for each method with no extra config. The two status methods get
  100 / 200, because polling at the 100-user design target is 50 rps at worst (D-17). Agent
  queries skip API Gateway, but at most 10 agent workers run at once, and the query worker is
  capped at 20 (D-14); every query is capped at 60 s. A test covers each bound. We deferred per-principal buckets and the shared
  DynamoDB rate-limit store from the earlier draft (D-6, D-12): they're worth building when
  one user actually starves others, not before.
- **R-6 One edge stack.** The `RestApi`, authorizer, submit/status Lambdas and docs Lambda
  live together in `app-{env}-api`. Adding methods to one API from several stacks invites
  cyclic references and export locks.
- **R-7 Spec-first enforcement:** hand-written OpenAPI 3.1, a test that the synthesized routes
  match the spec (R-14), response-conformance tests that validate handler output against the
  spec's schemas, `oasdiff breaking` and `redocly lint` on PRs. No generated models
  (constitution I).
- **R-8 Config:** a new `API_EDGE` section (`enabled`) that deep-merges `base.yaml` with the
  env file. The global merge stays shallow, because `config/dev.yaml`'s `NEPTUNE` block relies
  on replacing the base block wholesale.
- **R-9 No WAF.** With TLS from API Gateway, the stage throttle, the Synapse team gate and the
  handler checks, WAF adds cost without covering a real gap at this traffic. Revisit if we see
  abuse.
- **R-10 Packaging the shared service.** One plain Python package with minimal dependencies
  (`requests`, `botocore`), so it fits the ARM64 agent and the x86 query Lambdas alike. It
  ships as one shared, versioned Lambda layer.
- **R-11 Synapse bearer tokens are the only credential** (D-4, decided 2026-10-12). The
  authorizer's machine-key path is configured in no environment, and after R-4 the agent needs
  no machine credential.
- **R-13 Transient auth errors return 503 + `Retry-After` from day one** (D-13, decided
  2026-10-12), via the `AUTHORIZER_FAILURE` gateway response. The earlier draft held 500 until
  API Gateway was decommissioned; that phase no longer exists.
- **R-14 Routes built by hand, not `SpecRestApi`.** `SpecRestApi` needs
  `x-amazon-apigateway-integration` blocks with Lambda ARNs in the contract, which would leak
  into the published `/api` document. A CDK test compares the synthesized methods with the
  contract instead.
- **R-15 `/api` is a docs Lambda.** It bundles the Swagger UI assets and the contract at build
  time, needs no authorizer, and sets `servers` to the stage URL from its environment.
- **R-16 No custom domain yet** (decided 2026-10-12). API Gateway's `execute-api` URL is HTTPS
  already. A domain needs a certificate and a DNS record from IT, and is a follow-up.

- **R-17 Design target: 100 concurrent users** (decided 2026-10-12), each polling every 2 s.
  Prod's team gate has 9 members, so building for thousands now would be premature; thousands
  is a future spec after spec 002 opens access. The plan's scaling path records what breaks
  beyond the target and what it costs, without picking fixes.
- **R-18 API Gateway's authorizer cache stays off** (decided 2026-10-12), for parity
  (constitution II). Turning it on would make every poll after the first skip the authorizer,
  but the cached policy must then cover every route, including spec 002's future ACLs. It is
  the first thing to revisit if authorizer load shows up.
- **R-19 Big results fail explicitly** (D-16, decided 2026-10-12). Results live in a DynamoDB
  item (400 KB max), so the worker rejects anything over 350 KB with an actionable error.
  Offloading to S3 is its own spec.

(R-12, fixed Fargate task count, was retired with R-1.)

## Observed API Gateway behaviour (dev, 2026-10-03)
Probed against the dev API Gateway (account 442650749796) with a Synapse team-member PAT.

| Question | Observed | Decision |
|---|---|---|
| Missing `Authorization` | 401 `{"message":"Unauthorized"}` + ACAO `*` | Same |
| Bad bearer (authorizer Deny → ACCESS_DENIED remapped) | 401 `{"message":"User is not authorized to access this resource with an explicit deny in an identity-based policy"}` + ACAO `*` | 401 `{"message":"Unauthorized"}` for every auth failure, via a gateway response template (D-10) |
| Preflight `OPTIONS /query` | **204**, empty body. `Allow-Origin: *`, `Allow-Methods: POST,GET,OPTIONS`, `Allow-Headers: Content-Type,Authorization,X-Source,x-api-key`, no `Max-Age` | Same |
| `GET /query/` and unknown paths | 403 with a SigV4 parse error (`Invalid key=value pair … in Authorization header`), **no CORS** | 404 `{"message":"Not Found"}` + ACAO, via `MISSING_AUTHENTICATION_TOKEN` (D-3) |
| 11 MB body | 413 plain text `HTTP content length exceeded 10485760 bytes.`, **no CORS** | Unchanged above 10 MB (API Gateway's own cap). Between 256 KiB and 10 MB the handler returns 413 `{"message":"Request Too Long"}` + ACAO (D-5) |
| Non-string field / non-object body | 502 `{"message":"Internal server error"}` | 400 (D-2) |
| F3: authorizer raises on a transient Synapse error | Can't be induced from outside. AWS documents that an authorizer function error returns `AUTHORIZER_FAILURE` (500) | 503 + `Retry-After` (D-13) |
