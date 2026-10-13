# 003 — Plan

What gets built, not how: components and their responsibilities, phases and rollback.
File-level detail belongs in `tasks.md` and the code PRs.

## Approach
Run [Hindsight](https://github.com/vectorize-io/hindsight) locally on each computer and treat
its bank as a **build artifact**. It is rebuilt from sources, never edited by hand, and safe to
delete. No server is hosted centrally. Everyone's bank comes out alike because the build is
pinned in this repo (`tools/memory/bank.yaml`) and every import replaces a document with its
current content, so banks converge on the sources.

Agents query the bank over MCP instead of reading `docs/memory/` files. The markdown bank stays
the curated, PR-reviewed layer and is one source among several: this repo, the depositor repos,
GitHub, then Jira and Confluence. Relationships come from Hindsight's entity graph, made
reliable by passing explicit entities (`#N`, `SKG-N`, spec IDs, stack names) on every item.

## Components

**Local server.** `ghcr.io/vectorize-io/hindsight`, pinned by digest, in Docker with embedded
Postgres (pg0) on a named volume `hindsight-data`. API on `:8888`, UI on `:9999`, MCP at
`http://localhost:8888/mcp/sagebrain/`. There is one server per computer, shared by every
participating repo checked out on it.

**LLM: Bedrock via the `sage-bedrock-sso` profile** (account 042467473451, role `BedrockClient`,
us-east-1). `HINDSIGHT_API_LLM_PROVIDER=bedrock`, `~/.aws` mounted read-only, `AWS_PROFILE` set.
Embeddings (`BAAI/bge-small-en-v1.5`) and the reranker stay on Hindsight's local defaults, so
only extraction calls go to Bedrock.

**`tools/memory/bank.yaml`, the build definition** (FR-2). It holds:
- `version`
- the image digest
- the model ID
- the embedding and reranker models
- the extraction settings
- the mission
- the directives (the constitution's articles)
- the mental models
- the repos to ingest
- the Jira projects and Confluence spaces

A version bump makes every computer rebuild at its next session.

**Sources and their conventions** (FR-3, FR-8, FR-9):

| Source (phase) | Path | Cadence | document_id | tags |
|---|---|---|---|---|
| `docs/memory`, specs: this repo (1) | script | every session | `repo:<repo>/<path>#<slug>`, one per entry | `source:repo\|spec`, `kind:*`, `topic:*` |
| depositor repos' docs (1) | script | every session | same | `source:repo`, `repo:<name>` |
| GitHub PRs/issues, all listed repos (1) | script (`gh`) | every session | `gh:<repo>/pr/N`, `gh:<repo>/issue/N` | `source:github` |
| Jira (2) | skill, Atlassian MCP | weekly | `jira:<KEY>` | `source:jira` |
| Confluence (2) | skill, Atlassian MCP | weekly | `confluence:<page-id>` | `source:confluence` |

Each import replaces the whole document with its current content (`update_mode=replace`).
Banks therefore converge however often each computer syncs. LLM extraction isn't byte-for-byte
deterministic, so two banks hold the same documents and nearly the same facts; the
seed-question eval is what shows a bank is healthy.

**`tools/memory/`**
- `hindsight.sh up|down|rebuild|sync|check`. It runs a preflight
  `aws sts get-caller-identity --profile sage-bedrock-sso` and prints the SSO login hint if that
  fails. `rebuild` deletes the bank and replays every source. `check` prints the fingerprint:
  config hash, image, model, document count per source, last sync per source.
- `ingest.py` + `adapters/{markdown,github}.py` — each adapter yields
  `{content, document_id, tags, entities, timestamp, context}`; batched with
  `hindsight_client.retain_batch(..., retain_async=True)`. Depositor repos are read from a local
  checkout if one exists, otherwise via `gh`.
- `entities.py` — the shared extractor. Its patterns are also documented in the skill so both
  ingestion paths agree.
- `session_start.sh` — the `SessionStart` hook. It starts the container if it's down, rebuilds
  on a `bank.yaml` version change, syncs git/GitHub incrementally, and runs or prompts
  `/memory-sync` when the last Jira/Confluence sync is more than 7 days old. It never blocks the
  session: on any failure it prints the README fallback.
- `eval.yaml` + `eval.py` — ~15 seed questions with the entries that should come back, including
  depositor questions ("what changed in the S3 contract?").

**`.claude/skills/memory-sync/`** — reads Jira and Confluence through the Atlassian MCP server,
within `bank.yaml`'s scope and since the last sync, then calls Hindsight's `sync_retain` with
the same conventions. Because IDs are stable, re-runs are idempotent.

**Agent wiring, per participating repo.**
- `.mcp.json` points to the bank's MCP endpoint.
- `.claude/settings.json` registers `session_start.sh` as the `SessionStart` hook.
- `CLAUDE.md`: "call `recall` (low budget, ~1–2k tokens) before a task; if the server is down,
  read `docs/memory/README.md`."
- Depositor repos get these three from this repo: a copy step in `hindsight.sh install <repo>`,
  or the tooling packaged as a Claude Code plugin. Which one is an open decision.
- Read-only use in normal sessions (FR-11).
- **Mental models** (answers kept up to date in the background, no retrieval at read time):
  "infra changes affecting depositors", "open work by theme", "standing exceptions to the
  constitution", "prod vs dev drift".

## Phases (each independently mergeable)
- **Phase 0 — spike (no repo changes beyond notes).** Run the container against Bedrock with the
  SSO profile. Confirm SSO credentials resolve inside the container (fallback: inject
  `aws configure export-credentials --profile sage-bedrock-sso --format env`). Pick the model: a
  Haiku-class `us.` inference profile, possibly in the `bedrock/converse/<arn>` form, with
  `HINDSIGHT_API_RETAIN_MAX_COMPLETION_TOKENS` set within its output limit (Hindsight defaults to
  64k). Confirm the model's Marketplace agreement in 042467473451. Retain `decisions.md` by hand,
  time it, and retain it again on a second computer to see how close the two banks come out.
- **Phase 1 — this repo, depositor repos, GitHub.** `bank.yaml`, `hindsight.sh`, `ingest.py`, the
  adapters, the entity extractor, the session-start hook, `.mcp.json`, the `CLAUDE.md` change,
  the install step for depositor repos, and the seed-question eval.
- **Phase 2 — Jira and Confluence, weekly.** The `memory-sync` skill via the Atlassian MCP
  server, triggered by the hook's 7-day check. Memory Defense is on before the first retain.
- **Phase 3 (optional) — restructure the markdown.** Give every `docs/memory` entry a stable
  heading/slug if splitting bullets proves brittle.

## Risks / rollback
- **Banks differ between computers.** Mitigations: the pinned `bank.yaml`, replace-not-delta
  imports, version-triggered rebuilds, `check` fingerprints and the eval. What remains is the
  non-determinism in LLM extraction.
- **People can see different Jira/Confluence content.** Scope only projects and spaces everyone
  using the bank can read (FR-13).
- **Secrets or PII from Jira/Confluence** end up in a bank. Mitigations: allow-listed scope,
  Memory Defense, local-only server.
- **Rebuild cost** is repeated on every computer: one Bedrock extraction per item, per machine.
  It's small at this volume (weekly Jira/Confluence deltas, tens of items), and the first full
  build on a new machine is a one-time cost. Phase 0 measures it.
- **Freshness.** Jira/Confluence knowledge can be up to a week old. Git and GitHub, which carry
  the infra changes depositors most need, are current as of each session.
- **Depositors' access.** Data engineers need `sage-bedrock-sso` access, Docker and the Atlassian
  MCP server. Confirm this for each depositor team before Phase 1 rolls out to their repo.
- **Stale facts.** Timestamps let recall prefer recent facts. Deleting a markdown entry deletes
  its document on the next sync. A source deletion (an archived page or deleted ticket) needs the
  skill to reconcile the document IDs it no longer sees.
- **Rollback:** remove `.mcp.json` and the hook, and revert the `CLAUDE.md` line in each repo.
  `docs/memory/` never stopped being complete, so nothing else changes.

## Open decisions
- Which repos deposit into the data bucket and join the bank, and which Jira projects and
  Confluence spaces are in scope.
- How depositor repos get the tooling: a copy step (`hindsight.sh install`) or a Claude Code
  plugin installed from this repo.
- Whether `CLAUDE.md` is slimmed and its reference sections moved into the bank. It is the larger
  always-loaded context (~400 lines vs ~330 in `docs/memory/`). Flagged here, not solved.
- **When to host a bank centrally.** Not now: it costs roughly $50–100/month (Hindsight on
  Fargate + RDS pgvector) plus the work of running it. The sketch is:
  - a VPN-only ALB like Graph Explorer, with Hindsight's API key in Secrets Manager;
  - a GitHub Action on merge that retains changed entries;
  - an EventBridge Scheduler Lambda that polls Jira and Confluence with service-account tokens.

  Reconsider when any of these appears:
  - Slack becomes a source.
  - Someone needs Jira/Confluence fresher than weekly.
  - Depositors can't run Docker, Bedrock or the Atlassian MCP server locally.
  - The eval shows banks drifting in ways that matter.
- Whether agents should ever retain directly (e.g. a gotcha found mid-task). Today the answer is
  no: it goes into a markdown PR.
