# 006 — Agent-ready telemetry: curated OpenTelemetry traces to SigNoz

**Status:** Draft
**Owner:** Thomas Yu
**Contract changes:** none. `api/openapi.yaml` is untouched; the job item's new `trace_id` is
never returned.
**Interacts with:** spec 005 (NIST SP 800-171). SigNoz Cloud becomes an external system, and this
spec's allowlist is the control for what reaches it (N43).

## Problem
We want autonomous agents to operate Sage Brain. Examples: explain one failed job, find why the
error rate rose, confirm today's snapshot loaded. Today they have nothing good to read:

1. **No correlation.** No trace ids exist anywhere. One `/ask` job leaves separate lines in four
   log groups:
   - `question_submitted` in `jobs.py`;
   - `agent_invocation` in `agent.py`;
   - `sparql_query`, once per tool call, from `sagebrain_core.neptune`;
   - Lambda platform lines.

   The only thing joining them is `job_id`, and nothing joins the SQS hop or Bedrock.
2. **Raw logs are noisy and sensitive.** An agent searching CloudWatch wades through START/END/
   REPORT lines and access logs. The useful events carry full query text, question text, source
   IPs and Synapse ids. That's fine in the account, but not for a third-party store.
3. **No agent-facing surface.** The team already pays for one SigNoz Cloud instance
   (`sagebionetworks.us.signoz.cloud`). Its hosted MCP server (`mcp.us.signoz.cloud`) gives agents
   trace search, trace detail and aggregation. Nothing of ours reaches it.

## Approach
Our own code emits **OpenTelemetry spans**, one trace per job, from submit through the worker to
Neptune and Bedrock. Traces are exported over OTLP to SigNoz Cloud through an **allowlist
exporter**. Any attribute whose key isn't in a reviewed schema is dropped before it leaves the
Lambda. Agents read traces through the SigNoz MCP.

CloudWatch is unchanged as the system of record, the audit log and the source of alarms. The
audit events gain `trace_id`, so an agent can go from a trace to the in-account line (via the AWS
`cloudwatch-mcp-server`, read-only) when it needs detail that never leaves the account.

## User stories
- As an **operator agent**, given a `job_id` I find its trace, see every step and its timing, and
  name the failing component and error class.
- As an **on-call agent**, I break errors and latency down by component, error class and source
  over a time window, without reading raw logs.
- As a **pipeline agent**, I see whether a portal's snapshot loaded, how long it took, and its
  record and error counts.
- As an **assessor**, I can verify in code and tests that only allowlisted, non-content fields
  leave the account.
- As an **operator**, I turn telemetry off per env with one config value, and a SigNoz outage
  never fails or slows a job beyond a bounded export timeout.

## Functional requirements

### Redaction
- **FR-1 Allowlist exporter.** `sagebrain_core.telemetry` wraps the OTLP exporter. Before export
  it does the following:
  - It drops every span, resource and event attribute whose key isn't in the schema (FR-2).
  - It drops every span event except `exception`, and keeps only `exception.type` from it.
    Exception messages can quote query text.
  - It replaces the span status description with the exception type.
  - It fails closed: an unknown key is dropped, never passed through.

  Test: a span that carries every denied key, and several unknown ones, exports none of them.
- **FR-2 The schema is the telemetry contract.** It lives in one module,
  `core/sagebrain_core/telemetry_schema.py`. The table below mirrors it, and a test asserts the
  two are equal, as the `x-sagebrain-limits` test does. Adding a key is a spec change.

  | Group | Keys |
  |---|---|
  | Resource | `service.name`, `service.namespace`, `service.version`, `deployment.environment`, `cloud.region`, `faas.name` |
  | Invocation | `faas.trigger`, `faas.coldstart`, `faas.invocation_id`, `messaging.system`, `messaging.operation` |
  | Job | `sagebrain.job.id`, `sagebrain.job.kind` (`query`, `ask`), `sagebrain.job.outcome` (`complete`, `error`, `rejected`), `sagebrain.error.class`, `sagebrain.error.code`, `sagebrain.source` (Q-2), `sagebrain.principal.hmac` (Q-1) |
  | Query | `sagebrain.query.hash` (SHA-256 of the text), `sagebrain.query.length`, `sagebrain.query.form` (`SELECT`, `ASK`, …), `sagebrain.neptune.status_code`, `sagebrain.neptune.timeout`, `sagebrain.result.bytes` |
  | Agent | `gen_ai.system`, `gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.usage.*` (token counts), `gen_ai.tool.name`, `gen_ai.tool.call.id`, `tool.status`, `event_loop.cycle_id`, `sagebrain.agent.step_count`, `sagebrain.agent.retries` |
  | Load | `sagebrain.portal`, `sagebrain.snapshot`, `sagebrain.load.id`, `sagebrain.load.action`, `sagebrain.load.status`, `sagebrain.load.records`, `sagebrain.load.parse_errors`, `sagebrain.load.errors`, `sagebrain.sfn.execution` |

  **Denied by name, and tested as absent:**
  - query text, question text, answers, tool inputs and results;
  - `gen_ai.user.message`, `gen_ai.assistant.message`, `gen_ai.choice*`, `system_prompt`;
  - source IP, user agent, raw `user_id`, exception messages.

  Strands records the prompt, the completion and tool results as span attributes, with no off
  switch. FR-1 is what makes the agent's spans safe to export.
