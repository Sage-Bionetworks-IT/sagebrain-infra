# Plan of Action and Milestones (POA&M)

NIST SP 800-171 Rev 3 requirement 03.12.02, for the Sage Brain system: the boundary and requirement
status are in [spec 005](../../specs/005-nist-800-171/spec.md) and its
[control matrix](../../specs/005-nist-800-171/control-matrix.md).

**How to use it:**
- One entry per weakness. Its status is `Open`, `In progress`, `Closed` or `Risk accepted`.
- A matrix row that is Partial or Planned should have an entry here. N71 completes the list; this
  file was started 2026-10-27 with the authentication weaknesses, and the write path was added
  2026-10-28.
- Close an entry in the PR that fixes it. Record the date and the evidence (test, PR, saved
  output), and update the matrix row in the same PR.
- Dates are set by the owner when the work is scheduled. `TBD` means not yet scheduled.

**CMMC caveat:** if this system must meet CMMC Level 2, not every requirement may sit on a POA&M.
I believe items worth more than 1 point in the DoD scoring methodology, such as MFA, must be met
before certification. MFA is recorded as inherited from Synapse, not as a POA&M item. Confirm with IT before relying on a POA&M entry for one of them.

## Summary
| ID | Weakness | Rev 3 (Rev 2) | Risk | Status | Tickets |
|---|---|---|---|---|---|
| POAM-001 | API PATs are long-lived bearer tokens | 03.05.04, 03.05.12 (3.5.4) | Moderate | Open (Synapse) | N04 |
| POAM-002 | Authorizer cache TTL isn't the declared limit | 03.05.12, 03.01.11, 03.01.01 (3.1.11, 3.1.1) | Low | Open | N41 |
| POAM-003 | No limit on unsuccessful authentication attempts | 03.01.08 (3.1.8) | Low | Open | N33, N44 |
| POAM-004 | Unattributed, over-broad write path into the knowledge graph | 03.03.01, 03.03.02, 03.01.05, 03.08.02 (3.3.1, 3.3.2, 3.1.5, 3.8.2) | High | Open | N13, N46 |

---

## POAM-001 — API PATs are long-lived bearer tokens
**Requirements:** 03.05.04 Replay-Resistant Authentication, 03.05.12 Authenticator Management ·
**Owner:** Synapse (tracked by Thomas Yu) · **Status:** Open · **Identified:** 2026-10-23 (repo
scrub), verified 2026-10-27, re-scoped 2026-11-03

**Weakness.** `/query` and `/ask` accept a Synapse personal access token (PAT) as a bearer token
(`src/lambda_authorizer/authorizer.py:44-69`). MFA itself is inherited: Synapse requires it at
login, and a PAT is created from a logged-in session (decided 2026-11-03, evidence in N04). But a
PAT has no `exp` claim, and Synapse sets no maximum PAT age. A PAT stays valid until it's revoked,
or until Synapse retires it for inactivity, and using it involves no second factor. This is a
Synapse limitation, so the fix is Synapse's, not this repo's (decided 2026-11-03).

Checked 2026-10-27 against a real PAT: the claims are `token_type: PERSONAL_ACCESS_TOKEN`, an
`iat` and no `exp`.

**Risk: Moderate.** A leaked PAT from a team member gives read access to the whole graph, which
will include CUI. The access lasts until the token is revoked in Synapse, plus up to 300 s of
authorizer cache.

**Current mitigations:**
- Only members of the env's Synapse team are allowed, checked on every cache miss.
- TLS on every call.
- Tokens are never logged, enqueued or returned.
- Revocation and team removal take effect within 300 s.
- Creating a PAT needs an MFA login (to evidence in N04).

**Planned remediation (Synapse):** Synapse adds an expiry or a maximum age to PATs, so every PAT
in use traces to a recent MFA login. N04 also records the evidence for the inherited MFA.

If an assessor doesn't accept the inherited MFA, the fallbacks are a 2FA check at auth time
(`GET /auth/v1/2fa`, which works with a PAT, checked 2026-10-27) or accepting only OAuth access
tokens in CUI envs. The second breaks scripted use, so it needs a CLI login flow.

