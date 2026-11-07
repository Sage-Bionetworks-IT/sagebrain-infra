# 005 — Tickets

The epic's work items, one entry per ticket, ready to copy into a tracker. Field meanings:
- **Rev 3:** the SP 800-171r3 requirement IDs the ticket implements or evidences.
- **Owner:** `Repo` (a PR here), `IT` (Sage IT work or evidence) or `Shared`.
- **Size:** S ≤ 1 day, M ≤ 1 week, L > 1 week.
- **Gate:** in the CUI gate ([spec.md](spec.md#cui-gate)).

Evidence is file:line at `dev` `b313b47` (2026-10-23 scrub). Every acceptance criterion that
changes behaviour names its test. A PR that closes a ticket updates its rows in
[control-matrix.md](control-matrix.md) and, if it changes a recorded fact, `docs/memory/`.

---

## Phase 0: Scope and decisions

### N01 — System boundary, CUI inventory and data flows
**Rev 3:** 03.15.02, 03.04.10, 03.04.11, 03.08.04 · **Owner:** Shared (data owner + repo) · **Size:** M · **Gate:** yes

**Why:** Nothing records what is CUI, or where it lands. `aws_architecture.svg` is stale: it shows
two APIs and no `app-*-api`.

**Scope:**
- Name the portals, and the fields within them, that are CUI, with the CUI category (Basic or
  Specified, from the CUI Registry).
- Trace each CUI flow:
  - depositor → S3 `data/` → bulk loader → Neptune;
  - Neptune → query and agent workers → DynamoDB job items → status API → client;
  - copies in SQS messages and DLQs, CloudWatch logs (`sparql_query`, `agent_invocation`,
    `kg_load`), SNS email, and Bedrock prompts and responses.
- List the external systems and what crosses into each: Synapse, Bedrock, GitHub, npm.
- List the depositor principals that N13 needs.

**Acceptance criteria:**
- `docs/compliance/boundary.md` and a new boundary and data-flow diagram exist.
  `aws_architecture.svg` is redrawn or removed.
- Each CUI-holding resource is listed with the tag value N53 applies (`DataClassification=CUI`).
- The data owner signs off, recorded with the date in the doc.

### N02 — Control matrix and organization-defined parameters
**Rev 3:** all; 03.15.01 · **Owner:** Shared · **Size:** M · **Gate:** yes · **Depends on:** N01

**Why:** Rev 3 makes many values organization-defined. The repo's habit of one declared value
plus a test (constitution III) is how they get enforced.

**Scope:**
- Finish [control-matrix.md](control-matrix.md):
  - verify each Rev 3 ID and title against the final publication;
  - complete the Rev 2 crosswalk;
  - set each owner;
  - attach IT evidence references.
- Agree the ODP values (spec.md table) with IT and the data owner.
- Add a `COMPLIANCE:` block to `config/base.yaml` for ODPs that aren't API-facing. API-facing ones
  go in `x-sagebrain-limits`.

**Acceptance criteria:**
- The matrix has 97 rows. None is "TBD". Every Planned or Partial row links a ticket.
- Each ODP is declared once, and its test is listed in spec.md's thresholds table. The test lands
  with the ticket that enforces the ODP.

### N03 — Inherited controls and the account preflight
**Rev 3:** 03.03.01–08, 03.05.03, 03.06.01, 03.14.02, 03.14.03, 03.14.06, 03.02.x, 03.09.x, 03.10.x · **Owner:** IT (evidence) + Repo (preflight) · **Size:** M · **Gate:** yes

**Why:** Account-level services are IT's, but none is visible from this repo. The deploy role's
trust policy and permissions aren't in the repo either (`.github/workflows/deploy-*.yaml:15`).

**Scope:**
- Write `docs/compliance/inherited-controls.md`. For each service: what IT provides, the evidence
  location, and the requirement IDs. It covers:
  - org CloudTrail: multi-region and log-file validation, with logs in IT's log-archive account.
    Confirm it captures `AssumeRoleWithWebIdentity` management events. They record each
    depositor's GitHub `sub` (repo and ref), which N46 uses for the branch. The account's 90-day
    event history is the fallback.

    The data bucket's S3 data events are **not** IT's. This repo creates a bucket-scoped trail
    for them (N46). If the org trail already logs them, the preflight reports it, so the in-repo
    trail can be turned off rather than paid for twice;
  - GuardDuty, including S3, Lambda and runtime protection;
  - AWS Config, with the Operational Best Practices for NIST 800-171 conformance pack;
  - Security Hub, with NIST standards where available;
  - Inspector (Lambda);
  - IAM Identity Center with MFA, and SCPs;
  - the Bedrock model-invocation logging setting;
  - the CDK bootstrap (qualifier, trust), the OIDC provider and the deploy role;
  - GitHub org settings: SSO, 2FA, audit-log streaming;
  - the AT, PS, PE and org IR programs.
- `tools/compliance_preflight.py` makes read-only boto3 calls for each item. It prints a
  pass/fail table and exits non-zero on any failure, using `--profile` and `--region`.

**Acceptance criteria:**
- The preflight passes against dev and prod. Its output is saved as dated evidence.
- `tests/unit/test_compliance_preflight.py` covers pass and fail with stubbed clients.

### N04 — Evidence: MFA inherited from Synapse
**Rev 3:** 03.05.03, 03.05.04, 03.05.12 · **Owner:** Shared (Synapse team) · **Size:** S · **Gate:** yes

**Why:** Decided 2026-11-03 (spec.md Q-1): API users get MFA from Synapse, which requires it at
login, so the authorizer doesn't check 2FA. An assessor needs evidence that the chain holds from
the login to the token the API sees.

**Scope:** get dated, written answers from the Synapse team or Synapse's published policy:
- Every Synapse login requires MFA, and no account in the env's team can opt out.
- PATs and OAuth tokens can be created only from a logged-in session. In particular, a PAT can't
  create another PAT. If one can, the chain breaks, and the fallback in plan.md's risks applies.
- What a PAT's scopes are, and when Synapse retires an idle PAT. A PAT carries `token_type` and
  `iat` but no `exp` (checked 2026-10-27), and Synapse sets no maximum age (POAM-001).

**Acceptance criteria:**
- A row in `docs/compliance/inherited-controls.md` (N03) with the evidence and its date.
- A decisions.md entry.
- The 03.05.03 matrix row moves to Inherited.

### N05 — Spike: Bedrock and CUI
**Rev 3:** 03.16.03, 03.13.08, 03.04.11 · **Owner:** Shared · **Size:** S · **Gate:** yes

**Why:** `/ask` sends questions and query results to Bedrock. dev uses
`us.anthropic.claude-sonnet-5-5` and prod `us.anthropic.claude-sonnet-4-6` (`config/base.yaml`,
`config/prod.yaml`). Neither model's FedRAMP scope, nor where the `us.` profile routes, is
recorded.

**Scope:**
- Confirm Bedrock's authorization level and whether the specific models are inside it, in each
  region the `us.` profile can route to.
- Confirm the model-provider data-use terms.
- Confirm whether account-level model-invocation logging is on. If it is, prompts and responses
  land in S3 or CloudWatch, which must be inside the boundary.
- Decide: `/ask` allowed, GovCloud-only, or off in CUI envs.

**Acceptance criteria:**
- A decisions.md entry.
- If `/ask` must be off in some env: a config flag (`AGENT.enabled`) is specced in the contract
  for that env and becomes a sub-task of N43.

### N06 — Spike: hosting partition
**Rev 3:** 03.16.03, 03.16.01 · **Owner:** Shared · **Size:** S · **Gate:** yes · **Depends on:** N01

**Why:** Specified CUI, such as export-controlled data, can require GovCloud. The build has never
been synthesized for `aws-us-gov`.

**Scope:**
- Decide commercial or GovCloud from the N01 category.
- If GovCloud, confirm the availability of:
  - Neptune Serverless and its engine version;
  - Bedrock and the needed models (inference-profile prefix);
  - API Gateway REGIONAL and its TLS policies;
  - Step Functions and WAF managed rules.

**Acceptance criteria:**
- A decisions.md entry.
- A gap list added to N61 and N23.

---

## Phase 1: High-risk fixes

### N10 — Change control for deploys (done; evidence only)
**Rev 3:** 03.04.03, 03.04.05, 03.01.04 · **Owner:** Repo · **Size:** S · **Gate:** no

**Status:** accepted as is (2026-10-27).
- The `dev` ruleset requires a PR with 1 approving review and squash merges only. Bypass is the
  repository admin role.
- Only CI deploys, through the OIDC role.
- `--require-approval never` (`aws-deploy.yaml:57`) stays: review happens at the PR, not at
  deploy time (decisions.md).

**Scope:** export both rulesets (`gh api repos/{owner}/{repo}/rulesets/{id}`) to
`docs/compliance/evidence/` as dated evidence for 03.04.03 and 03.01.04. Re-export whenever a
ruleset changes.

**Acceptance criteria:** the exported JSON matches the live rulesets on its date.

### N11 — Owner-only job status (spec 002 FR-11, pulled forward)
**Rev 3:** 03.01.02, 03.01.03 · **Owner:** Repo · **Size:** M · **Gate:** yes

**Why:** `_create_job` stores no owner (`src/lambda_edge/jobs.py:120-137`). `_load_job` reads by
`job_id` alone (`:140-153`). Any team member with a job id can read the results and the agent's
steps.

**Scope:**
- Contract first: both status operations document 404 for a non-owner. Add vectors
  `query_status_other_owner` and `ask_status_other_owner` to `tests/contract/cases.yaml`.
- Store `user_id` on both job items. `_load_job` compares it with the caller from
  `_caller(event)` and returns the same 404 body as an unknown job.
- Log a `job_status_read` event with `job_id`, `principal`, `source_ip` and the outcome.
- Mark FR-11 as delivered here in spec 002.

**Acceptance criteria:**
- `tests/unit/test_edge_handlers.py` passes the new vectors.
- Old items with no owner return 404 to everyone. They expire within the 24 h TTL, so no
  migration is needed. Record this as a deviation (spec.md D-1).

### N13 — Data bucket write path
**Rev 3:** 03.01.05, 03.01.03, 03.03.01, 03.08.02, 03.08.03, 03.13.08 · **Owner:** Repo · **Size:** M · **Gate:** yes · **Depends on:** N01

**Why:**
- **Any principal in the account can write and delete.** The bucket policy gives `AccountPrincipal`
  `PutObject`, `GetObject`, `DeleteObject` and `ListBucket` with no conditions
  (`src/neptune_stack.py:61-77`), and `manifest.ttl` auto-loads Neptune.
- **The depositor roles are too broad.** They are GitHub OIDC roles that IT defined in
  `Sage-Bionetworks-IT/organizations-infra` #1605, in both the dev and prod accounts:

  | Role | Trusted repo | Uploads today |
  |---|---|---|
  | `…-sagebrain-infra-nf` | `nf-osi/kg-pipeline` (develop, main, tags, environments) | `nf/` |
  | `…-sagebrain-infra-sagebrain` | `amp-als/kg-pipeline` (the name doesn't say amp-als) | its portal prefix |
  | `…-sagebrain-infra-main` | `Sage-Bionetworks/Sage_Brain` | nothing: the repo has no workflows (checked 2026-10-28). It builds Reactome locally, so a future upload would go to `reactome/` |

  Every role can `PutObject`, `DeleteObject` and **`DeleteObjectVersion`** on every portal's
  prefix. One pipeline can overwrite another portal's data, and `DeleteObjectVersion` can erase
  the history that versioning preserves.
- **The load role's trust has no source condition** (`aws:SourceAccount` or `aws:SourceArn`).

**Scope:** all of it is enforced by this repo's bucket policy. The bucket and the roles share an
account, so an explicit Deny in the bucket policy overrides any Allow in a role's policy. No
change to IT's roles is required.
- **Remove the `AccountPrincipal` allow statements.**
- **Deny writes from anyone but the configured depositors.** `NEPTUNE.depositors` maps each role
  ARN to `[portal, ...]`. For each portal prefix, deny `s3:PutObject` (which also covers copy and
  multipart completion) when `aws:PrincipalArn` isn't one of that portal's roles or the
  break-glass role. Deny `PutObject` on keys outside every configured prefix.
- **Deny `s3:DeleteObject` and `s3:DeleteObjectVersion`** to everyone except the break-glass
  role. Lifecycle expiration is done by the S3 service and is unaffected.
- **Allow Get and List** for the depositors and the load role.
- **Deny requests below TLS 1.2** (`s3:TlsVersion`).
- **Protect the policy itself:** deny `s3:PutBucketPolicy` and `s3:DeleteBucketPolicy` except to
  the CDK deploy role and break-glass, so the Denies can't be quietly removed.
- Server access logs to a dedicated log bucket (encrypted, `RETAIN`). They are a secondary,
  best-effort record; they include the assumed-role ARN.
- A noncurrent-version lifecycle rule (ODP), longer than any audit look-back.
- Source conditions on the load-role trust.

**Optional cleanup in `organizations-infra` (IT's stacks deploy these roles, so don't edit them by
hand: that would drift).** None of this blocks the ticket, because the bucket policy already
enforces it:
- narrow each role's policy to match its prefixes, for least privilege at the identity layer too;
- remove `DeleteObject` and `DeleteObjectVersion`;
- rename the amp-als role;
- remove the unused `Sage_Brain` role, or scope it to `reactome/*` when a workflow exists.

Until then, the bucket policy simply gives the `Sage_Brain` role no prefix.

**Acceptance criteria:**
- `tests/unit/test_neptune_stack.py` asserts the per-role prefix statements, the delete denies,
  the TLS condition and the trust conditions.
- In dev, a probe proves:
  - the NF role can write `nf/` and is denied `amp-als/`;
  - any other principal is denied;
  - `DeleteObjectVersion` is denied.

### N14 — Neptune audit log on
**Rev 3:** 03.03.01, 03.03.03, 03.13.08 · **Owner:** Repo · **Size:** M · **Gate:** yes

**Why:** `create_parameter_group: false` (`config/base.yaml`), so `neptune_enable_audit_log=0`
even though `audit` is exported. The runbook said the log "still records" bastion queries. This
spec's PR corrects that.

**Scope:**
- `create_parameter_group: true`, with `neptune_enable_audit_log: 1`. Enforce TLS too, if the
  engine version has the parameter.
- Create `/aws/neptune/cluster/<id>/audit` and `/slowquery` in CDK, with ODP retention, before the
  export writes them.
- Backup retention = ODP (`NEPTUNE.backup_retention_days`).
- Reboot plan: dev first, then prod in the maintenance window.

**Acceptance criteria:**
- `tests/unit/test_neptune_stack.py` asserts the parameter and the backup retention.
- After the dev reboot, a bastion query appears in the audit log group (evidence saved).

### N15 — Every log group managed, with retention
**Rev 3:** 03.03.01, 03.03.08 · **Owner:** Repo · **Size:** M · **Gate:** yes · **Depends on:** N02 (retention ODP)

**Why:** No Lambda sets a log group, so `/aws/lambda/*` groups, including those holding every
audit event, never expire and aren't in IaC. API Gateway execution logs aren't managed either.
Managed groups keep 30 days (`src/api_edge_stack.py:176`, `src/neptune_pipeline_stack.py:302`).

**Scope:**
- Every `lambda_.Function` gets `log_group=logs.LogGroup(..., retention=ODP,
  removal_policy=RETAIN)`, via one helper in `src/utils.py`.
- The same for the API execution log group, the state machine and (N14) Neptune.
- The existing groups need adopting, not just creating: a CDK-created group with the same name
  conflicts with the one Lambda made, so the PR includes an import or rename step.

**Acceptance criteria:**
- `tests/compliance/test_logging.py` (03.03.08) fails if any function lacks a managed log group,
  or any log group has retention below the ODP.

### N16 — Bastion hardening
**Rev 3:** 03.07.04, 03.07.05, 03.07.06, 03.03.01, 03.01.12 · **Owner:** Repo · **Size:** M

**Why:** It is deployed from a laptop, has no automatic teardown, no SSM output logging and no
EBS encryption, and can delete data (`tools/neptune_bastion/app.py:56-90`, `bastion.sh:66-76`).

**Scope:**
- Encrypted EBS (AWS-managed key).
- SSM Run Command output to a managed log group.
- `bastion.sh up` requires a reason and an issue number, recorded as tags.
- A scheduled teardown: an EventBridge Scheduler one-shot deletes the stack after the ODP hours.
- Egress limited to Neptune and the VPC endpoints.
- The app's TAGS.
- A maintenance-log template in the runbook.

**Acceptance criteria:**
- A bastion stack synth test asserts encryption, the scheduler and egress.
- A dry run in dev shows the stack self-deletes.

---

## Phase 2: Cryptography

### N21 — Encrypt the SNS topic; TLS-only queues and topic
**Rev 3:** 03.13.08, 03.13.10, 03.13.11 · **Owner:** Repo · **Size:** S · **Gate:** yes

**Why:** The SNS alarm topic is unencrypted (`src/api_edge_monitoring.py:116`), and failure
emails can carry CUI until N43. Queues and the topic don't deny non-TLS access. Other storage
stays on AWS-managed or AWS-owned keys, which is accepted (2026-11-03, decisions.md).

**Scope:**
- One customer-managed key for the topic, in `api_edge_monitoring` next to it: rotation on,
  `RETAIN`, and an alias. The key policy grants `cloudwatch.amazonaws.com` and
  `events.amazonaws.com` (`kms:GenerateDataKey*`, `kms:Decrypt`), scoped to the account. The
  AWS-managed `aws/sns` key can't work: its policy can't be edited to admit those services.
- `enforce_ssl=True` on every SQS queue and DLQ, and the same deny on the topic.

**Acceptance criteria:**
- `tests/compliance/test_encryption.py` (03.13.08) fails if the topic has no key, or a queue or
  the topic allows non-TLS access.
- In dev, a test alarm and a failed load still email.

### N23 — TLS and FIPS in transit
**Rev 3:** 03.13.08, 03.13.11, 03.13.15 · **Owner:** Repo · **Size:** M · **Gate:** yes · **Depends on:** N06

**Why:** The API uses the default EDGE endpoint with no security policy
(`src/api_edge_stack.py:187-225`). No client uses FIPS endpoints.

**Scope:**
- Contract first (`servers`, endpoint notes).
- A REGIONAL endpoint with a TLS 1.2+ security policy, FIPS-capable if offered. With a custom
  domain, `disable_execute_api_endpoint`.
- Set `AWS_USE_FIPS_ENDPOINT=true` on Lambdas for each service that has a FIPS endpoint
  in the partition (verify per service).
- `neptune_enforce_ssl` (or the engine's equivalent) through N14's parameter group.
- Record the TLS posture of the outbound Synapse calls.

**Acceptance criteria:**
- Template assertions on the endpoint type and the policy.
- `tools/parity_probe.py` passes against the new URL.
- TLS 1.0 and 1.1 handshakes to the API fail (`openssl s_client -tls1` and `-tls1_1`, saved as
  evidence). Both succeed today (probed 2026-11-03).

### N24 — Lineage table: recoverable and append-only
**Rev 3:** 03.08.09, 03.03.08 · **Owner:** Repo · **Size:** S

**Why:** The table is called an "append-only audit trail" (`src/neptune_pipeline_stack.py:81`)
but is `DESTROY` with no PITR (`:95`). `_put_load` overwrites items (`src/lambda_loader/loader.py:199-203`).

**Scope:**
- `RETAIN` and PITR.
- Writes are conditional or versioned, so a re-publish keeps the prior record (for example, an
  `attempt` sort-key suffix, or a history list), coordinated with spec 004's registry.

**Acceptance criteria:** `tests/unit/test_loader_handler.py` shows a re-publish keeps the earlier
attempt. Stack test asserts PITR and `RETAIN`.

---

## Phase 3: Network

### N30 — VPC flow logs
**Rev 3:** 03.13.01, 03.03.01, 03.14.06 · **Owner:** Repo · **Size:** S · **Depends on:** N15

**Scope:** flow logs (traffic type ALL) to CloudWatch, using the logs key and ODP retention
(`src/network_stack.py`).

**Acceptance criteria:** `tests/unit/test_network_stack.py` asserts the flow log, its key and its
retention.

### N31 — VPC endpoints
**Rev 3:** 03.13.01, 03.13.06 · **Owner:** Repo · **Size:** M

**Why:** The only endpoint is the S3 gateway (`src/network_stack.py:41-46`). `VpcEndpointSG`
(`:27-38`) is created but unused. Every AWS call from the workers goes through NAT.

**Scope:**
- A DynamoDB gateway endpoint.
- Interface endpoints for STS, CloudWatch Logs, SQS, Bedrock runtime, and SSM,
  ssmmessages and ec2messages (for the bastion). They use `VpcEndpointSG` and have endpoint
  policies scoped to the account.
- Record the cost as a scaling path (signal plus cost).

**Acceptance criteria:**
- Stack test asserts the endpoints.
- In dev, flow logs show worker traffic to AWS services on endpoint ENIs, not NAT.

### N32 — Deny-by-default egress
**Rev 3:** 03.13.06, 03.01.03 · **Owner:** Repo · **Size:** M · **Gate:** yes · **Depends on:** N31

**Why:** `allow_all_outbound=True` on the query worker, agent worker and loader SGs
(`src/neptune_api_stack.py:80-86`, `src/neptune_agent_stack.py:87-93`,
`src/neptune_pipeline_stack.py:101-107`). Neptune egresses to 0.0.0.0/0:443
(`src/neptune_stack.py:27-43`; issue #15).

**Scope:**
- Each SG gets `allow_all_outbound=False`, with explicit rules to Neptune SG:8182, the endpoint
  SG:443 and the S3 prefix list.
- Neptune egress goes to the S3 prefix list only.

**Acceptance criteria:**
- `tests/compliance/test_network.py` (03.13.06) fails on any 0.0.0.0/0 or `::/0` egress not on a
  documented exception list.
- Dev soak: no flow-log REJECTs for expected traffic.

### N33 — WAF
**Rev 3:** 03.01.08, 03.13.01, 03.14.06 · **Owner:** Repo · **Size:** M

**Why:** No WAF anywhere. Spec 001's research rejected it for cost. CUI changes the trade-off.

**Scope:**
- A WebACL on the API stage with:
  - AWS managed rule groups: common, known bad inputs, IP reputation;
  - a per-IP rate-based rule (ODP).
- WAF logs to a managed log group.
- A decisions.md entry superseding the spec 001 rejection.

**Acceptance criteria:**
- Stack test asserts the association and the rules.
- `tools/load_probe.py burst` from one IP trips the rate rule in dev.

### N34 — Publicly accessible surfaces and the PR template
**Rev 3:** 03.01.22, 03.13.01, 03.04.04 · **Owner:** Repo · **Size:** S

**Why:** CORS allows `*` (`src/api_edge_stack.py:193-202,230`). `/api` and its contract are
public, as are the mock-404 routes.

**Scope:**
- Contract first: CORS origins come from config (`API_EDGE.cors_origins`), recorded as a
  deviation (spec.md D-5).
- A documented review confirming the public `/api` content carries no CUI. Re-review on every
  contract change: a PR-template checkbox.
- A security-impact checkbox in `.github/PULL_REQUEST_TEMPLATE.md` (auth, IAM, network, data
  flow, logging) for 03.04.04.
- Escape `<` in the docs page's spec URL (the low finding from the 2026-10-13 security review).

**Acceptance criteria:** the contract vectors cover the allowed and disallowed origins.

---

## Phase 4: Identity and audit content

### N41 — Authorizer hardening
**Rev 3:** 03.05.12, 03.01.11, 03.03.02, 03.05.11 · **Owner:** Repo · **Size:** S

**Why:**
- Raw tokens are dict keys in an unbounded cache with its own TTL constant (`authorizer.py:20-21`),
  not `limits.AUTH_CACHE_TTL_SECONDS`.
- `auth_deny` logs only the reason (`:37`).
- The fail-mode comment is stale (`:39-41`).
- The probes take a PAT through `--token` (`tools/load_probe.py`, `tools/parity_probe.py`).

**Scope:**
- Cache keyed by the SHA-256 of the token, a bounded LRU, and the TTL from `limits`.
- Deny logs carry the request id, source IP and reason.
- Fix the comment.
- The probes read `SYNAPSE_AUTH_TOKEN` from the environment.

**Acceptance criteria:** authorizer tests cover the hashed key, eviction, and the deny log
fields.

### N42 — Audit record content and traceability
**Rev 3:** 03.03.02, 03.03.03, 03.03.07 · **Owner:** Repo · **Size:** M · **Gate:** yes

**Why:**
- The access-log format is `json_with_standard_fields` (`src/api_edge_stack.py:207`). Its `user`
  and `caller` fields are empty under a Lambda authorizer, so log lines can't be joined to a
  principal.
- `agent_invocation` has no `source_ip` (`src/lambda_agent/agent.py:281-313`).
- `kg_load` has no actor: the state machine passes only bucket and object
  (`src/neptune_pipeline_stack.py:166-172`).
- Timestamps are epoch seconds.

**Scope:**
- A custom access-log format adding `$context.authorizer.principalId`, `requestId`,
  `extendedRequestId` and the user agent.
- `agent_invocation` adds `source_ip`.
- The state machine input carries the S3 event's `request-id`, `version-id`, `etag`, `sequencer`,
  `source-ip-address` and `requester`, and `kg_load` logs them.

  `requester` is only the AWS **account ID** (S3 EventBridge docs, checked 2026-10-28), so the
  uploader's identity comes from CloudTrail, joined on `request-id` (N46).
- Every audit event adds an ISO-8601 UTC `time`.
- Step Functions logging level `ALL` with execution data excluded.
- `docs/compliance/audit-events.md`: one schema for every event, and what each answers (who,
  what, when, where, outcome).

**Acceptance criteria:** handler and stack tests assert every field in the schema. One Logs
Insights query joins an access-log line to its `sparql_query` line by `job_id` and principal.

### N43 — CUI out of side channels
**Rev 3:** 03.13.08, 03.14.08, 03.08.02, 03.01.03 · **Owner:** Repo · **Size:** M · **Gate:** yes · Decides Q-4

**Why:** Failure emails include Neptune's error and cause (`src/neptune_pipeline_stack.py:416-421`),
and email leaves the boundary unencrypted. Logs hold full queries and questions (issue #21). DLQs
keep message bodies, with the query and question text, for 14 days.

**Scope:**
- Emails carry the execution link, the snapshot name and the status only.
- Decide Q-4, full text vs redaction (log groups holding CUI are logs-key encrypted, with
  read access restricted to a named role).
- DLQ retention and the job TTL become ODPs.
- Apply N05's `/ask` decision (the `AGENT.enabled` flag, if needed).

**Acceptance criteria:** pipeline stack test asserts the email template has no `error` or `cause`
field. ODP tests. A decisions.md entry for Q-4.

### N44 — Security monitoring and audit-failure alerts
**Rev 3:** 03.03.04, 03.03.05, 03.03.06, 03.14.06, 03.06.02 · **Owner:** Repo · **Size:** M · **Gate:** yes · **Depends on:** N33, N42

**Why:**
- Alarms are operational only.
- They all use `TreatMissingData.NOT_BREACHING` (`src/api_edge_monitoring.py:132`,
  `src/monitoring_stack.py`, `src/neptune_pipeline_stack.py`).
- The alarm topic exists only when `API_EDGE.enabled` (`app.py:90-106`).

**Scope:**
- Alarms on the 401/403 rate, WAF blocked requests, authorizer errors, and flow-log rejects to
  Neptune.
- An **audit heartbeat**: a metric filter on `sparql_query` against the API request count, which
  alarms when requests arrive but audit events don't (missing data = breaching).
- Move the alarm topic out of `ApiEdgeStack` into the monitoring stack, so it always exists. This
  needs a two-PR export move.
- Saved Logs Insights `QueryDefinition`s for audit review: per principal, per IP, failed auth,
  loads per snapshot (N46).

**Acceptance criteria:** `tests/unit/test_monitoring_stack.py` asserts each alarm and the
heartbeat's missing-data behaviour.

### N45 — Least-privilege pass
**Rev 3:** 03.01.05, 03.01.06, 03.01.07 · **Owner:** Shared · **Size:** M

**Why:**
- `grant_read_write_data`, `grant_write_data` and `grant_read_data` give more than the code uses
  (Delete, Scan and BatchWrite are unused; `src/api_edge_stack.py:105-110`, the worker stacks).
- Bedrock is granted on `arn:aws:bedrock:*::foundation-model/anthropic.claude-*`
  (`src/neptune_agent_stack.py:175`).
- Leftover roles remain in both accounts (open-work, T504).

**Scope:**
- Exact actions per role.
- Bedrock limited to the configured model and profile ARNs in the env's region.
- IT deletes the leftover roles, or they are documented as accepted.

**Acceptance criteria:** stack tests assert the exact action lists. cdk-nag (N50) reports no IAM
wildcard without a suppression.

---

### N46 — Load provenance: link each load to the uploads behind it
**Rev 3:** 03.03.01, 03.03.02, 03.03.03, 03.03.08, 03.08.02 · **Owner:** Repo · **Size:** M · **Gate:** yes · **Depends on:** N13, N24

**Why:**
- **Nothing links a named graph to the bytes and the uploader behind it.** The state machine
  passes only `bucket` and `object` (`src/neptune_pipeline_stack.py:166-172`), and the S3 event
  names only the account.
- **The bulk loader reads the whole `data/` prefix at load time, without any record of it.**
  Nothing records which object versions it read.

**Scope:**
- **Snapshot contents record.** At `StartLoad`, the loader lists `{portal}/{tag}/data/` with each
  object's version id, etag and size. It stores the list with the load's lineage row, or as a
  JSON object under a `_loads/` prefix if that is too big for an item. At completion it lists
  again. Any difference fails the load as `SnapshotChangedDuringLoad`, and the alarm fires.
- **A bucket-scoped CloudTrail trail, built here** (`NEPTUNE.data_events_trail`, on by default):
  - advanced event selectors log only **write data events on the data bucket**, with no
    management events, so there's no paid duplicate of the org trail;
  - logs go to a dedicated log bucket in this account, encrypted, with log-file validation,
    Object Lock and ODP retention;
  - depositors and the workers have no access to the log bucket.
- **Upload identity.** No change on the depositor side:
  1. A data event gives the role (`userIdentity.sessionContext.sessionIssuer`). Each role maps to
     one repo, so the role alone names the depositor repo. It also gives the session, source IP,
     time and the temporary access key.
  2. For the branch, that access key's `AssumeRoleWithWebIdentity` event gives the GitHub `sub`
     (repo and ref). It comes from the org trail, or the account's 90-day event history (N03).

  `docs/compliance/audit-events.md` documents the join. A saved query takes a graph URI and
  returns:
  - the manifest's upload, from its `request-id`;
  - every `data/` object version that was loaded;
  - each one's role, repo and branch, source IP and time.
- **Optional for depositors, one line:** `role-session-name` including `${{ github.run_id }}` in
  their workflow, so every S3 record names the exact Actions run. Without it, find the run by
  repo, branch and time.

**Acceptance criteria:**
- `tests/unit/test_loader_handler.py` covers the contents record and the changed-during-load
  failure.
- In dev, after one NF pipeline run, the saved query returns the repo, branch and object versions
  for its graph. The output is saved as evidence.

## Phase 5: Configuration management, supply chain, integrity

### N50 — cdk-nag and compliance-as-code tests
**Rev 3:** 03.04.01, 03.04.02, 03.12.03 · **Owner:** Repo · **Size:** M · **Depends on:** most of phases 1–3

**Scope:**
- `Aspects.of(app).add(NIST80053R5Checks())` in `app.py`. Rev 3 derives from the 800-53 moderate
  baseline, so this is the closest maintained pack.
- Suppressions in `src/nag_suppressions.py`, each with a reason and a requirement id.
- `tests/compliance/` collects the per-requirement assertions from earlier tickets.
- Adopt constitution **article VIII, "Compliance as code"**, with a decisions.md entry.

**Acceptance criteria:**
- `cdk synth` fails on an unsuppressed nag error.
- The CI synth job runs nag.
- The constitution and decisions.md are updated in this PR.

### N51 — Security scanning in CI
**Rev 3:** 03.11.02, 03.14.01, 03.14.02, 03.14.03, 03.17.03 · **Owner:** Repo · **Size:** M

**Why:** No scanning, and no Dependabot or Renovate. Pre-commit isn't run in CI.

**Scope:**
- On every PR and weekly: pip-audit (every requirements file and `core/`), bandit, checkov on
  `cdk.out`, trivy (the Lambda bundles), gitleaks, CodeQL (Python),
  and pre-commit. SARIF goes to code scanning.
- `.github/dependabot.yml` for pip and github-actions.
- Flaw-remediation ODPs, and a critical finding blocks merge.

**Acceptance criteria:** a seeded vulnerable pin in a test PR fails the check.

### N52 — Pinning and reproducible builds
**Rev 3:** 03.17.03, 03.04.08, 03.16.02 · **Owner:** Repo · **Size:** M

**Why:**
- Actions are pinned by tag (`actions/checkout@v3`, `configure-aws-credentials@v5.1.0`).
- `npm install -g aws-cdk` and `snap install aws-cli` are unpinned (`aws-deploy.yaml:40-42`).
- Requirements have no hashes, and `strands-agents>=1.40.0,<2.0.0` is a range.
- `core/` dependencies are unpinned.
- Lambdas are on 3.11 while the tooling is on 3.13.

**Scope:**
- SHA-pinned actions (Dependabot keeps them current).
- Pinned CDK CLI (to match `tools/setup.sh`) and AWS CLI.
- `.python-version`.
- Hash-locked requirement files, with `--require-hashes` in bundling.
- Bundling images by digest.
- An SBOM (syft) attached to each deploy run.
- A runtime end-of-life entry in the POA&M.

**Acceptance criteria:**
- CI fails on an unpinned action (a check such as `zizmor` or an equivalent).
- Bundling fails on a hash mismatch.

### N53 — Baseline tags, protection and inventory
**Rev 3:** 03.04.01, 03.04.10, 03.08.04 · **Owner:** Repo · **Size:** S · **Depends on:** N01

**Why:** The only org tag is `CostCenter: NO PROGRAM / 000000`. There is no stack termination
protection.

**Scope:**
- Tags `System`, `Environment`, `Owner`, `DataClassification` (per resource, from N01) and a real
  `CostCenter`.
- Termination protection on the network, neptune and pipeline stacks.
- `tools/inventory.py` lists every resource from `cdk.out` with its tags, as evidence.

**Acceptance criteria:** `tests/compliance/test_tags.py` fails on an untagged taggable resource.

### N54 — Least functionality
**Rev 3:** 03.04.06, 03.04.08 · **Owner:** Repo · **Size:** S

**Scope:**
- Remove the leftover template PNGs in `docs/`.
- Confirm dev-only tools (the probes, the bastion) can't run against a CUI env without the
  reason tag from N16.
- List each enabled API route and its purpose in the SSP.

**Acceptance criteria:** the leftover PNGs are gone, and the SSP's route list matches the
contract's paths.

---

## Phase 6: Portability

### N60 — Org-specific values into config
**Rev 3:** 03.04.02 · **Owner:** Repo · **Size:** M

**Why:**
- Role ARNs and account IDs are in the workflows (`deploy-dev.yaml:15`, `deploy-prod.yaml:15`).
  The stage workflow has a placeholder (`deploy-stage.yaml:15`).
- The env names are hardcoded (`src/utils.py:22`).
- The portal namespaces are in the agent prompt (`src/lambda_agent/agent.py:46-50`).
- `config/base.yaml` gives every env the prod team id.

**Scope:**
- Role ARNs move to GitHub Environment variables (`vars.AWS_DEPLOY_ROLE_ARN`).
- The env allow-list is derived from the files in `config/`.
- `AGENT.namespaces` in config.
- `AUTH.synapse_team_id` has no base default, and synth fails without one (spec.md D-7).
- The stage placeholder is resolved or the workflow deleted.

**Acceptance criteria:**
- `tests/unit/test_utils.py` covers the required keys.
- A grep test fails on a 12-digit account id, the egress IP, or a `sagebase.org` email address
  outside `config/` and `docs/`. The Synapse endpoints in `authorizer.py` are allowed (spec.md,
  Principles).

### N61 — Partition- and region-agnostic
**Rev 3:** 03.16.01 · **Owner:** Repo · **Size:** S · **Depends on:** N06

**Why:** Literal `arn:aws:` in `src/neptune_agent_stack.py:175-176`,
`src/lambda_authorizer/authorizer.py:157` and `tools/neptune_bastion/app.py:74`. `cdk.json`
targets only `aws` and `aws-cn`. The `us.` inference-profile prefix is fixed. Several places
default to `us-east-1`.

**Scope:**
- `Stack.partition` and `Arn.format` in stacks. The partition comes from the method ARN in the
  authorizer.
- `aws-us-gov` in `target-partitions`.
- The profile prefix from config.
- The region from config or the environment, with no literal defaults.

**Acceptance criteria:** `tests/compliance/test_partition.py` synthesizes for `aws-us-gov` and
asserts no `arn:aws:` literal.

### N62 — Deploying in a new organization
**Rev 3:** 03.15.02, 03.16.01 · **Owner:** Repo · **Size:** M · **Depends on:** N03, N60

**Scope:**
- `docs/compliance/deploying-in-a-new-org.md`.
- `config/_template.yaml`, with every required key commented.
- `tools/bootstrap/`: a least-privilege deploy role and OIDC provider template, and the CDK
  bootstrap command with its qualifier.
- The steps for Bedrock model access, SNS confirmation and running the preflight.

**Acceptance criteria:** someone outside the team follows the guide into a scratch account (with
the IdP stubbed), and synth and the preflight pass. Their notes are recorded.

---

## Phase 7: Plans, assessment, operations

### N70 — System Security Plan
**Rev 3:** 03.15.02, 03.12.05, 03.15.03 · **Owner:** Shared · **Size:** L · **Depends on:** N01, N02, N03

**Scope:** `docs/compliance/ssp.md`:
- system description, boundary (N01) and environments;
- users and roles;
- interconnections and information exchange (Synapse, Bedrock, GitHub, npm);
- an implementation statement per requirement, generated from or linked to the matrix;
- the rules of behaviour for team members, owned by IT and linked;
- org-specific values marked as parameters.

**Acceptance criteria:** IT's security lead reviews it, with the review recorded in
`docs/memory/security-reviews.md`.

### N71 — Plan of Action and Milestones
**Rev 3:** 03.12.02 · **Owner:** Shared · **Size:** S · **Gate:** yes

**Scope:**
- `docs/compliance/poam.md`: every Partial or Planned matrix row, with its weakness, owner,
  milestone date and ticket. Started 2026-10-27 with POAM-001 to POAM-003 (MFA, the authorizer
  cache TTL, failed attempts). This ticket adds the rest.
- Security-relevant open-work entries link to it, so it isn't mirrored.

**Acceptance criteria:** the POA&M matches the matrix. A check script counts both.

### N72 — Risk and supply-chain assessment
**Rev 3:** 03.11.01, 03.11.04, 03.17.01, 03.17.02, 03.16.03 · **Owner:** Shared · **Size:** M

**Scope:** `docs/compliance/risk-assessment.md`, covering:
- the threats to the boundary;
- each external service and supplier: Synapse (IdP, roster, data source), AWS (FedRAMP scope per
  service), Anthropic via Bedrock, GitHub Actions, npm (Swagger UI), and
  strands-agents with its transitive dependencies;
- the response per risk;
- a supply-chain risk-management plan section (03.17.01).

### N73 — Incident response annex
**Rev 3:** 03.06.01, 03.06.02, 03.06.03, 03.06.05, 03.08.03 · **Owner:** Shared · **Size:** M

**Scope:** `docs/compliance/incident-response.md` with system playbooks:
- **CUI spill or wrong data loaded:** drop the named graph (bastion), purge S3 versions, purge
  log events, DLQ messages and job items, and consider key rotation;
- **a leaked PAT:** revoke in Synapse, wait out the cache TTL, review the audit logs;
- **a compromised depositor role;**
- **Bedrock data exposure.**

Each playbook links to the org IR plan and its DFARS 252.204-7012 72-hour reporting step (IT). Run
one tabletop exercise.

**Acceptance criteria:** the tabletop is recorded with its date and findings. New gaps go on the
POA&M.

### N74 — Restore test and sanitization
**Rev 3:** 03.08.03, 03.08.09 · **Owner:** Repo · **Size:** M

**Scope:**
- A tested Neptune restore from a snapshot.
- A tested rebuild from the S3 lake.
- The lineage table's PITR restore.
- A decommission procedure: delete the stacks, their snapshots and the S3 versions, and
  document what `RETAIN` leaves behind. AWS-managed keys can't be scheduled for deletion, so
  there is no crypto-shred: data is destroyed by deleting it.

**Acceptance criteria:** a runbook in `docs/memory/runbooks/`, and dated evidence of each test.

### N75 — Continuous monitoring and assessment readiness
**Rev 3:** 03.01.01, 03.09.02, 03.12.01, 03.12.03, 03.14.03 · **Owner:** Shared · **Size:** S

**Scope:**
- A quarterly review of the Synapse team roster, which is the authorized-user list. Leavers are
  removed within the ODP, following IT's termination process.
- A monthly review of findings (Security Hub, scans, nag suppressions).
- Recurring entries in `docs/memory/security-reviews.md`.
- A self-assessment against the matrix before any external assessment.

**Acceptance criteria:** the first review of each kind is recorded.

### N76 — System use notification and CUI marking
**Rev 3:** 03.01.09, 03.08.04 · **Owner:** Repo · **Size:** S

**Scope:**
- Contract first: a system-use notice in `info.description` and on the `/api` page.
- An `x-data-classification` header on job responses from CUI envs.
- The notice text comes from IT.

**Acceptance criteria:** contract vectors assert the header. The docs handler test asserts the
notice.
