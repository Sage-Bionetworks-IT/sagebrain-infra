# Policy Engine as Source of Truth: The Full Vision

## The Evolution

### Phase 1 (Current): Synapse as Source of Truth
```
Synapse (policies) → Neptune (read-only copy) → Policy Engine (evaluates)
```
- Governance team manages in Synapse
- Policy Engine reads from Neptune
- Synapse controls authorization

### Phase 2 (Transition): Dual Source of Truth
```
Synapse (policies) ←→ Policy Engine (central) → Neptune
```
- Policies can be created in Synapse OR Policy Engine
- Bi-directional sync
- Both can authorize

### Phase 3 (Target): Policy Engine as Source of Truth
```
Policy Engine (policies) → Synapse (consumes tokens)
                        → Flower (consumes tokens)
                        → HuggingFace (consumes tokens)
                        → Neptune (consumes tokens)
```
- **Policy Engine is the single source of truth**
- ALL services defer to Policy Engine for authorization
- Synapse becomes a capability token consumer

---

## Architecture: Policy Engine as Central Authority

```
┌─────────────────────────────────────────────────────────────┐
│              GOVERNANCE ADMIN INTERFACE                      │
│         (Web UI for creating/managing policies)              │
│                                                              │
│  ✏️  Create policies (DUO-based rules)                       │
│  ✏️  Define Access Requirements                              │
│  ✏️  Configure DAC workflows                                 │
│  ✏️  Approve/deny access requests                            │
│  ✏️  View audit logs                                         │
│                                                              │
│  NEW: Policy management UI (not Synapse UI)                 │
└────────────────────────┬────────────────────────────────────┘
                         │
                         │ REST API
                         ↓
┌─────────────────────────────────────────────────────────────┐
│                   POLICY ENGINE                              │
│              (Central Source of Truth)                       │
│                                                              │
│  📋 Policy Store (Cedar/Rego + Neptune governance graph)    │
│  📋 Access Requirement definitions                           │
│  📋 User approvals and training records                      │
│  📋 DAC membership and workflows                             │
│                                                              │
│  Functions:                                                  │
│  - Evaluate evidence → Issue capability tokens               │
│  - Manage policies (CRUD)                                    │
│  - Audit all authorization decisions                         │
└────────────────────────┬────────────────────────────────────┘
                         │
                         │ Capability Tokens (JWT)
                         ↓
         ┌───────────────┼───────────────┬───────────────┐
         ↓               ↓               ↓               ↓
┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐
│  Synapse   │  │   Flower   │  │ HuggingFace│  │  Neptune   │
│ Platform   │  │ SuperNode  │  │   Models   │  │   Query    │
│            │  │            │  │            │  │    API     │
│ Validates  │  │ Validates  │  │ Validates  │  │ Validates  │
│  tokens    │  │  tokens    │  │  tokens    │  │  tokens    │
└────────────┘  └────────────┘  └────────────┘  └────────────┘
```

**All services become "Asset Guardians" that validate capability tokens.**

---

## Synapse Consuming Capability Tokens

### Current: Synapse Controls Access

```python
# User downloads file from Synapse (today)
import synapseclient
syn = synapseclient.login('user', 'password')

# Synapse checks:
# 1. Does user have ACL permission on syn123?
# 2. Does user have AR approvals?
# 3. Is user in allowed team?

file = syn.get('syn123', downloadLocation='./data')
# ✓ or ✗ Synapse decides
```

### Future: Synapse Validates Capability Tokens

```python
# User downloads file from Synapse (future)
import synapseclient
syn = synapseclient.login('user', 'password')

# Step 1: User requests capability from Policy Engine
capability = syn.request_capability(
    action="download",
    resources=["syn123"],
    evidence={
        "research_purpose": "DUO:0000007",
        "disease": "MONDO:0004975",
        "approved_ars": ["AR001"]
    }
)
# Policy Engine evaluates → Issues JWT token

# Step 2: User presents capability token to Synapse
file = syn.get('syn123',
               capability_token=capability.token,
               downloadLocation='./data')

# Synapse:
# 1. Decodes JWT
# 2. Validates signature (from Policy Engine)
# 3. Checks: syn123 in authorized_resources?
# 4. Checks: action="download" allowed?
# 5. Checks: token not expired?
# ✓ Downloads if all pass
```

**Synapse no longer makes authorization decisions - it only validates tokens.**

---

## How Synapse Changes

### Today: Synapse Authorization Logic

```python
# Synapse REST API (current)
@require_login
def download_entity(entity_id, user_id):
    # Synapse evaluates authorization
    if not has_acl_permission(user_id, entity_id, 'DOWNLOAD'):
        raise Forbidden("No ACL permission")

    if not has_access_requirement_approval(user_id, entity_id):
        raise Forbidden("Access Requirement not met")

    if is_in_restricted_team(entity_id) and not user_in_team(user_id):
        raise Forbidden("Not in required team")

    # Synapse decides
    return download_file(entity_id)
```

