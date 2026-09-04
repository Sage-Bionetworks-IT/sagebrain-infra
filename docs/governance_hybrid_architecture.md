# Hybrid Governance Architecture

## Overview

The Neptune query API supports **two governance enforcement modes** to enable gradual migration from external ReBAC authorization to native Neptune graph-based authorization.

### Mode 1: Post-Filter (Current, Transitional)
```
User → API Gateway → Submit → SQS → Query Worker → Neptune
                                              ↓
                                       Extract Resources
                                              ↓
                                       ReBAC Lambda ← Governance Graph (external)
                                              ↓
                                      Allow or Deny Results
```

**Pros:**
- Works immediately without governance data in Neptune
- Supports current ReBAC/AVP Cedar policy model
- Simple to implement and test

**Cons:**
- Wastes Neptune compute on denied queries
- Doesn't scale efficiently to 400K+ resources (must check all results)
- Can't filter unauthorized paths in graph traversals

### Mode 2: Query Rewrite (Future, Target)
```
User → API Gateway → Submit → Query Rewriter → Neptune (with governance filters)
                                       ↓
                              Inject governance patterns
                                       ↓
                                  Results (pre-filtered)
```

**Pros:**
- Neptune's query planner filters at query time (scales to millions)
- Works for complex graph traversals
- No external ReBAC calls
- Efficient at 400K+ resource scale

**Cons:**
- Requires governance triples loaded in Neptune
- Query rewriting adds complexity
- Must maintain SPARQL parsing logic

---

## Policy-as-Code Architecture (Target State)

From **Policy-as-Code_sketches and pitches.pdf**, the full architecture includes:

### Components

1. **Policy Engine** (Synapse Platform)
   - Evaluates user evidence (GA4GH Passport) against DUO policies
   - Returns signed capability tokens

2. **Asset Guardian** (Reverse Proxy)
   - Sits in front of Neptune
   - Validates capability tokens
   - Rewrites queries with governance filters
   - Forwards to Neptune

3. **Governance Graph** (Neptune Triples)
   ```turtle
   syn123 gov:hasACL :grant_1 .
   :grant_1 gov:principal "user:9000001" ;
            gov:permission "ACCESS" ;
            gov:bindingType "Direct" .
   ```

4. **Neptune** (Data + Governance)
   - Single source of truth for data AND permissions
   - Query planner optimizes governance joins

### Request Flow

```
┌─────────┐
│  User   │ Presents GA4GH Passport with evidence
└────┬────┘
     ↓
┌────────────────┐
│ Policy Engine  │ Evaluates evidence → Issues capability token
└────────┬───────┘
         ↓
┌─────────────────┐
│ Asset Guardian  │ Validates capability → Rewrites query
└────────┬────────┘
         ↓
┌─────────────────┐
│    Neptune      │ Executes query with governance filters
│ (Data + Gov)    │
└─────────────────┘
```

---

## Current Implementation: Hybrid Mode

### Environment Variable: `GOVERNANCE_MODE`

Set in `src/neptune_api_stack.py`:

```python
"GOVERNANCE_MODE": "post_filter"  # or "query_rewrite"
```

### Post-Filter Mode (Default)

**When to use:** Governance data not yet in Neptune

**How it works:**
1. Query runs on Neptune without modification
2. Extract Synapse resource IDs from results
3. Call ReBAC Lambda to authorize batch
4. If ANY resource denied → error with specific list
5. Otherwise → return results

**Code:** `src/lambda/query.py::_execute_query()`

### Query Rewrite Mode

**When to use:** Governance triples loaded into Neptune

**How it works:**
1. Parse SPARQL query
2. Identify resource variables (`?file`, `?dataset`, etc.)
3. Inject governance filter patterns:
   ```sparql
   ?file gov:hasACL ?__grant_file .
   ?__grant_file gov:principal "user:9000001" ;
                 gov:permission "ACCESS" .
   ```
4. Send rewritten query to Neptune
5. Neptune filters unauthorized resources at query time
6. No post-filtering needed

**Code:**
- `src/lambda/query_rewriter.py` - Query rewriting logic
- `src/lambda/query.py::_execute_query()` - Mode selection

---

## Query Rewriting Examples

### Example 1: Simple File Query

**Original Query:**
```sparql
SELECT ?file ?name WHERE {
  ?file a :File ;
        :name ?name .
}
```

**Rewritten Query:**
```sparql
SELECT ?file ?name WHERE {
  ?file a :File ;
        :name ?name .

  # Governance filter for ?file
  ?file gov:hasACL ?__grant_file .
  ?__grant_file gov:principal "user:9000001" ;
                gov:permission "ACCESS" .
}
```

**Result:** Only files user 9000001 can access are returned.

### Example 2: Graph Traversal

**Original Query:**
```sparql
SELECT ?related WHERE {
  <syn123> skos:related+ ?related .
}
```

**Rewritten Query:**
```sparql
SELECT ?related WHERE {
  <syn123> skos:related+ ?related .

  # Governance filter for ?related
  ?related gov:hasACL ?__grant_related .
  ?__grant_related gov:principal "user:9000001" ;
                   gov:permission "ACCESS" .
}
```

**Result:** Graph traversal stops at unauthorized nodes automatically.

---

## Migration Path

### Phase 1: Post-Filter (Current)
- [x] Implement post-query authorization via ReBAC Lambda
- [x] Extract resource IDs from results
- [x] All-or-nothing access control
- [x] Audit logging

**Status:** ✅ Complete (Spring 2026)

