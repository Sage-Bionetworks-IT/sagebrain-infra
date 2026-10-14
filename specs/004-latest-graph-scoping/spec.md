# 004 — Latest-snapshot query scoping, release-keyed snapshots, invisible failed loads

**Status:** Draft
**Owner:** Thomas Yu
**Issues:** #61 (release-keyed snapshots), #51 (failed loads stay live). Retention and deletion of
old graphs: #88 (out of scope here).
**Contract changes:** `api/openapi.yaml`: `POST /query` and `POST /ask` descriptions (results are
scoped to the latest published snapshot of each portal), a new `POST /query` 400 error for
`SERVICE`, and a new `x-sagebrain-limits` entry `live_graphs_cache_seconds`.

## Problem
Each snapshot loads into its own named graph (`urn:sagebrain:{portal}:{date}`), and Neptune's
default graph is the **union of every named graph**. So today:

1. **Every query reads every snapshot at once.** Old snapshots show up as stale extra triples (the
   union de-duplicates, so they don't double-count), and nothing tells a caller which version
   they're reading. "Scope queries to the latest snapshot per portal" has been the main open
   correctness gap since the pipeline shipped (`docs/memory/open-work.md`).
2. **Failed and in-progress loads are live** (#51). With `failOnError=TRUE` the loader stops, but
   triples loaded up to that point stay in the union. A re-publish of the same date merges into
   the stale graph.
3. **Release-keyed sources can't load** (#61). `parse_snapshot` only accepts a `YYYY-MM-DD` token,
   so `reactome/v97/manifest.ttl` fails as `BadManifestKey` and trips the pipeline alarm.

Callers could scope a query with `FROM`/`GRAPH` themselves, but they would have to know each
portal's current graph URI, and nothing would stop them reading older ones.

## Approach
Neptune stays **append-only**: every snapshot keeps its own named graph, and nothing is deleted
for correctness. The pipeline records each portal's **live** graph in a registry. The shared query
service sends that list as the SPARQL 1.1 Protocol dataset parameters, **`default-graph-uri` and
`named-graph-uri`**, on every query. Neptune honors both, and they override any dataset clause
in the query ([research.md](research.md)). Readers see only the latest published snapshot of
each portal. Failed, in-progress, superseded and untracked graphs are invisible.

## User stories
- As a **researcher**, my `/query` and `/ask` results come from the latest published snapshot of
  each portal, without my needing to know graph URIs or dates.
- As a **data depositor**, I publish a release-keyed source (`reactome/v97`) the same way as a
  dated one (`nf/2026-10-14`).
- As a **data depositor**, if my load fails, nobody sees the partial data. I fix `data/` in the
  same folder, re-upload `manifest.ttl`, and it loads cleanly.
- As an **operator**, I roll a portal back to an earlier snapshot by re-uploading that folder's
  `manifest.ttl`. It goes live at once without reloading.
- As an **operator**, every `sparql_query` audit line records which graphs the query could see.

## Functional requirements

### Snapshot keys (#61)
- **FR-1 Tag token.** The manifest key is `{portal}/{tag}/manifest.ttl`, where `tag` matches
  `^(\d{4}-\d{2}-\d{2}|v[0-9][0-9A-Za-z.\-]*)$`. Any other shape raises, so the execution ends in
  `BadManifestKey`, as today. A portal starting with `_` is rejected (that prefix is reserved for
  the registry).
- **FR-2 Graph URI.** `urn:sagebrain:{portal}:{tag}`, from `NEPTUNE_PIPELINE.graph_uri_template`
  (the placeholder is renamed `{date}` → `{tag}`).
- **FR-3 Tags are immutable once published.** A correction to published data goes under a new
  tag (the next date, or a new version). There is no suffix format for same-date corrections.

### Load lifecycle (#51)
- **FR-4 Arrival order.** Each execution records `released_at` = the Step Functions execution
  start time (ISO-8601). Of a portal's published snapshots, the one with the latest
  `released_at` is live.
- **FR-5 Duplicate delivery.** An S3 event whose `object.sequencer` matches the tracking row is a
  duplicate and is skipped. A re-upload is a new PUT, so it has a new sequencer, even when the
  bytes and etag are identical.
- **FR-6 Publish.** After `LOAD_COMPLETED`, the pipeline conditionally writes the portal's
  registry entry (only if absent, or older than this `released_at`). If a newer arrival already
  published, the row stays `complete` but not live, and the outcome is logged.
- **FR-7 Re-upload of a published tag** skips the load and goes straight to Publish. For an
  older tag this is a rollback; for the live tag it's a no-op.
- **FR-8 Re-upload of a failed tag** first deletes that tag's (never-published) graph in batches,
  then loads it. Readers never saw it, so the delete has no visible effect.
- **FR-9 Load already in progress.** A new manifest for a tag whose load is in progress is
  skipped and logged.
- **FR-10 A failed load is never published.** Its graph stays in Neptune for diagnosis, invisible.
  The execution fails and the pipeline alarm fires, as today.

### Query scoping
- **FR-11 Scoped dataset.** Every Neptune request from `sagebrain_core.execute_query` carries one
  `default-graph-uri` **and** one `named-graph-uri` per live graph. Sending only one of the two
  breaks either plain patterns or `GRAPH` patterns ([research.md](research.md) Q4).
- **FR-12 Server-side only.** The lists come from the registry and are never caller-supplied.
  `POST /query` and `POST /ask` accept no dataset field.
- **FR-13 Fail closed.** An empty registry sends the single sentinel `urn:sagebrain:none`, which
  matches nothing, never the union. A registry read error fails the query (the job ends in
  `error`).
- **FR-14 Registry cache.** The service caches the live list in memory for
  `live_graphs_cache_seconds` (60). A newly published snapshot is visible within that time.
- **FR-15 No federated queries.** `validate_query` rejects a query containing the `SERVICE`
  keyword with 400 `{"error": "SERVICE (federated query) is not permitted"}`. In `/ask` the same
  rejection becomes a `tool_result` error step, like other rejected queries.
- **FR-16 Audit.** Every `sparql_query` log line adds `graphs`: the list sent to Neptune.
- **FR-17 Agent guidance.** The agent's system prompt says the default graph is already the
  latest snapshot of each portal, and not to use `FROM`/`GRAPH` to choose versions.
- **FR-18 Flag.** `NEPTUNE_API.graph_scoping` (per env) turns FR-11–FR-14 and FR-16 on.
  Off means today's behaviour. FR-15 applies regardless of the flag.

## Non-functional requirements / thresholds
| Threshold | Value | Declared in | Enforced in | Test |
|---|---|---|---|---|
| Registry cache TTL | 60 s | `x-sagebrain-limits.live_graphs_cache_seconds` | `sagebrain_core.graphs` | `tests/unit/core/` cache test + limits-equal-contract test |
| Drop batch size (failed-tag retry) | dev 100000, prod 300000 | `NEPTUNE_PIPELINE.drop_batch_size` | loader `drop` action | stack env test |
| Loader Lambda timeout | 150 s (from 30 s) | `src/neptune_pipeline_stack.py` | Lambda | stack test |
| Neptune `neptune_query_timeout` | 120 s (existing) | cluster parameter group | Neptune | each drop batch must finish within it (sized above) |

## Deviations from current behaviour
| # | Today | New | Why |
|---|---|---|---|
| D-1 | Queries read the union of every named graph | Only the latest published graph of each portal | The core change ([research.md](research.md) Q1–Q6) |
| D-2 | `FROM`, `FROM NAMED`, `GRAPH <iri>` can read any snapshot | They can't reach unlisted graphs: protocol params override them | Callers can't read unpublished or old data |
| D-3 | `SERVICE` is accepted (and can exhaust Neptune memory) | 400 `SERVICE (federated query) is not permitted` | A self-`SERVICE` probe pinned dev's free memory at 0 for ~7 min (Q7); it's also an outbound-request path |
| D-4 | Only `YYYY-MM-DD` tags | Dates or `v…` release tags | #61 |
| D-5 | Duplicate S3 delivery detected by etag | By `object.sequencer` | An identical re-upload has the same etag and was wrongly skipped |
| D-6 | Re-publishing a date reloads into the same live graph | Published tags are immutable; a re-upload repoints (rollback); only failed tags reload | Append-only without merging stale data |
| D-7 | Failed/partial graphs are live | Invisible; cleared before a retry of that tag | #51 |
| D-8 | `sparql_query` log has no dataset | Adds `graphs` | Every result is reproducible |

Each deviation gets a test that records the old behaviour (constitution II). D-3 adds a
`tests/contract/cases.yaml` vector.

## Constitution notes
- **V (one query chokepoint):** scoping lives in `sagebrain_core`, so it covers `/query` and the
  agent. The pipeline's own `DELETE`/`ASK` SPARQL (FR-8) runs outside the service. It's a write
  path with no user-facing caller, recorded as a standing exception in `docs/memory/decisions.md`.
- **Graph Explorer and the operator bastion** query Neptune directly and keep seeing every graph.
  Both are existing VPN/operator-only exceptions; this spec doesn't change them.

## Out of scope
- Deleting old snapshots: retention N + sweep (#88). Until then Neptune grows by every snapshot
  (≈200 B/triple, [research.md](research.md) Q8).
- Letting callers query historical snapshots (would be an opt-in contract change).
- A staging cluster (#52).
- Surfacing Neptune error codes, SQS redelivery of Neptune 5xx failures, enabling Neptune
  audit/slow-query logs (follow-ups found during research; separate issues).

## Success criteria
- Unit tests for every FR above pass; `cdk synth` and contract lint pass.
- On dev with `graph_scoping` on, via the unified API:
  - `SELECT ?g WHERE { GRAPH ?g { ?s ?p ?o } } LIMIT 5` returns only `urn:sagebrain:nf:2026-07-28`.
  - `ASK { GRAPH <urn:sagebrain:nf:2026-07-14> { ?s ?p ?o } }` is `false`, and `FROM <…:2026-07-14>`
    is empty.
  - A `SERVICE` query returns 400.
  - `reactome/v97` loads and is visible within 60 s.
  - A broken snapshot (non-Turtle in `data/`) alarms and is never visible; fixed and re-uploaded,
    it loads and becomes visible.
  - Re-uploading an older `nf` manifest rolls back without a load; re-uploading the newer one
    rolls forward.
