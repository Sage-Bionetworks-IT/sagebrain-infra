# Exact Flow: User Query → Policy Engine → Asset Guardian → Neptune

## Current Flow (Post-Filter Mode)

```
┌──────┐
│ User │
└──┬───┘
   │ 1. POST /query + Synapse PAT
   │    {"query": "SELECT * WHERE { ?file a :File }"}
   ↓
┌────────────────┐
│  API Gateway   │ ← Validates Synapse PAT + Team membership
└───────┬────────┘
        │ 2. Invokes Submit Lambda
        ↓
┌────────────────┐
│ Submit Lambda  │ ← Creates job, enqueues to SQS
└───────┬────────┘
        │ 3. Returns job_id
        ↓
┌──────┐
│ User │ ← {"job_id": "abc-123", "status": "pending"}
└──┬───┘
   │ 4. Poll GET /query/abc-123
   ↓
┌────────────────┐
│ Status Lambda  │ ← Reads DynamoDB
└───────┬────────┘
        │ 5. Returns status
        ↓
┌──────┐
│ User │ ← {"status": "running"} ... {"status": "complete", "results": [...]}
└──────┘

Meanwhile, async in background:
┌────────────────┐
│  SQS Queue     │
└───────┬────────┘
        │ 6. Triggers Query Worker
        ↓
┌────────────────┐
│ Query Worker   │ ← Runs SPARQL on Neptune
└───────┬────────┘
        │ 7. Gets results
        ↓
┌────────────────┐
│    Neptune     │ ← Returns ALL files (no filtering)
└───────┬────────┘
        │ 8. Results: [syn1, syn2, syn3, ... syn9999]
        ↓
┌────────────────┐
│ Query Worker   │ ← Extracts resource IDs
└───────┬────────┘
        │ 9. Calls ReBAC Lambda
        ↓
┌────────────────┐
│  ReBAC Lambda  │ ← Checks if user can access [syn1, syn2, ...]
└───────┬────────┘
        │ 10. Returns: authorized=[syn1, syn2], denied=[syn3, ...]
        ↓
┌────────────────┐
│ Query Worker   │ ← If ANY denied → error, else → save results to DynamoDB
└───────┬────────┘
        │ 11. User polls again and gets results
        ↓
┌──────┐
│ User │ ← {"status": "complete", "results": [...]} OR {"status": "error", "denied_resources": [...]}
└──────┘
```

**Problem:** Wastes Neptune compute if user doesn't have access. Doesn't scale to 400K nodes.

---

## Target Flow: Policy Engine + Asset Guardian (Query Rewrite Mode)

### Part 1: User Obtains Capability Token (Once, at Session Start)

```
┌──────┐
│ User │ "I want to query Alzheimer's data"
└──┬───┘
   │ 1. POST /policy/issue-capability + Synapse PAT
   │    {
   │      "user_id": "9000001",
   │      "evidence": {
   │        "research_purpose": "DUO:0000007",
   │        "disease": "MONDO:0004975",
   │        "approved_access_requirements": ["AR001"]
   │      }
   │    }
   ↓
┌─────────────────┐
│  API Gateway    │ ← Validates Synapse PAT
└────────┬────────┘
         │ 2. Invokes Policy Engine Lambda
         ↓
┌─────────────────┐
│ Policy Engine   │
│   Lambda        │
└────────┬────────┘
         │ 3. Query Neptune Governance Graph for policies
         ↓
┌─────────────────┐
│    Neptune      │ ← SPARQL: Get DUO terms for disease-specific data
│ (Governance     │    Returns: "Alzheimer's data requires DUO:0000007 + AR001"
│    Graph)       │
└────────┬────────┘
         │ 4. Policy data returned
         ↓
┌─────────────────┐
│ Policy Engine   │ ← Evaluate: User has DUO:0000007 ✓, AR001 ✓
│   Lambda        │ ← Decision: GRANT capability for Alzheimer's data
└────────┬────────┘
         │ 5. Sign JWT capability token
         │    Payload: {
         │      "sub": "9000001",
         │      "authorized_policies": ["disease_specific:alzheimers"],
         │      "duo_term": "DUO:0000007",
         │      "disease": "MONDO:0004975",
         │      "exp": 1725372000
         │    }
         ↓
┌──────┐
│ User │ ← Response:
└──────┘    {
              "capability_token": "eyJhbGciOiJIUzI1NiIs...",
              "expires_at": "2026-09-03T12:00:00Z",
              "authorized_resources": ["disease_specific:alzheimers"]
            }

            User stores this token (valid for 1 hour)
```

---

### Part 2: User Queries Data (Many Times, with Same Token)

