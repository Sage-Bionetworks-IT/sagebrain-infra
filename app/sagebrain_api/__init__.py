"""Sage Brain HTTP API (FastAPI), spec 001.

The HTTP layer only: body parsing, auth, CORS, size/time limits, access logs. Query and
question validation, limit values and rate admission come from `sagebrain_core`, the shared
server-side service (constitution, article V).
"""
