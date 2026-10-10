# 002 — Plan

## Approach
governanceDUO's **effective access** is ACL AND every inherited AR satisfied. The two halves
ship in two stages:

- **Stage 1a — ACL as an ingestion invariant.** Before the data pipeline's `StartLoad`, a
  `GovernanceGate` Lambda streams the snapshot's `data/` objects and collects every Synapse IRI. It
  then checks each IRI's `gov:benefactor` in the private governance graph for an `acl:Authorization`
  granting `gov:Download` to `acl:agentClass acl:AuthenticatedAgent` (or `foaf:Agent`). Any failure
  refuses the whole snapshot and writes `governance_report.json`. So everything in the KG is
  downloadable by every registered Synapse user, and the ACL half holds by construction for
  every authenticated caller.
- **Stage 1b — query-time access requirements.** These live in the shared query chokepoint,
  `sagebrain_core` (constitution Article V). Before any SPARQL runs:
  1. `governance.enforce` builds a **touch-set preflight** from the parsed query and runs it on the
     data cluster to learn which Synapse resources the query touches.
  2. It asks the **policy decision point** (the PDP Lambda) whether the caller may `readKg` them.
     The PDP reads, from the latest governance snapshot:
     - `gov:benefactor` (to confirm the resource is governed);
     - `gov:requiresAR` along `gov:parent*`;
     - the caller's `gov:Approval` rows (status and `expiresAt`).

     It builds Cedar entities and calls AVP `BatchIsAuthorized`. Stage 1 policies:
     - `ingested_open` (permit);
     - `access_requirements` (forbid unless every inherited AR is approved and unexpired).

     **No team rosters are needed in Stage 1.**
  3. Any denial fails the whole job, listing the unmet ARs. After the real query, result IRIs are
     re-checked against the authorized set.
- **Stage 2 — query-time ACLs.**
  - The PDP adds `acl_download`, which expands `acl:agent` / `acl:agentGroup` + `vcard:hasMember` /
    `acl:agentClass`. It replaces `ingested_open`.
  - The sync adds team rosters.
  - The gate relaxes to "every IRI governed", which lets restricted content into the KG.

Only the two governance Lambdas (the gate and the PDP) share the security group that can reach the
governance cluster.

```
DATA PIPELINE (Stage 1a)
  manifest.ttl ─▶ EventBridge ─▶ Step Functions:
     GovernanceGate ──(gov SG→SG 8182)──▶ PRIVATE governance Neptune
        ├─ fail (enforce) → governance_report.json + REJECTED_GOVERNANCE → Fail → alarm
        └─ pass / report  → StartLoad → Wait/Check → RecordSuccess (+ governance_snapshot lineage)

QUERY (Stage 1b)
caller ─▶ submit (user_id on every job) ─▶ SQS ─▶ worker (/query) or agent tool
                                                  │
                     sagebrain_core.governance.enforce(query, principal)
                       1. rdflib parse → touch-set preflight  ──▶ DATA Neptune (read endpoint)
                       2. lambda:Invoke PDP {principal, readKg, resources}
                                     │
                     ┌───────────────▼────────────────────────────────────────┐
                     │ PDP (src/lambda_governance)   — own SG + role          │
                     │   SPARQL ─(SG→SG 8182)─▶ PRIVATE governance Neptune    │
                     │   BatchIsAuthorized   ─▶ AVP policy store (Cedar)      │
                     │   latest snapshot     ─▶ governance pipeline table     │
                     └───────────────┬────────────────────────────────────────┘
                       3. any DENY → GovernanceDenied (403) → job error governance_denied
                       4. execute_query(original) → rescan result IRIs ⊆ allowed
```

Why the gate and PDP are separate Lambdas and not in-process in `sagebrain_core`: only one IAM role and one
security group can ever touch the governance cluster or the policy store. The query worker, agent,
FastAPI task and Graph Explorer keep their current access to the data cluster and gain none to the
governance cluster.