### Future: Synapse Validates Capability Token

```python
# Synapse REST API (future)
@require_capability_token
def download_entity(entity_id, capability_token):
    # Synapse validates token (doesn't evaluate policy)
    token = validate_jwt(capability_token, issuer='policy-engine')

    # Check token covers this resource
    if entity_id not in token['authorized_resources']:
        raise Forbidden(f"Capability token does not cover {entity_id}")

    # Check token allows this action
    if 'download' not in token['allowed_actions']:
        raise Forbidden("Capability token does not allow download")

    # Check expiration
    if token['exp'] < now():
        raise Forbidden("Capability token expired")

    # Synapse only validates - doesn't decide
    return download_file(entity_id)
```

**Key Change:** Synapse moved from "making decisions" to "validating tokens"

---

## Policy Engine API: Creating Policies

### Policy Management API

```http
POST /policy/policies
Authorization: Bearer ADMIN_TOKEN

{
  "name": "alzheimers_disease_specific",
  "description": "Policy for Alzheimer's disease-specific research",
  "applies_to": {
    "datasets": ["syn123456", "syn789012"],
    "data_types": ["genomics", "imaging"]
  },
  "requires": {
    "duo_terms": ["DUO:0000007"],
    "disease_context": "MONDO:0004975",
    "access_requirements": ["AR001"]
  },
  "grants": {
    "actions": ["download", "query", "compute"],
    "ttl_seconds": 3600
  }
}

Response:
{
  "policy_id": "POL_12345",
  "status": "active"
}
```

### Access Requirement API

```http
POST /policy/access-requirements
Authorization: Bearer ADMIN_TOKEN

{
  "id": "AR001",
  "name": "Alzheimer's Research Training",
  "type": "training",
  "required_courses": [
    "CITI_ALZHEIMER_MODULE",
    "HIPAA_TRAINING"
  ],
  "approval_workflow": {
    "type": "dac_review",
    "dac_id": "DAC_ALZHEIMERS"
  }
}
```

### Approval API (DAC Workflow)

```http
POST /policy/approvals
Authorization: Bearer DAC_MEMBER_TOKEN

{
  "user_id": "9000001",
  "access_requirement_id": "AR001",
  "decision": "approved",
  "valid_until": "2027-09-04T00:00:00Z",
  "notes": "Training verified, research plan approved"
}

Response:
{
  "approval_id": "APR_67890",
  "user_id": "9000001",
  "ar_id": "AR001",
  "status": "active"
}
```

---

## Governance Team Workflow: Policy Engine as Source of Truth

### Step 1: Create Policy (Governance Admin)

**NEW Policy Management UI** (not Synapse):

```
Policy Engine Admin Console:

1. Navigate to "Policies" → "Create New"
2. Fill out form:
   - Policy Name: Parkinson's Imaging Access
   - Data Use Condition: DUO:0000007 (disease-specific)
   - Disease Context: MONDO:0005180 (Parkinson's)
   - Required Training: HIPAA + PD Ethics
   - DAC: Parkinson's Data Committee
   - Allowed Actions: download, query, federated_learning
   - Token TTL: 1 hour
3. Click "Create"
```

**Behind the scenes:**
```
POST /policy/policies
→ Stores in Neptune governance graph + Cedar policy store
→ Policy ID: POL_PD_2026
```

### Step 2: Link Datasets to Policy (Governance Admin)

```
Policy Engine Admin Console:

1. Navigate to Policy: POL_PD_2026
2. Click "Link Datasets"
3. Search: "Parkinson's MRI"
4. Select:
   - syn987654 (Synapse)
   - dataset_456 (Flower node)
   - model_789 (HuggingFace)
5. Click "Link"
```

**Behind the scenes:**
```
INSERT DATA {
  <https://synapse.org/syn987654> gov:governedBy <POL_PD_2026> .
  <flower://dataset_456> gov:governedBy <POL_PD_2026> .
  <hf://model_789> gov:governedBy <POL_PD_2026> .
}
```

**Note:** Datasets across platforms governed by ONE policy

### Step 3: User Requests Access (Researcher)

**NEW: Centralized Access Request Portal**

```
Policy Engine Access Portal:

1. User logs in with GA4GH Passport
2. Searches for datasets: "Parkinson's imaging"
3. Finds:
   - syn987654 (Synapse)
   - dataset_456 (Flower)
   - model_789 (HuggingFace)
4. Clicks "Request Access"
5. Fills out form:
   - Research purpose: Drug discovery
   - Disease: Parkinson's
   - IRB approval: [upload]
   - Training certificates: [upload]
6. Submits to DAC
```