**Milestones:**
| # | Milestone | Ticket | Target date | Done |
|---|---|---|---|---|
| 1 | Written evidence from Synapse: MFA at every login; no PAT from a PAT | N04 | TBD | |
| 2 | Request PAT expiry or a maximum age from the Synapse team | — | TBD | |
| 3 | Synapse ships it; record the limit in `docs/compliance/inherited-controls.md` | — | TBD | |

**Closure evidence:** Synapse's PAT expiry or maximum age, documented with its date.

## POAM-002 — Authorizer cache TTL isn't the declared limit
**Requirements:** 03.05.12 Authenticator Management, 03.01.11 Session Termination, 03.01.01 Account
Management · **Owner:** Thomas Yu · **Status:** Open · **Identified:** 2026-10-23, re-scoped 2026-10-27

**Weakness.**
- **The TTL constant (the real defect).** A cached allow lasts 300 s, which is how long a revoked
  PAT or a removed team member keeps access on a warm instance. That window is a defensible value,
  and it's declared as `auth_cache_ttl_seconds` in `api/openapi.yaml` and
  `limits.AUTH_CACHE_TTL_SECONDS`. But the authorizer enforces its own constant,
  `_CACHE_TTL = 300` (`src/lambda_authorizer/authorizer.py:21`). The limits test doesn't cover the
  value actually enforced.
- **Raw-token cache keys (hygiene).** The cache keys are raw tokens, held in that Lambda
  instance's memory only.
- **Not a weakness:** the cache's size. Only tokens that passed validation are cached, so it can
  hold no more entries than the team has valid tokens.

**Risk: Low.**

**Current mitigations:**
- The cache is per instance and in memory only.
- Tokens are never logged.
- API Gateway's own authorizer cache is off (`results_cache_ttl=0`).

**Planned remediation (N41):**
- The authorizer uses `limits.AUTH_CACHE_TTL_SECONDS`.
- Key the cache by the token's SHA-256.
- Bound it with an LRU.
- Under POAM-001, the 2FA and PAT-age results are cached in the same entry, so disabling 2FA also
  takes effect within the TTL.

**Milestones:**
| # | Milestone | Ticket | Target date | Done |
|---|---|---|---|---|
| 1 | Authorizer reads the TTL from `limits`, with a test that fails if they differ | N41 | TBD | |
| 2 | Hashed keys and a bounded LRU, with tests | N41 | TBD | |

**Closure evidence:** `tests/unit/test_authorizer.py` asserts the cache TTL equals
`limits.AUTH_CACHE_TTL_SECONDS` and that no raw token is stored.

## POAM-003 — No limit on unsuccessful authentication attempts
**Requirement:** 03.01.08 Unsuccessful Logon Attempts · **Owner:** Thomas Yu · **Status:** Open ·
**Identified:** 2026-10-23

**Weakness.**
- **No per-caller limit.** Nothing limits or alarms on repeated invalid tokens from one source.
- **No negative caching.** Failures aren't cached, so every invalid token costs a call to Synapse.
- **One shared throttle.** The only rate bound is the shared stage throttle: 50 requests/s with a
  burst of 100 (`x-sagebrain-limits`).

**Risk: Low.** Synapse tokens are signed, so guessing a valid one is infeasible. The residual risk
is nuisance load on Synapse, and failed attempts that nobody notices.

**Current mitigations:**
- The API Gateway stage throttle.
- Synapse's own lockout on interactive login (inherited).
- Every denial returns the same 401, with no detail about the failure.

**Planned remediation:**
- A WAF per-IP rate-based rule on the API, at a limit agreed in N02 (N33).
- An alarm on the 401/403 rate (N44).
- Deny logs carry the source IP and request id, so a burst can be traced (N41).

**Milestones:**
| # | Milestone | Ticket | Target date | Done |
|---|---|---|---|---|
| 1 | Agree the per-IP limit | N02 | TBD | |
| 2 | WAF on the API stage with the rate rule | N33 | TBD | |
| 3 | 401/403 rate alarm | N44 | TBD | |

