# Memory bank

Durable context for agents and humans working in this repo: **why** things are the way they
are, what is still undecided, and procedures that are hard to rediscover. Read this index at
the start of a task; open only the files whose hook matches it.

| File | Read it when… |
|---|---|
| [decisions.md](decisions.md) | you are about to change architecture, ingestion, auth or the query path — what was decided, in which PR, and why |
| [open-work.md](open-work.md) | you are picking up work or wondering "is this known?" — open threads grouped by theme, with issue numbers |
| [gotchas.md](gotchas.md) | something fails in a way that looks environmental (IAM, networking, timeouts, tooling) |
| [runbooks/direct-neptune-access.md](runbooks/direct-neptune-access.md) | you need to read/delete in Neptune directly (outside `/query`), e.g. drop a graph or debug a failed load |

## Rules (keep the maintenance cost near zero)

1. **Store only what the repo can't tell you.** No code structure, no file listings, no copies of
   `CLAUDE.md`. Git, `gh`, and the code are the source of truth for *what*; this is for *why* and
   *how-we-learned-it*.
2. **Link, don't mirror.** Reference PRs/issues as `#N`; never record their status (open/closed,
   assignee, labels) — it goes stale. Check live with `gh issue view N` / `gh pr view N`.
3. **Edit in the same PR that changes the fact.** If a PR reverses a decision or closes an open
   thread, update or delete its entry here in that PR. Wrong entries get deleted, not annotated.
4. **One line per entry where possible**, with an `(YYYY-MM-DD)` date on anything time-sensitive.
   If an entry needs more than a paragraph, it is a runbook — give it its own file under `runbooks/`.
5. **Add an entry when you lost >15 minutes** to something a future reader would also hit.