**Behind the scenes:**
```
POST /policy/access-requests
{
  "user_id": "9000002",
  "policy_id": "POL_PD_2026",
  "evidence": {
    "research_purpose": "DUO:0000007",
    "disease": "MONDO:0005180",
    "training_certificates": ["HIPAA", "PD_ETHICS"],
    "irb_approval": "IRB-2026-456"
  }
}
→ Routed to DAC_PARKINSONS queue
```

### Step 4: DAC Reviews (Governance Admin)

**NEW: DAC Portal in Policy Engine**

```
Policy Engine DAC Portal:

1. DAC member logs in
2. Views pending requests for POL_PD_2026
3. Reviews request from user 9000002:
   - Evidence submitted ✓
   - Training valid ✓
   - IRB approved ✓
4. Clicks "Approve"
5. Sets expiration: 1 year
```

**Behind the scenes:**
```
POST /policy/approvals
{
  "user_id": "9000002",
  "policy_id": "POL_PD_2026",
  "decision": "approved",
  "approved_by": "dac_member_456",
  "valid_until": "2027-09-04"
}
→ User can now request capability tokens for POL_PD_2026
```

### Step 5: User Gets Capability Token (Automatic)

**User's Python client** (works with ANY service):

```python
# Universal capability request
from gaas_client import GovernanceClient  # Governance-as-a-Service client

gov = GovernanceClient()
gov.login()

# Request capability for Parkinson's data
capability = gov.request_capability(
    research_purpose="DUO:0000007",
    disease="MONDO:0005180"
)

print(capability.token)
# eyJhbGci... (valid for syn987654, dataset_456, model_789)
```

**Policy Engine evaluates:**
```
1. User has approval for POL_PD_2026? ✓
2. Evidence matches policy requirements? ✓
3. Issue capability token covering:
   - syn987654 (Synapse)
   - dataset_456 (Flower)
   - model_789 (HuggingFace)
```

### Step 6: User Accesses Data Across Platforms

**a) Download from Synapse:**
```python
import synapseclient
syn = synapseclient.login()

# Present capability token to Synapse
file = syn.get('syn987654', capability_token=capability.token)
# Synapse validates token → Allows download
```

**b) Train Federated Model on Flower:**
```python
from flower_client import FlowerClient
flower = FlowerClient()

# Present capability token to Flower
flower.train(
    dataset='dataset_456',
    capability_token=capability.token
)
# Flower validates token → Allows training
```

**c) Fine-tune Model on HuggingFace:**
```python
from huggingface_hub import HfApi
hf = HfApi()

# Present capability token to HuggingFace
hf.fine_tune(
    model='model_789',
    capability_token=capability.token
)
# HuggingFace validates token → Allows fine-tuning
```

**ONE capability token works across all services!**

---

## Data Sync: Synapse → Policy Engine

### Initially: Migrate Existing Policies

When Policy Engine becomes source of truth, migrate existing Synapse policies:

```python
# One-time migration script
import synapseclient
from gaas_client import PolicyEngineAdmin

syn = synapseclient.login()
policy_admin = PolicyEngineAdmin()

# 1. Get all Synapse entities with governance
entities = syn.restGET("/entity/query?hasAnnotation=duo_term")

for entity in entities:
    # 2. Read Synapse metadata
    annotations = syn.getAnnotations(entity['id'])
    ars = syn.restGET(f"/entity/{entity['id']}/accessRequirement")

    # 3. Create policy in Policy Engine
    policy = policy_admin.create_policy(
        name=f"Policy for {entity['name']}",
        duo_terms=annotations.get('duo_term', []),
        disease=annotations.get('disease_context', []),
        access_requirements=[ar['id'] for ar in ars]
    )

    # 4. Link Synapse entity to new policy
    policy_admin.link_resource(
        policy_id=policy['id'],
        resource_uri=f"https://synapse.org/{entity['id']}"
    )

    print(f"Migrated {entity['id']} → {policy['id']}")
```

### Ongoing: New Datasets

**Option 1: Create in Policy Engine, sync TO Synapse**
```python
# Create policy in Policy Engine
policy = policy_admin.create_policy(...)

# Sync to Synapse (for backward compatibility)
sync_policy_to_synapse(policy['id'])
```

**Option 2: Deprecate Synapse policy management**
```
All new policies MUST be created in Policy Engine.
Synapse UI shows read-only policy view with link to Policy Engine.
```

---

## Governance Team's New World

### What Changes

**BEFORE (Synapse as source):**
```
Governance Admin → Synapse UI → Manage policies → Synapse enforces
```

**AFTER (Policy Engine as source):**
```
Governance Admin → Policy Engine UI → Manage policies → All services enforce
```

### New Admin Console (Policy Engine)

