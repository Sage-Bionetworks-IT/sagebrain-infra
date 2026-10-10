# 002 — Tasks

Test-first: each implementation task is preceded by the task that adds its failing test.
Order: access requirements are enforced at query time first, with the ACL held as an ingestion
invariant (Stage 1). Query-time ACLs come after (Stage 2).

## Phase 0 — spec
- [x] T001 `specs/002-kg-governance-rebac/`: `spec.md`, `plan.md`, `tasks.md`, `data-model.md`, `research.md`
- [ ] T002 `api/openapi.yaml`: `governance` object on the job error (`decision`, `reason`, `denied_resources` ≤50, `denied_count`, `unmet_requirements` ≤50) and owner-only 404 on both status ops. Redocly lint is clean.
- [ ] T003 `tests/contract/cases.yaml`: `query_status_governance_denied`, `query_status_other_owner`, `ask_status_other_owner`, `user_id` in the `query_submit_ok` message

## Phase A — identity fixes
- [ ] T101 Test: Lambda `POST /query` puts `user_id` on the SQS message and job item → `src/lambda/submit.py`
- [ ] T102 Test: FastAPI `POST /query` does the same → `app/sagebrain_api/routers/query.py`, `jobs.py`
- [ ] T103 Test: the `/query` worker audits `principal=<user_id>`
- [ ] T104 Test: `*_status_other_owner` (Lambda) → `src/lambda/status.py`, `src/lambda_agent/status.py`
- [ ] T105 Test: the same vectors (FastAPI) → `app/sagebrain_api/routers/{query,ask}.py`
- [ ] T106 Test: a legacy job without `user_id` is readable only by `machine`

## Phase B — private governance infrastructure + fixtures
- [ ] T201 Test: the `GOVERNANCE` config block loads; `enabled: false` → no governance stacks → `config/base.yaml`, `app.py`
- [ ] T202 Test: the governance cluster is IAM-auth, encrypted, private; its SG's only ingress is the governance-Lambda SG → `src/governance_stack.py`
- [ ] T203 Test: the governance bucket has no account-wide statement and has EventBridge on
- [ ] T204 Test: `app-{env}-governance-pipeline` targets the governance bucket, writer endpoint and load role with `urn:sagebrain:gov:{date}` and gate `off`
- [ ] T205 Vendor governanceDUO's worked example and `shapes/governance.shacl.ttl` into `tests/fixtures/governance/` and record `PINNED_COMMIT`. Derive `governance_graph.open.ttl`: add `acl:agentClass acl:AuthenticatedAgent ; acl:mode gov:Download` authorizations on `syn2343195` and `syn10081783`.
- [ ] T206 Test: `publish_governance_snapshot.py` rejects an export failing SHACL, writes `manifest.ttl` (commit, `GRAPH_VERSION`, scope), and uploads data before the manifest → implement it
- [ ] T207 Deploy the governance stacks to dev; publish the `open` fixture; confirm `LOAD_COMPLETED`

## Phase C — ACL ingestion gate (Stage 1a)
- [ ] T301 Test: Synapse IRIs are collected by streaming across multiple `data/` objects, including IRIs split across read-chunk boundaries, and deduped → `src/lambda_governance_gate/gate.py`
- [ ] T302 Test: the invariant matrix. AuthenticatedAgent DOWNLOAD and `foaf:Agent` DOWNLOAD pass. These fail: team-only (`not_open_to_authenticated`), user-only, READ-only (`not_downloadable`), and no benefactor (`ungoverned`). Batched `VALUES` queries are scoped to the latest governance snapshot graph, and an unpinned `GRAPH_VERSION` → error.
- [ ] T303 Test: FR-16a table against the as-published and `open` fixtures
- [ ] T304 Test: reject writes `governance_report.json` (capped at 1000 IRIs) and returns fail; pass returns `governance_snapshot`; `kg_governance_gate` log fields
- [ ] T305 Test: the data pipeline has `GovernanceGate` before `StartLoad` when the gate is `report`/`enforce` and none when `off`; `enforce` + fail → `RecordRejected` (`REJECTED_GOVERNANCE`) → `Fail`; `report` + fail → continue → `src/neptune_pipeline_stack.py`, `src/lambda_loader/loader.py`
- [ ] T306 Test: the gate role has read on the data bucket's `*/data/*` objects, put on `*/governance_report.json`, and neptune-db read on the governance cluster only
- [ ] T307 Deploy with dev `governance_gate: report`. Re-upload `nf/2026-07-14/manifest.ttl` and record runtime plus the pass/fail breakdown in `research.md`.
- [ ] T308 With a `test/` portal snapshot and gate `enforce`: an open-only snapshot loads; a snapshot with a team-only entity is refused