## Files
| File | Change |
|---|---|
| `api/openapi.yaml` | `governance` object on `JobStatus` error; 404 owner semantics on both status ops |
| `tests/contract/cases.yaml` | `query_status_governance_denied`, `*_status_other_owner`, `user_id` in `query_submit_ok` message |
| `src/lambda/submit.py`, `app/sagebrain_api/routers/query.py` | put `user_id` on the SQS message + job item |
| `src/lambda/status.py`, `src/lambda_agent/status.py`, `app/sagebrain_api/routers/{query,ask}.py`, `app/sagebrain_api/jobs.py` | owner check → 404 |
| `src/governance_stack.py` (new) | private Neptune cluster, SG, governance bucket, load role, governance-Lambda SG, AVP policy store + policies, PDP Lambda, gate Lambda (read on the data bucket snapshot prefix, write `governance_report.json`) |
| `src/neptune_pipeline_stack.py` | optional `governance_gate` (`off\|report\|enforce`) + gate function → `GovernanceGate` task before `StartLoad`; `REJECTED_GOVERNANCE` record path. Also instantiated a 2nd time (gate `off`) as `app-{env}-governance-pipeline` |
| `src/lambda_governance_gate/gate.py` (new) | stream `data/` objects, regex Synapse IRIs, batch-check the invariant against the governance graph, write the report, return pass/fail |
| `src/lambda_loader/loader.py` | `record` accepts `REJECTED_GOVERNANCE` and `governance_snapshot` |
| `src/lambda_governance/authorize.py` (new) | PDP: graph lookup → Cedar entities → `BatchIsAuthorized` → decision + log |
| `policies/governance/schema.cedarschema.json`, `ingested_open.cedar`, `access_requirements.cedar` (new; `acl_download.cedar` in Stage 2) | Cedar model; deployed by the stack, tested with cedarpy |
| `core/sagebrain_core/governance.py` (new) | touch-set builder, `enforce`, `check_results`, `GovernanceDenied` |
| `core/sagebrain_core/limits.py`, `errors.py`, `query_service.py` | thresholds; `GovernanceDenied(QueryRejected)`; call `enforce` in `run_query` |
| `core/requirements.txt` (new), `src/core_layer.py` | layer pip-installs `rdflib` |
| `src/lambda/query.py` | call `enforce` before `execute_query`, `check_results` after |
| `src/lambda_agent/agent.py` | `GovernanceDenied` → `tool_result` error step |
| `src/neptune_api_stack.py`, `src/neptune_agent_stack.py` | env `GOVERNANCE_MODE`, `GOVERNANCE_PDP_FUNCTION`; `lambda:InvokeFunction` on the PDP only |
| `config/base.yaml`, `config/dev.yaml` | `GOVERNANCE` block (`enabled`, `mode`, capacity, `max_touch_set`, `decision_cache_seconds`); dev `mode: shadow` |
| `app.py` | wire the governance stack and pipeline; synth guard enforce × viz |
| `tools/publish_governance_snapshot.py` (new), `config/governance/poc_scope.yaml` (new) | validate a governanceDUO export against the pinned SHACL; write `manifest.ttl` (commit, `GRAPH_VERSION`, scope); upload data then manifest. The **sync itself is governanceDUO's** `scripts/sync_governance_graph.py` (ACT credential) |
| `src/lambda_governance/governance_query.rq` (new) | pinned PDP query: the infra half of the governanceDUO contract (governanceDUO mirrors it in `tests/infra_contract/`) |
| `tests/fixtures/governance/` (new) | vendored governanceDUO worked example (as published) + derived `governance_graph.open.ttl` (adds AuthenticatedAgent DOWNLOAD) + `shapes/governance.shacl.ttl`, at a pinned commit (`PINNED_COMMIT` file) |
| `config/base.yaml` `NEPTUNE_PIPELINE.governance_gate` | `off` default; dev `report` |
| CLAUDE.md | "Governance" section; Graph Explorer caveat |
| `tests/unit/governance/`, `tests/unit/core/test_governance.py`, `tests/unit/test_governance_stack.py`, `tests/unit/governance/test_worked_example.py`, `tests/unit/tools/test_publish_governance_snapshot.py` | new tests |

