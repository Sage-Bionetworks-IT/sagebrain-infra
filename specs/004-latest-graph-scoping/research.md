# 004 — Research

All probes ran on dev on 2026-10-14, through the unified API
(`https://ftclvhk9nh.execute-api.us-east-1.amazonaws.com/dev`) and directly against Neptune from the
temporary bastion ([runbook](../../docs/memory/runbooks/direct-neptune-access.md)), which was torn
down after each session.
Dev had three named graphs: `nf:2026-07-14`, `nf:2026-07-28`, `test:2026-07-28`, each 3,042,907
triples, matching the tracking table. The instance is a single serverless writer (it also serves
the `-ro` endpoint), max 4 NCU.

## Q1. What does the default graph contain today?
The union of all named graphs, **de-duplicated**: `COUNT(*)` over the default graph is 3,045,803,
not 3 × 3,042,907. Old snapshots show up as stale *extra* triples, not duplicates.

## Q2. Can a query scope itself with SPARQL dataset clauses?
Yes. `FROM <nf:2026-07-28>` → 3,042,907; two `FROM`s → 3,044,355; `FROM <nonexistent>` → 0.
`FROM NAMED <g>` + `GRAPH ?g` works. The catch: the caller has to know the graph URI.

## Q3. Does the API forward SPARQL 1.1 Protocol dataset parameters?
No. `core/sagebrain_core/neptune.py` form-encodes only `query`, and the contract's `QueryRequest`
has only `query`. A `default-graph-uri` naming a nonexistent graph, sent as a JSON field or in the
query string, still returned rows.

## Q4. Does Neptune honor `default-graph-uri` / `named-graph-uri`?
**Yes**, on POST form and GET, though AWS's docs (`feature-sparql-compliance`,
`sparql-api-reference`) don't mention them.

| Request | Result |
|---|---|
| `default-graph-uri=<nonexistent>`, `SELECT ?s WHERE {?s ?p ?o} LIMIT 1` | `[]` |
| `default-graph-uri=<nf:07-28>` | a row |
| `named-graph-uri=<nonexistent>` + `GRAPH ?g` | `[]` |
| `named-graph-uri=<nf:07-28>` + `GRAPH ?g` | only `nf:07-28` |
| `default-graph-uri` **only**, `GRAPH ?g` query | `[]`: the named-graph set is empty |
| `named-graph-uri` **only**, plain pattern | `[]`: the default graph is empty |
| `FROM <nonexistent>` in query + `default-graph-uri=<nf:07-28>` | a row: **the param overrides `FROM`** |
| `FROM <nf:07-28>` in query + `default-graph-uri=<nonexistent>` | `[]` |
| `default-graph-uri` ×2 `[nf:07-14, nf:07-28]`, `COUNT(*)` | 3,044,355: **repeated values merge** |
| 10-portal list (`…:nf:2026-07-28`, `…:reactome:v24`, `…:htan:v3.1`, `…:arkportal:v1.0.0-rc1`, …; 8 nonexistent) | works for both params |

So the service must send **both** lists (FR-11).

## Q5. Can a caller reach a graph that isn't in the lists?
No. Lists `[nf:2026-07-28]`, or a single nonexistent graph so that any row would be a leak:

| Attempt | Result |
|---|---|
| `FROM <nf:07-28>` with lists `[nonexistent]` | `[]` |
| `FROM NAMED <nf:07-14>` + `GRAPH ?g` | only `nf:07-28` |
| `ASK { GRAPH <test:07-28> { ?s ?p ?o } }` | `false` |
| `VALUES ?g { <nf:07-14> } GRAPH ?g { … }` | `[]` |
| `DESCRIBE <subject>` with lists `[nonexistent]` | empty (`[nf]` → the subject's triples) |
| `CONSTRUCT { <s> ?p ?o } WHERE { <s> ?p ?o }` with lists `[nonexistent]` | empty Turtle |
| `SERVICE <https://<cluster>:8182/sparql> { ?s ?p ?o }` | 500 after 43 s (Neptune's own outgoing call isn't SigV4-signed) |
| `SERVICE SILENT <…>` | one empty solution, no data |

## Q6. Does query cost grow with retained snapshots?
`explain=dynamic`, with the store fixed at three graphs:

- **One listed graph:** Neptune binds the graph in the index lookup, e.g.
  `(?s, rdf:type, ?t, ?_internalVar1 [<urn:sagebrain:nf:2026-07-28>])`. Subject lookups take milliseconds.
- **Nonexistent graphs in the list** are dropped from the plan: `[nf, nonexistent]` gives the same plan
  and timing as `[nf]`.
- **Two or more real graphs:** the plan becomes `distinct(…)`. That's the de-duplicating merge the
  default graph requires, and the plan no longer prints the graph list.

Join `?s a ?t . ?s rdfs:label ?l`, summed execution time (second run; wall-clock on serverless is noisy):

| Graphs listed | Summed exec time | Wall clock |
|---|---|---|
| 1 | 37 s | 7.6 s |
| 2 (two different pairs) | 55 s, 56 s | 12.3 s, 13.5 s |
| 3 (all) | 73 s | 14.4 s |
| none (union) | 40 s | 11.5 s (18.1 s on the first run) |

Work grows with the number of **listed** graphs (in production, the number of portals), not with
graphs stored. That's inferred from the trend; explain doesn't report rows read. Listing every graph
costs more than the plain union. Scaling signal for open-work: summed time per scoped query vs portal count.

## Q7. What were the 500s?
`MemoryLimitExceededException: Operation terminated (out of memory)`. It showed up only when the
bastion returned Neptune's raw body: the query worker's `raise_for_status()` drops it, and the
cluster's audit/slow-query logs are exported but disabled in the parameter group
(`neptune_enable_audit_log=0`, slow query `disabled`).
- 14:52–14:58 UTC: full-graph scans pinned the instance at 4 NCU / 100% with FreeableMemory 0, and
  Neptune fast-failed every request (110–400 ms), even `LIMIT 1`. EngineUptime kept rising and there
  were no instance events, so it didn't restart.
- The query worker re-raises, so SQS redelivered each failed job about 3× at about 2 s intervals,
  adding load.
- ~10:15–10:22 PDT: the self-`SERVICE` probe (Q5) alone pinned free memory at 0 for about 7 minutes →
  FR-15.

## Q8. Storage growth if nothing is deleted
`VolumeBytesUsed` is 1.87 GB for about 9.1M quads, roughly 200 B/triple including indexes and the
dictionary. `nf:2026-07-14` and `nf:2026-07-28` differ by about 1,450 triples (0.05%). At 5–10 portals,
some daily, about 3M triples each: about 30M triples (about 6 GB) a day, about 2.2 TB a year, nearly
all copies. Scoped query cost stays flat (Q6). Storage, load I/O, backups, memory pressure and
unscoped operator tools don't. Retention N + sweep: #88.
