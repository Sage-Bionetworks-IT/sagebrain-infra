#!/bin/bash
# Integration Test: Policy Engine + Asset Guardian Flow
#
# This script demonstrates the exact HTTP flow for Policy-as-Code architecture.
#
# Prerequisites:
# - Policy Engine API deployed: https://api.sagebrain.org/policy/issue-capability
# - Query API deployed: https://api.sagebrain.org/query
# - Synapse PAT in environment: SYNAPSE_PAT

set -e

# Configuration
POLICY_ENGINE_URL="${POLICY_ENGINE_URL:-https://api.sagebrain.org/policy/issue-capability}"
QUERY_API_URL="${QUERY_API_URL:-https://api.sagebrain.org/query}"
SYNAPSE_PAT="${SYNAPSE_PAT:?SYNAPSE_PAT environment variable required}"

echo "======================================================================"
echo "Policy-as-Code Integration Test"
echo "======================================================================"
echo ""

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Step 1: Request Capability Token
echo -e "${BLUE}Step 1: Request Capability Token${NC}"
echo "----------------------------------------------------------------------"
echo "POST $POLICY_ENGINE_URL"
echo "Authorization: Bearer SYNAPSE_PAT"
echo ""

CAPABILITY_RESPONSE=$(curl -s -X POST "$POLICY_ENGINE_URL" \
  -H "Authorization: Bearer $SYNAPSE_PAT" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "9000001",
    "evidence": {
      "research_purpose": "DUO:0000007",
      "disease": "MONDO:0004975",
      "approved_access_requirements": ["AR001"],
      "institution": "Stanford University"
    }
  }')

echo "Response:"
echo "$CAPABILITY_RESPONSE" | jq '.'

# Extract capability token
CAPABILITY_TOKEN=$(echo "$CAPABILITY_RESPONSE" | jq -r '.capability_token')

if [ "$CAPABILITY_TOKEN" == "null" ] || [ -z "$CAPABILITY_TOKEN" ]; then
  echo -e "${YELLOW}⚠️  Capability token not issued (may be denied)${NC}"
  echo "Reason:"
  echo "$CAPABILITY_RESPONSE" | jq -r '.reasons // "Unknown"'
  exit 1
fi

echo ""
echo -e "${GREEN}✓ Capability token obtained${NC}"
echo "Token (first 50 chars): ${CAPABILITY_TOKEN:0:50}..."
echo "Expires at: $(echo "$CAPABILITY_RESPONSE" | jq -r '.expires_at')"
echo ""
echo "======================================================================"
echo ""

# Decode token to show payload (for demo purposes)
echo -e "${BLUE}Token Payload (decoded):${NC}"
echo "$CAPABILITY_TOKEN" | awk -F. '{print $2}' | base64 -d 2>/dev/null | jq '.' || true
echo ""
echo "======================================================================"
echo ""

# Step 2: Submit Query with Capability Token
echo -e "${BLUE}Step 2: Submit Query with Capability Token${NC}"
echo "----------------------------------------------------------------------"
echo "POST $QUERY_API_URL"
echo "Authorization: Bearer CAPABILITY_TOKEN"
echo ""

SUBMIT_RESPONSE=$(curl -s -X POST "$QUERY_API_URL" \
  -H "Authorization: Bearer $CAPABILITY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "SELECT ?file ?name WHERE { ?file a :File ; :name ?name } LIMIT 10"
  }')

echo "Response:"
echo "$SUBMIT_RESPONSE" | jq '.'

JOB_ID=$(echo "$SUBMIT_RESPONSE" | jq -r '.job_id')

if [ "$JOB_ID" == "null" ] || [ -z "$JOB_ID" ]; then
  echo -e "${YELLOW}⚠️  Query submission failed${NC}"
  exit 1
fi

echo ""
echo -e "${GREEN}✓ Query submitted${NC}"
echo "Job ID: $JOB_ID"
echo "Authentication mode: $(echo "$SUBMIT_RESPONSE" | jq -r '.authentication_mode')"
echo ""
echo "======================================================================"
echo ""

# Step 3: Poll for Results
echo -e "${BLUE}Step 3: Poll for Query Results${NC}"
echo "----------------------------------------------------------------------"

MAX_POLLS=30
POLL_INTERVAL=2
STATUS="pending"

for i in $(seq 1 $MAX_POLLS); do
  echo "GET $QUERY_API_URL/$JOB_ID (attempt $i/$MAX_POLLS)"

  STATUS_RESPONSE=$(curl -s -X GET "$QUERY_API_URL/$JOB_ID" \
    -H "Authorization: Bearer $CAPABILITY_TOKEN")

  STATUS=$(echo "$STATUS_RESPONSE" | jq -r '.status')

  if [ "$STATUS" == "complete" ]; then
    echo ""
    echo -e "${GREEN}✓ Query complete!${NC}"
    echo ""
    echo "Results:"
    echo "$STATUS_RESPONSE" | jq '.results.results.bindings | length' | xargs -I {} echo "  {} results returned"
    echo ""
    echo "First result:"
    echo "$STATUS_RESPONSE" | jq '.results.results.bindings[0]'
    echo ""
    break
  elif [ "$STATUS" == "error" ]; then
    echo ""
    echo -e "${YELLOW}⚠️  Query failed${NC}"
    echo "Error: $(echo "$STATUS_RESPONSE" | jq -r '.error')"
    echo ""
    exit 1
  else
    echo "  Status: $STATUS (waiting...)"
    sleep $POLL_INTERVAL
  fi
done

if [ "$STATUS" != "complete" ]; then
  echo ""
  echo -e "${YELLOW}⚠️  Query timed out after $MAX_POLLS attempts${NC}"
  exit 1
fi

echo "======================================================================"
echo ""
echo -e "${GREEN}✓ Policy-as-Code flow completed successfully!${NC}"
echo ""
echo "Summary:"
echo "  1. Obtained capability token from Policy Engine"
echo "  2. Submitted query with capability token (Asset Guardian)"
echo "  3. Query was rewritten with governance filters"
echo "  4. Neptune returned pre-filtered, authorized results"
echo ""
echo "======================================================================"
