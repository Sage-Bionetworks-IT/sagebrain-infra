# 005 — NIST SP 800-171 Rev 3: making Sage Brain fit to hold CUI

**Status:** Draft. Planning only; no implementation yet.
**Owner:** Thomas Yu
**Standard:** [NIST SP 800-171 Rev 3](https://csrc.nist.gov/pubs/sp/800/171/r3/final) (May 2024): 97
requirements in 17 families. [control-matrix.md](control-matrix.md) also gives the Rev 2 numbers,
which CMMC 2.0 Level 2 still assesses against.
**Interacts with:**
- spec 002: per-caller ACL and access requirements (03.01.02). Its FR-11 (job ownership) is pulled
  forward here as N11.
- spec 004: latest-snapshot scoping. This spec doesn't depend on it.
- spec 001 R-16: a custom domain, which N23 needs.

**Contract changes (planned, each in its own ticket):** `api/openapi.yaml`:
- status operations become owner-only (N11);
- `servers` and endpoint type change (N23);
- the CORS origin list narrows (N34);
- a system-use notice and an `x-data-classification` response header are added (N76).

**Files:** this spec (the epic), [plan.md](plan.md) (phases, ordering, rollback),
[tickets.md](tickets.md) (one entry per ticket, with acceptance criteria) and
[control-matrix.md](control-matrix.md) (all 97 requirements, with status, owner, ticket and
evidence).

## Problem
Sage Brain will hold **Controlled Unclassified Information (CUI)**. A repository scrub
(2026-10-23, at `dev` `b313b47`) found the build was designed for open research metadata. It
falls short of SP 800-171 in every family it controls. The worst gaps, with evidence:

1. **Job results aren't per user.** Job items store no owner, so any team member who has a job id
   can read its results and the agent's steps (`src/lambda_edge/jobs.py:120-153`).
2. **Long-lived bearer tokens.** MFA is Synapse's: every login requires it, so API users inherit
   it (N04). But API callers use Synapse PATs, which are bearer tokens with no `exp`, so a leaked
   PAT works until it's revoked. Synapse has no maximum PAT age (POAM-001). The authorizer's
   300 s cache uses its own constant, not the declared limit, and nothing limits repeated failed
   attempts (`src/lambda_authorizer/authorizer.py:18-69`). POA&M entries POAM-001 to POAM-003
   ([docs/compliance/poam.md](../../docs/compliance/poam.md)) track these.
3. **An unattributed write path into the graph.** A `manifest.ttl` upload loads Neptune with no
   human in the loop, and no record ties a graph to its uploader.
   - The bucket policy lets any principal in the account Put, Get or Delete objects
     (`src/neptune_stack.py:61-77`).
   - IT's three depositor OIDC roles (organizations-infra #1605) can each write, delete and
     `DeleteObjectVersion` on every portal's prefix. One of them (`Sage_Brain`) isn't used at all.
   - The S3 event names only the account, and no CloudTrail data events are confirmed for the
     bucket. POAM-004 tracks this.
4. **The Neptune audit log is exported but disabled.** There is no parameter group, so
   `neptune_enable_audit_log=0` (`specs/004-latest-graph-scoping/research.md`, Q7).
5. **Audit logs aren't managed.**
   - The Lambda log groups holding `sparql_query`, `agent_invocation` and `kg_load` aren't in IaC
     and never expire.
   - Managed groups keep 30 days.
   - Every alarm treats missing data as `NOT_BREACHING`, so if logging stops, nothing alarms.
6. **Gaps in encryption.**
   - The SNS alarm topic is unencrypted. Other storage uses AWS-managed or AWS-owned keys, which
     is accepted (2026-11-03).
   - No FIPS endpoints.
   - The API uses the default EDGE endpoint with no TLS security policy: dev and prod both accept
     TLS 1.0 and 1.1 (probed 2026-11-03).
7. **The network allows everything out.**
   - No VPC flow logs and no WAF.
   - The only VPC endpoint is S3, so AWS API traffic leaves through NAT.
   - Every Lambda and ECS security group allows all outbound, and Neptune egresses to
     0.0.0.0/0:443.
8. **CUI leaves the boundary in side channels.**
   - Failure emails carry Neptune's load errors (`src/neptune_pipeline_stack.py:416-421`).
   - Logs hold full queries and questions, and DLQs hold them for 14 days.
   - The agent's prompts go to Bedrock under an unverified FedRAMP scope.
9. **Supply chain.**
    - No dependency, SAST, IaC or image scanning; no Dependabot; no cdk-nag.
    - Actions are pinned by tag, not SHA.
    - The CDK and AWS CLIs are unpinned in CI.
    - No hash-locked requirements; `strands-agents` is a version range.
10. **Not transferable.**
    - Account IDs and role ARNs are in the workflows.
    - ARNs use a literal `arn:aws:`, which breaks GovCloud.
    - `config/base.yaml` defaults every env to the **prod** Synapse team.
    - `cdk.json` doesn't target `aws-us-gov`.
11. **No assessor documentation.**
    - No SSP, boundary, data classification, POA&M, risk assessment or incident-response annex.
    - `aws_architecture.svg` still shows the two pre-spec-001 APIs.

What already holds up, and counts as evidence:
- Change control: the `dev` ruleset requires a reviewed squash PR (1 approval, added 2026-10-27),
  and only CI deploys. `--require-approval never` is accepted, because review happens at the PR
  and not at deploy time (decisions.md).
- OIDC deploys with no static keys.
- Neptune has IAM auth, encrypted storage, SG-to-SG ingress only, and deletion protection in prod.
- The single `sparql_query` chokepoint (constitution V).
- S3 has BlockPublicAccess, `enforce_ssl` and versioning.
- The bastion uses SSM only and requires IMDSv2.
- Swagger UI is pinned with sha512 at bundle time.
- The authorizer fails closed and is team-gated.
- Every threshold is declared in one place with a test (constitution III), the natural home for
  Rev 3's organization-defined parameters.

## Scope and responsibility
- **System boundary:** each env that holds CUI (today, prod): its stacks in `app.py`
  (`app-{env}-*`), the temporary bastion (`tools/neptune_bastion/`), the CI/CD pipeline that
  deploys them, and the S3 data lake. N01 writes the authoritative boundary and data-flow diagram.
- **Dev never holds CUI** (2026-11-03), so it's outside the boundary. It stays the env where each
  control is proven before prod. Dev-only components, such as Graph Explorer (SKG-230, #102), need
  no CUI hardening.
- **External systems:**
  - Synapse: identity provider, team roster and data source.
  - Amazon Bedrock and the Anthropic models behind it.
  - GitHub (source and Actions).
  - npm (Swagger UI at bundle time).
- **Inherited from Synapse** (decided 2026-11-03): MFA for API users. Synapse requires MFA at
  login, and the PATs and OAuth tokens the API accepts are created from a logged-in session. N04
  records the evidence.
- **Inherited from Sage IT** (decided 2026-10-23). The repo **documents and checks** these but
  doesn't build them:
  - org CloudTrail, GuardDuty, AWS Config, Security Hub and Inspector;
  - IAM Identity Center with MFA for operators, and SCPs;
  - the CDK bootstrap, the OIDC provider and the deploy role;
  - the GitHub org settings;
  - the AT (03.02), PS (03.09) and PE (03.10) programs, and the org-level incident-response and
    maintenance programs.

  **Exception (2026-10-28):** the data bucket's S3 data events come from a bucket-scoped trail in
  this repo (N46), not the org trail. The bucket and its audit trail live in the sagebrain
  account, and the repo owns both.
- **Transferable by design.** Another org deploys the same code into its own CUI enclave by
  writing a config file and meeting the documented prerequisites (N62). The prerequisites are
  checked by `tools/compliance_preflight.py` (N03).

## Principles
- **No CUI until the gate closes.** No environment loads CUI until every ticket in the CUI gate
  (below) is done and verified in that environment. Dev never loads CUI. Remaining gaps then go on the POA&M (N71).
- **Compliance as code.** Every requirement implemented in this repo has an automated check named
  by its requirement ID: a cdk-nag rule, a template assertion in `tests/compliance/`, or a unit
  test. This spec proposes constitution **article VIII**, adopted in N50's PR with its
  decisions.md entry.
- **ODPs are thresholds.** Every Rev 3 organization-defined parameter the system enforces is a
  threshold under constitution III: declared once (`config/` `COMPLIANCE:` or `x-sagebrain-limits`),
  enforced in one place, with a test.
- **Inherited means checked.** An inherited control is backed by IT's evidence and by a preflight
  check that the service is on in the target account, not by an assumption.
- **Fail closed, as today.** New controls deny on error (the authorizer).
- **Config, not forks.** Anything that varies by org, account, partition or region comes from
  config. No Sage-specific value lives in code, except the Synapse endpoints: Synapse is the
  system's identity provider and data source wherever it's deployed, so its production URLs stay
  constants in `authorizer.py` (accepted 2026-10-27).

## User stories
- As an **assessor**, I can trace every Rev 3 requirement to its status, owner, implementing
  ticket and evidence (file:line, test, or IT artifact) in one matrix and in the SSP.
- As a **researcher**, only I can read my job results, and I sign in with MFA.
- As a **data steward**, only named depositor roles can write the data lake. Every load records
  who uploaded it, from where, and into which graph.
- As an **operator**, I'm alerted when logging stops, when authentication failures spike, or
  when a load fails. The alert itself contains no CUI.
- As **another organization**, I deploy the build into my own account or GovCloud by writing
  `config/<env>.yaml`, setting repository variables and running the preflight. I change no code.

## Functional requirements
Each FR names the tickets that deliver it. [tickets.md](tickets.md) holds the acceptance criteria
and their tests.

### Scope and governance
- **FR-1 Boundary and data classification** (N01). A boundary diagram and data-flow list name
  every component that stores, processes or transmits CUI, and the CUI category.
- **FR-2 Control matrix** (N02). All 97 Rev 3 requirements carry a status, an owner and evidence.
  Every Planned or Partial row links a ticket.
- **FR-3 Inherited controls are verified** (N03). `tools/compliance_preflight.py` checks every
  account-level prerequisite read-only and exits non-zero on a gap.

### Access control and identification
- **FR-4 Job ownership** (N11, spec 002 FR-11). A status read by anyone but the job's owner
  returns 404.
- **FR-5 MFA** (N04, inherited from Synapse). Every token the API accepts comes from a Synapse
  login, and every Synapse login requires MFA. N04 records the evidence. PATs have no maximum age, a
  Synapse limitation tracked as POAM-001.
- **FR-6 Unsuccessful attempts** (N33, N44). Per-IP rate limiting at WAF, and an alarm on the
  401/403 rate.
- **FR-7 Least privilege** (N13, N45). IAM grants name exact actions on exact resources.
  cdk-nag (N50) fails synth on a wildcard that isn't suppressed with a reason.
- **FR-8 No unauthenticated access to CUI** (done, #102). In prod, CUI is reached only through the
  authenticated API. Graph Explorer, which has no login, runs only in dev;
  `test_graph_explorer_is_dev_only` keeps it off in prod.

### Audit and accountability
- **FR-9 Complete audit events** (N14, N42, N46). The Neptune audit log is on. Every `/query`,
  `/ask`, status read and load record names the principal, source IP, request id and a UTC
  timestamp. Every named graph traces to the object versions it loaded and, through CloudTrail,
  to the depositor repo and branch that uploaded them.
- **FR-10 Managed, protected audit logs** (N15). Every log group is in IaC, with ODP
  retention and `RETAIN`.
- **FR-11 Logging-failure alerts** (N44). A heartbeat alarm fires when audit events stop while
  requests continue.

### Protection of CUI
- **FR-12 Encryption at rest** (N21). Everything that stores CUI is encrypted. AWS-managed and
  AWS-owned keys are accepted (2026-11-03): S3, Neptune, DynamoDB, SQS, logs and Step Functions
  keep them. The SNS topic gets the one customer-managed key, because CloudWatch alarms and
  EventBridge can't publish to a topic on the AWS-managed `aws/sns` key.
- **FR-13 Encryption in transit** (N23, N21). TLS 1.2 or higher, from FIPS-validated modules
  where the partition offers them. Every queue, topic and bucket denies non-TLS access.
- **FR-14 Deny-by-default network** (N30–N32). Flow logs on, AWS traffic through VPC endpoints,
  and no 0.0.0.0/0 egress except documented exceptions.
- **FR-15 No CUI in side channels** (N43). Emails, DLQ payload retention and logs are covered by
  the N43 decision.

### Configuration management and supply chain
- **FR-16 Controlled change** (N10, done). Changes reach `dev` only through a reviewed PR, and
  only CI deploys. N10 exports the rulesets as dated evidence.
- **FR-17 Scanned, pinned, inventoried** (N50–N53). Dependency, SAST, IaC, image and secret
  scanning in CI, with SHA-pinned actions, hash-locked dependencies, digest-pinned images, an SBOM
  per deploy, and a tag-based inventory.

### Transferability
- **FR-18 Org values in config** (N60). No Sage-specific value lives in code, except the Synapse
  endpoints (see Principles).
- **FR-19 Partition- and region-agnostic** (N61). Synth passes for `aws` and `aws-us-gov`.
- **FR-20 New-org guide** (N62). An org deploys with a config file, repository variables and the
  preflight.

### Plans and operations
- **FR-21 SSP, POA&M, risk assessment, incident-response annex** (N70–N73).
- **FR-22 Restore, sanitization and continuous monitoring** (N74, N75).
- **FR-23 System-use notice and CUI marking** (N76).

## Non-functional requirements / thresholds (organization-defined parameters)
These are **proposed** values. N02 confirms them with Sage IT and the data owner, and each lands
with its test.

| ODP | Proposed value | Rev 3 | Declared in | Enforced in | Test |
|---|---|---|---|---|---|
| Audit log retention | 365 days online | 03.03.08 | `COMPLIANCE.log_retention_days` | every `logs.LogGroup` (N15) | template assertion |
| Authorizer cache TTL (session re-check) | 300 s (today) | 03.01.11, 03.05.01 | `x-sagebrain-limits` `auth_cache_ttl_seconds` | authorizer | `tests/unit/core/test_limits.py` |
| Unsuccessful-attempt limit | per-IP WAF rate (N33 sets the value) | 03.01.08 | `x-sagebrain-limits` | WAF rate rule | template assertion |
| Job result TTL | 24 h (today) | 03.14.08 | `x-sagebrain-limits` `job_ttl_seconds` | `jobs.py` | existing limits test |
| DLQ payload retention | 14 days (today; N43 may lower it) | 03.14.08 | `COMPLIANCE.dlq_retention_days` | SQS DLQs | template assertion |
| Neptune backup retention | 35 days | 03.08.09 | `NEPTUNE.backup_retention_days` | cluster | template assertion |
| Noncurrent S3 version expiry | 90 days | 03.08.03 | `COMPLIANCE.noncurrent_version_days` | bucket lifecycle | template assertion |
| Flaw remediation | critical 15 d, high 30 d, other 90 d | 03.14.01 | `docs/compliance/` | Dependabot, scans (N51) | CI gate on critical |
| Vulnerability scanning | every PR and weekly | 03.11.02 | workflow schedule | N51 | workflow exists |
| Access review of the team roster | quarterly | 03.01.01 | SSP | N75 | review record |
| Minimum TLS | 1.2 | 03.13.08 | `COMPLIANCE.min_tls` | API policy, S3 `s3:TlsVersion` | template assertion |

## Deviations from current behaviour
Each deviation lands with the ticket that makes it, and its test records the old behaviour
(constitution II).
- **D-1 Owner-only job status** (N11). Any team member can read any job → only its owner; other
  callers get 404.
- **D-2 Graph Explorer removed from prod** (done, SKG-230, #102). Reachable over the Sage VPN in
  dev and prod → dev only. Prod users query through `/query` and `/ask`.
- **D-3 Data bucket writers** (N13). Any account principal can Put and Delete → named depositor
  roles can Put; Delete needs the break-glass role.
- **D-4 API endpoint** (N23). EDGE `execute-api` with the default TLS policy → REGIONAL with a
  TLS 1.2+ policy. The base URL may change, so clients get notice per spec 001's sunset rules.
- **D-5 CORS** (N34). `*` → configured origins.
- **D-6 Alarm emails** (N43). They carry Neptune's error and cause → a console link and the
  snapshot name only.
- **D-7 Default Synapse team** (N60). Stage silently inherits prod's team → synth fails until an
  env names its own team.

## CUI gate
No environment loads CUI until all of these are done there:

| Phase | Tickets |
|---|---|
| 0: Scope and decisions | N01, N02, N03, N04, N05, N06 |
| 1: High-risk fixes | N11, N13, N14, N15 |
| 2: Cryptography | N21, N23 |
| 3: Network | N32 |
| 4: Identity and audit | N42, N43, N44, N46 |
| 7: Assessment | N71 (the POA&M covers everything not yet done) |

## Out of scope
- **Building account-level security services.** They are inherited (see Scope). Only their
  preflight checks are in scope.
- **The IT-owned AT, PS and PE programs, and org-level IR and MA.** The repo adds the
  system-specific annexes only (N73, N16).
- **Graph-level ACLs and access requirements.** These are spec 002. This spec only requires that
  governance be on wherever CUI is (N01 decides which portals).
- **A formal third-party assessment (C3PAO).** N75 prepares for one.
- **Availability and DR beyond backups.** Rev 3 has no contingency-planning family. Restore
  testing is covered by N74.

## Open decisions
Each is settled by a spike ticket (or a dated decision) and recorded in decisions.md:
- **Q-1 MFA mechanism for API callers.** *Settled 2026-11-03:* inherited from Synapse, which
  requires MFA at login (N04 records the evidence). The alternatives were a 2FA check at auth time,
  OAuth-only tokens, or an org IdP. They're the fallback if an assessor doesn't accept the
  inheritance.
- **Q-2 Bedrock and CUI** (N05). Is the configured model inside the FedRAMP scope for its region,
  and where does the `us.` inference profile route? The outcome is `/ask` allowed, GovCloud-only,
  or off in CUI envs.
- **Q-3 Partition** (N06). Commercial (FedRAMP Moderate services) or GovCloud. It depends on the
  CUI category (N01) and on GovCloud's service availability.
- **Q-4 CUI in audit logs** (N43). Keep full query and question text in restricted log groups (the logs become CUI), or redact. Keeping it serves 03.03.02; redacting shrinks
  the CUI footprint.
- **Q-5 Graph Explorer's future.** *Settled 2026-11-03 (SKG-230, #102):* dev only; prod no longer
  runs it. Dev never holds CUI, so Graph Explorer needs no hardening. This replaces the 2026-10-27
  plan to harden it for prod with a Synapse OAuth login and an audit sidecar.

## Success criteria
- `control-matrix.md` has 97 rows. None is Planned without a ticket. Every gate ticket is done in
  the target env.
- `tools/compliance_preflight.py` passes against the target account.
- `cdk synth` with cdk-nag `NIST80053R5Checks` has no unsuppressed errors, and every suppression
  names a reason and a requirement id.
- `tests/compliance/` passes. Its assertions cover every ODP in the table above.
- Synth passes for both the `aws` and `aws-us-gov` partitions from config alone.
- The SSP, POA&M, risk assessment and incident-response annex exist. A tabletop exercise and a
  restore test are recorded.