```
┌────────────────────────────────────────────────────────────┐
│             POLICY ENGINE ADMIN CONSOLE                     │
├────────────────────────────────────────────────────────────┤
│                                                             │
│  Dashboard                                                  │
│  ├─ Active Policies (142)                                  │
│  ├─ Pending Access Requests (23)                           │
│  ├─ Active Capability Tokens (1,547)                       │
│  └─ Audit Events (last 24h)                                │
│                                                             │
│  Policies                                                   │
│  ├─ Create New Policy                                      │
│  ├─ View All Policies                                      │
│  ├─ Import from Synapse                                    │
│  └─ Policy Templates (DUO-based)                           │
│                                                             │
│  Access Requirements                                        │
│  ├─ Training Requirements                                  │
│  ├─ IRB Requirements                                       │
│  └─ Managed Access (DAC)                                   │
│                                                             │
│  DAC Management                                            │
│  ├─ Pending Requests                                       │
│  ├─ Approved Users                                         │
│  ├─ DAC Members                                            │
│  └─ Review History                                         │
│                                                             │
│  Resources                                                  │
│  ├─ Synapse Datasets (3,456)                               │
│  ├─ Flower Nodes (12)                                      │
│  ├─ HuggingFace Models (89)                                │
│  └─ Link New Resource                                      │
│                                                             │
│  Audit & Analytics                                          │
│  ├─ Authorization Decisions                                │
│  ├─ Capability Token Issuance                              │
│  ├─ Data Access Logs                                       │
│  └─ Compliance Reports                                     │
│                                                             │
└────────────────────────────────────────────────────────────┘
```

### Skills Governance Team Needs

**NEW Skills:**
- ✅ Use Policy Engine admin console
- ✅ Understand federated governance (Synapse + Flower + HF)
- ✅ Review capability token audit logs

**SAME Skills:**
- ✅ Understand DUO vocabulary
- ✅ DAC review workflows
- ✅ Compliance requirements

---

## Benefits of Policy Engine as Source of Truth

### 1. **Federated Governance**
```
ONE policy governs data across:
- Synapse (data storage)
- Flower (federated learning)
- HuggingFace (model training)
- Neptune (knowledge graph)
```

### 2. **Portable Credentials**
```
User gets ONE capability token
→ Works everywhere
→ No re-authentication per service
```

### 3. **Centralized Audit**
```
All authorization decisions in ONE place
→ Compliance reporting simplified
→ Cross-platform usage tracking
```

### 4. **Policy Evolution Without Service Changes**
```
Update policy in Policy Engine
→ All services enforce new policy immediately
→ No code deploys needed
```

### 5. **Multi-Institutional Collaboration**
```
Stanford Policy Engine issues token
→ MIT Flower node validates it
→ Harvard HuggingFace validates it
→ Trust chain established
```

---

## Migration Timeline

### Phase 1: Synapse Source of Truth (Current)
- ✅ Synapse manages policies
- ✅ Policy Engine reads from Neptune (synced from Synapse)
- ⏱️ 2026 Q3-Q4

### Phase 2: Dual Source of Truth (Transition)
- ⏱️ Policy Engine can create NEW policies
- ⏱️ Synapse policies still respected
- ⏱️ Bi-directional sync
- ⏱️ 2027 Q1-Q2

### Phase 3: Policy Engine Source of Truth (Target)
- ⏱️ ALL policies created in Policy Engine
- ⏱️ Synapse validates capability tokens
- ⏱️ Deprecate Synapse policy management UI
- ⏱️ 2027 Q3+

---

## Frequently Asked Questions

**Q: Does Synapse become obsolete?**
A: No! Synapse remains the data storage platform. It just delegates authorization to Policy Engine instead of managing it internally.

**Q: What happens to existing Synapse users?**
A: Transition period with backward compatibility. Old API works, new capability token API available.

**Q: Who operates the Policy Engine?**
A: Sage Bionetworks (or federated consortium). Governed like a shared service.

**Q: Can institutions run their own Policy Engine?**
A: Yes! Federated model where institutions trust each other's Policy Engines.

**Q: What if Policy Engine goes down?**
A: Services fall back to cached policies + local ACLs (degraded mode). Or federated backup Policy Engine.

**Q: How does billing work?**
A: Policy Engine issues tokens with usage tracking. Services report usage back for billing.

---

## Summary

**Today:** Synapse is the source of truth
**Future:** Policy Engine is the source of truth

**Governance team workflow:**
- Manage policies in Policy Engine admin console (not Synapse UI)
- ONE policy governs data across ALL services
- DAC workflows centralized in Policy Engine
- Capability tokens work everywhere

**Benefits:**
- Federated governance across institutions
- Portable credentials (one token, many services)
- Centralized audit and compliance
- Policy updates without service redeployment

**This is "Governance-as-a-Service" (GaaS) from the ISMB 2026 poster.**
