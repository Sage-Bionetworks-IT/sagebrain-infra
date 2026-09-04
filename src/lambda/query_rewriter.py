"""
SPARQL Query Rewriter for Policy-as-Code Architecture

Injects governance authorization patterns into SPARQL queries when
governance triples exist in Neptune. This enables Neptune's query planner
to filter unauthorized resources at query time rather than post-filtering.

Architecture:
    User Query → Query Rewriter → Neptune (with governance filters)

    Instead of:
    User Query → Neptune → Extract Resources → ReBAC Check → Filter

Target governance triple pattern in Neptune:
    ?resource gov:hasACL ?grant .
    ?grant gov:principal "user:123456" ;
           gov:permission "ACCESS" ;
           gov:bindingType "Direct" .

See: Policy-as-Code_sketches and pitches.pdf
"""

import re


class QueryRewriteError(Exception):
    """Raised when query rewriting fails."""

    pass


def inject_governance_filter(query: str, user_id: str) -> str:
    """
    Inject governance authorization patterns into a SPARQL query.

    Strategy: Identify all subject variables that represent Synapse resources
    (?file, ?dataset, ?entity, etc.) and add a governance filter block.

    Args:
        query: Original SPARQL query
        user_id: User's ID for governance check (e.g., "9000001")

    Returns:
        Rewritten SPARQL query with governance filters

    Raises:
        QueryRewriteError: If query cannot be safely rewritten

    Example:
        Input:
            SELECT ?file ?name WHERE {
              ?file a :File ; :name ?name .
            }

        Output:
            SELECT ?file ?name WHERE {
              ?file a :File ; :name ?name .
              # Governance filter
              ?file gov:hasACL ?__grant_file .
              ?__grant_file gov:principal "user:9000001" ;
                            gov:permission "ACCESS" .
            }
    """
    # Extract WHERE clause
    where_match = re.search(r"\bWHERE\s*\{", query, re.IGNORECASE | re.DOTALL)
    if not where_match:
        raise QueryRewriteError("Query missing WHERE clause")

    # Find the main WHERE block (handle nested braces)
    start = where_match.end()
    brace_count = 1
    pos = start
    while pos < len(query) and brace_count > 0:
        if query[pos] == "{":
            brace_count += 1
        elif query[pos] == "}":
            brace_count -= 1
        pos += 1

    if brace_count != 0:
        raise QueryRewriteError("Unbalanced braces in WHERE clause")

    where_body = query[start : pos - 1]

    # Identify Synapse resource variables
    # Pattern: variables used as subjects with Synapse URIs or in triple patterns
    resource_vars = _extract_resource_variables(where_body)

    if not resource_vars:
        # No identifiable resource variables - query might not reference Synapse resources
        # Return original query (will be caught by post-filtering if needed)
        return query

    # Build governance filter for each resource variable
    governance_filters = []
    for var in resource_vars:
        # Use unique grant variable per resource to avoid conflicts
        grant_var = f"?__grant_{var[1:]}"  # strip '?' prefix
        filter_block = f"""
  # Governance filter for {var}
  {var} gov:hasACL {grant_var} .
  {grant_var} gov:principal "user:{user_id}" ;
            gov:permission "ACCESS" ."""
        governance_filters.append(filter_block)

    # Inject filters before the closing brace of WHERE
    governance_block = "\n".join(governance_filters)
    rewritten_where = query[start : pos - 1] + governance_block + "\n"

    # Reconstruct query
    rewritten = query[:start] + rewritten_where + query[pos - 1 :]

    return rewritten


def _extract_resource_variables(where_clause: str) -> list[str]:
    """
    Extract variables likely representing Synapse resources.

    Heuristics:
    1. Variables in subject position with Synapse URI patterns
    2. Variables commonly named for resources (?file, ?dataset, ?entity, etc.)
    3. Variables used with predicates like rdf:type, :name, etc.

    Args:
        where_clause: Body of WHERE clause

    Returns:
        List of variable names (with ? prefix) representing resources
    """
    resource_vars = set()

    # Pattern 1: Explicit Synapse URIs
    # VALUES ?entity { <https://synapse.org/syn123> }
    values_matches = re.finditer(
        r"VALUES\s+(\?\w+)\s*\{[^}]*synapse\.org[^}]*\}",
        where_clause,
        re.IGNORECASE,
    )
    for match in values_matches:
        resource_vars.add(match.group(1))

    # Pattern 2: Subject variables with common resource names
    resource_names = [
        "file",
        "dataset",
        "entity",
        "resource",
        "folder",
        "project",
        "table",
    ]
    for name in resource_names:
        # Match ?file or ?files (plural)
        pattern = rf"\?{name}s?\b"
        if re.search(pattern, where_clause, re.IGNORECASE):
            # Find the exact variable name
            matches = re.finditer(pattern, where_clause, re.IGNORECASE)
            for match in matches:
                resource_vars.add(match.group(0))

    # Pattern 3: Variables in subject position with Synapse predicates
    # ?x :hasSynapseId ?id
    synapse_pred_matches = re.finditer(
        r"(\?\w+)\s+:?(?:hasSynapseId|synapseId|id)\s+",
        where_clause,
        re.IGNORECASE,
    )
    for match in synapse_pred_matches:
        resource_vars.add(match.group(1))

    return sorted(resource_vars)


def should_rewrite_query(query: str) -> bool:
    """
    Determine if query should be rewritten with governance filters.

    Heuristics:
    - Query references Synapse resources (URIs or common variable names)
    - Query is a SELECT (not ASK/CONSTRUCT/DESCRIBE)
    - Query doesn't already contain governance patterns

    Args:
        query: SPARQL query

    Returns:
        True if query should be rewritten, False otherwise
    """
    # Must be SELECT query
    if not re.search(r"\bSELECT\b", query, re.IGNORECASE):
        return False

    # Skip if already has governance filters (avoid double-rewriting)
    if "gov:hasACL" in query or "gov:principal" in query:
        return False

    # Check for Synapse resource indicators
    synapse_indicators = [
        r"synapse\.org",
        r"\?file\b",
        r"\?dataset\b",
        r"\?entity\b",
        r"\?resource\b",
        r"syn\d+",  # Synapse ID pattern
    ]

    for pattern in synapse_indicators:
        if re.search(pattern, query, re.IGNORECASE):
            return True

    return False
