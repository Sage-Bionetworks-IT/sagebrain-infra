# 002 — Knowledge-graph governance: access requirements first, then ACLs (Cedar over the governanceDUO graph)

**Status:** Draft (POC). Plan only; no implementation yet.
**Owner:** Thomas Yu
**Depends on:** spec 001 (FastAPI service + shared `sagebrain_core` query service, branch
`spec-driven-fastapi-phase0`) being merged into `dev` first. This spec builds on several things
spec 001 adds:
- `core/sagebrain_core` (the enforcement chokepoint, `run_query`, limits);
- `app/sagebrain_api`;
- `api/openapi.yaml`, `tests/contract/cases.yaml`;
- `.specify/memory/constitution.md` and `specs/_template/`.

None of these exist on `dev` yet. Implementation starts after that merge.
**Contract changes:**
- `api/openapi.yaml`:
  - the job `error` shape gains `governance` on `GET /query/{job_id}` and `GET /ask/{job_id}`;
  - both status ops become owner-only (404);
  - new `governance_denied` agent step.
- External: the decision Lambda's governance SPARQL becomes the infra half of the governanceDUO infra contract (data-model.md).

**Upstream model:** [governanceDUO graph design](https://mc2-center.github.io/governanceDUO/graph-design/#governance-graph-design),
[technical implementation](https://mc2-center.github.io/governanceDUO/graph-design-implementation/) and its
[worked example](https://mc2-center.github.io/governanceDUO/graph-design-implementation/#a-worked-example),
pinned at `GRAPH_VERSION` 0.2.x.

## Problem
Any member of Synapse team 3605470 (`AUTH.synapse_team_id`) can read **every** node in the
knowledge graph. Nothing stops content from being *ingested* whatever its Synapse permissions are,
and nothing checks at query time what the caller may see.

governanceDUO defines **effective access** as two halves: a matching ACL grant on the entity's
benefactor **AND** every access requirement (AR) inherited along `gov:parent*` approved and
unexpired.

This feature delivers those halves in two stages. The ACL half is first enforced as an
**ingestion invariant**: the knowledge graph only ever contains content every registered Synapse
user can download. That lets the first query-time stage evaluate access requirements alone.

| Stage | ACL half | AR half |
|---|---|---|
| **1 (this POC)** | **Ingestion gate.** A data snapshot is loaded only if every Synapse entity in it has a benefactor ACL granting `DOWNLOAD` to AUTHENTICATED_USERS (273948). Every node in the KG is therefore ACL-downloadable by any registered user, so the ACL half is true by construction for every authenticated caller. | **Query-time.** Cedar/AVP checks that the caller holds a current approval for every AR inherited by every touched resource. |
| **2 (follow-up)** | **Query-time.** Per-user and per-team ACL evaluation (WAC `acl:agent` / `acl:agentGroup` + `vcard:hasMember`). The gate relaxes from "AUTHENTICATED_USERS DOWNLOAD" to "governed" (has a benefactor in the governance graph). | unchanged |

Team rosters and approvals are sensitive, so the governance graph lives in a **separate, private
Neptune cluster** (deviation D-5 from governanceDUO, which assumes one Neptune). Stage 1 needs **no
team rosters** at all, which shrinks what has to be stored.

## User stories
- **Data steward / pipeline owner:** a snapshot containing anything not downloadable by all
  registered Synapse users is **refused before it reaches Neptune**. I get a report of the offending
  entities and their benefactors, so the KG can't accidentally expose restricted content.
- **Researcher:** my query or agent question succeeds when I've satisfied (clickwrap accepted, or
  managed AR approved and unexpired) every AR on everything it touches. Otherwise it fails with
  `governance_denied`, listing the resources and the unmet ARs.
- **Data governance lead:** AR policy is a Cedar policy over relationships from the governanceDUO
  graph (entity →`gov:parent*`→ `gov:requiresAR` ← `gov:satisfies` approval `gov:heldBy` user).
  The decision log names the determining policy.
- **governanceDUO maintainer:** I own the model and the Synapse sync. Infra pins a graph version
  and a contract query.
- **Operator:** I run the gate and the query enforcement in `report`/`shadow` before `enforce`.

## Functional requirements

### Shared infrastructure
- **FR-1 Private governance cluster.** `app-{env}-governance`:
  - a separate Neptune cluster;
  - IAM auth, encrypted, private subnets;
  - its SG's **only** ingress is the governance Lambdas' SG (the PDP and the ingest gate) on 8182.

  The query worker, agent, FastAPI task and Graph Explorer have no IAM or network path to it.
  *Tests:* `test_governance_stack.py::test_cluster_ingress_only_from_governance_lambdas`,
  `::test_data_plane_roles_have_no_governance_access`.
- **FR-2 Ingest governanceDUO exports.** A governanceDUO export (`sync_governance_graph.py`) is
  published by `tools/publish_governance_snapshot.py` to the private governance bucket as
  `gov/YYYY-MM-DD/data/*.ttl` + `manifest.ttl`. The tool validates the export against the pinned
  SHACL first, and the manifest records the commit, `GRAPH_VERSION` and scope.

  A second `NeptunePipelineStack` instance loads it into `urn:sagebrain:gov:{date}`. Consumers
  refuse a snapshot whose `GRAPH_VERSION` major.minor ≠ `GOVERNANCE.graph_version`.

  *Tests:* `test_governance_stack.py::test_bucket_not_account_wide`,
  `tests/unit/tools/test_publish_governance_snapshot.py`, `test_governance_authorize.py::test_rejects_unpinned_graph_version`.
- **FR-3 Latest snapshot only.** The gate and the PDP read the latest **successful** `gov`
  snapshot graph from the governance pipeline's tracking table. They never read the union default
  graph. *Test:* `::test_uses_latest_snapshot_graph`.

### Stage 1a — ACL ingestion gate (the "AUTHENTICATED_USERS can download" invariant)
- **FR-4 Gate before load.** The data pipeline (`app-{env}-neptune-pipeline`) runs a
  `GovernanceGate` task **before** `StartLoad` for every `{portal}/YYYY-MM-DD/manifest.ttl`. It
  streams every object under `{portal}/YYYY-MM-DD/data/` and collects every Synapse IRI
  (`https://www.synapse.org/Synapse:syn\d+`) appearing **anywhere** (subject or object). That's a
  conservative superset with no Turtle parse, and it works on the 144 MB `files.ttl`.
  *Tests:* `tests/unit/test_governance_gate.py::test_collects_iris_streaming`,
  `test_neptune_pipeline_stack.py::test_gate_precedes_start_load`.
- **FR-5 The invariant.** An IRI passes if the governance graph has:
  - `?iri gov:benefactor ?b`, **and**
  - an `acl:Authorization` with `acl:accessTo ?b`, `acl:agentClass acl:AuthenticatedAgent`
    (or `foaf:Agent`), and `acl:mode gov:Download`.

  Any other IRI fails. That includes IRIs that are ungoverned (no benefactor), team-only or
  user-only, or READ-only. *Tests:* `test_governance_gate.py::test_invariant_*`, worked-example
  table FR-16a.
- **FR-6 Whole-snapshot reject.** If any IRI fails, the snapshot is **not loaded**. Neptune's bulk
  loader can't filter a prefix, so partial loads are impossible. On failure:
  - the gate writes `{portal}/YYYY-MM-DD/governance_report.json` with counts, failing IRIs (capped
    at 1000), failure reason, benefactor and governance snapshot;
  - the tracking table records `status=REJECTED_GOVERNANCE`;
  - the execution ends `Fail` (existing alarm);
  - a `{"event":"kg_governance_gate", ...}` line is logged.

  A passing snapshot records `governance_snapshot` on its load item, for lineage.
  *Tests:* `test_governance_gate.py::test_reject_writes_report`, `::test_pass_records_lineage`.
- **FR-7 Gate modes.** `NEPTUNE_PIPELINE.governance_gate` sets the behaviour:
  - `off`: no gate task.
  - `report`: evaluate, write the report and log it, but always load. This is the dev default
    until the governance sync covers the whole NF portal.
  - `enforce`: reject per FR-6.

  *Test:* `test_neptune_pipeline_stack.py::test_gate_modes`.

### Stage 1b — query-time access requirements
- **FR-8 AR decision.** `readKg` on a Synapse resource is decided by AVP `BatchIsAuthorized` over
  `policies/governance/`:
  - **`ingested_open` (permit):** a resource that is in the governance graph with a benefactor is
    permitted. It's only in the KG because it passed FR-5.
  - **`access_requirements` (forbid):** unless the caller holds an approval for **every**
    `gov:requiresAR` on `gov:parent*`. The approval must be `gov:heldBy synuser:<id>`, have
    `gov:status gov:Approved`, and have `gov:expiresAt` absent or later than the decision time.

  This covers clickwrap ("self-sign") and managed ACT ARs alike.
  *Tests:* `tests/unit/governance/test_policies.py` (cedarpy, the same `.cedar` files the stack
  deploys).
- **FR-9 Fail closed.** The PDP denies:
  - with `reason: ungoverned` when a resource has no `gov:benefactor`;
  - with `reason: authorization_unavailable` on a Neptune, AVP or tracking-table error, or an
    unpinned graph version.

  An approval whose `requirementVersion` ≠ the AR's `versionNumber` still counts, but is logged as
  `stale_approval`.
- **FR-10 Touch-set preflight.** `sagebrain_core.governance` parses the query (rdflib) and runs a
  preflight with the same `WHERE`. The preflight projects `DISTINCT` every subject- and
  object-position variable plus constant IRIs, filtered to Synapse IRIs, with
  `LIMIT max_touch_set + 1`. That covers resources reached only inside `FILTER`, `OPTIONAL`,
  subqueries or aggregates.

  It rejects with 400:
  - non-SELECT queries;
  - `SERVICE`;
  - `IRI()`/`URI()`/`STRUUID()`;
  - a touch set over the cap.

  *Tests:* `tests/unit/core/test_governance.py::test_touch_set_*`.
- **FR-11 Whole-query deny.** Any denied touched resource fails the job with
  `error="governance_denied"` and
  `governance: {decision, reason, denied_resources[≤50], denied_count, unmet_requirements[≤50]}`.
  The `unmet_requirements` are the AR ids, so the caller knows what to request in Synapse.
  *Tests:* `cases.yaml` `query_status_governance_denied`, `test_governance.py::test_enforce_denies_whole_query`.
- **FR-12 Defense in depth.** Result-binding Synapse IRIs must be within the authorized touch set.
  Otherwise the job is denied with `reason: result_outside_touch_set`.
- **FR-13 One chokepoint, both callers.** `run_query` (the agent) and the `/query` worker call
  `governance.enforce`. In the agent, a denial becomes a `tool_result` error step.
- **FR-14 Query modes.** `GOVERNANCE.mode` sets the behaviour:
  - `off`: no-op;
  - `shadow`: decide and log, never block;
  - `enforce`: block.

  In `enforce`, `cdk synth` fails if `NEPTUNE_VIZ.enabled`, because Graph Explorer bypasses the
  chokepoint.

### Cross-cutting
- **FR-15 Caller identity and job ownership.** `POST /query` (Lambda and FastAPI) carries `user_id`
  on the SQS message and job item. `GET /query/{job_id}` and `GET /ask/{job_id}` return 404 unless
  the caller owns the job. *Tests:* `cases.yaml` `*_status_other_owner`,
  `test_job_api_handlers.py::test_query_message_carries_user_id`.
- **FR-16 Worked example is the golden test.** governanceDUO's worked example is vendored at a
  pinned commit (`tests/fixtures/governance/governance_graph.example.ttl`). Its study
  `syn:syn2343195` and fastq `syn:syn10081783` are real NF IRIs in our data graph.

  The as-published file's only grant is team 9000001 DOWNLOAD, so it **fails** the gate. A
  derived fixture, `governance_graph.open.ttl`, adds the study's and file's
  `acl:agentClass acl:AuthenticatedAgent ; acl:mode gov:Download` authorizations, and **passes**.

  **FR-16a: the gate**

  | Snapshot content | Governance fixture | Expected |
  |---|---|---|
  | NF triples mentioning `syn10081783` | as published | REJECT (`not_open_to_authenticated`: team-only grant) |
  | same | `open` variant | PASS |
  | NF triples mentioning an IRI absent from the fixture | either | REJECT (`ungoverned`) |
  | `open` variant with the authorization's mode changed to `gov:Read` | — | REJECT (`not_downloadable`) |

  **FR-16b: query-time ARs (`open` fixture, `readKg` on `syn10081783`)**

  | Principal | Decision time | Expected | Why |
  |---|---|---|---|
  | `synuser:2000001` | 2026-08-01 | ALLOW | AR-42 inherited from the study via `gov:parent`; approval 55001 Approved, not yet expired |
  | `synuser:2000001` | 2026-10-06 (today) | DENY (`access_requirements`, unmet `ar/42`) | approval 55001 expired 2026-08-14T19:33:20Z |
  | `synuser:1000002` | any | DENY (`access_requirements`) | no approval for AR-42 |
  | any user without a current AR-42 approval, on `syn2343195` | any | DENY (`access_requirements`) | the study carries AR-42 itself (`gov:parent*` is reflexive) |

  *Tests:* `tests/unit/governance/test_worked_example.py` (gate + cedarpy + PDP mapping, injectable
  clock). The `open` fixture is also the first dev governance snapshot.
- **FR-17 Decision audit.** Two log events:
  - `governance_decision` (PDP): principal, mode, snapshot, graph version, counts, denied and unmet
    samples, policy ids, warnings, duration.
  - `kg_governance_gate` (gate): portal, snapshot, governance snapshot, n_iris, n_benefactors,
    failed counts by reason, decision, duration.

### Stage 2 — query-time ACLs (follow-up, specified for continuity)
- **FR-18** The PDP adds `acl_download` (permit): an `acl:Authorization` on the benefactor with
  `gov:Download`, matched through any of:
  - `acl:agent synuser:<id>`;
  - `acl:agentGroup` + `vcard:hasMember`;
  - `acl:agentClass`.

  It replaces `ingested_open`. The sync adds team rosters. The gate relaxes to "every IRI
  governed" (FR-5 minus the agent-class clause), which allows restricted content into the KG.
  Worked-example rows: user 2000001 ALLOW / 1000002 DENY on the as-published fixture, at
  2026-08-01.

## Non-functional requirements / thresholds
| Threshold | Value | Declared in | Enforced in | Test |
|---|---|---|---|---|
| Failing IRIs listed in the gate report | 1000 | `lambda_governance_gate` | gate | `test_report_cap` |
| Gate runtime on the NF snapshot (~503k IRIs, ~150 MB TTL) | < 10 min | spec | gate Lambda (15 min, 3 GB) | measured, `research.md` |
| Max governed resources touched per query | 5000 | `sagebrain_core.limits.GOVERNANCE_MAX_TOUCH_SET` | `governance.enforce` | `test_touch_set_cap` |
| Denied resources / unmet ARs returned | 50 / 50 | `limits.GOVERNANCE_DENIED_SAMPLE` | `governance.enforce` | `test_denied_sample_cap` |
| AVP `BatchIsAuthorized` per call | 30 | PDP | PDP | `test_batches_of_30` |
| PDP decision cache TTL | 60 s | `GOVERNANCE.decision_cache_seconds` | PDP | `test_decision_cache_ttl` |
| Pinned governance graph version | 0.2.x | `GOVERNANCE.graph_version` | gate, PDP | `test_rejects_unpinned_graph_version` |
| Query-time governance overhead p50 | < 2 s | spec | measured | `research.md` |

## Deviations from current behaviour
- **D-1** Status reads become owner-only (404 otherwise).
- **D-2** The `/query` SQS message and job item gain `user_id`.
- **D-3** New terminal job error `governance_denied`.
- **D-4** In query `enforce`, `machine` and `legacy-apigw` hold no approvals. They can read only
  resources with no inherited ARs.
- **D-5 (vs governanceDUO).** The governance and domain graphs are in separate clusters, and the
  join happens in the gate/PDP rather than in SPARQL.
- **D-6 (data pipeline).** With `governance_gate: enforce`, a snapshot can be refused, and its
  tracking item records `REJECTED_GOVERNANCE`. Today every well-formed snapshot loads.

## Out of scope / known POC gaps
- **Query-time ACLs.** Covered by Stage 2 (FR-18). Until then, restricted content is kept out at
  ingest instead.
- **Governance coverage for the gate.** In `enforce`, the governance snapshot must cover **every**
  Synapse entity in a data snapshot (≈503k NF IRIs). governanceDUO's per-entity sync doesn't scale
  to that yet (research R-4). That's why dev runs the gate in `report`.
- **DUO conditions, derived and non-Synapse nodes, Policy Fabric, Passports.** Phase H (research R-6).
  Non-Synapse nodes such as `nf:Donor` don't pass through the gate, because only Synapse IRIs are
  checked. They are governed only indirectly, by the ACLs and ARs of the Synapse entities in the
  same snapshot.

## Success criteria
- Unit, synth, pre-commit and redocly checks are clean, and the FR-16a and FR-16b tables pass.
- **Dev gate, `report` then `enforce`.** Re-publishing the NF snapshot `nf/2026-07-14` produces
  `governance_report.json`. In `enforce`, a snapshot containing a team-only entity is refused, and
  one containing only open entities loads.
- **Dev query, `open` worked-example snapshot.**
  `SELECT ?a WHERE { <https://www.synapse.org/Synapse:syn10081783> <http://nf-osi.github.com/terms#assay> ?a }`
  as a caller mapped to `synuser:2000001` gives DENY (expired approval, unmet `ar/42`). As a
  caller with a current approval (live sync), it gives ALLOW.
- The governance cluster's only ingress is the governance Lambdas' SG.
