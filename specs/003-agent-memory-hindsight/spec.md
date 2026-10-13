# 003 — Agent memory on Hindsight

**Status:** Draft
**Owner:** Thomas Yu
**Contract changes:** none

## Problem
The memory bank (`docs/memory/`, ~330 lines in 6 files) is markdown that agents read into
context: the README index first, then whole files to find one or two entries. Three problems:

- **Cost per task.** Every task pays for files it mostly doesn't need, and the cost grows with
  the bank. The rule "one line per entry" exists to keep this tolerable.
- **Relationships are prose.** A decision, the PR that made it, the open work it left, the gotcha
  it caused and the Jira ticket behind it are linked only by `#N` mentions an agent has to follow
  by hand. Knowledge that lives in Jira and Confluence isn't reachable at all.
- **The knowledge stays in this repo.** The repos that write knowledge-graph snapshots into the
  data bucket (`{portal}/YYYY-MM-DD/data/` + `manifest.ttl`) depend on infra decisions such as
  "only Turtle under `data/`", one named graph per snapshot, and date-only snapshot tokens (#61).
  Their engineers learn that one of those changed only if someone tells them.

[Hindsight](https://github.com/vectorize-io/hindsight) (MIT) is an agent memory server. On
retain an LLM extracts facts and entities into an entity graph; recall runs semantic, BM25,
graph and temporal search in parallel and returns only the facts that fit a token budget. It
serves retain/recall/reflect over MCP, one endpoint per bank.

## User stories
- As an agent starting a task, I want to recall the handful of facts relevant to it so that I
  don't read every memory file into context.
- As an agent, I want to follow a decision to its PR, issue and Jira ticket so that I get the
  *why* without searching each system.
- As a data engineer in a repo that deposits snapshots into the data bucket, I want my agent to
  know the current S3 layout, manifest contract and pipeline behaviour, and to see infra changes
  that affect me, without anyone having to tell me.
- As a developer, I want my bank to match my colleagues' banks, and to rebuild it with one command
  so that a stale or broken bank is never a problem.
- As a maintainer, I want curated memory to stay PR-reviewed so that wrong entries are caught in
  review, not discovered by an agent.

## Functional requirements
- **FR-1 Derived, disposable, per-computer bank.** The bank (`bank_id = sagebrain`) lives on
  each developer's computer. It is never hosted centrally and is never the source of truth. A
  rebuild deletes it and replays every source. Sharing happens through the sources (git, GitHub,
  Jira, Confluence), not through a shared server.
- **FR-2 Every bank is built the same way.** Everything that shapes a bank is pinned in one
  committed file, `tools/memory/bank.yaml`, with a `version`: the Hindsight image digest, the
  Bedrock model ID, the embedding and reranker models, the extraction settings, the bank mission,
  the directives and mental models, and the source scope (repos, Jira projects, Confluence
  spaces). Changing `version` triggers a full rebuild on every computer at its next session.
  `hindsight.sh check` prints a fingerprint (config hash, image, model, document count per
  source, last sync per source), so two banks can be compared.
- **FR-3 Idempotent ingest that converges.** Every item has a stable `document_id`
  (`repo:<repo>/<path>#<slug>`, `gh:<repo>/pr/N`, `gh:<repo>/issue/N`, `jira:<KEY>`,
  `confluence:<page-id>`). Items are retained with `update_mode=replace` and their *current*
  content, never a delta. Re-running a source creates no duplicates and reprocesses only changed
  items, and two computers that synced the same sources hold the same documents whenever they synced.
- **FR-4 Multi-repo.** One local server and bank serves every Sage Brain repo on that computer:
  this repo and the repos that deposit into the data bucket. The repo list is in `bank.yaml`, and
  every item carries a `repo:<name>` tag. Each participating repo adds the same `.mcp.json` entry
  and session-start hook, and gets them from this repo.
- **FR-5 Sync cadence.** A Claude Code `SessionStart` hook syncs git and GitHub sources
  incrementally at every session, so merged infra PRs and memory edits arrive with no
  announcement. Jira and Confluence sync weekly: the same hook runs that sync when the last one
  is more than 7 days old, so a computer that was off catches up at its next session.
- **FR-6 Depositor view.** Infra entries that affect depositors (S3 layout, manifest contract,
  snapshot naming, load behaviour) are tagged `audience:depositor`. A mental model "infra changes
  affecting depositors" answers that question without retrieval.
- **FR-7 One item per entry.** Markdown memory is split per entry (bullet or heading), not per
  file, so recall returns an entry rather than a file.
- **FR-8 Explicit entity links.** Each item passes `entities` from a shared extractor (`#N`,
  `SKG-N`, `spec NNN D-n/R-n/FR-n`, `app-*-*` stack names, package and config-key names) and a
  `timestamp` (entry date or PR merge date). The same PR or ticket is one node across sources.
- **FR-9 Tags for provenance.** Every item carries `source:<repo|github|spec|jira|confluence>`,
  `repo:<name>` where it applies, and for markdown entries `kind:<decision|gotcha|open-work|runbook|rule>`
  and `topic:<section>`. Recall can prefer curated (`source:repo`) facts.
- **FR-10 Two ingestion paths, one convention.** Repo, GitHub and specs are ingested by a script
  (`tools/memory/ingest.py`). Jira and Confluence are ingested by a Claude Code skill
  (`memory-sync`) that reads through the Atlassian MCP server. Repo code holds no Atlassian
  credentials. Both paths follow FR-3, FR-8 and FR-9.
- **FR-11 Read-only agents.** Normal sessions use only recall, reflect, document reads and mental
  models. Only the ingest script and the `memory-sync` skill retain.
- **FR-12 Fallback.** If the local server is down, agents fall back to `docs/memory/README.md`
  as today. The markdown bank stays complete and readable.
- **FR-13 Scoped sources.** Jira projects and Confluence spaces are an explicit allow-list in
  `bank.yaml`, chosen so everyone who uses the bank can read all of them. Hindsight's Memory
  Defense (secret/PII redaction) is on before any Jira or Confluence item is retained.

## Non-functional requirements / thresholds
| Threshold | Value | Declared in | Enforced in | Test |
|---|---|---|---|---|
| Recall budget per task | ~1–2k tokens (`max_tokens`) | `CLAUDE.md` instruction | agent | seed-question eval |
| Seed-question hit rate | ≥ 13/15 answered from recall alone | `tools/memory/eval.yaml` | — | `tools/memory/eval.py` |
| Git/GitHub sync cadence | every session start | `bank.yaml` | `SessionStart` hook | hook test |
| Jira/Confluence sync cadence | weekly (last sync > 7 days) | `bank.yaml` | `SessionStart` hook | hook test |
| Full rebuild time | to be measured in Phase 0 | plan | — | Phase 0 spike |

## Deviations from current behaviour
- **D-1** Agents start a task by reading `docs/memory/README.md` → by calling `recall`. The README
  stays as the fallback (FR-12).

## Out of scope
- A central, shared or AWS-hosted Hindsight server (see the plan's open decisions for when to
  reconsider).
- Slack as a source.
- Agents writing memory directly; new knowledge enters through a markdown PR or a source system.
- Any production data, and any change to the deployed stacks or the API contract.
- Moving `CLAUDE.md` content into the bank (open decision in the plan).

## Success criteria
- Tokens loaded into context for memory per task drop vs reading the README + files (measured on
  the seed questions).
- Seed-question hit rate meets the threshold above, including questions that need a link across
  sources (e.g. "why was FastAPI dropped, and where is it tracked?").
- Two computers built from the same `bank.yaml` print the same document counts in
  `hindsight.sh check` and both pass the seed-question eval.
- From a depositor repo, after an infra PR changing the S3 contract merges, the agent's next
  session recalls that change with no manual step.
