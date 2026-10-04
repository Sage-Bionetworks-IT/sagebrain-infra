# 001 — Research & decisions

## Decisions
- **R-1 Hosting: ECS Fargate behind ALB + WAF.** We rejected Lambda + Mangum behind API
  Gateway, because it keeps the 29 s ceiling and still splits the limits across two layers.
  We rejected App Runner because we'd have less control over the VPC and network for Neptune. We reuse the setup in `src/neptune_viz_stack.py`.
- **R-2 Workers stay as SQS + Lambda.** This keeps the DLQs, retries and the agent's reserved
  concurrency of 10. The submit, status and authorizer functions move to FastAPI. The only
  change inside a worker is the agent tool (R-4).
- **R-3 Tables, queues and workers stay in their current stacks with the same logical IDs.**
  Nothing is replaced, so no data is lost.
- **R-4 The agent calls the shared query service in-process, not `POST /query`.** This is the
  user's decision of 2026-10-03. The agent's `query_neptune` tool calls
  `sagebrain_core.run_query(..., principal=<caller user_id>, source="agent")`, which
  enforces the same limits and audit log as the HTTP path. As a result:
  - The audit chokepoint becomes the service function, not the endpoint (constitution V).
  - No bearer token rides in SQS (D-8), and F8 disappears.
  - The agent worker needs read-only `neptune-db` IAM, port 8182 ingress into the Neptune SG,
    and `UpdateItem` on the rate-limit table. It no longer needs `NEPTUNE_QUERY_URL`, so the AgentStack →
    ApiStack URL dependency and the cross-stack cycle risk go away.
  - We rejected two alternatives. An HTTP loopback through the ALB puts the limits at the edge,
    adds latency and hits WAF from the NAT IP. Enqueueing to the query SQS queue and polling
    adds two hops and a 3 s poll granularity.
- **R-10 Packaging `sagebrain_core`.** It is a plain package at `core/sagebrain_core/`, so its
  code doesn't change the Lambda asset hashes until it is imported. It is bundled three ways:
  - into the FastAPI image (`COPY core/` + `pip install`)
  - into the query worker asset
  - into the agent worker asset

  The two Lambda assets use a bundling command that also copies `core/sagebrain_core`. That
  means changing the asset root to the repo with `exclude`, or using a shared Lambda layer
  (the layer is preferred: one build, versioned). Dependencies stay minimal (`requests`,
  `botocore`), so it fits the ARM64 agent and the x86 query worker alike.
- **R-5 One service, two global buckets (`query`, `ask`).** This mirrors the two API Gateway
  stages.
- **R-6 ARM64 image.** It builds natively on Apple Silicon, Graviton Fargate is cheaper, and it
  matches the agent Lambda.
- **R-7 Spec-first enforcement:** hand-written OpenAPI 3.1, then `datamodel-code-generator`
  (output committed and checked by a diff in CI), a drift test between `app.routes` and the
  spec, Schemathesis response conformance, `oasdiff breaking` on PRs, and `redocly lint`.
- **R-8 Config:** a new `API_APP` section, deep-merged via an allowlist in `src/utils.py`.
  The global merge stays shallow, because `config/dev.yaml`'s `NEPTUNE` block depends on
  replacing base wholesale (F10).
- **R-9 WAF:** `AWSManagedRulesCommonRuleSet` with `SizeRestrictions_BODY` and
  `CrossSiteScripting_BODY` overridden to Count. No SQLi rule set, because SPARQL looks like
  SQL (F11). The managed rules start in Count mode in dev. F12 (shared NAT IP) no longer
  applies, because agent traffic doesn't come back through the ALB (R-4), so no NAT exemption
  is needed.

## Observed API Gateway behaviour (dev, 2026-10-03, `tools/parity_probe.py --legacy apigw`)
All 19 runnable `cases.yaml` vectors passed against dev (`--profile sagebrain-dev`, account
442650749796). Reproduce with:
```bash
P=sagebrain-dev
Q=$(aws --profile $P cloudformation describe-stacks --stack-name app-dev-neptune-api \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text | sed 's#/query$##')
A=$(aws --profile $P cloudformation describe-stacks --stack-name app-dev-neptune-agent \
  --query "Stacks[0].Outputs[?OutputKey=='AgentApiUrl'].OutputValue" --output text | sed 's#/ask$##')
python tools/parity_probe.py --legacy apigw --target apigw "$Q" "$A"   # uses $SYNAPSE_AUTH_TOKEN
```

| Question | Observed | Decision for FastAPI |
|---|---|---|
| Missing `Authorization` | 401 `{"message":"Unauthorized"}` + ACAO `*` | Same |
| Bad bearer (authorizer Deny → ACCESS_DENIED remapped) | 401 `{"message":"User is not authorized to access this resource with an explicit deny in an identity-based policy"}` + ACAO `*` | 401 `{"message":"Unauthorized"}` for every auth failure. Deviation **D-10**: callers only branch on the status, and the old body leaks IAM wording |
| Preflight `OPTIONS /query` | **204**, empty body. `Allow-Origin: *`, `Allow-Methods: POST,GET,OPTIONS`, `Allow-Headers: Content-Type,Authorization,X-Source,x-api-key`, no `Max-Age` | The same allow-lists. The status is 200 (Starlette CORSMiddleware) plus `Max-Age: 600`. Both are accepted, because browsers treat any 2xx preflight the same |
| `GET /query/` and unknown paths | 403 with a SigV4 parse error (`Invalid key=value pair … in Authorization header`), **no CORS** | 404 `{"message":"Not Found"}` + ACAO (D-3) |
| 11 MB body | 413 plain text `HTTP content length exceeded 10485760 bytes.`, **no CORS** | 413 `{"message":"Request Too Long"}` + ACAO, at 256 KiB (D-5) |
| Non-string field / non-object body | 502 `{"message":"Internal server error"}` (confirms F5) | 400 (D-2) |
| F3: authorizer raises on a transient Synapse error | Can't be induced from outside. AWS documents that an authorizer function error returns `AUTHORIZER_FAILURE` (500) | Default `AUTH_TRANSIENT_STATUS=500` |

## Doc corrections found while researching (fix in Phase 7)
- **F1:** the deployed Synapse team is `3605470` (`config/base.yaml`), not `273957`.
- **F2:** API Gateway does not cache authorizer results (TTL 0). The 300 s cache lives inside
  the authorizer Lambda.
- The README's description of `/query` as synchronous is stale. It has been async (submit +
  poll) since the job pattern was introduced.
