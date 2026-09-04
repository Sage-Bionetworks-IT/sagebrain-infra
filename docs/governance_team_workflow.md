# Governance Team Workflow: How Policies Are Actually Managed

## The Confusion

**Question:** "How does our governance team codify governance with Policy Engine, ARs, and ACLs?"

**Answer:** The Policy Engine **doesn't create policies** - it **evaluates** them. Your governance team continues to manage policies in **Synapse** (the source of truth), and they get synced to Neptune for enforcement.

---

## Separation of Concerns

```
┌─────────────────────────────────────────────────────────┐
│                  GOVERNANCE LAYER                        │
│  (Where governance team MANAGES policies)                │
│                                                          │
│  Synapse Platform:                                       │
│  - Create/edit Access Requirements                       │
│  - Set DUO terms on datasets                            │
│  - Grant ACLs to users/teams                            │
│  - Manage DAC workflows                                  │
└─────────────────┬───────────────────────────────────────┘
                  │
                  │ Sync (daily/hourly)
                  ↓
┌─────────────────────────────────────────────────────────┐
│                ENFORCEMENT LAYER                         │
│  (Where policies are EVALUATED)                          │
│                                                          │
│  Neptune + Policy Engine:                                │
│  - Governance triples (read-only copy)                   │
│  - Policy Engine evaluates evidence                      │
│  - Asset Guardian enforces at query time                 │
└─────────────────────────────────────────────────────────┘
```

**Key:** Governance team doesn't touch Neptune directly. They use **Synapse** (existing tools).

---

## Governance Team Workflow: Before vs After

### Current Workflow (in Synapse)

Your governance team already does this in Synapse today:

#### 1. Create Access Requirement for a Dataset
```
Synapse Web UI:
1. Go to dataset syn123456
2. Settings → Access Requirements
3. Create new requirement:
   - Type: Managed Access
   - Name: "Alzheimer's Disease Training Requirement"
   - Instructions: "Complete CITI training..."
   - Approvers: DAC members
4. Save
```

**Behind the scenes in Synapse DB:**
```sql
INSERT INTO access_requirement (id, type, name)
VALUES ('AR001', 'MANAGED_ACCESS', 'Alzheimer Training');

INSERT INTO node_access_requirement (node_id, ar_id)
VALUES ('syn123456', 'AR001');
```

#### 2. Set DUO Terms on Dataset
```
Synapse Web UI:
1. Go to dataset syn123456
2. Annotations → Add:
   - Key: duo_term
   - Value: DUO:0000007 (disease-specific research)
3. Annotations → Add:
   - Key: disease_context
   - Value: MONDO:0004975 (Alzheimer's)
4. Save
```

**Behind the scenes:**
```sql
INSERT INTO node_annotation (node_id, key, value)
VALUES ('syn123456', 'duo_term', 'DUO:0000007'),
       ('syn123456', 'disease_context', 'MONDO:0004975');
```

#### 3. Approve User's Access Request
```
Synapse DAC Portal:
1. View pending requests
2. Review user's research plan
3. Verify training completion
4. Click "Approve"
```

**Behind the scenes:**
```sql
INSERT INTO access_approval (user_id, ar_id, approved_by)
VALUES ('9000001', 'AR001', 'dac_member_123');
```

---

### NEW: What Changes with Policy Engine?

**Answer: NOTHING for the governance team!**

They continue using Synapse exactly as before. The difference is what happens **after** they set policies:

```
┌──────────────────────────────────────────────────────────┐
│ 1. GOVERNANCE TEAM CREATES POLICY (in Synapse)           │
│    - Set DUO:0000007 on syn123456                        │
│    - Create AR001 requirement                            │
│    - Approve user 9000001                                │
└────────────────────┬─────────────────────────────────────┘
                     │
                     │ Synapse APIs
                     ↓
┌──────────────────────────────────────────────────────────┐
│ 2. SYNC TO NEPTUNE (automated pipeline)                  │
│    - Read from Synapse REST API                          │
│    - Transform to RDF triples                            │
│    - Load to Neptune governance graph                    │
└────────────────────┬─────────────────────────────────────┘
                     │
                     │ Governance triples in Neptune
                     ↓
┌──────────────────────────────────────────────────────────┐
│ 3. POLICY ENGINE EVALUATES (at runtime)                  │
│    - User requests capability                            │
│    - Policy Engine queries Neptune triples               │
│    - Evaluates: DUO match? AR satisfied?                 │
│    - Issues capability token if authorized               │
└────────────────────┬─────────────────────────────────────┘
                     │
                     │ Capability token
                     ↓
┌──────────────────────────────────────────────────────────┐
│ 4. ASSET GUARDIAN ENFORCES (at query time)               │
│    - Validates token                                     │
│    - Rewrites query with governance filters              │
│    - Neptune enforces at data layer                      │
└──────────────────────────────────────────────────────────┘
```

