# 002 — Plan

## Approach
governanceDUO's **effective access** is ACL AND every inherited AR satisfied. Stage 1 checks both
at query time, in the shared query chokepoint `sagebrain_core` (constitution V):

1. `governance.enforce` builds a **touch-set preflight** from the parsed query and runs it on the
   data cluster (same `WHERE`, same dataset) to learn which Synapse resources the query touches.
2. It invokes the **decision Lambda** with the caller and those resources. The decision Lambda:
   - reads each resource's item from the entities table (`open_to_authenticated`, inherited `ar_ids`);
   - reads the caller's grants (granted and unexpired);
   - builds Cedar entities and calls AVP `BatchIsAuthorized` with `acl_open` (permit) and
     `access_requirements` (forbid unless every inherited AR is held).
3. Any denial fails the whole job, listing the unmet ARs. After the real query, result IRIs are
   re-checked against the authorized set.

The two tables are a flattened copy of the Synapse data warehouse. For the POC an operator loads
them by hand with an export script; automation comes before prod.

Governance is enforced wherever it's deployed. `GOVERNANCE.enabled` says only whether it's
deployed in an env (constitution VI), and there are no modes: missing data or a backend error
denies.

```
caller ─▶ unified API submit (user_id on every job) ─▶ SQS ─▶ /query worker or agent tool
                                                  │
                     sagebrain_core.governance.enforce(query, user_id)
                       1. rdflib parse → touch-set preflight  ──▶ DATA Neptune (read endpoint)
                       2. lambda:Invoke decision Lambda {user_id, readKg, resources}
                                     │
                     ┌───────────────▼─────────────────────────────────────┐
                     │ decision Lambda   — own role, no VPC needed         │
                     │   GetItem/Query ─▶ entities + grants (DynamoDB, KMS)│
                     │   BatchIsAuthorized ─▶ AVP policy store (Cedar)     │
                     └───────────────┬─────────────────────────────────────┘
                       3. any DENY → GovernanceDenied (403) → job error governance_denied
                       4. execute_query(original) → rescan result IRIs ⊆ allowed

operator (POC) ─ tools/governance_export ─▶ Snowflake (own login) ─▶ entities + grants
```

The decision Lambda is separate from `sagebrain_core` so only one role can read the grants table
(which users hold which ARs) and call the policy store. The query worker, agent and unified-API
handlers gain only `lambda:InvokeFunction` on it.

## Components
- **Governance stack** (`app-{env}-governance`): the two tables, their KMS key, the AVP policy store
  with the schema and one static policy per `.cedar` file, and the decision Lambda.
- **Cedar model** (`policies/governance/`): schema plus `acl_open` and `access_requirements`,
  tested with cedarpy against the same files the stack deploys.
- **Decision Lambda**: table lookups → Cedar entities (deduped by (open, AR set), chunks of 30) →
  `BatchIsAuthorized` → decision, a 60 s decision cache, `governance_decision` log. Fail closed.
- **Enforcement in `sagebrain_core`**: touch-set builder, `enforce`, `check_results`,
  `GovernanceDenied`; called from `run_query` and the `/query` worker; the agent turns a denial
  into a `tool_result` error step. The layer gains `rdflib`.
- **Job ownership**: the unified API's status handlers return 404 unless the caller owns the job.
- **POC export** (`tools/governance_export/`): Snowflake SQL for the two tables over a scope file,
  run by an operator; writes every item, then deletes items absent from this export.
- **Ingest gate** (after spec 004): a step before `StartLoad` that looks every Synapse IRI in the
  snapshot up in the entities table and refuses the snapshot on any `ungoverned` or non-open IRI.

## Phases (each independently mergeable)
- **A — Job ownership (no governance yet).** Contract first: owner-only 404 on both status
  operations, `*_other_owner` vectors, then the handlers. Ships on its own.
- **B — Governance data.**
  - Confirm the Snowflake tables and columns (research Q-10) and the AR inheritance rule (Q-9).
  - The governance stack's tables and KMS key, with `GOVERNANCE.enabled: false` everywhere.
  - The export script, its scope file for the ~4 POC studies, and the worked-example fixtures.
  - Dev: enable, then run one export by hand and record counts in `research.md`.
- **C — Cedar model and decision Lambda.** cedarpy tests (FR-13 table), the AVP store, the
  decision Lambda with stubbed-backend tests, deployed to dev. `aws lambda invoke` reproduces the
  FR-13 rows against the dev tables.
- **D — Enforcement.** Contract: the `governance` error object and the `governance_denied` step.
  The touch-set builder, `enforce`, `check_results`; wired into `run_query`, the `/query` worker
  and the agent. Merges only after B and C are live on dev, because it enforces from the moment it
  deploys: anything outside the POC scope becomes `ungoverned` and is denied on dev.
- **E — Measure on dev.** Run the success-criteria scenarios; record overhead (preflight +
  decision p50/p95) in `research.md`.
- **F — Ingest gate (after spec 004 merges).** Its entry condition is a full-portal export.
- **G — Before prod.** Export automation (scheduled job, Snowflake key pair in Secrets Manager,
  versioned writes + pointer flip, 48 h freshness cutoff with a 26 h alarm), then prod
  `enabled: true`, then remove the flag so governance is unconditional.
- **Stage 2 — per-user and team ACLs, other AR types.** Separate spec.

## Risks / rollback
- **Rollback.** Revert the PR. Setting `GOVERNANCE.enabled: false` in an env undeploys it there;
  there is no "deployed but not enforcing" state.
- **Dev denies most queries once D merges.** The POC export covers ~4 studies, and everything
  else is `ungoverned`. That's the fail-closed rule working; widen the scope (one export run) to
  open more of dev.
- **No shadow run.** A touch-set builder bug that *misses* touched resources would leak instead
  of deny. Mitigations: an allow-listed SPARQL subset (reject what we can't analyse), the FR-9
  result rescan, and tests over every supported query shape. A builder bug that over-reports only
  over-denies.
- **Preflight cost.** It doubles Neptune work, and broad queries exceed `max_touch_set`; those are
  rejected with guidance to narrow the query. The later optimisation is a per-user allowed-set
  `VALUES` injection (research R-1).
- **Stale grants.** Until automation (phase G), a revoked or new grant takes effect only on the
  next manual export. Acceptable on dev only.
- **Warehouse semantics.** We rebuild Synapse's grant logic from raw tables. A mismatch (e.g. a
  state spelling, or a revocation path we don't see) shows up as a wrong decision. Mitigation:
  phase B compares exported grants for a few known users with Synapse's own answer
  (`POST /restrictionInformation` as that user).
- **Cost.** Two on-demand tables, a KMS key, an AVP store and a Lambda: small; no second Neptune.

## Open decisions
See research.md Q-2 (non-Synapse nodes), Q-5 (purpose of use), Q-6 (gate scope), Q-8 (other AR
types), Q-9 (AR inheritance rule), Q-10 (Snowflake tables).
