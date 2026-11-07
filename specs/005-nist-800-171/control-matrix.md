# 005 — Control matrix (NIST SP 800-171 Rev 3)

All 97 Rev 3 requirements, as found by the 2026-10-23 scrub at `dev` `b313b47`. A PR that changes
a row's status updates the row in that PR.

**Columns:**
- **Rev 2:** a best-effort crosswalk, for CMMC 2.0 Level 2. N02 verifies it against NIST's
  published mapping, along with every ID and title.
- **Status:**
  - `Implemented`: the requirement is met by this repo, with evidence.
  - `Partial`: some of it is met; the gap is named.
  - `Planned`: not met; a ticket exists.
  - `Inherited`: met by Sage IT or AWS, with evidence in `docs/compliance/inherited-controls.md`
    (N03).
  - `N/A`: not applicable to this system, with a reason.
- **Owner:** `Repo`, `IT` or `Shared`.
- **Tickets:** see [tickets.md](tickets.md).

## 03.01 Access Control
| ID | Requirement | Rev 2 | Status | Owner | Tickets | Evidence / gap |
|---|---|---|---|---|---|---|
| 03.01.01 | Account Management | 3.1.1 | Partial | Shared | N75, N03 | The Synapse team roster is the authorized-user list (`authorizer.py:131-144`; `AUTH.synapse_team_id`). No periodic review. AWS accounts are IT's. |
| 03.01.02 | Access Enforcement | 3.1.2 | Partial | Repo | N11, spec 002 | Team-gated authorizer, fails closed. Any member can read any job (`jobs.py:140-153`). No per-entity ACL (spec 002). |
| 03.01.03 | Information Flow Enforcement | 3.1.3 | Partial | Repo | N32, N43, N13 | Neptune ingress is SG-to-SG only. All egress open. CUI flows to email and DLQs. Account-wide bucket writes. |
| 03.01.04 | Separation of Duties | 3.1.4 | Implemented | Repo | N10 | Changes reach `dev` through a PR approved by someone other than the author (`dev` ruleset, 2026-10-27), and only CI deploys. Evidence: ruleset export (N10). |
| 03.01.05 | Least Privilege | 3.1.5 | Partial | Repo | N13, N45 | Workers have read-only `neptune-db` actions. `grant_*` over-grants. Bedrock `*` region. Account-wide bucket policy (`neptune_stack.py:61-77`). Depositor roles can write and delete every portal's prefix (organizations-infra #1605). POAM-004. |
| 03.01.06 | Least Privilege – Privileged Accounts | 3.1.6 | Partial | Shared | N03, N16 | Operator roles are IT's (Identity Center). The bastion role can `DeleteDataViaQuery`. |
| 03.01.07 | Least Privilege – Privileged Functions | 3.1.7 | Partial | Shared | N16, N42 | Deploys go only through CI with OIDC. Bastion deletes aren't in the `sparql_query` log, and Neptune's audit log is off (N14). |
| 03.01.08 | Unsuccessful Logon Attempts | 3.1.8 | Planned | Shared | N33, N44 | No per-caller lockout or rate limit, and no 401 alarm. Synapse handles interactive login. POAM-003. |
| 03.01.09 | System Use Notification | 3.1.9 | Planned | Repo | N76 | No notice on `/api`. |
| 03.01.10 | Device Lock | 3.1.10 | Inherited | IT | — | Endpoint lock is IT's. The system has no interactive session of its own. |
| 03.01.11 | Session Termination | 3.1.11 | Partial | Repo | N41 | The authorizer re-checks a token every 300 s (`limits.AUTH_CACHE_TTL_SECONDS`). POAM-002. |
| 03.01.12 | Remote Access | 3.1.12–3.1.15 | Partial | Shared | N16 | Remote access is the API (TLS, Synapse token, team gate) and the bastion through SSM. The VPN is IT's. |
| 03.01.16 | Wireless Access | 3.1.16, 3.1.17 | Inherited | IT | — | Office and VPN networks are IT's. There is no wireless component in the boundary. |
| 03.01.18 | Access Control for Mobile Devices | 3.1.18, 3.1.19 | Inherited | IT | — | Device management is IT's. |
| 03.01.20 | Use of External Systems | 3.1.20, 3.1.21 | Planned | Shared | N01, N72 | Synapse, Bedrock, GitHub and npm are used but not documented as external systems. |
| 03.01.22 | Publicly Accessible Content | 3.1.22 | Partial | Repo | N34 | `/api` docs and the contract are public by design (`api_edge_stack.py:312-322`). No review step. |

