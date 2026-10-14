# 002 — Knowledge-graph governance: ACL and access requirements at query time (Cedar/AVP)

**Status:** Draft (POC). Plan only; no implementation yet.
**Owner:** Thomas Yu
**Depends on:** spec 001 (unified API), merged into `dev` through Phase 4 (#74–#87). This spec
builds on what it added:
- `core/sagebrain_core` (the enforcement chokepoint, `run_query`, limits), shipped as a Lambda layer;
- `src/lambda_edge/jobs.py`, the unified API's submit/status handlers (`app-{env}-api`). It already
  puts `user_id` on the `/query` message and both job items (spec 001 D-19);
- `api/openapi.yaml`, `tests/contract/cases.yaml`;
- `docs/memory/constitution.md` and `specs/_template/`.

**Interacts with:** spec 004 (latest-snapshot scoping, draft). Only the ingest gate (FR-12) waits
for it; query-time enforcement does not.
**Contract changes:** `api/openapi.yaml`:
- the job `error` shape gains `governance` on `GET /query/{job_id}` and `GET /ask/{job_id}`;
- both status operations become owner-only (404 for anyone else);
- new `governance_denied` agent step.

**Upstream model:** [governanceDUO graph design](https://mc2-center.github.io/governanceDUO/graph-design/#governance-graph-design)
and its [worked example](https://mc2-center.github.io/governanceDUO/graph-design-implementation/#a-worked-example).
We follow its access rules (effective access = ACL AND every inherited AR satisfied) and use the
worked example as the golden test. We don't load its graph (D-5).

## Problem
Any member of the environment's Synapse team (`AUTH.synapse_team_id`) can read **every** node in
the knowledge graph. Nothing checks at query time whether the caller could see the underlying
Synapse entity, either through its ACL or its access requirements (ARs).

governanceDUO defines **effective access** as two halves:
- **ACL:** a grant on the entity's benefactor;
- **ARs:** every AR on the entity or any ancestor approved for the caller and unexpired.

## Principles
- **Governance is part of the service, not a feature.** Wherever it's deployed it is enforced, and
  it fails closed: missing governance data, an unknown entity, or a backend error returns no
  information. There is no `off`, `shadow` or `report` mode.
- **Govern what needs governing today.** Stage 1 governs Synapse entities. Nodes that aren't
  Synapse entities are allowed through, and how to govern them is an open question (research Q-2).
- **Relational work happens once, at export.** Synapse's governance data is relational and spread
  across many tables. The export flattens it into two decision-ready tables, so a query-time
  decision is a few key lookups plus a Cedar evaluation.

## Stages
| Stage | ACL half | AR half |
|---|---|---|
| **1 (this POC)** | **Query-time, coarse.** A resource is readable only if its benefactor ACL grants `DOWNLOAD` to AUTHENTICATED_USERS (273948). | **Query-time.** Every managed ACT AR inherited along the parent path must be granted to the caller and unexpired. |
| **2 (follow-up)** | **Query-time, per user and team.** ACL entries for `acl:agent` / `acl:agentGroup` + team rosters. | unchanged, plus the other AR types (research Q-8) |

In Stage 1 everything readable is readable by every registered Synapse user, apart from ARs. So
team-only content in the KG is denied to everyone until Stage 2, not exposed.

## User stories
- **Researcher:** my query or agent question succeeds when I've been granted every AR on
  everything it touches. Otherwise it fails with `governance_denied`, listing the resources and the
  unmet ARs, so I know what to request in Synapse.
- **Data governance lead:** AR and ACL policy is Cedar policy text over relationships taken from
  Synapse (entity → ancestors → AR ← grant ← user). The decision log names the determining policy.
- **Data steward / pipeline owner (after spec 004):** a snapshot containing an entity that isn't
  governed, or isn't open to registered users, is refused before it reaches Neptune, with a report.
- **Operator:** I can see every decision in CloudWatch and roll back by reverting the PR.

## Functional requirements

### Governance data
- **FR-1 Two decision tables.** `app-{env}-governance` holds two DynamoDB tables (data-model.md):
  - **entities:** `syn id → {benefactor, open_to_authenticated, ar_ids}`. `ar_ids` is every AR on
    the entity or any ancestor (FR-3).
  - **grants:** `(user id, AR id) → {granted, expires_at}`: whether the user has been granted the
    AR, and until when.

  Both are encrypted with a dedicated KMS key. Only the decision Lambda's role can read them;
  the query worker, agent, unified-API handlers and Graph Explorer have no access.
  *Tests:* `test_governance_stack.py::test_tables_readable_only_by_decision_lambda`, `::test_data_plane_roles_have_no_governance_access`.
- **FR-2 Source: the Synapse data warehouse.** A POC export script (`tools/governance_export/`)
  queries Snowflake under the operator's own login and loads both tables in full for a configured
  scope (`config/governance/poc_scope.yaml`). No credential is stored. Items not in the new
  export are deleted after the write. Automation is follow-up work (Out of scope).
  *Tests:* `tests/unit/tools/test_governance_export.py` (row mapping, deletion of stale items).
- **FR-3 ARs inherit down the parent path; ACLs come from the benefactor.** An AR on a project or
  folder applies to every descendant, including ones with local sharing settings (their own
  benefactor). The export computes `ar_ids` with a recursive walk up the parent chain;
  `open_to_authenticated` comes from the benefactor's ACL. *Test:* `test_governance_export.py::test_ar_inherited_past_local_sharing`.
- **FR-4 A grant (managed ACT only).** For each (user, AR), take the accessor changes for that user
  on the AR's **Approved** data-access submissions, ordered by review time:
  - the latest is `GRANT_ACCESS` or `RENEW_ACCESS` → `granted: true`;
  - the latest is `REVOKE_ACCESS` → `granted: false`;
  - `expires_at` = review time + the AR's expiration period, if it has one.

  Pending, rejected and cancelled submissions count for nothing. Other AR types (self-sign, terms
  of use, legacy ACT, lock, post-message) have no grant rows, so they are **always unmet** in
  Stage 1. *Tests:* `test_governance_export.py::test_grant_*`.

### Decision
- **FR-5 Cedar policies.** `readKg` on a Synapse resource is decided by AVP `BatchIsAuthorized`
  over `policies/governance/`:
  - **`acl_open` (permit):** `resource.openToAuthenticated`.
  - **`access_requirements` (forbid):** unless the caller holds every one of
    `resource.accessRequirements`. A held AR is a grant row with `granted: true` and `expires_at`
    absent or later than the decision time.

  *Tests:* `tests/unit/governance/test_policies.py` (cedarpy, on the same `.cedar` files the stack
  deploys).
- **FR-6 Fail closed.** The decision Lambda denies:
  - with `reason: ungoverned` when a resource has no entities item;
  - with `reason: authorization_unavailable` on any DynamoDB or AVP error, or a missing table.

  A job with no `user_id` is denied the same way: there is no caller to check.
- **FR-7 Touch-set preflight.** `sagebrain_core.governance` parses the query (rdflib) and runs a
  preflight with the same `WHERE` and the same dataset as the real query. It projects `DISTINCT`
  every subject- and object-position variable plus constant IRIs, filtered to Synapse IRIs, with
  `LIMIT max_touch_set + 1`. That covers resources reached only inside `FILTER`, `OPTIONAL`,
  subqueries or aggregates. It rejects with 400:
  - non-SELECT queries;
  - `SERVICE`;
  - `IRI()`/`URI()`/`STRUUID()`;
  - a touch set over the cap.

  *Tests:* `tests/unit/core/test_governance.py::test_touch_set_*`.
- **FR-8 Whole-query deny.** Any denied touched resource fails the job with
  `error="governance_denied"` and
  `governance: {decision, reason, denied_resources[≤50], denied_count, unmet_requirements[≤50]}`.
  *Tests:* `cases.yaml` `query_status_governance_denied`, `test_governance.py::test_enforce_denies_whole_query`.
- **FR-9 Result rescan.** Synapse IRIs in result bindings must be within the authorized touch set.
  Otherwise the job is denied with `reason: result_outside_touch_set`.
- **FR-10 One chokepoint, both callers.** `run_query` (the agent) and the `/query` worker call
  `governance.enforce`. In the agent, a denial becomes a `tool_result` error step.

### Cross-cutting
- **FR-11 Job ownership.** `GET /query/{job_id}` and `GET /ask/{job_id}` return 404 unless the
  caller's `user_id` matches the job's. *Tests:* `cases.yaml` `query_status_other_owner`,
  `ask_status_other_owner`.
- **FR-12 Ingest gate (after spec 004).** Before `StartLoad`, a `GovernanceGate` step collects
  every Synapse IRI in the snapshot's `data/` objects (a regex over the streamed bytes, no Turtle
  parse) and looks each up in the entities table. Any IRI that is absent (`ungoverned`) or not
  `open_to_authenticated` refuses the whole snapshot; the gate writes
  `{portal}/{tag}/governance_report.json` and the snapshot is never published (spec 004). This is
  defence in depth and early feedback for depositors; query time is the enforcement point. Its
  entry condition is a full-portal export (Out of scope).
- **FR-13 Worked example is the golden test.** governanceDUO's worked example, vendored at a
  pinned commit and translated into the two tables' items (`tests/fixtures/governance/`), with
  the study's and file's benefactors open to AUTHENTICATED_USERS. Its study `syn2343195` and file
  `syn10081783` are real NF IRIs in our data graph. AR 42 is a managed ACT AR.

  | Principal | Decision time | Resource | Expected | Why |
  |---|---|---|---|---|
  | `2000001` | 2026-08-01 | `syn10081783` | ALLOW | AR 42 inherited from the study; granted, not yet expired |
  | `2000001` | 2026-10-14 | `syn10081783` | DENY (`access_requirements`, unmet `42`) | the grant expired 2026-08-14T19:33:20Z |
  | `1000002` | any | `syn10081783` | DENY (`access_requirements`) | no grant for AR 42 |
  | any user without a current AR 42 grant | any | `syn2343195` | DENY (`access_requirements`) | the study carries AR 42 itself |
  | `2000001` | 2026-08-01 | `syn10081783`, benefactor team-only (as published) | DENY (`acl_open` not satisfied) | ACL half: not open to registered users |
  | any | any | a Synapse IRI absent from the fixture | DENY (`ungoverned`) | fail closed |

  *Tests:* `tests/unit/governance/test_worked_example.py` (cedarpy + decision mapping, injectable clock).
- **FR-14 Decision audit.** `governance_decision` log event: principal, job id, counts, denied and
  unmet samples, policy ids, export id, duration. The gate (FR-12) logs `kg_governance_gate`.

## Non-functional requirements / thresholds
| Threshold | Value | Declared in | Enforced in | Test |
|---|---|---|---|---|
| Max Synapse resources touched per query | 5000 | `sagebrain_core.limits.GOVERNANCE_MAX_TOUCH_SET` | `governance.enforce` | `test_touch_set_cap` |
| Denied resources / unmet ARs returned | 50 / 50 | `limits.GOVERNANCE_DENIED_SAMPLE` | `governance.enforce` | `test_denied_sample_cap` |
| AVP `BatchIsAuthorized` per call | 30 | decision Lambda | decision Lambda | `test_batches_of_30` |
| Decision cache TTL | 60 s | `GOVERNANCE.decision_cache_seconds` | decision Lambda | `test_decision_cache_ttl` |
| Query-time governance overhead p50 | < 2 s | spec | measured | `research.md` |

## Deviations from current behaviour
- **D-1** Status reads become owner-only (404 otherwise). Breaking for anyone reading another
  user's job; `legacy:` entry in `cases.yaml`.
- **D-2** New terminal job error `governance_denied`, and a 400 for queries the preflight can't
  analyse (FR-7).
- **D-3** A job with no `user_id` (only the old APIs' `/query` front door writes these) is denied
  whenever governance is enabled. The old APIs are removed in spec 001 Phase 5.
- **D-4** Team-only content and content behind any AR type other than managed ACT is denied to
  everyone in Stage 1.
- **D-5 (vs governanceDUO).** Governance data is a flattened DynamoDB projection of the Synapse
  warehouse, not governanceDUO's graph loaded into Neptune. The rules and the Cedar policies are
  the same; whether Stage 2 needs a graph store is decided then.
- **D-6 (data pipeline, after spec 004).** With the gate, a snapshot can be refused.

## Out of scope / known POC gaps
- **Export automation.** The POC loads the tables by hand. Before prod: a scheduled export in
  this infra (Snowflake service key pair in Secrets Manager), versioned writes with a pointer flip
  so a half-written export is never read, and a freshness cutoff (deny everything after 48 h
  without a fresh export, alarm at 26 h). Until then revocations take effect only on the next
  manual export.
- **Non-Synapse nodes are allowed through (open question, research Q-2).** Only Synapse IRIs are
  checked; minted nodes such as `nf:Donor` and external vocabulary pass unchecked. A query
  touching only those nodes is not governed.
- **Graph Explorer** is a standing exception (constitution V): Sage staff on the VPN, signing
  directly to Neptune, not governed. Revisit at Stage 2, when restricted content could enter the KG.
- **AR types other than managed ACT** (research Q-8), **per-user and team ACLs** (Stage 2),
  **DUO conditions, derived nodes, Policy Fabric, Passports** (research R-6).
- **Prod.** `GOVERNANCE.enabled` stays `false` in prod until export automation exists.

## Success criteria
- Unit, synth, pre-commit and redocly checks are clean, and the FR-13 table passes.
- **Dev, after a manual export of the POC scope:**
  `SELECT ?a WHERE { <https://www.synapse.org/Synapse:syn10081783> <http://nf-osi.github.com/terms#assay> ?a }`
  returns `governance_denied` (unmet AR) for a caller without a grant and results for a caller
  with a current grant. A resource outside the scope returns `governance_denied` (`ungoverned`).
- The agent turns a denial into a `tool_result` error step and still answers.
- Another user's `job_id` returns 404.
- Only the decision Lambda's role can read the governance tables.
