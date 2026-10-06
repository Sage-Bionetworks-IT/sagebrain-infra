# 002 — Research

## R-1 Preflight + post-check vs in-cluster query rewrite
The `policy-engine` branch (commit c259f58, `docs/governance_hybrid_architecture.md`) proposed a
**query rewrite**. It injects `?x gov:hasACL ?g . ?g gov:principal "user:…"` joins so Neptune filters
at query time. That requires the governance triples to be **in the data cluster**.

This feature keeps governance in a **separate, private cluster**, because team membership and
approvals are sensitive and the data cluster is readable by every read-only role and by Graph
Explorer. A SPARQL query can't join across Neptune clusters. Neptune has no federation to a
private cluster, and `SERVICE` is rejected anyway. So the decision has to happen outside the data
query:

- **Chosen: a touch-set preflight followed by a whole-query deny.** The preflight covers resources
  touched anywhere in the `WHERE`, including in aggregates and filters. A plain post-filter on result
  IRIs would miss those: `SELECT (COUNT(?f) AS ?n) WHERE { ?f nf:parentStudy <embargoed> }` returns
  no IRIs but leaks a count. The cost is a second Neptune query plus a cap on how broad a query can be.
- **Later optimisation:** the PDP returns the caller's allowed benefactor set, and the data query
  gets a `VALUES ?benefactor {…}` restriction. For that, the data graph needs `benefactor` edges
  (non-sensitive) but no ACLs. That is the scalable path once all portals are synced.

## R-2 AVP vs embedded cedarpy
AVP was chosen: a managed policy store, CloudTrail on policy changes, determining policy ids in the
response, and continuity with the earlier `docs/avp/` work. `BatchIsAuthorized` takes ≤30 requests.
Deduping by (benefactor, AR set) keeps a typical query to 1–3 calls, because files inherit from a
small number of benefactors.

cedarpy is used **only in tests**, to evaluate the exact `.cedar` files the stack deploys.

## R-3 Lessons from the prior branches
- `agents/governance-graph-rebac-poc-implementation`:
  - Generic Cedar policies that take grant matching as **context flags** (`principalMatchesGrant`
    etc.) push the real logic into Python. That makes Cedar a rubber stamp.
  - This design moves the relationships into Cedar **entities** (Entity.accessRequirements, User.approvedRequirements; Stage 2 adds User ∈ Team and Entity.downloadTeams),
    so the ReBAC logic is in the policy text.
  - Kept from it: fail-closed on backend error, per-resource results.
- `policy-engine`:
  - The post-filter only checked result IRIs (the R-1 leak).
  - Kept from it: the `gov:` vocabulary and the idea of a capability/Policy Engine as the long-term
    shape (GA4GH Passport / DUO evaluation). That's out of scope here.
- Both branches put the ReBAC authorizer behind API Gateway as `POST /authorize`. Here the PDP is a
  private Lambda invoked only by the workers.

## R-4 Synapse sources (superseded by governanceDUO's sync, see R-5)
governanceDUO's `sync_governance_graph.py` calls these, under an ACT credential:
- `GET /entity/{id}/path` and the benefactor;
- `GET /entity/{id}/acl`;
- `GET /teamMembers`;
- `GET /entity/{id}/accessRequirement`;
- `POST /accessRequirement/{id}/submissions`;
- `POST /accessApproval/search`.

It works per entity. At NF scale (~500k files) that's too many calls. One upstream optimisation is
to take `benefactorId` for every file of a study from a single fileview query, since Study nodes
carry `nf:studyFileviewId`. That would be a governanceDUO change, not an infra one.

