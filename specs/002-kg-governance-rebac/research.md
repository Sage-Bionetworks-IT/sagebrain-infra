# 002 — Research

## R-1 Preflight + post-check vs in-cluster query rewrite
The `policy-engine` branch (commit c259f58, `docs/governance_hybrid_architecture.md`) proposed a
**query rewrite**. It injects `?x gov:hasACL ?g . ?g gov:principal "user:…"` joins so Neptune filters
at query time. That requires the governance data to be **in the data cluster**.

Grants (which users hold which ARs) are sensitive, and the data cluster is readable by every
read-only role and by Graph Explorer. So governance data lives outside it, in tables only the
decision Lambda can read, and the decision has to happen outside the data query:

- **Chosen: a touch-set preflight followed by a whole-query deny.** The preflight covers resources
  touched anywhere in the `WHERE`, including in aggregates and filters. A plain post-filter on result
  IRIs would miss those: `SELECT (COUNT(?f) AS ?n) WHERE { ?f nf:parentStudy <embargoed> }` returns
  no IRIs but leaks a count. The cost is a second Neptune query plus a cap on how broad a query can be.
- The preflight reads the same dataset as the real query: today the union of all snapshots, and
  after spec 004 the latest-snapshot dataset. So it never sees fewer resources than the query.
- **Later optimisation:** the decision Lambda returns the caller's allowed set, and the data query
  gets a `VALUES ?benefactor {…}` restriction. For that, the data graph needs `benefactor` edges
  (non-sensitive) but no ACLs or grants.

## R-2 AVP vs embedded cedarpy
AVP was chosen: a managed policy store, CloudTrail on policy changes, determining policy ids in the
response, and continuity with the earlier `docs/avp/` work. `BatchIsAuthorized` takes ≤30 requests.
Deduping by (open, AR set) keeps a typical query to 1–3 calls, because files inherit their ARs
from a small number of studies.

cedarpy is used **only in tests**, to evaluate the exact `.cedar` files the stack deploys.

Cedar doesn't read any store. Every request carries its entities, so the policies don't care
whether those came from a graph or from DynamoDB items. That's what makes D-5 cheap to reverse.

## R-3 Lessons from the prior branches
- `agents/governance-graph-rebac-poc-implementation`:
  - Generic Cedar policies that take grant matching as **context flags** (`principalMatchesGrant`
    etc.) push the real logic into Python. That makes Cedar a rubber stamp.
  - This design moves the relationships into Cedar **entities** (`Entity.accessRequirements`,
    `User.approvedRequirements`; Stage 2 adds `User in Team` and `Entity.downloadTeams`), so the
    ReBAC logic is in the policy text.
  - Kept from it: fail-closed on backend error, per-resource results.
- `policy-engine`:
  - The post-filter only checked result IRIs (the R-1 leak).
  - Kept from it: the idea of a capability/Policy Engine as the long-term shape (GA4GH Passport /
    DUO evaluation). That's out of scope here.
- Both branches put the ReBAC authorizer behind API Gateway as `POST /authorize`. Here the decision
  Lambda is private and invoked only by the workers.

## R-4 Source: the Synapse data warehouse (replaces governanceDUO's per-entity sync)
The first draft used governanceDUO's `sync_governance_graph.py`, which calls the Synapse REST API
per entity under an ACT credential. At NF scale (~500k files) that's too many calls, and
`POST /accessApproval/search` is ACT-only.

The Synapse data warehouse (Snowflake, `SYNAPSE_DATA_WAREHOUSE.RDS_RAW`) mirrors the repository
database, so the whole projection is a few set-based queries with no ACT credential:
- the node table gives the parent chain and benefactor for every entity, so the recursive AR walk
  and the benefactor lookup cover a whole portal at once;
- the ACL tables give the AUTHENTICATED_USERS / PUBLIC `DOWNLOAD` check;
- managed ACT grants come from Approved data-access submissions and their per-accessor changes
  (data-model.md). Approval is per **accessor** (`accessor_changes`), not per submitter.