**Governance team only touches step 1. Everything else is automated.**

---

## The Sync Pipeline: Synapse → Neptune

### What Needs to Be Synced?

#### From Synapse REST API → Neptune Triples

**1. Access Requirements**
```python
# Synapse API call
ar = syn.restGET(f"/entity/{syn_id}/accessRequirement")

# Becomes Neptune triple
<https://synapse.org/syn123456> gov:hasAccessRequirement <AR001> .
<AR001> a gov:AccessRequirement ;
        rdfs:label "Alzheimer's Training" ;
        gov:requiresTraining "CITI_Alzheimer_Module" .
```

**2. DUO Terms (from annotations)**
```python
# Synapse API call
annotations = syn.getAnnotations(syn_id)
duo_term = annotations['duo_term']

# Becomes Neptune triple
<https://synapse.org/syn123456> gov:hasDataUseCondition <http://purl.obolibrary.org/obo/DUO_0000007> .
<https://synapse.org/syn123456> gov:diseaseContext <http://purl.obolibrary.org/obo/MONDO_0004975> .
```

**3. User Approvals**
```python
# Synapse API call
approvals = syn.restGET(f"/user/{user_id}/accessApproval")

# Becomes Neptune triple (stored in user's "passport")
<user:9000001> gov:hasApproval <AR001> ;
               gov:approvedBy <dac_member_123> ;
               gov:approvedDate "2026-09-01"^^xsd:date .
```

**4. ACLs**
```python
# Synapse API call
acl = syn.restGET(f"/entity/{syn_id}/acl")

# Becomes Neptune triple
<https://synapse.org/syn123456> gov:hasACL <grant_1> .
<grant_1> gov:principal <user:9000001> ;
          gov:permission "DOWNLOAD" ;
          gov:bindingType "Direct" .
```

### Sync Pipeline Architecture

```python
# Pseudocode for sync pipeline
# This runs as a scheduled job (e.g., hourly)

import synapseclient
import rdflib

syn = synapseclient.login()
graph = rdflib.Graph()

# 1. Get all datasets with governance metadata
datasets = syn.restGET("/entity/query?type=DATASET&hasAnnotation=duo_term")

for dataset in datasets:
    syn_id = dataset['id']

    # 2. Get access requirements
    ars = syn.restGET(f"/entity/{syn_id}/accessRequirement")
    for ar in ars:
        graph.add((
            URIRef(f"https://synapse.org/{syn_id}"),
            gov.hasAccessRequirement,
            URIRef(f"AR:{ar['id']}")
        ))

    # 3. Get DUO annotations
    annotations = syn.getAnnotations(syn_id)
    if 'duo_term' in annotations:
        graph.add((
            URIRef(f"https://synapse.org/{syn_id}"),
            gov.hasDataUseCondition,
            URIRef(annotations['duo_term'][0])
        ))

    # 4. Get ACLs
    acl = syn.restGET(f"/entity/{syn_id}/acl")
    for entry in acl['resourceAccess']:
        grant_uri = URIRef(f"grant:{syn_id}:{entry['principalId']}")
        graph.add((
            URIRef(f"https://synapse.org/{syn_id}"),
            gov.hasACL,
            grant_uri
        ))
        graph.add((
            grant_uri,
            gov.principal,
            URIRef(f"user:{entry['principalId']}")
        ))

# 5. Load to Neptune
upload_to_neptune(graph)
```

---

## What the Governance Team DOESN'T Do

❌ **They don't write RDF triples**
❌ **They don't touch Neptune**
❌ **They don't configure the Policy Engine**
❌ **They don't write SPARQL queries**

✅ **They DO what they do today: manage policies in Synapse**

