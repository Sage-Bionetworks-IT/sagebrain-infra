# Security reviews

One entry per review, newest first: when, what was in scope (PR and commit, so it can be
re-scoped later), what came out, and what was checked and ruled out. A finding that needs work
goes in [open-work.md](open-work.md); a fix is recorded by its PR, not here. A later change to a
reviewed path isn't covered by the earlier review: review it again.

## 2026-10-13 — spec 001 Phase 2, the edge stack (#76)
- **Scope:** `spec001-edge` at `a13c903` against `dev` at `ce90d74`: the edge stack, its
  submit/status and docs Lambdas, the docs bundle step, the authorizer's `x-api-key` removal and
  `tools/load_probe.py`. Code reading only (`/security-review`); no exploits run, nothing deployed.
- **Result:** no HIGH or MEDIUM findings (bar: ≥ 0.8 confidence of real exploitability).
- **Ruled out:**
  - Auth bypass: the only change removes a way in; allow needs a Synapse bearer and team
    membership; API Gateway's authorizer cache is off, so the `api_id/*/*` policy can't be
    reused for a request without a valid token.
  - Route gaps: every `/query` and `/ask` method has the authorizer; only the three `/api` docs
    routes are public, and the docs Lambda has no table or queue grants.
  - Cross-caller job reads: same model as the old APIs (any team member, by uuid4 job id); the
    id is only a DynamoDB key value.
  - IAM: submit Lambdas write their own table and send to their own queue; status Lambdas only
    read their own table; no VPC or Neptune access.
  - Credentials: none enqueued, logged or returned; `source_ip` comes from API Gateway.
  - XSS on `/api`: the only request value in the page is the path, which is always the fixed
    `/{stage}/api` on an `execute-api` URL; inlined Swagger UI is pinned build-time content.
  - Build step: Swagger UI is pinned and sha512-checked before the tarball is opened; four fixed
    member names go to fixed paths, so no tar path traversal.
  - YAML: `safe_load` on the committed contract only. CORS `*`: bearer auth, no cookies.
- **Follow-up (low, hardening):** escape `<` in the docs page's spec URL before a custom domain or
  base-path mapping lands (open-work.md).
