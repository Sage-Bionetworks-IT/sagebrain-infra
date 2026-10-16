"""Shared server-side query service for Sage Brain.

Every SPARQL caller (POST /query, the query worker, the agent) goes through this package, so
query limits and the `sparql_query` audit log are enforced in one place (constitution, article V).
Request rate is not enforced here: API Gateway throttles HTTP callers, and each worker's reserved
concurrency bounds its own callers (spec 001 R-5).

Composition:
  validate_query  — length/type checks, no SERVICE          (POST /query, run_query)
  execute_query   — SigV4 Neptune call, timeout, audit log  (query worker, run_query),
                    scoped to each portal's live graph (graphs.live_graphs, spec 004)
  run_query       — validate + execute, for in-process callers (the agent)
"""