```
┌──────┐
│ User │ "Show me all Alzheimer's files"
└──┬───┘
   │ 1. POST /query + Capability Token (NOT Synapse PAT)
   │    Authorization: Bearer eyJhbGciOiJIUzI1NiIs...
   │    {
   │      "query": "SELECT ?file ?name WHERE { ?file a :File ; :name ?name }"
   │    }
   ↓
┌─────────────────┐
│  Asset Guardian │ ← This is the Submit Lambda (enhanced)
│ (Submit Lambda) │
└────────┬────────┘
         │ 2. Decode & validate JWT capability token
         │    - Check signature (HS256)
         │    - Check expiration
         │    - Extract authorized_policies
         ↓
         │ Token payload:
         │   {
         │     "authorized_policies": ["disease_specific:alzheimers"],
         │     "duo_term": "DUO:0000007",
         │     "disease": "MONDO:0004975"
         │   }
         │
         │ 3. Rewrite user's SPARQL query to inject governance filters
         │
         │    Original:
         │      SELECT ?file ?name WHERE {
         │        ?file a :File ; :name ?name .
         │      }
         │
         │    Rewritten:
         │      SELECT ?file ?name WHERE {
         │        ?file a :File ; :name ?name .
         │        # INJECTED FROM CAPABILITY TOKEN
         │        ?file gov:hasDataUseCondition duo:DUO0000007 ;
         │              gov:diseaseContext mondo:MONDO0004975 .
         │      }
         ↓
┌─────────────────┐
│  Asset Guardian │ ← Enqueue rewritten query to SQS
└────────┬────────┘
         │ 4. Returns job_id
         ↓
┌──────┐
│ User │ ← {"job_id": "xyz-456", "status": "pending"}
└──┬───┘
   │ 5. Poll GET /query/xyz-456
   ↓
┌─────────────────┐
│ Status Lambda   │
└────────┬────────┘
         │ {"status": "running"}...
         ↓
┌──────┐
│ User │
└──────┘

Meanwhile, async in background:
┌─────────────────┐
│  SQS Queue      │
└────────┬────────┘
         │ 6. Triggers Query Worker
         ↓
┌─────────────────┐
│ Query Worker    │ ← Runs REWRITTEN query on Neptune
└────────┬────────┘
         │ 7. Neptune executes query WITH governance filters
         ↓
┌─────────────────┐
│    Neptune      │ ← Query includes authorization filters
│  (Data +        │    Neptune's query planner:
│   Governance)   │    - Filters 400K files
│                 │    - Returns ONLY Alzheimer's files (50K)
│                 │    - User NEVER sees unauthorized data
└────────┬────────┘
         │ 8. Pre-filtered results: [syn_alz_001, syn_alz_002, ...]
         ↓
┌─────────────────┐
│ Query Worker    │ ← NO post-filtering needed!
│                 │    All results are already authorized
│                 │    Save to DynamoDB
└────────┬────────┘
         │ 9. User polls again
         ↓
┌──────┐
│ User │ ← {"status": "complete", "results": [syn_alz_001, syn_alz_002, ...]}
└──────┘
```

---

## Side-by-Side Comparison: Exact Request/Response

### Current Flow (Post-Filter)

#### Request 1: Submit Query
```http
POST https://api.sagebrain.org/query HTTP/1.1
Authorization: Bearer SYNAPSE_PAT_abc123
Content-Type: application/json

{
  "query": "SELECT ?file WHERE { ?file a :File } LIMIT 100"
}
```

#### Response 1:
```http
HTTP/1.1 202 Accepted
Content-Type: application/json

{
  "job_id": "abc-123",
  "status": "pending"
}
```

#### Request 2: Poll Status
```http
GET https://api.sagebrain.org/query/abc-123 HTTP/1.1
Authorization: Bearer SYNAPSE_PAT_abc123
```

#### Response 2a (Running):
```http
HTTP/1.1 200 OK
Content-Type: application/json

{
  "job_id": "abc-123",
  "status": "running"
}
```

#### Response 2b (Denied - Post-Filter caught it):
```http
HTTP/1.1 200 OK
Content-Type: application/json

{
  "job_id": "abc-123",
  "status": "error",
  "error": "Access denied. You don't have permission to access: syn456, syn789. Please request access to these resources.",
  "denied_resources": ["syn456", "syn789"]
}
```

**Problem:** Query ran on Neptune, wasted compute, then denied.

---

### Target Flow (Policy Engine + Query Rewrite)

#### Request 1: Get Capability Token (Once)
```http
POST https://api.sagebrain.org/policy/issue-capability HTTP/1.1
Authorization: Bearer SYNAPSE_PAT_abc123
Content-Type: application/json

{
  "user_id": "9000001",
  "evidence": {
    "research_purpose": "DUO:0000007",
    "disease": "MONDO:0004975",
    "approved_access_requirements": ["AR001"],
    "institution": "Stanford University"
  }
}
```