The submission-reporting query the data team already maintains (submission type, renewal cycle,
reviewer names) is the workflow view. The decision only needs its outcome, the latest accessor
change per (user, AR), so the export reads the same raw tables but projects much less, and
copies no usernames or reviewer data.

Synapse REST stays the reference for checking the export: `POST /restrictionInformation` as a
given user says whether they have unmet ARs on an entity (plan risk "warehouse semantics").

## R-5 Alignment with governanceDUO
Sources, read 2026-10-05:
- [Graph design](https://mc2-center.github.io/governanceDUO/graph-design/#governance-graph-design)
- [Technical implementation](https://mc2-center.github.io/governanceDUO/graph-design-implementation/),
  including the [worked example](https://mc2-center.github.io/governanceDUO/graph-design-implementation/#a-worked-example)
- [Downstream changes](https://mc2-center.github.io/governanceDUO/downstream_changes/)
- [Policy Fabric](https://mc2-center.github.io/governanceDUO/policy-fabric/)
- Repository branch `synapse-curator-infra`: `plans/rebac_governance_graph_alignment.md`,
  `linkml/examples/graph/rdf/governance_graph.ttl`

governanceDUO is one LinkML schema with four layers: governance (WAC ACLs, vCard teams, ARs,
approvals), conditions (DUO/MONDO), provenance (PROV-O) and derivation policy (`ControlLabel`,
tiers). Its rules:
- **Inheritance is walked, not stored** (`gov:benefactor`, `gov:parent*`).
- **Effective access is ACL AND every AR satisfied.**
- **Unknown fails closed.**
- **Permission = `Download`.** A READ-only grantee is denied.

What we take and what we don't:
- **Take the rules and the worked example.** The decision Lambda applies the same rules, and the
  worked example (study `syn2343195`, file `syn10081783`, AR 42, an approval that expired
  2026-08-14) is the golden test, translated into table items.
- **Don't load its graph (D-5).** Stage 1 needs one lookup per entity and one per user. A second
  Neptune cluster, a second pipeline and a SHACL-validated publish step would cost more than the
  model Stage 1 uses. The walk happens in the export instead of at query time.
- **Revisit at Stage 2.** Team rosters, DUO conditions and derivation labels are where a graph
  starts to pay off. The Cedar entity shape doesn't change either way (R-2).

## R-6 Example governance models surveyed
| Model | What it gives | Use here |
|---|---|---|
| **W3C Web Access Control** (`acl:`) | Authorization = agent / agentGroup / agentClass × mode × accessTo | ACL vocabulary (via governanceDUO) |
| **vCard groups** | team rosters (`vcard:hasMember`) | Stage 2: `User in Team` in Cedar |
| **GA4GH DUO** (+ MONDO) | machine-readable data-use conditions on ARs | Later: one Cedar `forbid` per DUO code over a declared purpose |
| **W3C PROV-O** + governanceDUO derivation labels | what derived content inherits (`ControlLabel`, tiers, flagged `DerivationReview`) | Later: govern derived and non-Synapse nodes (Q-2) |
| **Policy Fabric** ([hasan7n/tmp-policies](https://github.com/hasan7n/tmp-policies), MLCommons) | Rego/OPA policy cards, one per DUO code, evaluated over W3C Verifiable Credentials | Not adopted. Reference for credential-evidence conditions; a Policy Fabric decision could become Cedar context |
| **GA4GH DRS / Passports** ([DRS spec](https://ga4gh.github.io/data-repository-service-schemas/docs/)) | `PassportAuth` + trusted visa issuers per object; a ControlledAccessGrants visa per approved submission | Not adopted. A future alternative source of grant evidence |
| **Amazon Verified Permissions / Cedar** | ReBAC through the entity hierarchy, policy store, determining-policy audit | The decision point (R-2) |

## R-7 Why both halves at query time, with the ingest gate later
- **The gate alone leaves a gap.** The first draft enforced the ACL only at ingest. But the gate
  needs spec 004's pipeline changes, and it never checks what's already loaded. Until it ships,
  team-only content in today's NF snapshot would be readable by every team member.
- **The ACL check is nearly free at query time.** `open_to_authenticated` is one attribute on the
  entities item the decision Lambda reads anyway for `ar_ids`.
- **No sensitive rosters in Stage 1.** "Open to AUTHENTICATED_USERS" is a property of the entity,
  not of any caller, so Stage 1 never needs team membership.
- **It matches the deployment's auth model.** Every API caller is an authenticated Synapse user,
  so "open to registered users" is the population the API already admits.
- **ARs are the real gap for open data.** Many Synapse datasets are AUTHENTICATED_USERS DOWNLOAD
  but sit behind an AR. Their metadata, including participant-level annotations, is readable in the
  KG today without the AR.
- **The gate is still worth having** as defence in depth and as feedback to depositors before a
  load, once a full-portal export exists.

## Open questions
- **Q-2 Governance for non-Synapse nodes (open; flagged, not solved).** Decision for this spec:
  non-Synapse nodes are **allowed through** the preflight (and the gate). Stage 1 governs only the
  data that needs it today, Synapse entities. Findings from `nf/2026-07-14` (2026-10-14 scan):
  - External vocabulary is identifiers only (DOI, PubMed, ORCID, EDAM, RRID); no MONDO/NCIT/gene IRIs.
  - Minted `nf:<kind>/<uuid>` nodes (~5k) almost never link straight to Synapse. Only
    `nf:hasModelSystem` (inbound) and `nf:biobankURL` do. Donors reach Synapse at 2–4 hops
    (18% not at all); PatientDerivedModel (47, free-text `clinicalData` with age/sex/variant)
    has no path at all. `nf:parentStudy` links Synapse to Synapse only.
  - So a query that touches only minted nodes (e.g. donors' age/sex/race) is not checked.
    That's acceptable only if these nodes are public by origin (believed to be NF Research Tools
    Central; unconfirmed).
  - Candidate answer: classify minted types as public or governed in config; governed types get an
    explicit anchor predicate emitted by the depositor; an unclassified type fails the gate.
    Alternatives: governanceDUO's derivation labels (`gov:ControlLabel`).
  - Revisit before Stage 2, or as soon as a depositor adds controlled non-Synapse content.
- **Q-5 Purpose of use.** How does a caller declare purpose for DUO evaluation: a request field,
  a Synapse research project, or a Passport visa?
- **Q-6 Gate scope: subjects only, or every IRI?** The gate scans IRIs in any position. A study
  referenced via `nf:parentStudy` must therefore itself be governed and open, even when only its
  files are described. Is that intended, or should only subject IRIs be checked?
- **Q-8 AR types other than managed ACT.** Self-sign, terms-of-use and legacy ACT ARs are approved
  without a data-access submission (Synapse writes an access approval directly). In Stage 1 they
  have no grant rows, so content behind them is denied to everyone. Next step: source them from the
  warehouse's access-approval table (state `APPROVED`, `expired_on`). Lock ARs stay always unmet.
  Measure how much NF content this affects in phase B.
- **Q-9 AR inheritance rule.** The spec assumes an AR applies to every descendant of its subject
  along the parent path, even past local sharing settings (FR-3). Confirm against Synapse's docs
  or `POST /restrictionInformation` on such an entity in phase B.
- **Q-10 Snowflake tables.** Confirm that `RDS_RAW` has the node, ACL, subject-access-requirement,
  AR revision and submission accessor-change tables with the columns data-model.md expects, and how
  often they refresh. Not yet checked: needs an interactive Snowflake login.

## Measurements
_To be filled in by phase B (export counts, grants per AR), phase E (overhead p50/p95) and phase F
(gate runtime on `nf/2026-07-14`)._