## 03.02 Awareness and Training
| ID | Requirement | Rev 2 | Status | Owner | Tickets | Evidence / gap |
|---|---|---|---|---|---|---|
| 03.02.01 | Literacy Training and Awareness | 3.2.1, 3.2.3 | Inherited | IT | N03 | Org security awareness program. |
| 03.02.02 | Role-Based Training | 3.2.2 | Inherited | IT | N03 | Org program. CUI-handling training for team members is IT's. |

## 03.03 Audit and Accountability
| ID | Requirement | Rev 2 | Status | Owner | Tickets | Evidence / gap |
|---|---|---|---|---|---|---|
| 03.03.01 | Event Logging | 3.3.1 | Partial | Shared | N14, N13, N30, N03, N46 | `sparql_query`, `agent_invocation`, `kg_load` and API access logs exist. Neptune's audit log is off. No S3 or flow logs. CloudTrail is IT's. |
| 03.03.02 | Audit Record Content | 3.3.1, 3.3.2 | Partial | Repo | N42, N46 | `sparql_query` has the principal and IP (`core/sagebrain_core/neptune.py:37-56`). Access logs lack the principal. `kg_load` lacks an actor: the S3 event names only the account. POAM-004. |
| 03.03.03 | Audit Record Generation | 3.3.1, 3.3.3 | Partial | Repo | N42, N14, N46 | Status reads aren't logged. Step Functions logs at `ERROR` only. |
| 03.03.04 | Response to Audit Logging Process Failures | 3.3.4 | Planned | Repo | N44 | Every alarm is `NOT_BREACHING` on missing data. No heartbeat. |
| 03.03.05 | Audit Record Review, Analysis, and Reporting | 3.3.5 | Planned | Shared | N44, N75 | No security alarms and no review cadence. |
| 03.03.06 | Audit Record Reduction and Report Generation | 3.3.6 | Planned | Repo | N44 | One example Insights query in CLAUDE.md. No saved `QueryDefinition`s. |
| 03.03.07 | Time Stamps | 3.3.7 | Partial | Shared | N42 | AWS time sync is inherited. Events use epoch seconds, not ISO-8601 UTC. |
| 03.03.08 | Protection of Audit Information | 3.3.8, 3.3.9 | Planned | Shared | N15, N03, N46 | Lambda and Neptune log groups are unmanaged and never expire. Others keep 30 days. Logs use CloudWatch's default encryption (accepted 2026-11-03). |

## 03.04 Configuration Management
| ID | Requirement | Rev 2 | Status | Owner | Tickets | Evidence / gap |
|---|---|---|---|---|---|---|
| 03.04.01 | Baseline Configuration | 3.4.1 | Partial | Repo | N50, N53 | All infrastructure is CDK, with per-env `config/`. No cdk-nag baseline. |
| 03.04.02 | Configuration Settings | 3.4.2 | Partial | Repo | N50, N60 | Hardened CDK feature flags (`cdk.json`). Org values hardcoded in code. |
| 03.04.03 | Configuration Change Control | 3.4.3 | Implemented | Repo | N10 | Reviewed squash PRs into `dev` (1 approval); `dev` → `prod` promotion PRs; CI deploys only. `--require-approval never` accepted (decisions.md). |
| 03.04.04 | Impact Analyses | 3.4.4 | Partial | Repo | N34 | Specs, the constitution and `cdk diff`. No security-impact step in the PR template. |
| 03.04.05 | Access Restrictions for Change | 3.4.5 | Partial | Shared | N10, N16 | Deploys only through the OIDC role, after a reviewed PR. The bastion deploys from a laptop (`bastion.sh:67`). |
| 03.04.06 | Least Functionality | 3.4.6, 3.4.7 | Partial | Repo | N54 | Minimal routes (contract-checked). Dev-only tools aren't yet barred from prod. |
| 03.04.08 | Authorized Software – Allow by Exception | 3.4.8 | Partial | Repo | N52 | Exact pins on most dependencies. Ranges and no hashes. `:latest` image. |
| 03.04.10 | System Component Inventory | 3.4.1 | Partial | Repo | N53, N01 | The IaC is the inventory. No tags or generated listing. |
| 03.04.11 | Information Location | — | Planned | Shared | N01, N05 | Where CUI is stored and processed isn't documented (including Bedrock routing). |
| 03.04.12 | System and Component Configuration for High-Risk Areas | — | Inherited | IT | — | Travel and device policy is IT's. |