## Phases (each independently mergeable)
- **A — Identity fixes (no governance yet).** `user_id` on `/query` jobs, plus job ownership on
  status (FR-15).
- **B — Private governance infrastructure.**
  - `GovernanceStack`: cluster, SG, bucket, load role, governance-Lambda SG.
  - The second pipeline instance and `publish_governance_snapshot.py`.
  - The worked-example fixtures, published as the first dev governance snapshot (the `open`
    variant).
- **C — ACL ingestion gate (Stage 1a).**
  - The gate Lambda, the `GovernanceGate` task in the data pipeline, the report and the
    `REJECTED_GOVERNANCE` record.
  - FR-16a tests. Deploy in `report` on dev.
  - Replay `nf/2026-07-14` to measure runtime and see how much of the current NF snapshot is not
    open to registered users. That number is the first real governance finding.
- **D — AR Cedar model + PDP (Stage 1b).** `ingested_open` + `access_requirements` with cedarpy
  tests (FR-16b), the AVP store, the PDP with stubbed-backend tests, and the pinned
  `governance_query.rq`.
- **E — Query enforcement in `sagebrain_core`, shadow mode.** Touch-set builder, `enforce`,
  `check_results`; wired into `run_query`, the `/query` worker and the agent.
- **F — Live governance sync.**
  - governanceDUO `sync_governance_graph.py` under an ACT credential for `poc_scope.yaml`.
    Approvals and ARs are needed for 1b; benefactor ACLs (AUTHENTICATED_USERS entries only) for 1a.
  - Re-sync governanceDUO's `tests/infra_contract/authorize_query.rq`.
  - Push upstream the fileview-based benefactor optimisation needed for portal-wide gate coverage
    (R-4).
- **G — Enforce on dev.** The query side goes to `enforce` (`NEPTUNE_VIZ.enabled: false`). The
  gate goes to `enforce` once the governance snapshot covers the NF portal.
- **S2 — Query-time ACLs (Stage 2, FR-18).** Rosters in the sync, `acl_download` replaces
  `ingested_open`, and the gate relaxes to "governed".
- **H — Conditions and derivation (after the POC).** DUO purpose policies, `gov:ControlLabel` for
  derived and non-Synapse nodes, and an evaluation of Policy Fabric and Passports.

## Risks / rollback
- **Rollback.** `GOVERNANCE.mode: off` plus a redeploy makes `enforce` a no-op. The infrastructure
  from phases B–D can stay deployed while unused.
