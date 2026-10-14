# 003 — Plan

What gets built, not how: components and their responsibilities, phases and rollback.
File-level detail belongs in `tasks.md` and the code PRs.

## Approach
Run [Hindsight](https://github.com/vectorize-io/hindsight) locally on each computer and treat
its bank as a **build artifact**. It is rebuilt from sources, never edited by hand, and safe to
delete. No server is hosted centrally. Everyone's bank comes out alike because the build is
pinned in this repo (`tools/memory/bank.yaml`), repo content is read from each repo's default
branch, and every import replaces a document with its current content, so banks converge on the
sources.

Hindsight is passive: it has no connectors and only learns what is retained into it. So sync is
a pull-then-push loop outside it, run on a timer:

```
launchd (hourly) ──> ingest.py ──> gh api: default-branch docs/specs, PRs/issues updated since last sync
launchd (weekly) ──> claude -p /memory-sync ──> Atlassian MCP (SKG) ──> ingest.py --stdin
                     ingest.py ──> one document per item, stable ID ──> retain(update_mode=replace)
                                                                       └─ Hindsight worker: extraction (Bedrock)
                               ──> ~/.sagebrain-memory/state.json (last sync, hashes, errors)
```

Agents query the bank over MCP instead of reading `docs/memory/` files. The markdown bank stays
the curated, PR-reviewed layer and is one source among several: this repo, the depositor repos,
GitHub, then Jira. Relationships come from Hindsight's entity graph, made reliable by passing
explicit entities (`#N`, `SKG-N`, spec IDs, stack names) on every item.

## Components

**Local server.** `ghcr.io/vectorize-io/hindsight`, pinned by digest, in Docker with embedded
Postgres (pg0) on a named volume `hindsight-data`. API on `:8888`, UI on `:9999`, MCP at
`http://localhost:8888/mcp/sagebrain/`. One server per computer, left running, shared by every
participating repo.

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
- the repos to ingest, each with its default branch (`nf-osi/kg-pipeline` uses `develop`)
- the Jira projects (`SKG`)
- the cadences and caps in the spec's thresholds table

A version bump makes every computer rebuild at its next sync.

**Sources and their conventions** (FR-3, FR-8, FR-9, FR-16):

| Source (phase) | Path | Cadence | document_id | tags |
|---|---|---|---|---|
| `docs/memory`, specs: this repo (1) | script, default branch | hourly, full scan | `repo:<repo>/<path>#<slug>`, one per entry | `source:repo\|spec`, `kind:*`, `topic:*` |
| depositor repos' docs (1) | script, default branch | hourly, full scan | same | `source:repo`, `repo:<name>` |
| GitHub PRs/issues, all listed repos (1) | script (`gh`) | hourly, updated since last sync | `gh:<repo>/pr/N`, `gh:<repo>/issue/N` | `source:github` |
| Jira `SKG` (2) | skill, Atlassian MCP, headless | weekly | `jira:<KEY>` | `source:jira` |

Every source also runs a weekly reconcile: list the IDs it still has, delete documents it no
longer has (FR-14). Each import replaces the whole document with its current content
(`update_mode=replace`), so banks converge however often each computer syncs. LLM extraction
isn't byte-for-byte deterministic, so two banks hold the same documents and nearly the same
facts; the seed-question eval is what shows a bank is healthy.

Depositor repos, in join order (FR-4):

| Repo | Portal / owner | Default branch | Joins |
|---|---|---|---|
| `nf-osi/kg-pipeline` | NF, Anh | `develop` | Phase 1 (loads today; delta pipeline in #53) |
| `Sage-Bionetworks/sagebrain-tap` | TAP (reactome) | `main` | once spec 004's release keys land |
| `amp-als/kg-pipeline` | ALS, Ram | `main` | once it loads into Neptune |

All three are public, so `gh api` reads them with anyone's login.

**`tools/memory/`**
- `hindsight.sh install|up|down|rebuild|sync|check`. `install` (once per computer) starts the
  server and writes the launchd job. `rebuild` deletes the bank and `state.json` together and
  replays every source. `check` prints the fingerprint: config hash, image, model, document count
  per source, last sync and last error per source.
- `ingest.py` + `adapters/{markdown,github}.py`: each adapter yields
  `{content, document_id, tags, entities, timestamp, context}`; batched with
  `hindsight_client.retain_batch(..., retain_async=True)`. Before any retain it checks
  `aws sts get-caller-identity --profile sage-bedrock-sso` and, if that fails, records the error
  and exits (FR-15). `--stdin` takes items from the `memory-sync` skill under the same conventions.
- `entities.py`: the shared extractor. Its patterns are also documented in the skill so both
  ingestion paths agree.
- `session_start.sh`: the `SessionStart` hook. It rebuilds on a `bank.yaml` version change,
  starts a background sync if the last one is more than an hour old, and prints one line if the
  sync is paused for credentials or Jira hasn't synced in 14 days. It always exits 0; if the
  server is down it prints the README fallback.
- `eval.yaml` + `eval.py`: 15 seed questions with the entries that should come back, written
  before `ingest.py` (see the spec's success criteria for their mix).

**`memory-sync` skill.** Reads `SKG` through the Atlassian MCP server, since the last sync and
within `bank.yaml`'s scope, and pipes items to `ingest.py --stdin`. Run weekly by launchd as
`claude -p "/memory-sync"` with `--allowedTools` limited to the Atlassian read tools and that one
`Bash` command. Because IDs are stable, re-runs are idempotent.

**Claude Code plugin, published from this repo.** Every participating repo installs the same
plugin, which bundles:
- the MCP entry for the bank's endpoint;
- the `SessionStart` hook;
- the deny rules for Hindsight's retain and delete tools (FR-11);
- the `memory-sync` skill.

It must merge with a repo's existing `.claude/` (amp-als already has one). Each repo's
`CLAUDE.md` gets one line: "call `recall` (budget ~1–2k tokens) at the start of a task and again
only on a change of topic; if the server is down, read `docs/memory/README.md`."

**Mental models** (answers kept up to date in the background, no retrieval at read time):
"infra changes affecting depositors", "depositor changes affecting loading", "open work by
theme", "standing exceptions to the constitution", "prod vs dev drift".

## Phases (each independently mergeable)
- **Phase 0 — spike (no repo changes beyond notes).** Run the container against Bedrock with the
  SSO profile. Pick the model: a Haiku-class `us.` inference profile, possibly in the
  `bedrock/converse/<arn>` form, with `HINDSIGHT_API_RETAIN_MAX_COMPLETION_TOKENS` set within its
  output limit (Hindsight defaults to 64k). Confirm the model's Marketplace agreement in
  042467473451. Retain `decisions.md` by hand on two computers. **Go only if all hold:**
  - SSO credentials resolve inside the container without the
    `aws configure export-credentials` fallback;
  - a full rebuild of the Phase 1 sources takes ≤ 15 min and ≤ $2 of Bedrock per computer;
  - the two computers hold equal document counts;
  - the hand-built bank answers ≥ 13/15 of the seed questions.

  Otherwise stop and keep markdown only.
- **Phase 1 — this repo, NF, GitHub.** The seed-question eval first, then `bank.yaml`,
  `hindsight.sh` (with `install` and the launchd job), `ingest.py`, the adapters, the entity
  extractor, `state.json` and the reconcile, the session hook, the plugin, the `CLAUDE.md` line,
  and the spec 004 replay. TAP and ALS join afterwards as `bank.yaml` entries plus the plugin.
- **Phase 2 — Jira `SKG`, weekly.** The `memory-sync` skill, run headless by the weekly launchd
  job. Memory Defense is on before the first retain.
- **Phase 3 (optional) — restructure the markdown.** Give every `docs/memory` entry a stable
  heading/slug if splitting bullets proves brittle.

## Risks / rollback
- **Banks differ between computers.** Mitigations: the pinned `bank.yaml`, default-branch reads,
  replace-not-delta imports, version-triggered rebuilds, `check` fingerprints and the eval. What
  remains is the non-determinism in LLM extraction.
- **People can see different Jira content.** Scope only projects everyone using the bank can read
  (FR-13).
- **Secrets or PII from Jira** end up in a bank. Mitigations: allow-listed scope, Memory Defense,
  local-only server.
- **Rebuild cost** is repeated on every computer: one Bedrock extraction per item, per machine.
  It's small at this volume and the first full build on a new machine is a one-time cost. Phase
  0's gate bounds it.
- **Silent background failures.** Expired SSO sessions (8–12 h) and an expired Atlassian MCP
  login stop the scheduled syncs. Mitigations: the credential check before any retain, errors in
  `state.json`, the session hook's one-line warnings, and `check`.
- **Freshness.** Git and GitHub are at most an hour old while the computer is awake; Jira up to a
  week.
- **Depositors' access.** Data engineers need `sage-bedrock-sso` access, Docker, a Mac, and (for
  Jira) the Atlassian MCP server. Confirm this for each depositor team before their repo joins.
- **Stale facts.** Timestamps let recall prefer recent facts; the weekly reconcile deletes
  documents whose source item is gone.
- **Rollback:** uninstall the plugin and revert the `CLAUDE.md` line in each repo, and
  `hindsight.sh down` plus removing the launchd job on each computer. `docs/memory/` never stopped
  being complete, so nothing else changes.

## Open decisions
- Whether `CLAUDE.md` is slimmed and its reference sections moved into the bank. It is the larger
  always-loaded context (~410 lines vs ~340 in `docs/memory/`). Revisit only after the eval shows
  recall is reliable.
- **When to host a bank centrally.** Not now: it costs roughly $50–100/month (Hindsight on
  Fargate + RDS pgvector) plus the work of running it. The sketch is:
  - a VPN-only ALB like Graph Explorer, with Hindsight's API key in Secrets Manager;
  - a GitHub Action on merge that retains changed entries;
  - an EventBridge Scheduler Lambda that polls Jira with a service-account token.

  Reconsider when any of these appears:
  - Slack or Confluence becomes a source.
  - Someone needs Jira fresher than weekly.
  - Depositors can't run Docker, Bedrock or the Atlassian MCP server locally.
  - The eval shows banks drifting in ways that matter.
- Whether agents should ever retain directly (e.g. a gotcha found mid-task). Today the answer is
  no: it goes into a markdown PR.