### Phase 2: Load Governance Triples
- [ ] Design governance triple schema in Neptune
- [ ] Build governance data loader
- [ ] Sync existing ACLs from Synapse → Neptune triples
- [ ] Test governance graph queries

**Status:** 🔄 In Progress

### Phase 3: Query Rewrite Testing
- [x] Implement SPARQL query rewriter
- [ ] Test query rewriting in dev environment
- [ ] Performance benchmarking (post-filter vs query rewrite)
- [ ] Feature flag to enable query_rewrite mode

**Status:** 🔄 In Progress (code complete, needs testing)

### Phase 4: Asset Guardian (Full Policy-as-Code)
- [ ] Implement Asset Guardian as reverse proxy
- [ ] GA4GH Passport validation
- [ ] Capability token issuance
- [ ] Replace API Gateway authorizer

**Status:** 📋 Planned (Fall 2026)

---

## Configuration

### Switch to Query Rewrite Mode

1. Ensure governance triples are loaded in Neptune
2. Update `src/neptune_api_stack.py`:
   ```python
   "GOVERNANCE_MODE": "query_rewrite"
   ```
3. Deploy:
   ```bash
   cdk deploy app-dev-neptune-api --profile sagebrain
   ```
4. Monitor CloudWatch logs for `query_rewritten` events

### Rollback to Post-Filter

If query rewriting has issues:
```python
"GOVERNANCE_MODE": "post_filter"
```

The worker automatically falls back to post-filtering if rewriting fails.

---

## Testing

### Test Post-Filter Mode

```bash
# Query with resources user doesn't have access to
curl -X POST $API_URL/query \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"query": "SELECT * WHERE { <syn999> ?p ?o }"}'

# Should return: 403 with denied_resources: ["syn999"]
```

### Test Query Rewrite Mode

```bash
# Set mode to query_rewrite
export GOVERNANCE_MODE=query_rewrite

# Query should be rewritten with governance filters
# Check CloudWatch logs for "query_rewritten" event

# Results should only include authorized resources
```

---

## Performance Comparison

### Post-Filter Mode

| Query Returns | ReBAC Calls | Latency | Notes |
|--------------|-------------|---------|-------|
| 10 resources | 1 (batch) | +50ms | Fast for small result sets |
| 1K resources | 1 (batch) | +200ms | ReBAC checks 1K resources |
| 10K resources | 1 (batch) | +2s | Approaches limits |

**Bottleneck:** ReBAC Lambda invocation + batch check at scale

### Query Rewrite Mode

| Query Complexity | Governance Overhead | Latency | Notes |
|-----------------|-------------------|---------|-------|
| Simple SELECT | Minimal | +10ms | One extra join |
| Graph traversal (5 hops) | Moderate | +100ms | Filters at each hop |
| Complex (10+ patterns) | Higher | +500ms | Multiple governance joins |

**Bottleneck:** Neptune query planner complexity, not scale

**At 400K scale:** Query rewrite mode is 10-100x faster.

---

## Audit Logging

All governance decisions are logged to CloudWatch:

### Post-Filter Mode Events
```json
{"event": "sparql_access_denied", "user_id": "9000001", "denied_resources": ["syn456"], "governance_mode": "post_filter"}
```

### Query Rewrite Mode Events
```json
{"event": "query_rewritten", "user_id": "9000001", "original_length": 120, "rewritten_length": 280}
{"event": "sparql_query_authorized_at_query_time", "user_id": "9000001", "governance_mode": "query_rewrite"}
```

### Rewrite Failures (Auto-Fallback)
```json
{"event": "query_rewrite_failed", "error": "Unbalanced braces", "fallback": "post_filter"}
```

---

## Security Considerations

### Query Rewrite Mode

**Risk:** User can craft queries that bypass governance filters

**Mitigation:**
1. Query rewriter validates structure before rewriting
2. Queries that can't be safely rewritten fall back to post-filter
3. Audit logs track all rewrites
4. Neptune enforces governance at triple level (data-layer security)

**Not vulnerable:** User can't remove injected governance patterns (query is rewritten server-side)

### Post-Filter Mode

**Risk:** Expensive queries run before authorization check

**Mitigation:**
1. 60s Neptune timeout
2. SQS visibility timeout prevents duplicate work
3. Query complexity limits (8000 char max)

---

## Future: Asset Guardian Reverse Proxy

The ultimate architecture moves authorization **entirely out of Lambda**:

```
┌──────────────────────────────────────────────┐
│          Asset Guardian (ALB + Lambda)       │
│  1. Validate GA4GH Passport                  │
│  2. Call Policy Engine → Capability Token    │
│  3. Rewrite Query with Governance Filters    │
│  4. Forward to Neptune                       │
└──────────────────────────────────────────────┘
                     ↓
              ┌─────────────┐
              │   Neptune   │
              │ (Read-Only) │
              └─────────────┘
```

**Benefits:**
- No one can bypass Asset Guardian (Neptune has no public endpoint)
- Capability tokens are portable (can be used across services)
- Policy Engine is centralized (Synapse, Flower, HuggingFace use same engine)

**Implementation:** Fall 2026 (per policy-as-code roadmap)

---

## References

- [Policy-as-Code_sketches and pitches.pdf](../Policy-as-Code_sketches and pitches.pdf)
- [governance_rebac_concept.md](./governance_rebac_concept.md)
- [src/lambda/query_rewriter.py](../src/lambda/query_rewriter.py)
- [src/lambda/query.py](../src/lambda/query.py)
- NeurIPS 2026: "Decentralized AI Governance Must Decouple Policy Processing from Capability Enforcement"
