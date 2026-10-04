# 001 — Data model

These shapes are pinned by `tests/unit/test_job_api_handlers.py`, and the workers depend on
them. They must not change as part of this feature.

## Query jobs table — `{api-stack}-query-jobs` (PK `job_id`, TTL attr `ttl`)
| Attribute | Written by | Notes |
|---|---|---|
| job_id | submit | uuid4 |
| status | submit → worker | `pending` → `running` → `complete` \| `error` |
| created_at | submit | epoch seconds (int) |
| ttl | submit | created_at + 86400 |
| results, content_type, duration_ms | worker | on `complete` |
| error | worker | on `error` |

## Agent jobs table — `{agent-stack}-jobs`
The same columns as the query jobs table, plus:
- `question`, written by submit
- `status_detail`, written by the worker while it runs
- `steps[]`, written by the worker. Each entry is `{type: tool_call|tool_result, tool, sparql|preview|error}`.
- `answer`, written by the worker when the job is `complete`

## SQS messages
- **Query queue:** `{job_id, query, source, source_ip, user_agent}`
- **Agent queue:**
  - **Legacy:** `{job_id, question, authorization}`. `authorization` is the caller's raw
    `Authorization` header, which the worker re-presents to `POST /query`.
  - **New (D-8):** `{job_id, question, user_id, source_ip}`. There is no credential. The agent
    calls `sagebrain_core.run_query(principal=user_id, source="agent", source_ip=source_ip)`.
  - **Cutover:** during the transition the agent worker must accept both shapes. If it sees
    `authorization` without `user_id`, the message was enqueued by the old `/ask` Lambda.
    The worker still runs the job through `run_query`, attributing it to
    `principal="legacy-apigw"`, because the old authorizer already authenticated it.

## `sparql_query` audit event (emitted by `run_query` for every caller)
`{event, job_id, query, query_length, source, principal, source_ip, user_agent, status_code,
duration_ms, timestamp}`. This is today's field set plus `principal`. Agent calls carry
`source: "agent"` and the agent job's `job_id`.

## FastAPI source-IP rule
`source_ip` is the **rightmost** `X-Forwarded-For` entry, which is the one the ALB appends.
It is never the client-supplied leftmost entry.