## 03.05 Identification and Authentication
| ID | Requirement | Rev 2 | Status | Owner | Tickets | Evidence / gap |
|---|---|---|---|---|---|---|
| 03.05.01 | User Identification, Authentication, and Re-Authentication | 3.5.1, 3.5.2 | Partial | Repo | N04, N41 | Synapse bearer plus team check on every API call, re-checked every 300 s (N41). MFA at the Synapse login (N04). |
| 03.05.02 | Device Identification and Authentication | 3.5.1, 3.5.2 | Implemented | Repo | — | Service-to-service calls are IAM/SigV4: Neptune IAM auth (`neptune_stack.py:142`). User devices are IT's. |
| 03.05.03 | Multi-Factor Authentication | 3.5.3 | Inherited | Shared | N04, N03 | API users: Synapse requires MFA at login, and every token the API accepts comes from a login (Synapse, to evidence in N04). Operators: Identity Center MFA (IT, to evidence). |
| 03.05.04 | Replay-Resistant Authentication | 3.5.4 | Partial | Shared | N04 | TLS and SigV4 for services. The MFA login is Synapse's. A PAT is a bearer token with no `exp` or maximum age, replayable until revoked: a Synapse limitation. POAM-001. |
| 03.05.05 | Identifier Management | 3.5.5, 3.5.6 | Inherited | IT | — | Synapse user ids and AWS identities are issued outside the system. |
| 03.05.07 | Password Management | 3.5.7–3.5.10 | Inherited | IT | — | No passwords in the system. Synapse and Identity Center policies apply. |
| 03.05.11 | Authentication Feedback | 3.5.11 | Implemented | Repo | — | Every denial returns the same 401 `Unauthorized` (`api_edge_stack.py:231-245`). No token is echoed. |
| 03.05.12 | Authenticator Management | — | Partial | Repo | N41, N04 | The cache TTL is the authorizer's own constant, not the declared limit. Raw tokens are cache keys (in memory only). Probes take a PAT as an argument. POAM-002. |

## 03.06 Incident Response
| ID | Requirement | Rev 2 | Status | Owner | Tickets | Evidence / gap |
|---|---|---|---|---|---|---|
| 03.06.01 | Incident Handling | 3.6.1 | Partial | Shared | N73 | Org IR program (IT). No system playbooks. |
| 03.06.02 | Incident Monitoring, Reporting, and Response Assistance | 3.6.1, 3.6.2 | Partial | Shared | N44, N73 | Operational alarms only. DFARS reporting is IT's. |
| 03.06.03 | Incident Response Testing | 3.6.3 | Planned | Shared | N73 | No tabletop exercise. |
| 03.06.04 | Incident Response Training | — | Inherited | IT | — | Org program. |
| 03.06.05 | Incident Response Plan | — | Partial | Shared | N73 | Org plan (IT). The system annex is missing. |