**Closure evidence:**
- A stack test asserts the WAF association and the rate rule.
- `tools/load_probe.py burst` from one IP is blocked in dev.
- A test for the alarm.

## POAM-004 — Unattributed, over-broad write path into the knowledge graph
**Requirements:** 03.03.01 Event Logging, 03.03.02 Audit Record Content, 03.01.05 Least Privilege,
03.08.02 Media Access · **Owner:** Thomas Yu · **Status:** Open · **Identified:** 2026-10-23, detailed 2026-10-28

**Weakness.**
- **The loader can't name the uploader.** Uploading `{portal}/{tag}/manifest.ttl` loads that
  snapshot's `data/` into Neptune automatically. The S3 event the pipeline receives names only
  the AWS account (`requester` is the account id, per the S3 EventBridge docs, checked
  2026-10-28).
- **No CloudTrail data events are confirmed for the data bucket.** So no record ties a named
  graph to the role, repo or person that uploaded its files.
- **Access is broader than needed:**
  - The bucket policy lets any principal in the account Put, Get and Delete objects
    (`src/neptune_stack.py:61-77`).
  - The three depositor OIDC roles (`Sage-Bionetworks-IT/organizations-infra` #1605, dev and prod
    accounts) can each write, `DeleteObject` and **`DeleteObjectVersion`** on every portal's
    prefix. `DeleteObjectVersion` can permanently erase versions the bucket's versioning would
    otherwise keep.
  - The `Sage_Brain` role has no workflow using it. The repo has none on any branch, checked
    2026-10-28.

**Risk: High.**
- A compromised or mistaken pipeline could load, overwrite or erase another portal's data,
  including CUI, and no record would say who did it.
- Erased versions also remove the evidence of what was loaded.

**Current mitigations:**
- **Depositor roles are pinned to their repos.** The OIDC trust allows only that repo's `develop`
  and `main` branches, tags and environments. No static keys.
- **Versioning is on.** Overwrites keep the earlier version, unless `DeleteObjectVersion` is used.
- **Loads are recorded.** The loader records the manifest etag per snapshot in the lineage table,
  and logs `kg_load`.
- **Failures alert.** Failed loads fail closed and email the alarm address.

**Planned remediation (all in this repo).** The bucket and the roles share the sagebrain account,
so the bucket policy's explicit Denies override whatever the roles allow.
1. **The bucket policy enforces the write path** (N13):
   - writes only from the configured depositor role for each portal prefix;
   - `DeleteObject` and `DeleteObjectVersion` denied except to a break-glass role;
   - the policy itself protected from removal;
   - the unused `Sage_Brain` role gets no prefix.
2. **A bucket-scoped CloudTrail trail** logs write data events for the data bucket into a locked,
   encrypted log bucket. The role in each event names the depositor repo (N46).
3. **Each load records the object versions it read**, and fails if they change mid-load. A saved
   query traces a graph URI to its uploads, role, repo and branch (N46). The branch comes from
   `AssumeRoleWithWebIdentity`, through the org trail or the 90-day event history.
4. **Optional:** depositors add a `role-session-name` with the GitHub run id. Separately, IT can
   narrow the role policies in `organizations-infra` to match. Neither blocks closure.

**Milestones:**
| # | Milestone | Ticket | Owner | Target date | Done |
|---|---|---|---|---|---|
| 1 | List what each role actually writes (bucket prefixes) into `NEPTUNE.depositors` | N13, N01 | Repo | TBD | |
| 2 | Bucket policy: per-prefix write denies, delete denies, policy protection | N13 | Repo | TBD | |
| 3 | Bucket-scoped data-event trail and its log bucket | N46 | Repo | TBD | |
| 4 | Snapshot contents record, change detection, saved provenance query | N46 | Repo | TBD | |
| 5 | Optional: IT narrows the role policies; depositors set `role-session-name` | — | IT, depositors | TBD | |

**Closure evidence:**
- The trail's event selectors in a stack test, plus one real upload found in its logs.
- `tests/unit/test_neptune_stack.py` policy assertions.
- Dev probe results: a cross-prefix write and `DeleteObjectVersion` are both denied.
- The saved query's output for one real NF load, naming the repo, branch and object versions.