- **Touch-set soundness.** A query shape the builder mishandles could under-report touched
  resources. Mitigations: an allow-listed SPARQL subset (reject what we can't analyse), the FR-8
  result rescan, and shadow-mode logs compared against the real results before `enforce`.
- **Preflight cost.** It doubles Neptune work, and broad queries exceed `max_touch_set`. They're
  rejected with guidance to narrow the query. A later optimisation is a per-user allowed-benefactor
  `VALUES` injection (R-1).
- **Over-denial / gate coverage.** Any IRI outside the governance snapshot is `ungoverned`. At
  query time it's denied; at the gate it rejects the whole snapshot. The gate stays in `report`
  until the governance sync covers every NF benefactor, and prod stays `off`.
- **Gate scale.** ~503k IRIs per NF snapshot. Dedupe IRIs → benefactors in batched `VALUES` queries
  (≈1000 IRIs each), and stream S3 rather than parse Turtle. If the 15-minute Lambda limit is at
  risk, move the gate to a Step Functions Distributed Map, one item per `data/` object.
- **Conservative IRI scan.** Scanning objects as well as subjects means a referenced container,
  such as a study via `nf:parentStudy`, must also be open. If that's too strict, restrict to
  subject IRIs (research Q-6).
- **Rollback (gate).** `NEPTUNE_PIPELINE.governance_gate: off` removes the task. A rejected
  snapshot can be loaded after fixing ACLs by re-uploading its `manifest.ttl`; a new etag reruns.
- **ACT credential.** governanceDUO's sync needs ACT membership: `POST /accessApproval/search` and
  `/submissions` are ACT-only. Without it, approvals are empty and every AR-gated resource is denied,
  which is fail-closed. Until a service ACT credential exists, the worked-example fixture covers
  testing.
- **Upstream maturity.** governanceDUO's live sync is "tested offline, not yet run against live
  Synapse", and the `w3id.org` namespace isn't registered yet. Pin a commit; validate every export
  against the pinned SHACL before publishing; fail the PDP closed on an unpinned `GRAPH_VERSION`.
- **Contract drift.** Both repos run the contract check: our `test_worked_example.py` against the
  vendored fixture, and governanceDUO's `make infra-contract-check SAGEBRAIN_INFRA=<this repo>`.
- **Cost.** A second serverless Neptune cluster (min 1 NCU). Dev sets
  `GOVERNANCE.serverless_min_capacity: 1.0`, `max: 2.0`.
- **Pipeline reuse.** `NeptunePipelineStack` names resources from `construct_id`, so a second
  instance doesn't collide. Its loader must target the governance cluster's writer endpoint and
  load role.

## Verification
- **Unit:**
  - Gate: IRI streaming (multi-object, chunk boundaries), the invariant matrix (AuthenticatedAgent
    or foaf:Agent DOWNLOAD pass; team-only, user-only, READ-only, ungoverned fail), report cap,
    modes. FR-16a.
  - cedarpy policy matrix: no ARs → allow; one/many ARs approved → allow; missing, expired or
    revoked approval → deny; inherited via `gov:parent*`; ungoverned → deny. FR-16b.
  - Touch-set builder: plain BGP, OPTIONAL, UNION, FILTER EXISTS, subquery, COUNT/GROUP BY, constant
    IRI; SERVICE/`IRI()`/CONSTRUCT rejected.
  - PDP: dedupe by (benefactor, AR set), chunks of 30, cache, ungoverned → deny, backend error →
    deny, latest-snapshot graph.
  - `enforce` modes, plus the rescan.
  - Ownership 404s, and the contract vectors.
- **Stack:** the governance SG's only ingress is the PDP SG; worker, agent and viz roles have no
  `neptune-db` on the governance cluster resource id; the bucket has no `AccountRootPrincipal`
  statement; the AVP store and two policies exist; the enforce × viz synth guard holds.
- **Commands:** `python -m pytest tests/ -s -v` · `cdk synth --context env=dev` ·
  `pre-commit run --all-files` ·
  `npx @redocly/cli@2 lint api/openapi.yaml --config api/.redocly.yaml`
- **Live (dev, `--profile sagebrain-dev`):**
  1. `cdk deploy app-dev-governance app-dev-governance-pipeline app-dev-neptune-pipeline app-dev-neptune-api app-dev-neptune-agent`
  2. `python tools/publish_governance_snapshot.py tests/fixtures/governance/governance_graph.open.ttl`
     to publish the worked example (`open` variant). Later, publish governanceDUO's `sync_governance_graph.py`
     export for `poc_scope.yaml`. Then confirm the governance pipeline
     execution `LOAD_COMPLETED`.
  3. Gate in `report`: re-upload `nf/2026-07-14/manifest.ttl` and read `governance_report.json`.
     Then a test portal snapshot (`test/…`) with gate `enforce`: one open entity loads, one
     team-only entity is refused.
  4. In shadow mode, run the scenario queries as both test users and confirm the
     `governance_decision` logs in CloudWatch Insights.
  5. In enforce mode, repeat. Expect `governance_denied` vs success as in the spec's success
     criteria. Then the agent question, then another user's `job_id` → 404.
