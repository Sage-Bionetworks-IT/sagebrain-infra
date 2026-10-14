# 003 — Agent memory on Hindsight

**Status:** Draft
**Owner:** Thomas Yu
**Contract changes:** none

## Problem
Infra knowledge reaches the people who depend on it only by word of mouth, and the links behind
it are prose. In order of weight:

- **The knowledge stays in this repo.** The repos that write knowledge-graph snapshots into the
  data bucket (`{portal}/{snapshot}/data/` + `manifest.ttl`) depend on infra decisions such as
  "only Turtle under `data/`", one named graph per snapshot, and the snapshot key format (date-only
  today; release-keyed tags such as `reactome/v97` in spec 004, #61). Their engineers learn that
  one of those changed only if someone tells them. The reverse holds too: infra learns about a
  depositor change that affects loading (e.g. the NF delta pipeline, nf-osi/kg-pipeline#53) only
  by hearing about it.
- **Relationships are prose.** A decision, the PR that made it, the open work it left, the gotcha
  it caused and the Jira ticket behind it are linked only by `#N` mentions an agent has to follow
  by hand. Knowledge that lives in Jira isn't reachable at all.
- **Cost per task.** The memory bank (`docs/memory/`, ~340 lines in 6 files) is markdown that
  agents read into context: the README index first, then whole files to find one or two entries.
  It is small today (~5k tokens if read whole), but the cost grows with the bank, and the rule
  "one line per entry" exists to keep it tolerable.

[Hindsight](https://github.com/vectorize-io/hindsight) (MIT) is an agent memory server. On
retain an LLM extracts facts and entities into an entity graph; recall runs semantic, BM25,
graph and temporal search in parallel and returns only the facts that fit a token budget. It
serves retain/recall/reflect over MCP, one endpoint per bank. It is passive: it has no source
connectors and never pulls from GitHub or Jira, so something outside it must push every item.

## User stories
- As a data engineer in a repo that deposits snapshots into the data bucket, I want my agent to
  know the current S3 layout, manifest contract and pipeline behaviour, and to see infra changes
  that affect me, without anyone having to tell me.
- As an infra maintainer, I want to recall depositor changes that affect loading (a new delta
  mode, a new portal, a new key format) so that a contract break is caught before the load fails.
- As an agent, I want to follow a decision to its PR, issue and Jira ticket so that I get the
  *why* without searching each system.
- As an agent starting a task, I want to recall the handful of facts relevant to it so that I
  don't read every memory file into context.
- As a developer, I want my bank to match my colleagues' banks, and to rebuild it with one command
  so that a stale or broken bank is never a problem.
- As a maintainer, I want curated memory to stay PR-reviewed so that wrong entries are caught in
  review, not discovered by an agent.

## Functional requirements
- **FR-1 Derived, disposable, per-computer bank.** The bank (`bank_id = sagebrain`) lives on
  each developer's computer. It is never hosted centrally and is never the source of truth. A
  rebuild deletes it, together with its sync state (FR-14), and replays every source. Sharing
  happens through the sources (git, GitHub, Jira), not through a shared server.
- **FR-2 Every bank is built the same way.** Everything that shapes a bank is pinned in one
  committed file, `tools/memory/bank.yaml`, with a `version`: the Hindsight image digest, the
  Bedrock model ID, the embedding and reranker models, the extraction settings, the bank mission,
  the directives and mental models, and the source scope (repos with their default branches, Jira
  projects). Changing `version` triggers a full rebuild on every computer at its next sync.
  `hindsight.sh check` prints a fingerprint (config hash, image, model, document count per
  source, last sync and last error per source), so two banks can be compared.
- **FR-3 Idempotent ingest that converges.** Every item has a stable `document_id`
  (`repo:<repo>/<path>#<slug>`, `gh:<repo>/pr/N`, `gh:<repo>/issue/N`, `jira:<KEY>`). Items are
  retained with `update_mode=replace` and their *current* content, never a delta. Re-running a
  source creates no duplicates and reprocesses only items whose content hash changed. Repo content
  is read from each repo's **default branch on GitHub** (`gh api`), never from a local working
  tree, so every computer ingests exactly what is merged and two computers that synced the same
  sources hold the same documents.
- **FR-4 Multi-repo.** One local server and bank serves every Sage Brain repo on that computer:
  this repo and the repos that deposit into the data bucket. The repo list is in `bank.yaml`, and
  every item carries a `repo:<name>` tag. A repo joins when its contract with infra is real (it
  loads, or is about to): first `nf-osi/kg-pipeline` (NF), then `Sage-Bionetworks/sagebrain-tap`
  (TAP, once spec 004's release keys land) and `amp-als/kg-pipeline` (ALS, once it loads into
  Neptune).
- **FR-5 Scheduled sync.** Syncing doesn't depend on a Claude session. A launchd job installed by
  `hindsight.sh install` runs the git/GitHub sync hourly and the Jira sync weekly. A computer that
  was asleep or off catches up at its next run. The `SessionStart` hook only starts a background
  sync if the last one is more than an hour old; it never blocks the session and always exits 0.
- **FR-6 Depositor view.** Infra entries that affect depositors (S3 layout, manifest contract,
  snapshot naming, load behaviour) are tagged `audience:depositor`. A mental model "infra changes
  affecting depositors" answers that question without retrieval.
- **FR-7 One item per entry.** Markdown memory is split per entry (bullet or heading), not per
  file, so recall returns an entry rather than a file.
- **FR-8 Explicit entity links.** Each item passes `entities` from a shared extractor (`#N`,
  `<owner>/<repo>#N`, `SKG-N`, `spec NNN D-n/R-n/FR-n`, `app-*-*` stack names, package and
  config-key names) and a `timestamp` (entry date or PR merge date). The same PR or ticket is one
  node across sources. This is linking only: reconciling Jira tickets with GitHub issues is out of
  scope.
- **FR-9 Tags for provenance.** Every item carries `source:<repo|github|spec|jira>`, `repo:<name>`
  where it applies, and for markdown entries `kind:<decision|gotcha|open-work|runbook|rule>` and
  `topic:<section>`. Recall can prefer curated (`source:repo`) facts.
- **FR-10 Two ingestion paths, one writer.** Repo, specs and GitHub are read by
  `tools/memory/ingest.py`. Jira is read by a Claude Code skill (`memory-sync`) through the
  Atlassian MCP server, run headless (`claude -p`) by the weekly launchd job; the skill hands the
  items to `ingest.py --stdin`, which is the only thing that retains. Repo code holds no Atlassian
  credentials. Both paths follow FR-3, FR-8 and FR-9.
- **FR-11 Read-only agents, enforced.** Normal sessions use only recall, reflect, document reads
  and mental models. Each participating repo's settings deny Hindsight's retain and delete MCP
  tools. The headless `memory-sync` run is limited by `--allowedTools` to the Atlassian read tools
  and `Bash(python tools/memory/ingest.py --stdin*)`.
- **FR-12 Fallback.** If the local server is down, agents fall back to `docs/memory/README.md`
  as today. The markdown bank stays complete and readable.
- **FR-13 Scoped sources.** Jira projects are an explicit allow-list in `bank.yaml` (`SKG` to
  start), chosen so everyone who uses the bank can read all of them. Hindsight's Memory Defense
  (secret/PII redaction) is on before any Jira item is retained.
- **FR-14 Sync state and reconcile.** Each source's last sync time, last error and per-item
  content hashes live in `~/.sagebrain-memory/state.json`, created and deleted with the bank.
  Markdown and specs are scanned in full every run (hashes skip unchanged entries); GitHub pulls
  items updated since the last sync. Weekly, every source lists the IDs it still has and
  documents it no longer has are deleted.
- **FR-15 Credentials fail fast.** Every sync checks `aws sts get-caller-identity --profile
  sage-bedrock-sso` first. If the SSO session has expired it retains nothing, records the error,
  and the session hook prints one line: "memory sync paused, run `aws sso login --profile
  sage-bedrock-sso`". The hook also warns when the last successful Jira sync is more than 14 days
  old.
- **FR-16 What a GitHub document holds.** A PR: title, description, state, merge date, linked
  issues and the review summary, not inline diff comments or the diff. An issue: title, body,
  state and comments. Open and closed. Capped at ~8k characters.

## Non-functional requirements / thresholds
| Threshold | Value | Declared in | Enforced in | Test |
|---|---|---|---|---|
| Recall budget per task | ~1–2k tokens (`max_tokens`) | `CLAUDE.md` instruction | agent | seed-question eval |
| Seed-question hit rate | ≥ 13/15 answered from recall alone | `tools/memory/eval.yaml` | — | `tools/memory/eval.py` |
| Git/GitHub sync cadence | hourly | `bank.yaml` | launchd job | install test |
| Session-hook catch-up | background sync if last > 1 h | `bank.yaml` | `SessionStart` hook | hook test |
| Jira sync cadence | weekly | `bank.yaml` | launchd job | install test |
| Stale Jira warning | last success > 14 days | `bank.yaml` | `SessionStart` hook | hook test |
| GitHub document size | ≤ ~8k characters | `bank.yaml` | `ingest.py` | adapter test |
| Phase 0 go/no-go | see plan | plan | — | Phase 0 spike |

## Deviations from current behaviour
- **D-1** Agents start a task by reading `docs/memory/README.md` → by calling `recall` once
  (again only on a change of topic; no auto-recall on every prompt). The README stays as the
  fallback (FR-12).

## Out of scope
- A central, shared or AWS-hosted Hindsight server (see the plan's open decisions for when to
  reconsider).
- Confluence and Slack as sources.
- Reconciling SKG Jira tickets with GitHub issues (done separately); the bank only links them
  (FR-8).
- Hindsight's own Claude Code plugin (auto-retain of conversations, auto-recall per prompt).
- Agents writing memory directly; new knowledge enters through a markdown PR or a source system.
- Linux scheduling (launchd only; a systemd user timer is a small addition if needed).
- Any production data, and any change to the deployed stacks or the API contract.
- Moving `CLAUDE.md` content into the bank (open decision in the plan).

## Success criteria
- Seed-question hit rate meets the threshold above. The 15 questions are written before
  `ingest.py` and include at least 3 depositor questions (asked from `nf-osi/kg-pipeline`), at
  least 3 that need a link across sources (e.g. "why was FastAPI dropped, and where is it
  tracked?"), one about a depositor change (nf-osi/kg-pipeline#53) and one freshness question.
- **Spec 004 replay.** A bank built from sources as of before spec 004's PR merged, then synced,
  recalls the release-keyed snapshot change from the NF repo with no manual step.
- Two computers built from the same `bank.yaml` print the same document counts in
  `hindsight.sh check` and both pass the seed-question eval.
- Tokens loaded into context for memory per task drop vs reading the README + files (measured on
  the seed questions).
