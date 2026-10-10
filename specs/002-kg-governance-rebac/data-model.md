# 002 — Data model

The governance graph is **not defined here**. It is the canonical **graph layer** of
[governanceDUO](https://mc2-center.github.io/governanceDUO/graph-design/#governance-graph-design)
(LinkML `linkml/graph/governance.yaml`, TBox/shapes `shapes/governance.{owl,shacl}.ttl`,
`GRAPH_VERSION` 0.2.0). This repo **consumes** it: it pins a version, loads exports into the
private governance cluster, and reads it in the policy decision point (PDP). See
[research.md](research.md) R-5 for why the PDP reads the canonical layer and not the
`authorizer_v1` projection.

## Namespaces (as published by governanceDUO)
| Prefix | IRI | Use |
|---|---|---|
| `gov:` | `https://w3id.org/synapse/governance#` | graph-layer classes, properties, SKOS concepts |
| `govid:` | `https://w3id.org/synapse/governance/` | minted instances (`govid:ar/42`, `govid:approval/55001`, `govid:authorization/syn…-…`) |
| `syn:` | `https://www.synapse.org/Synapse:` | Synapse entities. These are **the same IRIs as the NF data graph**, which is the join key. |
| `synuser:` | `https://www.synapse.org/Profile:` | users |
| `synteam:` | `https://www.synapse.org/Team:` | teams |
| `acl:` | `http://www.w3.org/ns/auth/acl#` | W3C Web Access Control |
| `vcard:` | `http://www.w3.org/2006/vcard/ns#` | team membership |
| `foaf:` | `http://xmlns.com/foaf/0.1/` | `foaf:Agent` = PUBLIC |
| `prov:` | `http://www.w3.org/ns/prov#` | provenance |
| `DUO:` / `MONDO:` | `http://purl.obolibrary.org/obo/DUO_` / `…/MONDO_` | data-use conditions / disease context |

## What the gate and PDP read
Stage 1 uses only these rows: benefactor and ACL entries **with `acl:agentClass`** (the gate),
plus hierarchy, AR and approval (the PDP). Team rosters and `acl:agent` / `acl:agentGroup` entries
are Stage 2.

| Fact | Graph pattern | Synapse source (governanceDUO `sync_governance_graph.py`) |
|---|---|---|
| ACL owner | `?e gov:benefactor ?b` (recorded once, never materialised) | `GET /entity/{id}/benefactor`, `/path` |
| Container hierarchy | `?e gov:parent ?p`, walked as `gov:parent*` | `GET /entity/{id}/path` |
| ACL entry | `?auth a acl:Authorization ; acl:accessTo ?b ; acl:mode ?mode` plus **exactly one** of `acl:agent synuser:…` / `acl:agentGroup synteam:…` / `acl:agentClass foaf:Agent \| acl:AuthenticatedAgent` | `GET /entity/{id}/acl` |
| Permission | `acl:mode` ∈ Synapse `ACCESS_TYPE` concepts (`gov:Read`, `gov:Download`, …), each `rdfs:subClassOf` a WAC mode | ACL `accessType` |
| Team roster | `synteam:T a vcard:Group ; vcard:hasMember synuser:U` | `GET /teamMembers/{id}` |
| Access requirement | `?ancestor gov:requiresAR ?ar` (attached only to its own `subjectIds`) | `GET /entity/{id}/accessRequirement` |
| AR shape | `?ar gov:requirementType gov:ManagedACTRequirement \| …`, `gov:accessType gov:Download`, `gov:versionNumber` | AR object |
| Approval | `?ap a gov:Approval ; gov:heldBy synuser:U ; gov:satisfies ?ar ; gov:status gov:Approved ; gov:expiresAt ?t ; gov:requirementVersion ?v` | `POST /accessApproval/search` (**ACT only**). `AccessApproval.expiredOn` becomes `expiresAt`. |
| Conditions (Phase H) | `?ar gov:hasCondition ?c . ?c gov:dataUseTerm DUO:… ; gov:diseaseContext MONDO:…` | ACT AR annotation (Curator Record Set). The read side isn't written in governanceDUO yet. |
| Derivation label (Phase H) | `?l a gov:ControlLabel ; gov:subject ?x ; gov:dataTier ?tier ; gov:sourceAccessRequirements ?ar` | `build_derivation_policy.py` (PROV-O ancestry) |

Rules, all taken from governanceDUO and evaluated by the PDP:
- **Effective access needs both halves.** There must be a matching `acl:Authorization` on the
  benefactor, **and** every AR on `gov:parent*` must be satisfied.
  - In Stage 1 the ACL half is guaranteed at ingest. The gate admits only IRIs whose benefactor
    grants `gov:Download` to `acl:AuthenticatedAgent` or `foaf:Agent`.
  - The query-time PDP evaluates only the AR half.
- **An approval is satisfied** when `status = gov:Approved` and `expiresAt` is absent or later
  than the decision time. If `requirementVersion` ≠ the AR's `versionNumber`, the PDP logs
  `stale_approval`. It still counts the approval, matching governanceDUO's "flag, don't correct"
  stance.
- **Unknown fails closed.** An entity with no `gov:benefactor` is `ungoverned` and is denied. A
  `gov:ControlLabel` with no tier ranks as `gov:UnclassifiedTier`, which is treated as most
  restrictive.
- **`readKg` requires `gov:Download`.** That's the same mapping as governanceDUO's
  `authorizer_v1` (`ACCESS` only where `DOWNLOAD` exists), and the same as its
  `infra-contract-check`: a DOWNLOAD grantee is allowed, a READ-only grantee is denied, and an
  unlisted principal is denied. Files are gated at DOWNLOAD, and the KG exposes file-level
  annotations.

### Example (excerpt of governanceDUO `linkml/examples/graph/rdf/governance_graph.ttl`)
```turtle
syn:syn2343195  a gov:SynapseEntity ; gov:benefactor syn:syn2343195 ; gov:requiresAR govid:ar/42 .
syn:syn10081783 a gov:SynapseEntity ; gov:parent syn:syn2343195 ; gov:benefactor syn:syn10081783 .

govid:authorization/syn10081783-9000001 a acl:Authorization ;
    acl:accessTo syn:syn10081783 ; acl:default syn:syn10081783 ;
    acl:agentGroup synteam:9000001 ; acl:mode gov:Download .
synteam:9000001 a vcard:Group ; vcard:hasMember synuser:2000001 .

govid:ar/42 a gov:AccessRequirement ;
    gov:requirementType gov:ManagedACTRequirement ; gov:accessType gov:Download ;
    gov:hasCondition govid:ar/42/condition/DUO_0000007 .
govid:ar/42/condition/DUO_0000007 a gov:Condition ;
    gov:dataUseTerm DUO:0000007 ; gov:diseaseContext MONDO:0004975 .

govid:approval/55001 a gov:Approval ;
    gov:heldBy synuser:2000001 ; gov:satisfies govid:ar/42 ;
    gov:status gov:Approved ; gov:expiresAt "2026-08-14T19:33:20+00:00"^^xsd:dateTime .
```
This repo vendors this file at a pinned governanceDUO commit as the test fixture
`tests/fixtures/governance/governance_graph.example.ttl`. Its only grant is team 9000001
DOWNLOAD, so it **fails** the Stage 1 gate.

The derived `governance_graph.open.ttl` adds these, and **passes**:
```turtle
govid:authorization/syn2343195-authenticated a acl:Authorization ;
    acl:accessTo syn:syn2343195 ; acl:default syn:syn2343195 ;
    acl:agentClass acl:AuthenticatedAgent ; acl:mode gov:Download .
govid:authorization/syn10081783-authenticated a acl:Authorization ;
    acl:accessTo syn:syn10081783 ; acl:default syn:syn10081783 ;
    acl:agentClass acl:AuthenticatedAgent ; acl:mode gov:Download .
```

## Stage 1a — gate query (`src/lambda_governance_gate/gate_query.rq`)
Run per batch of ≈1000 distinct IRIs collected from the snapshot's `data/` objects:
```sparql
PREFIX gov:  <https://w3id.org/synapse/governance#>
PREFIX acl:  <http://www.w3.org/ns/auth/acl#>
PREFIX foaf: <http://xmlns.com/foaf/0.1/>
SELECT ?iri ?benefactor ?openMode WHERE {
  GRAPH <{snapshot_graph}> {
    VALUES ?iri { {iris} }
    OPTIONAL { ?iri gov:benefactor ?benefactor .
      OPTIONAL { ?auth a acl:Authorization ; acl:accessTo ?benefactor ; acl:mode ?openMode ;
                       acl:agentClass ?cls .
                 FILTER(?cls IN (acl:AuthenticatedAgent, foaf:Agent)) } } } }
```
Each IRI's result:
- **`ungoverned`**: no `?benefactor`.
- **`not_open_to_authenticated`**: a benefactor, but no agent-class authorization.
- **`not_downloadable`**: an agent-class authorization, but no `?openMode = gov:Download`.
- **pass**: otherwise.

Report (`{portal}/YYYY-MM-DD/governance_report.json`):
```json
{"portal": "nf", "snapshot": "2026-07-14", "governance_snapshot": "urn:sagebrain:gov:2026-10-06",
 "graph_version": "0.2.0", "mode": "report", "decision": "FAIL",
 "n_iris": 503512, "n_benefactors": 412,
 "failed": {"ungoverned": 498000, "not_open_to_authenticated": 1200, "not_downloadable": 0},
 "failed_sample": [{"iri": "https://www.synapse.org/Synapse:syn…", "reason": "not_open_to_authenticated",
                    "benefactor": "https://www.synapse.org/Synapse:syn…"}]}
```
The numbers above are illustrative.

## Stage 1b — PDP query (pinned contract, `src/lambda_governance/governance_query.rq`)
```sparql
PREFIX gov: <https://w3id.org/synapse/governance#>
SELECT ?resource ?benefactor ?ar WHERE {
  GRAPH <{snapshot_graph}> {
    VALUES ?resource { {resource_iris} }
    ?resource gov:benefactor ?benefactor .
    OPTIONAL { ?resource gov:parent* ?anc . ?anc gov:requiresAR ?ar } } }
```
A second query reads the caller's approvals:
```sparql
SELECT ?ar ?status ?expiresAt ?approvalVersion ?arVersion WHERE {
  GRAPH <{snapshot_graph}> {
    ?ap a gov:Approval ; gov:heldBy <https://www.synapse.org/Profile:{user_id}> ;
        gov:satisfies ?ar ; gov:status ?status .
    OPTIONAL { ?ap gov:expiresAt ?expiresAt }
    OPTIONAL { ?ap gov:requirementVersion ?approvalVersion }
    OPTIONAL { ?ar gov:versionNumber ?arVersion } } }
```
These files are the infra half of the governanceDUO contract. governanceDUO's
`tests/infra_contract/authorize_query.rq` currently mirrors the old `policy-engine` query at
`fc6de51`, and is to be re-synced (T603). Stage 2 adds the `acl:Authorization` and
`vcard:hasMember` patterns.

## Cedar schema (`policies/governance/schema.cedarschema.json`, namespace `Sage`)
```
entity AccessRequirement;                            // govid:ar/<id>
entity Team;                                         // Stage 2: synteam:<id>, "PUBLIC", "AUTHENTICATED"
entity User in [Team] {                              // synuser:<id>
  approvedRequirements: Set<AccessRequirement>,      // Approved and unexpired at decision time
};
entity Entity {                                      // one per distinct AR set among touched resources
  governed: Bool,                                    // has gov:benefactor (so it passed the ingest gate)
  accessRequirements: Set<AccessRequirement>,        // gov:parent* / gov:requiresAR
  downloadTeams?: Set<Team>,                         // Stage 2
  downloadUsers?: Set<User>,                         // Stage 2
};
action readKg appliesTo { principal: User, resource: Entity,
                          context: { purpose?: String } };   // purpose: Phase H
```

### Stage 1 policies
```cedar
// ingested_open.cedar: the ingest gate guarantees AUTHENTICATED_USERS DOWNLOAD on every governed entity
@id("ingested_open")
permit (principal, action == Sage::Action::"readKg", resource)
when { resource.governed };

// access_requirements.cedar: every AR on gov:parent* needs a current approval
@id("access_requirements")
forbid (principal, action == Sage::Action::"readKg", resource)
unless { principal.approvedRequirements.containsAll(resource.accessRequirements) };
```

### Stage 2 policy (replaces `ingested_open`)
```cedar
@id("acl_download")
permit (principal, action == Sage::Action::"readKg", resource)
when { resource has downloadTeams && resource has downloadUsers &&
       (principal in resource.downloadTeams || resource.downloadUsers.contains(principal)) };
```
In Phase H, DUO conditions become further `forbid` policies keyed on `gov:dataUseTerm` and
`context.purpose`, one per DUO code. That mirrors Policy Fabric's one-card-per-code layout.

## Graph → Cedar mapping (in the PDP)
| Cedar | Built from | Stage |
|---|---|---|
| `Sage::User::"<id>"` | the caller | 1 |
| `User.approvedRequirements` | `gov:Approval` with `heldBy synuser:<id>`, `status gov:Approved`, `expiresAt` absent or > now | 1 |
| `Sage::Entity::"<sha1(AR set)>"` | one per distinct AR set among touched resources (all governed resources share the ACL half in Stage 1) | 1 |
| `Entity.governed` | `?resource gov:benefactor ?b` exists; otherwise `ungoverned`, with no AVP call | 1 |
| `Entity.accessRequirements` | `?resource gov:parent* / gov:requiresAR ?ar` | 1 |
| User parents | `vcard:hasMember` teams ∪ {`PUBLIC`, `AUTHENTICATED`} | 2 |
| `Entity.downloadTeams` / `downloadUsers` | `acl:agentGroup` / `acl:agentClass` / `acl:agent` with `acl:mode gov:Download` on the benefactor; the entity key becomes benefactor + AR set | 2 |

## PDP request / response
```json
{"principal": "2000001", "action": "readKg", "job_id": "…",
 "resources": ["https://www.synapse.org/Synapse:syn10081783"]}
{"decision": "DENY", "reason": "policy_denied" | "ungoverned" | "authorization_unavailable",
 "unmet_requirements": ["https://w3id.org/synapse/governance/ar/42"],
 "snapshot": "urn:sagebrain:gov:2026-10-05", "graph_version": "0.2.0",
 "allowed": [], "denied": ["…"], "policy_ids": ["access_requirements"],
 "warnings": ["stale_approval:govid:approval/55001"]}
```

## Job item / SQS message changes
- `/query` SQS message: `{job_id, query, source, source_ip, user_agent, user_id}` (`user_id` is new).
- Both job tables store `user_id` on submit, for the ownership check.
- Error job item: `{status: "error", error: "governance_denied", governance: {decision, reason, denied_resources, denied_count}}`.