---

## Example: Governance Team Creates New Restricted Dataset

### Step-by-Step: Governance Team Perspective

**Scenario:** New Parkinson's disease dataset needs governance.

#### Step 1: Upload Data to Synapse (Governance Admin)
```
Synapse Web UI:
1. Create new folder: "Parkinson's MRI Study 2026"
2. Upload 50,000 DICOM files
3. Set as Dataset entity type
4. Synapse ID assigned: syn987654
```

#### Step 2: Set DUO Terms (Governance Admin)
```
Synapse Web UI → syn987654 → Annotations:
- duo_term: DUO:0000007 (disease-specific research)
- disease_context: MONDO:0005180 (Parkinson's disease)
- data_type: imaging
- consent_code: DS-PD
```

#### Step 3: Create Access Requirement (Governance Admin)
```
Synapse Web UI → syn987654 → Settings → Access Requirements:
- Type: Managed Access
- Name: "Parkinson's Imaging Access"
- Required training: "HIPAA + Parkinson's Ethics Module"
- DAC: Parkinson's Data Access Committee
- Terms: "Data must be destroyed within 2 years"
```

#### Step 4: Governance Metadata Syncs to Neptune (Automated)
```
# Hourly job runs, pulls from Synapse API:

PREFIX gov: <https://sagebionetworks.org/governance/>
PREFIX duo: <http://purl.obolibrary.org/obo/>
PREFIX mondo: <http://purl.obolibrary.org/obo/>

INSERT DATA {
  <https://synapse.org/syn987654> a gov:Dataset ;
    rdfs:label "Parkinson's MRI Study 2026" ;
    gov:hasDataUseCondition duo:DUO_0000007 ;
    gov:diseaseContext mondo:MONDO_0005180 ;
    gov:hasAccessRequirement <AR:PD_IMAGING_2026> .

  <AR:PD_IMAGING_2026> a gov:AccessRequirement ;
    rdfs:label "Parkinson's Imaging Access" ;
    gov:requiresTraining "HIPAA_TRAINING" ;
    gov:requiresTraining "PD_ETHICS_MODULE" .
}
```

**Governance admin never saw this RDF - it was generated automatically.**

#### Step 5: Researcher Requests Access (DAC Portal in Synapse)
```
Researcher:
1. Logs into Synapse
2. Finds syn987654
3. Clicks "Request Access"
4. Fills out form:
   - Research purpose: "Drug target discovery for PD"
   - IRB approval: [uploads document]
   - Training certificates: [uploads HIPAA + PD Ethics]
5. Submits to DAC
```

#### Step 6: DAC Reviews & Approves (Governance Admin)
```
Synapse DAC Portal:
1. View pending request from user 9000002
2. Verify training certificates ✓
3. Review research plan ✓
4. Check IRB approval ✓
5. Click "Approve" → User granted AR:PD_IMAGING_2026
```

#### Step 7: Approval Syncs to Neptune (Automated)
```
# Next sync job updates Neptune:

INSERT DATA {
  <user:9000002> gov:hasApproval <AR:PD_IMAGING_2026> ;
    gov:approvedBy <dac_member_456> ;
    gov:approvedDate "2026-09-04"^^xsd:date .
}
```

---

## Now: User Queries with Policy Engine

### Step 8: User Requests Capability Token
```
User runs in Python client:
```python
capability = syn.request_capability(
    research_purpose="DUO:0000007",
    disease="MONDO:0005180"  # Parkinson's
)
```

**Behind the scenes:**

```
POST /policy/issue-capability

Policy Engine:
1. Queries Neptune: "What datasets require DUO:0000007 + MONDO:0005180?"
   → Returns: syn987654 (and other PD datasets)

2. Queries Neptune: "What ARs does syn987654 require?"
   → Returns: AR:PD_IMAGING_2026

3. Queries Neptune: "Does user 9000002 have AR:PD_IMAGING_2026?"
   → Returns: Yes, approved 2026-09-04