## 03.07 Maintenance
| ID | Requirement | Rev 2 | Status | Owner | Tickets | Evidence / gap |
|---|---|---|---|---|---|---|
| 03.07.04 | Maintenance Tools | 3.7.2, 3.7.4 | Partial | Repo | N16 | The bastion is the one maintenance tool: SSM, IMDSv2. No EBS encryption or auto-teardown. |
| 03.07.05 | Nonlocal Maintenance | 3.7.5 | Partial | Shared | N16, N03 | Through SSM with SSO credentials (MFA is IT's). SSM output isn't logged. |
| 03.07.06 | Maintenance Personnel | 3.7.6 | Partial | Shared | N16 | Who may run the bastion isn't recorded. A reason and issue tag is planned. |

## 03.08 Media Protection
| ID | Requirement | Rev 2 | Status | Owner | Tickets | Evidence / gap |
|---|---|---|---|---|---|---|
| 03.08.01 | Media Storage | 3.8.1 | Implemented | Shared | — | Physical media is AWS's (inherited). Digital storage is encrypted with AWS-managed or AWS-owned keys (accepted 2026-11-03). |
| 03.08.02 | Media Access | 3.8.2 | Partial | Repo | N13, N43, N46 | Account-wide bucket access, and depositor roles that can `DeleteObjectVersion`. Log groups holding CUI aren't restricted. |
| 03.08.03 | Media Sanitization | 3.8.3, 3.7.3 | Planned | Shared | N74, N73, N13 | No sanitization or decommission procedure. Versioning keeps deleted objects forever. |
| 03.08.04 | Media Marking | 3.8.4 | Planned | Repo | N53, N76 | No `DataClassification` tags or CUI markings. |
| 03.08.05 | Media Transport | 3.8.5, 3.8.6 | Inherited | IT | — | No physical media. Digital transport is covered by 03.13.08. |
| 03.08.07 | Media Use | 3.8.7, 3.8.8 | Inherited | IT | — | Removable-media policy is IT's. |
| 03.08.09 | System Backup – Cryptographic Protection | 3.8.9 | Partial | Repo | N14, N24 | Neptune backups are encrypted with the AWS-managed key, kept 7 days. The lineage table has no PITR. |

## 03.09 Personnel Security
| ID | Requirement | Rev 2 | Status | Owner | Tickets | Evidence / gap |
|---|---|---|---|---|---|---|
| 03.09.01 | Personnel Screening | 3.9.1 | Inherited | IT | N03 | Team members with CUI access are screened by IT and HR. |
| 03.09.02 | Personnel Termination and Transfer | 3.9.2 | Partial | Shared | N75 | IT and HR process. Removal from the Synapse team roster isn't tied to it. |

## 03.10 Physical Protection
| ID | Requirement | Rev 2 | Status | Owner | Tickets | Evidence / gap |
|---|---|---|---|---|---|---|
| 03.10.01 | Physical Access Authorizations | 3.10.1 | Inherited | IT | N03 | AWS data centers (FedRAMP) and Sage offices. |
| 03.10.02 | Monitoring Physical Access | 3.10.2 | Inherited | IT | N03 | As above. |
| 03.10.06 | Alternate Work Site | 3.10.6 | Inherited | IT | N03 | Remote-work policy is IT's. |
| 03.10.07 | Physical Access Control | 3.10.3–3.10.5 | Inherited | IT | N03 | As above. |
| 03.10.08 | Access Control for Transmission | — | Inherited | IT | N03 | AWS physical network. |

## 03.11 Risk Assessment
| ID | Requirement | Rev 2 | Status | Owner | Tickets | Evidence / gap |
|---|---|---|---|---|---|---|
| 03.11.01 | Risk Assessment | 3.11.1 | Planned | Shared | N72 | None. |
| 03.11.02 | Vulnerability Monitoring and Scanning | 3.11.2 | Planned | Shared | N51, N03 | No scanning in CI. Inspector is IT's, to evidence. |
| 03.11.04 | Risk Response | 3.11.3 | Planned | Shared | N72, N71 | No POA&M. |

## 03.12 Security Assessment and Monitoring
| ID | Requirement | Rev 2 | Status | Owner | Tickets | Evidence / gap |
|---|---|---|---|---|---|---|
| 03.12.01 | Security Assessment | 3.12.1 | Partial | Shared | N75 | One code-reading review (`docs/memory/security-reviews.md`, 2026-10-13). |
| 03.12.02 | Plan of Action and Milestones | 3.12.2 | Partial | Shared | N71 | Started 2026-10-27 with POAM-001 to POAM-003 (`docs/compliance/poam.md`). N71 adds the remaining weaknesses. |
| 03.12.03 | Continuous Monitoring | 3.12.3 | Planned | Shared | N75, N50 | No cadence. |
| 03.12.05 | Information Exchange | — | Planned | Shared | N70, N72 | No interconnection terms recorded for Synapse or Bedrock. |

## 03.13 System and Communications Protection
| ID | Requirement | Rev 2 | Status | Owner | Tickets | Evidence / gap |
|---|---|---|---|---|---|---|
| 03.13.01 | Boundary Protection | 3.13.1, 3.13.5 | Partial | Repo | N30, N31, N32, N33 | Private subnets. Neptune SG-to-SG ingress. No WAF or flow logs. |
| 03.13.04 | Information in Shared System Resources | 3.13.4 | Inherited | IT | N03 | AWS tenant isolation (FedRAMP). |
| 03.13.06 | Network Communications – Deny by Default – Allow by Exception | 3.13.6 | Planned | Repo | N32 | `allow_all_outbound=True` on every Lambda SG. Neptune egresses to 0.0.0.0/0:443. |
| 03.13.08 | Transmission and Storage Confidentiality | 3.13.8, 3.8.6, 3.13.16 | Partial | Repo | N21, N23 | TLS to Neptune, S3 (`enforce_ssl`) and the API, but the API accepts TLS 1.0 and 1.1 (probed 2026-11-03; N23). Queues and topic don't enforce TLS, and the topic is unencrypted (N21). Storage on AWS-managed or AWS-owned keys (accepted 2026-11-03). |
| 03.13.09 | Network Disconnect | 3.13.9 | Implemented | Repo | — | API request timeout 10 s (`x-sagebrain-limits`). |
| 03.13.10 | Cryptographic Key Establishment and Management | 3.13.10 | Partial | Shared | N21 | AWS manages the storage keys (AWS-managed and AWS-owned, accepted 2026-11-03). The one customer-managed key, for the SNS topic, doesn't exist yet (N21). |
| 03.13.11 | Cryptographic Protection | 3.13.11 | Partial | Repo | N23 | AWS KMS uses FIPS-validated HSMs, for AWS-managed keys too. No FIPS endpoints. No API TLS policy: TLS 1.0 and 1.1 are accepted. |
| 03.13.12 | Collaborative Computing Devices and Applications | 3.13.12 | N/A | — | — | No collaborative computing devices in the boundary. |
| 03.13.13 | Mobile Code | 3.13.13 | Implemented | Repo | — | Swagger UI is version-pinned and sha512-checked, served inline (`lambda_docs/bundle.py:21-32`). |
| 03.13.15 | Session Authenticity | 3.13.15 | Partial | Repo | N23 | TLS on the API (default policy until N23). |

## 03.14 System and Information Integrity
| ID | Requirement | Rev 2 | Status | Owner | Tickets | Evidence / gap |
|---|---|---|---|---|---|---|
| 03.14.01 | Flaw Remediation | 3.14.1 | Partial | Repo | N51, N52 | Neptune auto minor-version upgrades. No dependency scanning or Dependabot. |
| 03.14.02 | Malicious Code Protection | 3.14.2, 3.14.4, 3.14.5 | Planned | Shared | N51, N03 | GuardDuty and Inspector are IT's, to evidence. No image scanning. |
| 03.14.03 | Security Alerts, Advisories, and Directives | 3.14.3 | Partial | Shared | N51, N75 | Org advisories (IT). No Dependabot alerts. |
| 03.14.06 | System Monitoring | 3.14.6, 3.14.7 | Partial | Shared | N44, N30, N33, N03 | Operational alarms and dashboards. No security monitoring in the repo. |
| 03.14.08 | Information Management and Retention | — | Partial | Repo | N43, N15 | Job TTL 24 h, DLQs 14 days. Logs never expire. Emails carry content. |

## 03.15 Planning
| ID | Requirement | Rev 2 | Status | Owner | Tickets | Evidence / gap |
|---|---|---|---|---|---|---|
| 03.15.01 | Policy and Procedures | — | Partial | Shared | N02 | Engineering rules in `docs/memory/constitution.md`. Org security policy is IT's. |
| 03.15.02 | System Security Plan | — | Planned | Shared | N70, N01 | None. |
| 03.15.03 | Rules of Behavior | — | Inherited | IT | N70 | Org acceptable-use policy, linked from the SSP. |

## 03.16 System and Services Acquisition
| ID | Requirement | Rev 2 | Status | Owner | Tickets | Evidence / gap |
|---|---|---|---|---|---|---|
| 03.16.01 | Security Engineering Principles | 3.13.2 | Partial | Repo | N50, N61, N62 | The constitution: least privilege, one query chokepoint, contract first, every threshold tested. |
| 03.16.02 | Unsupported System Components | — | Partial | Repo | N52 | Pinned Lambda Python 3.11 and Neptune `1.3.2.1`. No end-of-life tracking. |
| 03.16.03 | External System Services | — | Planned | Shared | N05, N06, N72 | The FedRAMP scope of Bedrock and its models is unverified. Synapse terms aren't recorded. |

## 03.17 Supply Chain Risk Management
| ID | Requirement | Rev 2 | Status | Owner | Tickets | Evidence / gap |
|---|---|---|---|---|---|---|
| 03.17.01 | Supply Chain Risk Management Plan | — | Planned | Shared | N72 | None. |
| 03.17.02 | Acquisition Strategies, Tools, and Methods | — | Planned | Shared | N72 | None. |
| 03.17.03 | Supply Chain Requirements and Processes | — | Partial | Repo | N51, N52 | Swagger UI integrity check. Actions tag-pinned. No hashes, SBOM or scanning. |
