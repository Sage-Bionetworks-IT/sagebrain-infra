# Governance graph + ReBAC concept

This concept extends the Neptune governance graph approach with relationship-based access control (ReBAC) decisions in Amazon Verified Permissions (AVP), following the Neptune + AVP pattern from AWS.

## Goal

Authorize a request using both:

1. **Governance provenance from Neptune** (which grant/access requirement governs a resource), and
2. **Policy decisions from AVP** (whether the principal can perform the action on that governed resource relationship).

This keeps policy evaluation explicit and auditable while preserving graph-native provenance and lineage.

## Example governance triples (from the concept prompt)

```turtle
@prefix gov: <https://sagebionetworks.org/governance/> .
@prefix syn: <https://www.synapse.org/Synapse:> .

gov:grant-001 a gov:AccessGrant ;
    gov:bindingType gov:Direct ;
    gov:createdOn "1755100000000"^^<http://www.w3.org/2001/XMLSchema#long> ;
    gov:permission gov:DOWNLOAD ;
    gov:principal gov:principal-9000001 ;
    gov:resource syn:syn10081783 ;
    gov:source gov:Synapse ;
    gov:sourceAclId 42001 ;
    gov:sourceAclResourceAccessId 42101 .

syn:syn10081783 a gov:SynapseEntity ;
    gov:createdByUserId 1000001 ;
    gov:createdOn "1755000000000"^^<http://www.w3.org/2001/XMLSchema#long> ;
    gov:etag "3f9c9b8a-1a2b-4c3d-9e5f-6a7b8c9d0e1f" ;
    gov:hasACL gov:grant-001 ;
    gov:hasAccessRequirement gov:AR-42 ;
    gov:name "HS01_CUDC907_Run1_S2_R1_001.fastq" ;
    gov:nodeType "file" ;
    gov:parentId syn:syn2343195 .
```

## Concept API flow

`POST /authorize` (Lambda: `src/lambda_rebac/authorize.py`) performs:

1. Query Neptune for `gov:hasACL` + `gov:AccessGrant` + `gov:hasAccessRequirement` for the resource.
2. Keep only grants matching `{principal_id, action}`.
3. Call `verifiedpermissions:IsAuthorized` once per matching grant.
4. Merge direct/inferred decisions:
   - `intersection`: `direct_allow AND all_inferred_allow`
   - `union`: `any_allow`
5. Return `ALLOW`/`DENY` with evaluated grants and determining policy IDs.
6. If Neptune/AVP errors, return `503` with `decision=DENY` (`authorization_unavailable`) to fail closed.

## Why this matches the governance design

- **Provenance-aware**: authorization starts with governance edges from Neptune, not detached app-side ACL lookups.
- **Auditable**: response includes governing grants and determining AVP policy IDs.
- **Supports inferred-edge semantics**: explicit merge mode (`intersection` vs `union`) is configurable.
- **Fail-closed**: transient policy/graph outages deny authorization rather than silently allowing.

## Example AVP schema + policy sketch

```cedar
entity User;
entity SynapseEntity in [AccessGrant] {
  accessRequirements: Set<String>
};
entity AccessGrant {
  permission: String,
  bindingType: String,
  principal: String
};
action DOWNLOAD appliesTo {
  principal: User,
  resource: SynapseEntity
};

permit (
  principal,
  action == Action::"DOWNLOAD",
  resource in AccessGrant
)
when {
  resource.accessRequirements.contains("https://sagebionetworks.org/governance/AR-42")
};
```

Use this as a starting point; production policies should encode Synapse-specific requirements and principal relationships (team membership, ACT approval, etc.).

## Import-ready AVP artifacts

For direct import into Amazon Verified Permissions, use:

- Schema definition JSON: [rebac_concept_schema.json](./avp/rebac_concept_schema.json)
- Sample policy statement: [rebac_concept_policy.cedar](./avp/rebac_concept_policy.cedar)
- `create-policy` CLI payload: [rebac_concept_policy_import.json](./avp/rebac_concept_policy_import.json)

Create a policy store with the schema:

```bash
aws --profile sagebrain verifiedpermissions create-policy-store \
  --validation-settings mode=STRICT \
  --schema-definition file://docs/avp/rebac_concept_schema.json
```

Then import the sample policy:

```bash
aws --profile sagebrain verifiedpermissions create-policy \
  --policy-store-id <POLICY_STORE_ID> \
  --definition file://docs/avp/rebac_concept_policy_import.json
```

## Stack and config

- Stack: `src/neptune_rebac_concept_stack.py`
- Lambda: `src/lambda_rebac/authorize.py`
- Config block: `NEPTUNE_REBAC_CONCEPT` in `config/base.yaml`

Required config:

```yaml
NEPTUNE_REBAC_CONCEPT:
  enabled: true
  policy_store_id: "ps-xxxxxxxxxxxxxxxx"
```