4. Evaluation: ✓ All conditions met
5. Issues capability token for "disease_specific:parkinsons"
```

**Response:**
```json
{
  "capability_token": "eyJhbGci...",
  "authorized_policies": ["disease_specific:parkinsons"]
}
```

### Step 9: User Queries Data
```python
results = syn.query_neptune(
    "SELECT ?file WHERE { ?file a :File }",
    capability=capability
)
```

**Asset Guardian rewrites query:**
```sparql
SELECT ?file WHERE {
  ?file a :File .
  # INJECTED FROM CAPABILITY
  ?file gov:hasDataUseCondition duo:DUO_0000007 ;
        gov:diseaseContext mondo:MONDO_0005180 .
}
```

**Neptune returns only Parkinson's files that user has access to.**

---

## Governance Team's Mental Model

### What They Manage (in Synapse)
1. **Datasets** - Upload data, set metadata
2. **DUO Terms** - Annotate datasets with research use restrictions
3. **Access Requirements** - Create training/approval requirements
4. **DAC Workflows** - Review and approve access requests
5. **ACLs** - Grant/revoke individual file permissions

### What Happens Automatically
1. **Sync** - Synapse → Neptune (hourly)
2. **Policy Evaluation** - Policy Engine reads Neptune triples
3. **Query Enforcement** - Asset Guardian rewrites queries
4. **Data Filtering** - Neptune filters at query time

### What Governance Team Sees
- **Synapse UI** - Same as today
- **Audit logs** - CloudWatch logs (who queried what)
- **Capability tokens** - Optional: see who has active tokens

---

## Summary: Governance Team Workflow

```
┌─────────────────────────────────────────────────────────┐
│              GOVERNANCE TEAM'S WORLD                     │
│                  (Synapse Platform)                      │
│                                                          │
│  ✏️  Create datasets                                     │
│  ✏️  Set DUO terms                                       │
│  ✏️  Create Access Requirements                          │
│  ✏️  Review access requests (DAC)                        │
│  ✏️  Approve/deny users                                  │
│  ✏️  Manage ACLs                                         │
│                                                          │
│  Tools: Synapse Web UI, DAC Portal, REST API            │
│  Same workflow as today!                                 │
└─────────────────────────────────────────────────────────┘
                       │
                       │ (Automated sync pipeline)
                       │ They never see this!
                       ↓
┌─────────────────────────────────────────────────────────┐
│           ENFORCEMENT ENGINE'S WORLD                     │
│         (Neptune + Policy Engine + Asset Guardian)       │
│                                                          │
│  🤖 Read governance triples                              │
│  🤖 Evaluate evidence                                    │
│  🤖 Issue capability tokens                              │
│  🤖 Rewrite queries                                      │
│  🤖 Filter data                                          │
│                                                          │
│  Fully automated!                                        │
└─────────────────────────────────────────────────────────┘
```

**Key Insight:** Governance team uses familiar Synapse tools. Policy Engine is invisible infrastructure that enforces their decisions at scale.

---

## What Governance Team DOES Need to Understand

1. **DUO Vocabulary** - Use standardized ontology terms
   - Already using this today in Synapse annotations

2. **Policy Consistency** - Policies should be machine-readable
   - Don't write "must be Alzheimer's researcher" in free text
   - Use `duo_term: DUO:0000007` + `disease: MONDO:0004975`

3. **Approval Workflow** - DAC approvals grant access to ARs
   - Same as today

4. **Audit Logs** - Where to look for usage analytics
   - CloudWatch logs instead of Synapse download logs

---

## FAQ

**Q: Do we need to retrain governance admins?**
A: No. They continue using Synapse. IT team handles Neptune sync.

**Q: What if a governance admin makes a mistake in Synapse?**
A: Fix it in Synapse. Next sync will update Neptune automatically.

**Q: How often does Neptune sync from Synapse?**
A: Configurable (e.g., hourly). Near-real-time for critical updates.

**Q: Can governance admins see the RDF triples?**
A: Optional - expose read-only Neptune query UI for transparency.

**Q: What if Synapse and Neptune get out of sync?**
A: Synapse is source of truth. Re-run sync pipeline. Neptune is read-only copy.

**Q: Do we need to migrate existing policies?**
A: No. One-time sync pulls all existing Synapse policies into Neptune.

---

## Next Steps for Governance Team

**Nothing!** They continue managing policies in Synapse as they do today.

**For IT team:**
1. Build Synapse → Neptune sync pipeline
2. Schedule sync job (e.g., hourly cron)
3. Monitor sync logs
4. Expose audit logs to governance team