## Phase D — AR Cedar model + PDP (Stage 1b)
- [ ] T401 Test (cedarpy): `ingested_open` permits a governed entity. `access_requirements` behaves as follows: no ARs → allow; all approved → allow; one missing / expired / revoked → deny → `policies/governance/schema.cedarschema.json`, `ingested_open.cedar`, `access_requirements.cedar`
- [ ] T402 Test: FR-16b table (cedarpy + PDP mapping, injectable clock)
- [ ] T403 Test: the stack creates a STRICT AVP store with the schema and one static policy per `.cedar` file read from disk
- [ ] T404 Test: the PDP resolves the latest snapshot and runs the pinned `governance_query.rq`; rows become Cedar entities (ARs via `gov:parent*`; approvals only if Approved and unexpired); `stale_approval` warning → `src/lambda_governance/authorize.py`
- [ ] T405 Test: dedupe by AR set, chunks of 30, decision cache, `ungoverned` → deny, backend error → deny, `governance_decision` log including `unmet_requirements`
- [ ] T406 Deploy; `aws lambda invoke` reproduces the FR-16b rows against the dev `open` snapshot

## Phase E — query enforcement in `sagebrain_core` (shadow)
- [ ] T501 Test: `limits.GOVERNANCE_MAX_TOUCH_SET`, `GOVERNANCE_DENIED_SAMPLE`
- [ ] T502 Test: the touch-set builder on BGP, OPTIONAL, UNION, FILTER EXISTS, subquery, COUNT/GROUP BY, constant IRIs → `core/sagebrain_core/governance.py`
- [ ] T503 Test: SERVICE, `IRI()`/`URI()`, non-SELECT and over-cap queries → `QueryRejected(400)`
- [ ] T504 Test: `enforce` in off/shadow/enforce; DENY → `GovernanceDenied(403)` with reason, samples and `unmet_requirements`
- [ ] T505 Test: `check_results` rescan
- [ ] T506 Test: `run_query` calls `enforce` after `admit`; the `/query` worker calls `enforce` and `check_results`; the agent turns a denial into a `tool_result` error step
- [ ] T507 Test: the layer contains `rdflib`; workers have `GOVERNANCE_MODE`, `GOVERNANCE_PDP_FUNCTION` and invoke permission on the PDP only; synth fails for `enforce` × viz
- [ ] T508 Deploy API and agent stacks with dev `mode: shadow`

## Phase F — live governance sync
- [ ] T601 Pick the ~4 POC studies from `data/nf/2026-07-14`: open with no AR, open with a clickwrap AR, open with a managed ACT AR, and one not open (to exercise the gate). Write `config/governance/poc_scope.yaml`.
- [ ] T602 With an ACT credential, run governanceDUO `sync_governance_graph.py` for the scope; publish; record live findings in `research.md`
- [ ] T603 Re-sync governanceDUO's `tests/infra_contract/authorize_query.rq` to our pinned query (a PR to governanceDUO); `make infra-contract-check SAGEBRAIN_INFRA=<this repo>` passes
- [ ] T604 Propose upstream the fileview-based benefactor sync (one table query per study) needed for portal-wide gate coverage (research R-4)

## Phase G — enforce on dev
- [ ] T701 Query shadow run: scenario queries for test users with and without current approvals; record `governance_decision` results
- [ ] T702 Query `enforce` (`NEPTUNE_VIZ.enabled: false`): rerun the scenarios, the agent question and the cross-user status read
- [ ] T703 Gate `enforce` on dev once the governance snapshot covers every NF benefactor
- [ ] T704 Record the measured overhead (gate runtime; preflight + PDP p50/p95)
- [ ] T705 CLAUDE.md "Governance" section; Graph Explorer caveat

## Phase S2 — query-time ACLs (Stage 2)
- [ ] T801 Test (cedarpy): `acl_download` permits via `acl:agent`, `acl:agentGroup` + `vcard:hasMember`, and `acl:agentClass`; READ-only → deny. It replaces `ingested_open`.
- [ ] T802 Test: PDP mapping for rosters and agent classes; the as-published worked example gives 2000001 ALLOW and 1000002 DENY at 2026-08-01
- [ ] T803 Test: the gate's `invariant: governed` mode (benefactor required, agent-class clause dropped)
- [ ] T804 Sync includes team rosters; shadow → enforce

## Phase H — conditions and derivation (after the POC)
- [ ] T901 Contract: optional `purpose` (DUO term) on `POST /query` and `POST /ask` → Cedar `context.purpose`
- [ ] T902 Test (cedarpy): one `forbid` per supported DUO code (`DUO:0000007` + `gov:diseaseContext` first)
- [ ] T903 `gov:ControlLabel` for touched non-Synapse IRIs (needs derivation edges in the NF data graph)
- [ ] T904 Evaluate Policy Fabric (Rego + VC policy cards) and GA4GH Passports; record the outcome in `research.md`
