# Policy Engine Scaling: How It Works at 400K+ Nodes

## The Problem

**How do we authorize access to 400,000+ files without checking each one individually?**

## The Solution: Policy-Level Capabilities

Instead of node-by-node authorization, we authorize based on **data use policies**.

---

## Example: Researcher Accessing Alzheimer's Data

### Step 1: User Submits Evidence (ONCE, not per-node)

```http
POST /policy/issue-capability

{
  "user_id": "9000001",
  "evidence": {
    "research_purpose": "DUO:0000007",  // disease-specific research
    "disease": "MONDO:0004975",         // Alzheimer's disease
    "approved_access_requirements": ["AR_ALZHEIMERS_TRAINING"],
    "institution": "Stanford University"
  }
}
```

**Key:** User is NOT listing 400K file IDs. They're stating their **research intent**.

---

### Step 2: Policy Engine Evaluates Evidence

Policy Engine queries Neptune for **policy templates**, not individual files:

```sparql
PREFIX gov: <https://sagebionetworks.org/governance/>
PREFIX duo: <http://purl.obolibrary.org/obo/>

SELECT ?policyClass ?duoTerm ?requiredAR WHERE {
  # Query for POLICY CLASSES, not individual files
  ?policyClass a gov:DataUsePolicy ;
               gov:requiredResearchPurpose ?duoTerm ;
               gov:requiredAccessRequirement ?requiredAR .

  FILTER(?duoTerm = duo:DUO0000007)  # disease-specific
}
```

**Returns ~10 policy templates**, not 400K nodes.

Policy Engine logic:
```python
# User evidence
user_has_disease = "MONDO:0004975"  # Alzheimer's
user_has_training = "AR_ALZHEIMERS_TRAINING"

# Policy requires
policy_requires_disease_specific = True
policy_requires_training = ["AR_ALZHEIMERS_TRAINING"]

# Match
if user_has_disease == alzheimers AND user_has_training in policy_requires_training:
    grant_capability = "disease_specific:alzheimers"
```

---

### Step 3: Issue Capability Token

```json
{
  "capability_token": "eyJhbGciOiJIUzI1NiIs...",
  "payload": {
    "sub": "9000001",
    "aud": "neptune-query-api",
    "exp": 1725372000,
    "authorized_policies": [
      {
        "type": "disease_specific",
        "disease": "MONDO:0004975",
        "duo_term": "DUO:0000007"
      }
    ]
  }
}
```

**This ONE token covers ALL files matching the policy.**

---

### Step 4: User Queries Data (with Capability Token)

```http
POST /query
Authorization: Bearer eyJhbGciOiJIUzI1NiIs...

{
  "query": "SELECT ?file ?name WHERE { ?file a :File ; :name ?name }"
}
```

---

### Step 5: Asset Guardian Rewrites Query

Asset Guardian decodes the capability token and sees:
- User authorized for: `disease_specific:alzheimers`
- DUO term: `DUO:0000007`

It **rewrites the query** to inject the authorization filter:

```sparql
SELECT ?file ?name WHERE {
  ?file a :File ;
        :name ?name .

  # INJECTED BY ASSET GUARDIAN (from capability token)
  ?file gov:hasDataUseCondition duo:DUO0000007 ;
        gov:diseaseContext mondo:MONDO0004975 .
}
```

---

### Step 6: Neptune Filters 400K Nodes

