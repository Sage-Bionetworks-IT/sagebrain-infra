# 003 — Agent memory on Hindsight

**Status:** Draft
**Owner:** Thomas Yu
**Contract changes:** none

## Problem
The memory bank (`docs/memory/`, ~330 lines in 6 files) is markdown that agents read into
context: the README index first, then whole files to find one or two entries. Two problems:

- **Cost per task.** Every task pays for files it mostly doesn't need, and the cost grows with
  the bank. The rule "one line per entry" exists to keep this tolerable.
- **Relationships are prose.** A decision, the PR that made it, the open work it left, the gotcha
  it caused and the Jira ticket behind it are linked only by `#N` mentions an agent has to follow
  by hand. Knowledge that lives in Jira, Confluence and Slack isn't reachable at all.

[Hindsight](https://github.com/vectorize-io/hindsight) (MIT) is an agent memory server. On
retain an LLM extracts facts and entities into an entity graph; recall runs semantic, BM25,
graph and temporal search in parallel and returns only the facts that fit a token budget. It
serves retain/recall/reflect over MCP, one endpoint per bank.

## User stories
- As an agent starting a task, I want to recall the handful of facts relevant to it so that I
  don't read every memory file into context.
- As an agent, I want to follow a decision to its PR, issue, Jira ticket and Slack thread so that
  I get the *why* without searching each system.
- As a developer, I want to rebuild my memory bank from its sources with one command so that a
  stale or broken bank is never a problem.
- As a maintainer, I want curated memory to stay PR-reviewed so that wrong entries are caught in
  review, not discovered by an agent.

## Functional requirements
- **FR-1 Derived, disposable bank.** The bank (`bank_id = sagebrain-infra`) is never the source
  of truth. A rebuild deletes it and replays every source; the result depends only on the sources.
- **FR-2 Idempotent, incremental ingest.** Every item has a stable `document_id`
  (`repo:<path>#<slug>`, `gh:pr/N`, `gh:issue/N`, `jira:<KEY>`, `confluence:<page-id>`,
  `slack:<channel>:<thread_ts>`) and is retained with `update_mode=replace`. Re-running a source
  creates no duplicates and reprocesses only changed items.
- **FR-3 One item per entry.** Markdown memory is split per entry (bullet or heading), not per
  file, so recall returns an entry rather than a file.
- **FR-4 Explicit entity links.** Each item passes `entities` from a shared extractor (`#N`,
  `SKG-N`, `spec NNN D-n/R-n/FR-n`, `app-*-*` stack names, package and config-key names) and a
  `timestamp` (entry date or PR merge date). The same PR or ticket is one node across sources.
- **FR-5 Tags for provenance.** Every item carries `source:<repo|github|spec|jira|confluence|slack>`
  and, for repo entries, `kind:<decision|gotcha|open-work|runbook|rule>` and `topic:<section>`.
  Recall can prefer curated (`source:repo`) facts.
- **FR-6 Two ingestion paths, one convention.** Repo, GitHub and specs are ingested by a script
  (`tools/memory/ingest.py`). Jira, Confluence and Slack are ingested by a Claude Code skill
  (`memory-sync`) that reads through their MCP servers. Repo code holds no Atlassian or Slack
  credentials. Both paths follow FR-2, FR-4 and FR-5.
- **FR-7 Read-only agents.** Normal sessions use only recall, reflect, document reads and mental
  models. Only the ingest script and the `memory-sync` skill retain.
- **FR-8 Fallback.** If the local server is down, agents fall back to `docs/memory/README.md`
  as today. The markdown bank stays complete and readable.
- **FR-9 Scoped sources.** Slack channels, Jira projects and Confluence spaces are an explicit
  allow-list in `tools/memory/config.yaml`. Hindsight's Memory Defense (secret/PII redaction) is
  on before any Slack or Jira item is retained.

## Non-functional requirements / thresholds
| Threshold | Value | Declared in | Enforced in | Test |
|---|---|---|---|---|
| Recall budget per task | ~1–2k tokens (`max_tokens`) | `CLAUDE.md` instruction | agent | seed-question eval |
| Seed-question hit rate | ≥ 13/15 answered from recall alone | `tools/memory/eval.yaml` | — | `tools/memory/eval.py` |
| Full rebuild time (repo + GitHub) | to be measured in Phase 0 | plan | — | Phase 0 spike |

## Deviations from current behaviour
- **D-1** Agents start a task by reading `docs/memory/README.md` → by calling `recall`. The README
  stays as the fallback (FR-8).

## Out of scope
- A shared or AWS-hosted Hindsight server. Each developer runs it locally in Docker.
- Agents writing memory directly; new knowledge enters through a markdown PR or a source system.
- Any production data, and any change to the deployed stacks or the API contract.
- Moving `CLAUDE.md` content into the bank (open decision in the plan).

## Success criteria
- Tokens loaded into context for memory per task drop vs reading the README + files (measured on
  the seed questions).
- Seed-question hit rate meets the threshold above, including questions that need a link across
  sources (e.g. "why was FastAPI dropped, and where is it tracked?").
- A rebuild from scratch produces the same document set twice in a row.