#### Response 1:
```http
HTTP/1.1 200 OK
Content-Type: application/json

{
  "decision": "ALLOW",
  "capability_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzYWdlLXBvbGljeS1lbmdpbmUiLCJzdWIiOiI5MDAwMDAxIiwiYXVkIjoibmVwdHVuZS1xdWVyeS1hcGkiLCJleHAiOjE3MjUzNzIwMDAsImlhdCI6MTcyNTM2ODQwMCwiYXV0aG9yaXplZF9wb2xpY2llcyI6WyJkaXNlYXNlX3NwZWNpZmljOmFsemhlaW1lcnMiXSwiZHVvX3Rlcm0iOiJEVU86MDAwMDAwNyIsImRpc2Vhc2UiOiJNT05ETzowMDA0OTc1In0.signature",
  "expires_at": "2026-09-03T12:00:00Z",
  "authorized_policies": ["disease_specific:alzheimers"]
}
```

**User saves this token for the session**

---

#### Request 2: Submit Query (with Capability Token)
```http
POST https://api.sagebrain.org/query HTTP/1.1
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
Content-Type: application/json

{
  "query": "SELECT ?file WHERE { ?file a :File } LIMIT 100"
}
```

**Asset Guardian (Submit Lambda):**
1. Decodes token
2. Sees: `authorized_policies: ["disease_specific:alzheimers"]`
3. Rewrites query:
   ```sparql
   SELECT ?file WHERE {
     ?file a :File .
     ?file gov:hasDataUseCondition duo:DUO0000007 ;
           gov:diseaseContext mondo:MONDO0004975 .
   } LIMIT 100
   ```
4. Enqueues rewritten query

#### Response 2:
```http
HTTP/1.1 202 Accepted
Content-Type: application/json

{
  "job_id": "xyz-456",
  "status": "pending"
}
```

---

#### Request 3: Poll Status
```http
GET https://api.sagebrain.org/query/xyz-456 HTTP/1.1
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

#### Response 3:
```http
HTTP/1.1 200 OK
Content-Type: application/json

{
  "job_id": "xyz-456",
  "status": "complete",
  "results": {
    "results": {
      "bindings": [
        {"file": {"value": "https://synapse.org/syn_alz_001"}},
        {"file": {"value": "https://synapse.org/syn_alz_002"}},
        ...
      ]
    }
  },
  "content_type": "application/sparql-results+json"
}
```

**All results are pre-authorized** - no unauthorized data ever returned.

---

## Key Differences

| Aspect | Post-Filter (Current) | Policy Engine + Query Rewrite (Target) |
|--------|----------------------|----------------------------------------|
| **Authorization Point** | After query execution | Before query execution |
| **Token Type** | Synapse PAT (user identity) | Capability token (user + policies) |
| **Query Rewriting** | No | Yes (inject governance filters) |
| **Neptune Filtering** | No (returns all data) | Yes (filters at query time) |
| **Post-Filtering** | Yes (check every result) | No (already filtered) |
| **Wasted Compute** | Yes (if denied) | No (filtered by Neptune) |
| **Scalability** | Bad (O(results)) | Good (O(1) + Neptune native) |
| **Token Lifespan** | Session | 1 hour (configurable) |
| **Errors** | After query runs | At capability issuance |

---

## Token Lifespan & Refresh

### Capability Token Expires:
```http
GET /query/job-123 HTTP/1.1
Authorization: Bearer expired_token

HTTP/1.1 401 Unauthorized
{
  "error": "Capability token expired",
  "expired_at": "2026-09-03T12:00:00Z"
}
```

### User Refreshes Token:
```http
POST /policy/issue-capability HTTP/1.1
Authorization: Bearer SYNAPSE_PAT_abc123
Content-Type: application/json

{
  "user_id": "9000001",
  "evidence": {...}  // Same evidence
}
```

Gets new token with fresh expiration.

---

## What User Sees (UX)

### Current Flow (Post-Filter):
```python
# User Python client
from synapseclient import Synapse
syn = Synapse()
syn.login()

# Query API
results = syn.query_neptune("SELECT ?file WHERE { ?file a :File }")
# ❌ Error after 30 seconds: "Access denied: syn456, syn789"
```

### Target Flow (Policy Engine):
```python
from synapseclient import Synapse
syn = Synapse()
syn.login()

# Step 1: Declare research intent (once)
capability = syn.request_capability(
    research_purpose="DUO:0000007",
    disease="MONDO:0004975"
)
# ✓ Capability granted for Alzheimer's data

# Step 2: Query (many times with same capability)
results = syn.query_neptune(
    "SELECT ?file WHERE { ?file a :File }",
    capability=capability
)
# ✓ Returns only Alzheimer's files (fast, no post-filter)

results2 = syn.query_neptune(
    "SELECT ?file WHERE { ?file :size > 1000000 }",
    capability=capability
)
# ✓ Same capability works for different queries
```

---

## Summary: The Exact Flow

1. **User → Policy Engine:** "Here's my research intent" → Get capability token
2. **User → Asset Guardian:** "Run this query" + capability token
3. **Asset Guardian → Neptune:** Rewritten query with governance filters
4. **Neptune → User:** Pre-filtered results (only authorized data)

**No 400K node checks** - Neptune filters based on policies embedded in the query.