- **FR-3 Static span names.** Span names never carry request data. A test asserts every span name
  our code creates is a literal. Strands' names (`invoke_agent <agent name>`,
  `execute_event_loop_cycle`, `chat`, `execute_tool <tool name>`) are built only from the agent
  name and tool names, which we set.

### Correlation
- **FR-4 Trace per job.**
  - Submit (`jobs.py`) starts the trace context and sends `traceparent` as an SQS
    MessageAttribute.
  - The query and agent workers continue it as a consumer span.
  - `execute_query` adds a `neptune.query` child span, so the agent's tool calls appear under its
    Strands tool spans.
  - Submit exports no span of its own in this spec (see Out of scope). The worker span is the
    first exported span of the trace.
- **FR-5 Pivot keys.**
  - `trace_id` is added to the job's DynamoDB item. It is not returned: the status handlers'
    responses are unchanged, and `tests/contract/cases.yaml` holds them to that.
  - `trace_id` is also added to the `query_submitted`, `question_submitted`, `sparql_query`,
    `agent_invocation` and `kg_load` events.
  - Every worker span carries `sagebrain.job.id`, and every loader span carries
    `sagebrain.snapshot`.
- **FR-6 Loader traces.** One trace per Step Functions execution. The state machine passes
  `$$.Execution.Id` into each `start`, `check` and `record` payload. The loader derives the trace
  id from it deterministically, so each step joins the same trace without any propagation.

### Operation
- **FR-7 Fail open, bounded.**
  - Exporter errors are swallowed and logged as one JSON warning (stdout stays JSON-only). The job
    outcome never changes.
  - Workers `force_flush` in `finally`, because Lambda freezes after return. The flush is bounded
    by `export_timeout_seconds`.
- **FR-8 Kill switch.** `TELEMETRY.enabled` per env. When it's off, the handlers run under a no-op
  provider, with no export and no secret read.
- **FR-9 Ingestion key.**
  - It is stored as the Secrets Manager secret `sagebrain/{env}/signoz-ingestion-key`, in the form
    `{"ingestion_key": …}` (plus `principal_hmac_key` if Q-1 picks the HMAC).
  - It is read once per cold start, and each traced Lambda's role allows `GetSecretValue` on that
    secret only.
  - It never appears in config, env vars or logs (constitution IV).
- **FR-10 Agent access.** `docs/memory/runbooks/agent-triage.md` covers:
  - the MCP setup (OAuth for people; a viewer-role API key for a headless agent, never one that
    can write alerts or dashboards);
  - the investigation recipes: job → trace, error spike → `signoz_aggregate_traces` by
    `sagebrain.error.class`, snapshot → load trace, and trace → CloudWatch line by `trace_id`.

## Non-functional requirements / thresholds
| Threshold | Value | Declared in | Enforced in | Test |
|---|---|---|---|---|
| Export / flush timeout | 2 s | `TELEMETRY.export_timeout_seconds` | `sagebrain_core.telemetry` | an exporter that hangs returns within the bound |
| Head sampling ratio | 1.0 | `TELEMETRY.sample_ratio` | provider sampler | stack env + provider test |
| Span queue size | 2048 | `TELEMETRY.max_queue_size` | batch processor | provider test |
| Ingest endpoint | `ingest.us.signoz.cloud` | `TELEMETRY.ingest_host` | exporter | stack env test |

**Scaling path:** sampling starts at 100 %, because a job is about 5–30 spans and current volume
is small. Signal: SigNoz ingest for `service.namespace=sagebrain` passes the budget set in Phase 6.
Cost: lowering `sample_ratio` drops whole traces (head sampling), so an agent may find no trace
for a given job, and the CloudWatch pivot still works.