Neptune executes this query and:
- Checks ALL 400K files
- Returns ONLY files matching `DUO:0000007` (disease-specific) AND `MONDO:0004975` (Alzheimer's)
- Maybe 50K files match

**No per-node authorization check needed** - Neptune's query planner does the filtering.

---

## Scaling Analysis

### Without Policy Engine (Current Post-Filter)

| User Query Returns | Authorization Checks | Latency |
|-------------------|---------------------|---------|
| 100 files | 100 | +100ms |
| 10K files | 10K | +5s |
| 100K files | 100K | ❌ Timeout |

### With Policy Engine + Query Rewrite

| User Query Could Return | Policy Evaluation | Query Rewrite | Neptune Filtering | Latency |
|------------------------|------------------|---------------|-------------------|---------|
| 100 files | 1 (upfront) | +10ms | Native | +50ms |
| 10K files | 1 (upfront) | +10ms | Native | +200ms |
| 400K files | 1 (upfront) | +10ms | Native | +500ms |

**Key:** Authorization is O(1) - evaluated once at capability issuance, not per-query or per-node.

---

## Real-World Example: Neurofibromatosis Portal

### Governance Setup (One-Time)

```turtle
# Policy template for NF disease-specific data
:NF_DiseaseSpecificPolicy a gov:DataUsePolicy ;
    gov:appliesToDataset :NF_Portal ;
    gov:requiredResearchPurpose duo:DUO0000007 ;  # disease-specific
    gov:diseaseContext mondo:MONDO0005901 ;        # neurofibromatosis
    gov:requiredAccessRequirement :AR_NF_TRAINING .

# Apply policy to 50,000 files (via portal relationship)
:file_nf_001 gov:governedBy :NF_DiseaseSpecificPolicy .
:file_nf_002 gov:governedBy :NF_DiseaseSpecificPolicy .
# ... 50,000 files ...
```

**Alternative (more efficient):**
```turtle
# Don't tag every file individually - tag at portal level
:NF_Portal gov:defaultPolicy :NF_DiseaseSpecificPolicy .

# Files inherit policy from portal
:file_nf_001 gov:inPortal :NF_Portal .
:file_nf_002 gov:inPortal :NF_Portal .
```

---

### User Workflow

1. **User requests capability** (once, at login or project start):
   ```json
   {
     "research_purpose": "DUO:0000007",
     "disease": "MONDO:0005901",
     "approved_ars": ["AR_NF_TRAINING"]
   }
   ```

2. **Policy Engine grants capability** (covers all 50K NF files):
   ```json
   {
     "authorized_policies": ["NF_DiseaseSpecificPolicy"]
   }
   ```

3. **User runs ANY query** (capability token auto-filters):
   ```sparql
   # User query
   SELECT ?file WHERE { ?file a :File }

   # Rewritten by Asset Guardian
   SELECT ?file WHERE {
     ?file a :File ;
           gov:governedBy :NF_DiseaseSpecificPolicy .
   }
   ```

4. **Neptune returns only authorized files** (out of 50K)

---

## Edge Case: Mixed-Policy Queries

### Scenario: User Authorized for Alzheimer's, Queries Alzheimer's + Parkinson's Data

**User capability:**
```json
{
  "authorized_policies": ["AlzheimersDiseaseSpecific"]
}
```

**User query:**
```sparql
SELECT ?file WHERE {
  VALUES ?disease { mondo:MONDO0004975 mondo:MONDO0005180 }
  ?file gov:diseaseContext ?disease .
}
```

**Asset Guardian rewrites:**
```sparql
SELECT ?file WHERE {
  VALUES ?disease { mondo:MONDO0004975 mondo:MONDO0005180 }
  ?file gov:diseaseContext ?disease .

  # INJECTED: Only return files user can access
  ?file gov:governedBy :AlzheimersDiseaseSpecific .
}
```

**Result:** Returns Alzheimer's files only (Parkinson's files filtered out).

---

## When Do You Check Individual Nodes?

### Rare Case: Ad-Hoc Access Grants

Sometimes a user has access to specific files outside a policy:

```json
{
  "authorized_policies": ["AlzheimersDiseaseSpecific"],
  "explicit_grants": ["syn12345", "syn67890"]  // PI granted access to these 2 files
}
```

Asset Guardian rewrites:
```sparql
SELECT ?file WHERE {
  ?file a :File .

  # User can access via policy OR explicit grant
  {
    ?file gov:governedBy :AlzheimersDiseaseSpecific .
  } UNION {
    VALUES ?file { <syn12345> <syn67890> }
  }
}
```

**This still scales** - explicit grants are rare (dozens, not thousands).

---

## Summary: How Policy Engine Scales

| Component | Input Size | Output Size | Complexity |
|-----------|-----------|-------------|------------|
| **Policy Engine** | ~10 evidence claims | 1 capability token | O(policies) ≈ O(10) |
| **Asset Guardian** | 1 SPARQL query | 1 rewritten query | O(1) |
| **Neptune** | 400K nodes | Filtered results | O(n) but native |

**Total:** O(1) authorization overhead + O(n) Neptune filtering (which happens anyway).

---

## Migration Path for Sage

### Phase 1: Post-Filter (Current)
- Check 400K nodes AFTER query
- ❌ Doesn't scale

### Phase 2: Policy Engine + Query Rewrite (Next)
- Evaluate policies upfront
- Issue capability tokens
- Rewrite queries with filters
- ✅ Scales to millions

### Phase 3: Federated (Future)
- Capability tokens work across Synapse, Flower, HuggingFace
- One token, many systems
- ✅ Scales across ecosystem

---

## Key Takeaway

**You don't authorize 400K nodes individually.**

You authorize based on **data use policies** (disease-specific, research purpose, etc.), then Neptune's query engine filters the nodes for you.

The Policy Engine operates at the **policy level** (10s of policies), not the **node level** (400K files).
