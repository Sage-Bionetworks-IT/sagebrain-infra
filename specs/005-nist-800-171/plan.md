# 005 — Plan

What gets built, not how: components, phases, ordering and rollback. The work items are in
[tickets.md](tickets.md). A ticket's `tasks.md`-style checklist lands with its code PR, not here.

## Approach
The epic is 8 phases and 41 tickets. Phase 0 settles scope and the decisions every later ticket
depends on. Phases 1–4 close the **CUI gate** ([spec.md](spec.md#cui-gate)). Phases 5–7 make the
system assessable and transferable, and can run alongside phases 2–4 once phase 0 is done.

Each ticket is one or more PRs. Every PR stays deployable and reversible on its own (constitution
VI). The usual order applies: contract first (I), parity tests before deliberate deviations (II),
and a test for each threshold or ODP (III).

Inherited controls stay with Sage IT. The repo's side of each one is a preflight check (N03) and a
row in the control matrix, so another org knows exactly what it must supply.

## Components
- **`docs/compliance/`** (new): `boundary.md` with its diagram, `inherited-controls.md`,
  `ssp.md`, `poam.md`, `risk-assessment.md`, `incident-response.md` and `audit-events.md`. They
  are written to be org-neutral, with org-specific values called out.
- **`config/` `COMPLIANCE:` block** (new): the ODPs that aren't API-facing. API-facing ones go in
  `x-sagebrain-limits`.
- **`tools/compliance_preflight.py`** (new): read-only account checks for the inherited
  prerequisites.
- **`tests/compliance/`** (new): template assertions named by requirement id.
- **cdk-nag** `NIST80053R5Checks` as an Aspect in `app.py`, with suppressions in one file
  (`src/nag_suppressions.py`).
- **Changes to existing stacks:**
  - `network_stack`: flow logs, endpoints.
  - `neptune_stack`: parameter group, backup retention, bucket policy, access logs.
  - `neptune_api_stack`, `neptune_agent_stack`, `neptune_pipeline_stack`, `api_edge_stack`,
    `api_edge_monitoring`, `monitoring_stack`: log groups, egress, WAF, alarms; the SNS topic's key in `api_edge_monitoring`.
  - The bastion app.
- **Handler changes:** `lambda_edge/jobs.py` (ownership, audit fields),
  `lambda_authorizer/authorizer.py` (cache, config), `lambda_agent/agent.py` (audit fields),
  `lambda_loader/loader.py` (uploader identity, append-only lineage).
- **CI:** `.github/workflows/*` (gating, pinning, scanning), `.github/dependabot.yml`, and
  repository/environment variables replacing the hardcoded role ARNs.

## Phases (each independently mergeable)
| Phase | Tickets | Gate? | Depends on |
|---|---|---|---|
| 0: Scope and decisions | N01–N06 | yes | none |
| 1: High-risk fixes | N11, N13, N14, N15, N16; N10 (evidence only) | yes, except N10 and N16 | N11 needs no decision; N13 needs the N01 depositor list |
| 2: Cryptography | N21, N23, N24 | yes, except N24 | N23 needs N06 |
| 3: Network | N30, N31, N32, N33, N34 | N32 only | N31 → N32; N33 → N44 |
| 4: Identity and audit content | N41–N46 | N42, N43, N44, N46 | N43 needs Q-4; N46 needs N03's data events and N13 |
| 5: Configuration management and supply chain | N50–N54 | no | N50 after most of phases 1–3, so it suppresses less |
| 6: Portability | N60–N62 | no | N61 needs N06; N62 needs N03 and N60 |
| 7: Plans, assessment, operations | N70–N76 | N71 only | N70 needs the matrix to be mostly final |

Suggested PR order inside the gate:
1. N11, N15, N14
2. N13
3. N21, N23
4. N30, N31, N32
5. N42, N43, N44, N46

## Ordering constraints
- **Cross-stack exports.** `app.py` deploys every stack at once (`cdk deploy --all`), so adding or
  dropping an export that a deployed stack imports takes two PRs, each deployed before the next
  merges (CLAUDE.md, Deployment). This affects:
  - **N31/N32:** consumers get endpoint SG references only after the network stack exports them.
- **The Neptune parameter group swap (N14)** needs an instance reboot to take effect. Apply it in
  dev first, then in prod in the maintenance window (`sun:04:00`). Both clusters run one serverless
  instance, so expect a short outage.
- **API endpoint type (N23).** EDGE → REGIONAL changes the `execute-api` hostname. Run both stages
  in parallel and retire the old URL per spec 001's sunset rules, or land it with the custom domain
  (spec 001 R-16).
- **WAF (N33)** reopens a spec 001 research decision (WAF rejected for cost). It needs a
  decisions.md entry in its PR.

## Risks / rollback
| Risk | Mitigation / rollback |
|---|---|
| Narrowing the bucket policy or the depositor roles blocks a real pipeline | Map each role to its prefixes in config from what the bucket actually holds (N01). Probe each pipeline in dev before prod. The bucket policy is the only change needed; IT's role cleanup is optional and can follow. |
| The SNS key policy blocks alarm or load-failure emails (N21) | Grant `cloudwatch.amazonaws.com` and `events.amazonaws.com`, scoped to the account. Check in dev that an alarm and a failed load still email. Revert to the unencrypted topic; the key stays `RETAIN`. |
| The audit-log reboot interrupts prod | Rehearse in dev, then use the maintenance window. If it fails, revert to the default parameter group. |
| Owner-only status breaks shared scripts | 404 matches the contract's "unknown job". Announce it in the release note. Clients that poll their own jobs are unaffected. |
| Bucket policy narrowing blocks a real depositor | N01 lists the depositors first. Run a dev upload probe before prod. Revert restores the account principal. |
| Deny-by-default egress breaks a dependency (Synapse, Bedrock) | The authorizer runs outside the VPC. Endpoints come first (N31), then a dev soak with flow logs to catch rejects before prod. |
| cdk-nag floods synth | Start with warnings, then turn on errors once the backlog is suppressed or fixed. Every suppression names a ticket. |
| An assessor doesn't accept MFA inherited from Synapse, or N04 finds a PAT can mint a PAT | Fall back to a 2FA check at auth time (`GET /auth/v1/2fa`, which works with a PAT, checked 2026-10-27) or to OAuth-only tokens. Either is an authorizer change behind the same fail-closed path. |
| Spikes find Bedrock out of scope | `/ask` is disabled in CUI envs through config: the stack takes a flag, and the contract documents 404/503 for `/ask` there. |
| Cost (endpoints, WAF, KMS, flow logs, longer retention) | Record it in each PR as a scaling path with a signal and a cost, per the memory bank's convention. Don't pre-optimize. |

## Open decisions
See [spec.md](spec.md#open-decisions). Q-5 is settled: Graph Explorer is dev only (#102). Q-1 is settled: MFA is inherited from Synapse (N04 gathers the evidence). Each remaining spike (N05, N06, and the Q-4 decision inside N43) ends in a
decisions.md entry before its dependent tickets start.