## R-5 Alignment with governanceDUO
Sources, read 2026-10-05:
- [Graph design](https://mc2-center.github.io/governanceDUO/graph-design/#governance-graph-design)
- [Technical implementation](https://mc2-center.github.io/governanceDUO/graph-design-implementation/),
  including the [worked example](https://mc2-center.github.io/governanceDUO/graph-design-implementation/#a-worked-example)
- [Knowledge graph](https://mc2-center.github.io/governanceDUO/knowledge-graph/)
- [Use cases](https://mc2-center.github.io/governanceDUO/use-cases/)
- [Downstream changes](https://mc2-center.github.io/governanceDUO/downstream_changes/)
- [Policy Fabric](https://mc2-center.github.io/governanceDUO/policy-fabric/)
- [DRS interop](https://mc2-center.github.io/governanceDUO/drs-interop/)
- Repository branch `synapse-curator-infra`:
  - `plans/rebac_governance_graph_alignment.md`
  - `plans/governance_graph_ingestion.md`
  - `tests/infra_contract/authorize_query.rq`
  - `linkml/examples/graph/rdf/governance_graph.ttl`

What governanceDUO is:
- One LinkML schema with four content layers:
  - **governance**: WAC ACLs, vCard teams, ARs, submissions, approvals;
  - **conditions**: DUO/MONDO;
  - **provenance**: PROV-O;
  - **derivation policy**: `ControlLabel` / `DerivationReview`, tiers ranked Anonymous < Open <
    Controlled < Private < Unclassified.
- The schema produces OWL/SHACL artifacts.
- **Inheritance is walked, not stored.** `gov:benefactor` / `gov:parent*`.
- **Effective access is ACL AND every AR satisfied.**
- **Unknown fails closed.**
- Projections such as `authorizer_v1` (the old `https://sagebionetworks.org/governance/`
  namespace, mirroring our `policy-engine` branch at `fc6de51`) exist only for backward
  compatibility.

Decisions:
- **Consume, don't redefine.** This spec drops its own `gov:` vocabulary and its own Synapse sync.
  governanceDUO owns the model, the sync (`sync_governance_graph.py`) and the SHACL. We pin a
  commit and `GRAPH_VERSION`.
- **Read the canonical layer, not `authorizer_v1`.** This follows the downstream-changes guidance.
  `authorizer_v1` can't express `acl:agentClass` (PUBLIC / AUTHENTICATED_USERS) grants. It keeps
  team grants at the team id unless the opt-in teams projection is used. Its `intersection` mode
  over-denies inherited grants. And infra previously took approvals from the caller ("fails open").
  The PDP fixes all four: it expands `vcard:hasMember` and agent classes itself, uses the
  benefactor's ACL, and evaluates `gov:Approval` status and expiry at decision time.
- **Permission = `gov:Download`**, matching `authorizer_v1`'s ACCESS⇐DOWNLOAD mapping and the
  contract check (a READ-only grantee is denied).
- **Separate cluster (deviation D-5).** governanceDUO places governance and domain graphs in one
  Neptune, joined on `syn:` IRIs. We split them so rosters and approvals aren't readable by every
  data-plane role. The join moves from SPARQL into the PDP. That's why R-1's preflight exists.
- **Named graphs.** governanceDUO exports one graph without named-graph structure. We wrap each
  export in `urn:sagebrain:gov:{date}` at load time, which is invisible to governanceDUO.
- **Worked example = golden test.** Its study and file (`syn2343195`, `syn10081783`) are in our
  NF data. Its approval (`expiresAt` 2026-08-14) is already expired as of today, which exercises
  the expiry rule the old authorizer lacked.

## R-6 Example governance models surveyed
| Model | What it gives | Use here |
|---|---|---|
| **W3C Web Access Control** (`acl:`) | Authorization = agent / agentGroup / agentClass × mode × accessTo | ACL layer (via governanceDUO) |
| **vCard groups** | team rosters (`vcard:hasMember`) | User ∈ Team parents in Cedar |
| **GA4GH DUO** (+ MONDO) | machine-readable data-use conditions on ARs | Phase H: one Cedar `forbid` per DUO code over a declared purpose |
| **W3C PROV-O** + governanceDUO derivation labels | what derived content inherits (`ControlLabel`, tiers, flagged `DerivationReview`) | Phase H: govern derived and non-Synapse nodes |
| **Policy Fabric** ([hasan7n/tmp-policies](https://github.com/hasan7n/tmp-policies), MLCommons) | Rego/OPA policy cards, one per DUO code, evaluated over W3C Verifiable Credentials; an Asset Guardian enforces capabilities (`do_download`). governanceDUO's `policy_fabric_bindings.yaml` maps 21 DUO codes to cards and credentials | Not adopted. It's the reference for credential-evidence conditions (location, affiliation) in Phase H. Cedar stays the PDP; a Policy Fabric decision could become Cedar context |
| **GA4GH DRS / Passports** ([DRS spec](https://ga4gh.github.io/data-repository-service-schemas/docs/)) | `PassportAuth` + trusted visa issuers per object; a ControlledAccessGrants visa per approved submission | Not adopted. A future alternative source of approval evidence |
| **Amazon Verified Permissions / Cedar** | ReBAC through the entity hierarchy, policy store, determining-policy audit | The PDP (R-2) |

## R-7 Why access requirements first, with the ACL as an ingestion invariant
- **One enforcement point per half, each where it's cheapest.**
  - The ACL half is checked once per snapshot, at ingest, against a handful of benefactors.
  - The AR half has to be per caller, so it's checked at query time.
- **No sensitive rosters in Stage 1.** "Downloadable by AUTHENTICATED_USERS" is a property of the
  entity, not of any caller. Stage 1 never needs team membership, which is the most sensitive and
  highest-churn part of the governance graph. The private cluster then holds only ACL agent-class
  entries, ARs and approvals.
- **It matches the deployment's auth model.** Every API caller is an authenticated Synapse user
  (plus `machine`). "Open to registered users" is exactly the population the API already admits,
  so in Stage 1 the ACL half is true for every caller by construction.
- **ARs are the real gap for open data.** In Synapse, many datasets are AUTHENTICATED_USERS
  DOWNLOAD but sit behind a clickwrap or managed ACT access requirement. Data like that is in the
  KG today, and its metadata, including participant-level annotations, is readable without the
  AR. Stage 1b closes exactly that.
- **Fail closed at both ends.** The gate refuses `ungoverned` IRIs, and Neptune can't partially
  load a prefix, so the KG never holds an entity the governance graph doesn't know about. That
  is what makes the PDP's `ingested_open` permit sound.
- **Caveat: time-of-check vs time-of-use.** An ACL tightened in Synapse *after* ingest isn't
  caught until the next governance snapshot plus a data re-ingest. Two mitigations:
  - the PDP can re-run the gate's agent-class check per decision (cheap, benefactor-keyed). That's
    an optional FR-8 hardening.
  - Stage 2 makes the ACL query-time anyway.
- **Rejected alternative.** Filtering restricted entities out of the snapshot at ingest. That
  would mean rewriting a 144 MB Turtle file and silently dropping data. Rejection plus a report
  puts the fix where it belongs: in the transform, or in the Synapse ACL.

## Open questions
- **Q-6 Gate scope: subjects only, or every IRI?** The gate scans IRIs in any position. A study
  referenced via `nf:parentStudy` must therefore itself be open to registered users, even when only
  its files are described. Is that intended, or should only subject IRIs (nodes with metadata) be
  checked?
- **Q-7 Gate coverage.** The gate in `enforce` needs a governance snapshot covering every NF
  benefactor. Should coverage come from governanceDUO's sync (scaled with the fileview
  optimisation), or from a lighter ACL-only sync that needs no ACT credential (ACLs are readable
  without ACT; approvals aren't)?
- **Q-1 Approvals (resolved by governanceDUO's ingestion plan).** `POST /accessApproval/search`
  and `POST /accessRequirement/{id}/submissions` are **ACT-only**. `AccessApproval.expiredOn`
  supplies `gov:expiresAt`. The sync must run under an ACT (or AR-reviewer) credential. Without
  one, approvals are empty and AR-gated resources fail closed.
- **Q-2 Governance for non-Synapse nodes.** Donor demographics are the most sensitive ungoverned
  data. governanceDUO's answer is derivation labels via PROV-O / `sagebrain:derived_from`, but the
  NF data graph has no such edges. Does the NF transform emit them, or do we infer them via
  `nf:parentStudy`?
- **Q-3 Machine principal.** Should it get a configured service team instead of public-only?
- **Q-4 Service ACT credential.** Who owns it, and where does it live (Secrets Manager), if the
  sync is ever scheduled? governanceDUO's sync is on-demand CLI today.
- **Q-5 Purpose of use.** How does a caller declare purpose for DUO evaluation: a request field,
  a Synapse research project, or a Passport visa?

## Measurements
_To be filled in by T307 (gate runtime and the pass/fail breakdown of `nf/2026-07-14`), T701 and T704._
