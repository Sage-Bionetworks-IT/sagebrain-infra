"""Shared server-side query service for Sage Brain.

Every SPARQL caller (POST /query, the query worker, the agent) goes through this package, so
limits and the `sparql_query` audit log are enforced in one place (constitution, article V).

Composition:
  validate_query  — length/type checks                      (POST /query, run_query)
  admit           — global + per-principal token buckets    (run_query)
                    = admit_global + admit_principal       (HTTP: global before auth, principal after)
  execute_query   — SigV4 Neptune call, timeout, audit log  (query worker, run_query)
  run_query       — validate + admit + execute, for in-process callers (the agent)
"""
