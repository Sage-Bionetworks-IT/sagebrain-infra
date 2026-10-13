# 003 — Plan

What gets built, not how: components and their responsibilities, phases and rollback.
File-level detail belongs in `tasks.md` and the code PRs.

## Approach
Run [Hindsight](https://github.com/vectorize-io/hindsight) locally and treat its bank as a
**build artifact**: rebuilt from sources, never edited by hand, safe to delete. Agents query it
over MCP instead of reading `docs/memory/` files. The markdown bank stays the curated,
PR-reviewed layer and one source among several (repo, GitHub, specs, then Jira, Confluence,
Slack). Relationships come from Hindsight's entity graph, made reliable by passing explicit
entities (`#N`, `SKG-N`, spec IDs, stack names) on every item.

## Components

**Local server.** `ghcr.io/vectorize-io/hindsight` in Docker with embedded Postgres (pg0) on a
named volume `hindsight-data`. API on `:8888`, UI on `:9999`, MCP at
`http://localhost:8888/mcp/sagebrain-infra/`.

**LLM: Bedrock via the `sage-bedrock-sso` profile** (account 042467473451, role `BedrockClient`,
us-east-1). `HINDSIGHT_API_LLM_PROVIDER=bedrock`, `~/.aws` mounted read-only, `AWS_PROFILE` set.
Embeddings (`BAAI/bge-small-en-v1.5`) and the reranker stay on Hindsight's local defaults, so
nothing else leaves the laptop except extraction calls to Bedrock.

**Sources and their conventions** (FR-2, FR-4, FR-5):

| Source (phase) | Path | document_id | tags |
|---|---|---|---|
| repo memory (1) | script | `repo:docs/memory/<file>#<slug>`, one per entry | `source:repo`, `kind:*`, `topic:*` |
| GitHub PRs/issues (1) | script (`gh`) | `gh:pr/N`, `gh:issue/N` | `source:github` |
| specs (1) | script | `repo:specs/NNN/<file>#<D-n\|R-n\|FR-n>` | `source:spec`, `spec:NNN` |
| Jira (2) | skill, Atlassian MCP | `jira:<KEY>` | `source:jira` |
| Confluence (2) | skill, Atlassian MCP | `confluence:<page-id>` | `source:confluence` |
| Slack (3) | skill, Slack MCP | `slack:<channel>:<thread_ts>` (thread = document) | `source:slack` |

**`tools/memory/`**
- `hindsight.sh up|down|rebuild|sync` — container lifecycle. It runs a preflight
  `aws sts get-caller-identity --profile sage-bedrock-sso` and prints the SSO login hint if that
  fails. `rebuild` deletes the bank and replays every enabled source.
- `ingest.py` + `adapters/{repo,github,specs}.py` — each adapter yields
  `{content, document_id, tags, entities, timestamp, context}`; batched with
  `hindsight_client.retain_batch(..., retain_async=True)`.
- `entities.py` — the shared extractor; its patterns are also documented in the skill so both
  paths agree.
- `config.yaml` — enabled sources, Slack channel allow-list, Jira project keys, Confluence spaces.
- `eval.yaml` + `eval.py` — ~15 seed questions with the entries that should come back.

**`.claude/skills/memory-sync/`** — reads Jira, Confluence and Slack through their MCP servers
within the `config.yaml` scope and a since-date, then calls Hindsight's `sync_retain` with the
same conventions. Because IDs are stable, re-runs are idempotent.

**Agent wiring.**
- `.mcp.json` (project scope, committed) points to the bank's MCP endpoint.
- `CLAUDE.md`: "call `recall` (low budget, ~1–2k tokens) before a task; if the server is down,
  read `docs/memory/README.md`."
- Read-only use in normal sessions (FR-7).
- Bank config: a `mission` describing the repo, and the constitution's articles retained as
  `kind:rule` and registered as **directives**. `constitution.md` itself stays a file read when
  writing a spec.
- **Mental models** (answers kept up to date in the background, no retrieval at read time):
  "open work by theme", "standing exceptions to the constitution", "prod vs dev drift".

## Phases (each independently mergeable)
- **Phase 0 — spike (no repo changes beyond notes).** Run the container against Bedrock with the
  SSO profile. Confirm SSO credentials resolve inside the container (fallback: inject
  `aws configure export-credentials --profile sage-bedrock-sso --format env`). Pick the model: a
  Haiku-class `us.` inference profile, possibly in the `bedrock/converse/<arn>` form, with
  `HINDSIGHT_API_RETAIN_MAX_COMPLETION_TOKENS` set within its output limit (Hindsight defaults to
  64k). Confirm the model's Marketplace agreement in 042467473451. Retain `decisions.md` by hand
  and time it.
- **Phase 1 — repo, GitHub, specs.** `hindsight.sh`, `ingest.py`, the three adapters, the entity
  extractor, `.mcp.json`, the `CLAUDE.md` change, and the seed-question eval.
- **Phase 2 — Jira and Confluence.** The `memory-sync` skill via the Atlassian MCP server.
- **Phase 3 — Slack.** Extend the skill to the Slack MCP server, with the channel allow-list.
  Memory Defense is on before the first Slack retain.
- **Phase 4 (optional) — restructure the markdown.** Give every `docs/memory` entry a stable
  heading/slug if splitting bullets proves brittle.

## Risks / rollback
- **Secrets or PII from Slack/Jira** end up in the bank. Mitigations: allow-listed scope, Memory
  Defense, local-only server, no shared bank.
- **Rebuild cost** scales with source volume (one Bedrock extraction per item). Mitigations:
  incremental `replace` by `document_id`, async batches, a cheap extraction model.
- **Server down or stale.** Agents fall back to the README (FR-8); `rebuild` is one command.
- **Banks drift between developers.** Each is rebuilt from the same sources; the eval shows
  whether a bank is healthy.
- **Stale facts.** Timestamps let recall prefer recent facts; deleting a markdown entry deletes
  its document on the next sync. A source deletion (closed Slack thread, archived page) needs the
  skill to reconcile document IDs it no longer sees.
- **Rollback:** remove `.mcp.json` and revert the `CLAUDE.md` line. `docs/memory/` never stopped
  being complete, so nothing else changes.

## Open decisions
- Which Slack channels, Jira projects and Confluence spaces are in scope.
- Whether `CLAUDE.md` is slimmed and its reference sections moved into the bank. It is the larger
  always-loaded context (~400 lines vs ~330 in `docs/memory/`). Flagged here, not solved.
- Whether a shared, AWS-hosted bank is ever wanted. Signal: developers' banks diverge in ways
  that matter, or Slack ingest is too slow per laptop. Cost: Fargate + RDS pgvector plus auth
  in front of MCP.
- Whether agents should ever retain directly (e.g. a gotcha found mid-task). Today the answer is
  no: it goes into a markdown PR.
