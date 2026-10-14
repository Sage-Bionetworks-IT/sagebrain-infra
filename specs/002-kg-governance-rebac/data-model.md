# 002 — Data model

Synapse's governance data is relational: entities and their parent chain, benefactor ACLs, ARs and
their revisions, data-access submissions, their status and per-accessor changes. The export
(`tools/governance_export/`) does the joins once in Snowflake and writes the result as two
DynamoDB tables shaped for one decision each. The decision Lambda never joins anything.

The rules are governanceDUO's ([graph design](https://mc2-center.github.io/governanceDUO/graph-design/#governance-graph-design)):
effective access = ACL AND every inherited AR satisfied; inheritance is walked; unknown fails closed.
Only the storage differs (spec D-5).

## Tables (`app-{env}-governance`)
Both: on-demand billing, a dedicated KMS key, point-in-time recovery off (a full re-export
rebuilds them), and read access for the decision Lambda's role only.

### `app-{env}-governance-entities`
One item per Synapse entity in the export scope.

| Attribute | Type | Meaning |
|---|---|---|
| `entity_id` (PK) | S | `syn10081783` |
| `benefactor_id` | S | the entity whose ACL applies |
| `open_to_authenticated` | BOOL | the benefactor's ACL grants `DOWNLOAD` to AUTHENTICATED_USERS (273948) or PUBLIC (273949) |
| `ar_ids` | SS (absent if none) | every AR whose subject is the entity **or any ancestor** (parent path, not benefactor chain; spec FR-3) |
| `export_id` | S | the export that wrote it (`2026-10-14T18:02:11Z`) |

### `app-{env}-governance-grants`
One item per (user, managed ACT AR) that has ever had an accessor change on an Approved submission.

| Attribute | Type | Meaning |
|---|---|---|
| `user_id` (PK) | S | Synapse principal id (`2000001`) |
| `ar_id` (SK) | S | `42` |
| `granted` | BOOL | the latest accessor change is `GRANT_ACCESS` or `RENEW_ACCESS` (true) or `REVOKE_ACCESS` (false) |
| `expires_at` | S (ISO-8601, absent if none) | review time of that submission + the AR's expiration period |
| `submission_id` | S | the submission that decided it, for audit |
| `export_id` | S | as above |

The decision Lambda reads one `Query` on `user_id` per caller, and counts an AR as held when
`granted` and (`expires_at` absent or > decision time).

## Snowflake sources (`SYNAPSE_DATA_WAREHOUSE.RDS_RAW`; to confirm, research Q-10)
| Fact | Source |
|---|---|
| Parent chain, benefactor | node table (`parent_id`, `benefactor_id`) |
| Benefactor ACL | ACL + resource-access tables (principal 273948 / 273949, access type `DOWNLOAD`) |
| AR subjects | subject-access-requirement mapping (AR id → entity id) |
| AR type, expiration period | `access_requirement` (`concrete_type`), `access_requirement_revision` (`serialized_entity`, current `number`) |
| Grants | `data_access_submission` (AR id, review state via `data_access_submission_status`), `data_access_submission_accessor_changes` (`accessor_id`, `access_type`) |

### Grant derivation (spec FR-4)
```sql
-- one row per (accessor, AR) from Approved managed-ACT submissions; latest review wins
WITH approved AS (
  SELECT s.id AS submission_id, s.access_requirement_id AS ar_id,
         TO_TIMESTAMP(st.modified_on / 1000) AS reviewed_on
  FROM RDS_RAW.data_access_submission s
  JOIN RDS_RAW.data_access_submission_status st ON st.submission_id = s.id
  JOIN RDS_RAW.access_requirement ar ON ar.id = s.access_requirement_id
  WHERE st.state = 'APPROVED'
    AND ar.concrete_type = 'org.sagebionetworks.repo.model.ManagedACTAccessRequirement'
)
SELECT c.accessor_id AS user_id, a.ar_id, a.submission_id, a.reviewed_on, c.access_type
FROM approved a
JOIN RDS_RAW.data_access_submission_accessor_changes c ON c.submission_id = a.submission_id
QUALIFY ROW_NUMBER() OVER (PARTITION BY c.accessor_id, a.ar_id
                           ORDER BY a.reviewed_on DESC, a.submission_id DESC) = 1;
```
`granted = access_type IN ('GRANT_ACCESS', 'RENEW_ACCESS')`. `expires_at` adds the AR revision's
`expirationPeriod` (ms) to `reviewed_on`. Column and state spellings are confirmed in Phase B.

