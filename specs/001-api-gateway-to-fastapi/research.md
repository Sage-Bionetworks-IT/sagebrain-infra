# 001 — Research & decisions

## Decisions
- **R-0 Write the contract first, as a hand-written OpenAPI 3.1 file.** Today the contract
  exists only in prose that contradicts itself and the code. The contract (`api/openapi.yaml`)
  merges right after this plan and before any code, so the characterization tests, the
  parity probe and the new service are all written against it. We rejected generating it from
  FastAPI (code-first), because the contract would then follow the implementation instead of
  pinning today's behaviour ahead of it, and we rejected keeping prose docs.
- **R-1 Hosting: ECS Fargate behind ALB + WAF.** We rejected Lambda + Mangum behind API
  Gateway, because it keeps the 29 s ceiling and still splits the limits across two layers.
  We rejected App Runner because we'd have less control over the VPC and network for Neptune.
  We reuse the Fargate + ALB setup of the Graph Explorer stack.
- **R-2 Workers stay as SQS + Lambda.** This keeps the DLQs, retries and the agent's reserved
  concurrency of 10. The submit, status and authorizer functions move to FastAPI. The only
  change inside a worker is the agent tool (R-4).
- **R-3 Tables, queues and workers stay in their current stacks with the same logical IDs.**
  Nothing is replaced, so no data is lost.
- **R-4 The agent calls the shared query service in-process, not `POST /query`** (decided
  2026-10-03). The agent's `query_neptune` tool runs its queries through the shared service as
  the original caller with `source="agent"`, so it gets the same limits and audit log as the
  HTTP path. As a result:
  - The audit chokepoint becomes the service, not the endpoint (constitution V).
  - No bearer token rides in SQS (D-8), and F8 disappears.
  - The agent worker needs read-only `neptune-db` IAM, port 8182 ingress into the Neptune SG,
    and write access to the rate-limit store. It no longer needs `NEPTUNE_QUERY_URL`, so the
    agent stack's dependency on the API stack's URL goes away.
  - We rejected two alternatives. An HTTP loopback through the ALB puts the limits at the edge,
    adds latency and hits WAF from the NAT IP. Enqueueing to the query SQS queue and polling
    adds two hops and a 3 s poll granularity.
- **R-5 One service, two global buckets (`query`, `ask`).** This mirrors the two API Gateway
  stages.
- **R-6 ARM64 image.** It builds natively on Apple Silicon, Graviton Fargate is cheaper, and it
  matches the agent Lambda.
- **R-7 Spec-first enforcement:** hand-written OpenAPI 3.1, models generated from it (checked
  for drift in CI), a test that the service's routes match the spec, response-conformance
  tests, `oasdiff breaking` and `redocly lint` on PRs.
- **R-8 Config:** a new `API_APP` section that deep-merges `base.yaml` with the env file. The
  global merge stays shallow, because `config/dev.yaml`'s `NEPTUNE` block relies on replacing
  the base block wholesale.
- **R-9 WAF:** `AWSManagedRulesCommonRuleSet` with `SizeRestrictions_BODY` and
  `CrossSiteScripting_BODY` overridden to Count. No SQLi rule set, because SPARQL looks like
  SQL and would be blocked. The managed rules start in Count mode in dev. Agent traffic doesn't
  come back through the ALB (R-4), so the shared NAT IP needs no WAF exemption.
- **R-10 Packaging the shared service.** One plain Python package with minimal dependencies
  (`requests`, `botocore`), so it fits the ARM64 agent and the x86 query worker alike. It is
  installed into the service image and shipped to both worker Lambdas as one shared, versioned
  Lambda layer.
- **R-11 Synapse bearer tokens are the only credential** (D-4, decided 2026-10-12). The
  authorizer's machine-key path is configured in no environment, and after R-4 the agent needs
  no machine credential.
- **R-12 Fixed task count** (decided 2026-10-12). Phase 2 runs 2 tasks with no autoscaling, so
  the rate-limit fallback divisor is unambiguous.
- **R-13 Transient auth errors stay 500 until API Gateway is gone** (D-13, decided 2026-10-12),
  then become 503 + `Retry-After`.

## Observed API Gateway behaviour (dev, 2026-10-03)
Probed against the dev API Gateway (account 442650749796) with a Synapse team-member PAT.

| Question | Observed | Decision for FastAPI |
|---|---|---|
| Missing `Authorization` | 401 `{"message":"Unauthorized"}` + ACAO `*` | Same |
| Bad bearer (authorizer Deny → ACCESS_DENIED remapped) | 401 `{"message":"User is not authorized to access this resource with an explicit deny in an identity-based policy"}` + ACAO `*` | 401 `{"message":"Unauthorized"}` for every auth failure (D-10): callers only branch on the status, and the old body leaks IAM wording |
| Preflight `OPTIONS /query` | **204**, empty body. `Allow-Origin: *`, `Allow-Methods: POST,GET,OPTIONS`, `Allow-Headers: Content-Type,Authorization,X-Source,x-api-key`, no `Max-Age` | The same allow-lists. The status is 200 (Starlette CORSMiddleware) plus `Max-Age: 600`. Both are accepted, because browsers treat any 2xx preflight the same |
| `GET /query/` and unknown paths | 403 with a SigV4 parse error (`Invalid key=value pair … in Authorization header`), **no CORS** | 404 `{"message":"Not Found"}` + ACAO (D-3) |
| 11 MB body | 413 plain text `HTTP content length exceeded 10485760 bytes.`, **no CORS** | 413 `{"message":"Request Too Long"}` + ACAO, at 256 KiB (D-5) |
| Non-string field / non-object body | 502 `{"message":"Internal server error"}` | 400 (D-2) |
| F3: authorizer raises on a transient Synapse error | Can't be induced from outside. AWS documents that an authorizer function error returns `AUTHORIZER_FAILURE` (500) | 500 until decommission, then 503 (D-13) |