## Deviations from current behaviour
| # | Today | New | Why |
|---|---|---|---|
| D-1 | The job SQS message has no attributes | It carries `traceparent` | FR-4 |
| D-2 | The job item has no `trace_id` | It has one, not returned by the API | FR-5 |
| D-3 | Audit events have no `trace_id` | They add `trace_id` | FR-5. The exact key-set tests (`core/test_neptune.py`, `test_agent_worker.py`) change with it |
| D-4 | No egress to SigNoz | The query, agent and loader workers call `ingest.us.signoz.cloud:443` (through NAT) | FR-4 |

Each deviation is recorded with a test of the old behaviour first (constitution II).

## Constitution notes
- **IV:** the key lives in Secrets Manager, is read at runtime, and is scoped by IAM to one secret.
- **V:** tracing wraps `execute_query` and doesn't change it. Validation, timeout and the
  `sparql_query` log are untouched apart from `trace_id`.
- **Spec 005:**
  - SigNoz Cloud joins the external-systems list.
  - Only allowlisted, non-content fields reach it (FR-1, FR-2). So spec 005's Q-4 (whether
    query and question text in logs is CUI) doesn't block dev.
  - Prod enablement still needs its own decisions.md entry.
  - The SigNoz tenant is shared by every Sage team, so any SigNoz user can see our traces. The
    allowlist is what makes that acceptable.

## Rejected alternatives
- **Export every CloudWatch log group to SigNoz** (built and parked on local branch
  `signoz-log-export-parked`): it's noisy, has no correlation, and ships content and identifiers.
- **Amazon Managed Grafana over CloudWatch:** it keeps data in place but adds no correlation. It
  is also a new service, and its dashboards sit outside IaC.
- **SigNoz's one-click AWS integration:** it creates and recreates its own CloudFormation stacks
  outside our IaC and CI.

## Out of scope
- **Running a headless triage agent.** This spec makes the data and the runbook ready for one.
- **Spans from the edge handlers and the authorizer.** A synchronous flush would add export
  latency to every API request (Q-3).
- **Alerting in SigNoz.** Alarms stay in CloudWatch and email `ALARM_EMAIL`.
- **Exporting raw logs.**

## Open decisions
- **Q-1 Principal.**
  - Option A: `sagebrain.principal.hmac`, an HMAC-SHA256 under a per-env key from the secret,
    truncated. An agent can see "one caller is driving the errors" without learning who.
  - Option B: omit the principal entirely.

  A plain hash is out, because Synapse ids are small integers and enumerable.
- **Q-2 `X-Source`.** It's caller-supplied free text (≤ 64 chars). Options: export it as
  `sagebrain.source`, map it to a fixed set (`direct`, `agent`, `other`), or drop it.
- **Q-3 Edge and authorizer spans.** Options:
  - never;
  - later, through the OpenTelemetry Lambda collector extension, which exports after the response;
  - now, with a synchronous flush that costs latency on every request.
- **Q-4 CloudWatch metrics to SigNoz.** Should a metric stream (AWS/Lambda, SQS, DynamoDB,
  Neptune and so on) send metrics to SigNoz through Firehose? It would show agents saturation and
  DLQ depth next to traces, and it carries no content. The parked branch has it built.
- **Q-5 Exporter packaging.**
  - Option A: the OTel SDK plus `opentelemetry-exporter-otlp-proto-http` in the core layer. It
    would use pure-Python protobuf, so one layer serves x86 and ARM, with versions pinned to what
    Strands bundles and a test for the pin. The agent bundles its own SDK, which shadows the
    layer's.
  - Option B: a small in-house OTLP/JSON exporter with no protobuf.

## Success criteria
- Unit tests for FR-1 to FR-9 pass, including the denied-key and unknown-key tests, and
  `cdk synth` passes for dev and prod.
- In dev, for one `/query` job and one `/ask` job:
  - SigNoz shows one trace each: the worker span, `neptune.query` children and, for `/ask`, the
    Strands model and tool spans with token counts.
  - A SigNoz search for any denied key, or for the job's query text, returns nothing.
- A Claude Code session with only the SigNoz MCP, given the `job_id` of a deliberately failed
  job, names the failing component and error class.
- With `TELEMETRY.enabled: false`, synth shows no SigNoz egress config, and jobs behave exactly as
  today.