## Cedar schema (`policies/governance/schema.cedarschema.json`, namespace `Sage`)
```
entity AccessRequirement;                          // AR id
entity User {                                      // Synapse principal id
  approvedRequirements: Set<AccessRequirement>,    // granted and unexpired at decision time
};
entity Entity {                                    // one per distinct (openToAuthenticated, AR set)
  openToAuthenticated: Bool,
  accessRequirements: Set<AccessRequirement>,
};
action readKg appliesTo { principal: User, resource: Entity,
                          context: { purpose?: String } };   // purpose: later (research R-6)
```

### Stage 1 policies
```cedar
// acl_open.cedar: the ACL half, coarse: open to every registered Synapse user
@id("acl_open")
permit (principal, action == Sage::Action::"readKg", resource)
when { resource.openToAuthenticated };

// access_requirements.cedar: every inherited AR needs a current grant
@id("access_requirements")
forbid (principal, action == Sage::Action::"readKg", resource)
unless { principal.approvedRequirements.containsAll(resource.accessRequirements) };
```

### Stage 2 policy (replaces `acl_open`)
```cedar
@id("acl_download")
permit (principal, action == Sage::Action::"readKg", resource)
when { principal in resource.downloadTeams || resource.downloadUsers.contains(principal) };
```
Stage 2 adds `User in [Team]`, `Entity.downloadTeams` and `Entity.downloadUsers`, and a rosters
table or a graph store (spec D-5).

## Table → Cedar mapping (in the decision Lambda)
| Cedar | Built from |
|---|---|
| `Sage::User::"<id>"` | the job's `user_id` |
| `User.approvedRequirements` | grants items for the user with `granted` and `expires_at` absent or > now |
| `Sage::Entity::"<sha1(open, AR set)>"` | one per distinct (open, AR set) among touched resources, so a query over thousands of files is a handful of requests |
| `Entity.openToAuthenticated` | entities item `open_to_authenticated` |
| `Entity.accessRequirements` | entities item `ar_ids` |
| (no AVP call) | a touched resource with no entities item → `ungoverned` |

## Decision Lambda request / response
```json
{"principal": "2000001", "action": "readKg", "job_id": "…",
 "resources": ["https://www.synapse.org/Synapse:syn10081783"]}
{"decision": "DENY", "reason": "policy_denied" | "ungoverned" | "authorization_unavailable",
 "unmet_requirements": ["42"], "export_id": "2026-10-14T18:02:11Z",
 "allowed": [], "denied": ["https://www.synapse.org/Synapse:syn10081783"],
 "policy_ids": ["access_requirements"]}
```

## Worked example as table items (`tests/fixtures/governance/`)
From governanceDUO's `governance_graph.ttl` at a pinned commit (`PINNED_COMMIT`), with both
benefactors made open to AUTHENTICATED_USERS (the as-published file's only grant is team 9000001):
```json
{"entities": [
  {"entity_id": "syn2343195",  "benefactor_id": "syn2343195",  "open_to_authenticated": true, "ar_ids": ["42"]},
  {"entity_id": "syn10081783", "benefactor_id": "syn10081783", "open_to_authenticated": true, "ar_ids": ["42"]}],
 "grants": [
  {"user_id": "2000001", "ar_id": "42", "granted": true, "expires_at": "2026-08-14T19:33:20Z", "submission_id": "55001"}]}
```
`syn10081783` inherits AR 42 from its parent study, the case FR-3 is about. A second fixture,
`as_published.json`, sets `open_to_authenticated: false` on both (spec FR-13, ACL row).

## Job item / SQS message changes
- No new fields: `user_id` is already on the `/query` message and both job items (spec 001 D-19).
- Error job item: `{status: "error", error: "governance_denied", governance: {decision, reason, denied_resources, denied_count, unmet_requirements}}`.
