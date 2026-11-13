# 08 — Database

**Status:** ✅ Verified (2026-08-14)
**Owner:** Shashank Mishra

> MongoDB Atlas is the sole production datastore. This document is the verified collection and
> schema reference. Read after `07_AUTHENTICATION_AND_SECURITY.md`.

---

## 1. Connection Facts (Verified)

| Item | Value |
|------|-------|
| Engine | MongoDB Atlas (db `bhiv_hr`) |
| Gateway client | Motor (async), `app/database.py`, pool `maxPoolSize=10` / `minPoolSize=2` |
| Agent / LangGraph clients | PyMongo (sync) |
| Env vars | `DATABASE_URL` / `MONGODB_URI`, `MONGODB_DB_NAME` (default `bhiv_hr`) |
| Migration status | PostgreSQL → MongoDB Atlas completed **2026-01-22**; Postgres is legacy/reference only |
| `schema_version` document | `4.3.0` (seeded) |

---

## 2. Collections (34 — extracted from service source)

| Collection | Created by | Purpose |
|-----------|-----------|---------|
| `application_documents` | gateway | Per-application document uploads (resume/NDA) |
| `audit_logs` | gateway | Security + control-center audit trail |
| `candidates` | gateway/agent | Candidate profiles (email unique, bcrypt `password_hash`; `kyc` sub-document holds the identity-verification verdict) |
| `challenges` | gateway governance | Governance challenges |
| `client_connected_recruiter` | gateway | Client ↔ recruiter connection records |
| `clients` | gateway | Tenant clients (email unique, `client_code` unique) |
| `decisions` | gateway governance | Decision ledger entries |
| `departments` | gateway workforce | Department nodes (lineage-enveloped) |
| `divisions` | gateway workforce | Division nodes |
| `employees` | gateway workforce | Employee lifecycle records |
| `feedback` | gateway | Candidate feedback submissions |
| `interviews` | gateway | Scheduled interviews |
| `job_applications` | gateway | Applications (candidate_id+job_id unique) |
| `jobs` | gateway | Job postings (status, client_code, created_at) |
| `matching_cache` | gateway/agent | Semantic match results cache |
| `notification_logs` | langgraph | Sent-notification history |
| `offers` | gateway | Job offers |
| `organizations` | gateway workforce | Org hierarchy roots (lineage-enveloped) |
| `policy_definitions` | gateway governance | Policy rule definitions |
| `policy_evaluations` | gateway governance | Policy evaluation results |
| `policy_overrides` | gateway governance | Policy override records |
| `policy_registry` | gateway governance | Policy registry |
| `portal_notifications` | gateway | Bell-feed notifications per user |
| `reviews` | gateway governance | Governance review assignments |
| `rl_feedback` | langgraph RL | RL feedback (prediction_id) |
| `rl_model_performance` | langgraph RL | Model performance metrics |
| `rl_predictions` | langgraph RL | RL predictions (candidate+job, created_at) |
| `rl_training_data` | langgraph RL | RL training samples |
| `schema_version` | seed | Schema version (4.3.0) |
| `setu_signals` | gateway SETU | Inbound SETU signals (signal_type) |
| `tenant_invites` | gateway tenancy | Single-use company share links (token unique, TTL, revocable) |
| `units` | gateway workforce | Unit nodes (lineage-enveloped) |
| `workflow_overrides` | gateway governance | Workflow override records |
| `workflows` | langgraph | LangGraph workflow instances |

Additional runtime collections (doc'd in legacy docs): `api_keys`, `rate_limits` (TTL),
`csp_violations`, `sessions`, `ml_feedback`, `performance_metrics`,
`company_scoring_preferences`.

### 2a. Governance & Workforce Collections (documented 2026-08-17)

These collections support the workforce governance, policy engine, and SETU subsystems. They are
created on first use by the corresponding gateway modules.

| Collection | Module | Purpose | Key Fields |
|-----------|--------|---------|------------|
| `decision_ledger` | `decision_ledger.py` | Immutable governance decision records | `decision_id`, `action`, `resource`, `user_id`, `details`, `correlation_id`, `timestamp` |
| `policy_registry` | `policy_engine.py` | Policy definitions registered in the system | `policy_id`, `name`, `scope`, `rules`, `created_at` |
| `policy_evaluations` | `policy_engine.py` | Results of policy evaluations against resources | `evaluation_id`, `policy_id`, `resource_id`, `result`, `timestamp` |
| `policy_overrides` | `policy_engine.py` | Policy exception/override records | `override_id`, `policy_id`, `scope`, `authorized_by`, `reason`, `expires_at` |
| `workforce` | `workforce_runtime.py` | Employee/organization records (lineage-enveloped) | `employee_id`, `org_id`, `department_id`, `status`, `lifecycle_state`, `lineage_envelope` |
| `setu_signals` | `setu_participation.py` | Inbound SETU signals from partner systems | `signal_id`, `signal_type`, `source_system`, `payload`, `trace_id`, `received_at` |
| `challenges` | `decision_workflow.py` | Governance challenge records | `challenge_id`, `decision_id`, `challenger`, `reason`, `status` |
| `reviews` | `decision_workflow.py` | Review assignment records | `review_id`, `resource_id`, `reviewer`, `status`, `decision` |
| `workflow_overrides` | `decision_workflow.py` | Workflow override records | `override_id`, `workflow_id`, `scope`, `authorized_by`, `applied_at` |

---

## 3. Key Indexes (from `seed_mongodb.py` + gateway migrations)

| Collection | Index |
|-----------|-------|
| `candidates` | `email` (unique), `status`, `created_at` |
| `jobs` | `status`, `client_code`, `created_at` |
| `job_applications` | `{candidate_id, job_id}` (unique), `status` |
| `clients` | `email` (unique), `client_code` (unique) |
| `users` | `email` (unique), `username` (unique) |
| `rl_predictions` | `{candidate_id, job_id}`, `created_at` |
| `rl_feedback` | `prediction_id` |
| `rate_limits` | `{client_ip, endpoint}`, `expires_at` (TTL) |
| `csp_violations` | `timestamp` |
| `tenant_invites` | `token` (unique), `{tenant_id, created_at}` |

---

## 4. Seed Script (`backend/seed_mongodb.py`, 640 lines)

Run: `cd backend && .\venv\Scripts\python.exe seed_mongodb.py`
Reads `DATABASE_URL` (required), `MONGODB_DB_NAME` (default `bhiv_hr`). Drops collections if
`jobs` already has data (prompts).

| Entity | Quantity / detail |
|--------|-------------------|
| Jobs | 5 (incl. Senior Python Developer, AI/ML Engineer) |
| Candidates | 20 (random from a skills pool) |
| Applications | 15 |
| Clients | 3 — `TECH001`, `AI002`, `INFRA003` |
| Users | 3 — admin, hr_manager, recruiter1 (placeholder hashes) |
| Interviews | 5 |
| Feedback | 10 |
| RL | `rl_predictions` 10, `rl_feedback` 5, `rl_model_performance` 2, `rl_training_data` 2 |
| Workflows / offers | 2 each |
| Audit logs | 2 |
| Matching cache | 1 |
| Company scoring prefs | 2 |
| Schema version | 1 (`4.3.0`) |

> Placeholder user hashes mean seeded login credentials require re-hashing in production; demo
> passwords live in the owner's secure channel.

---

## 5. Schema/Index Management Scripts

| Script | Purpose |
|--------|---------|
| `backend/services/gateway/create_mongodb_indexes.py` | Create application indexes |
| `backend/services/gateway/migrate_mongodb_schema.py` | Schema migration |
| `scripts/migrate_interview_dates.py` | String `interview_date` → datetime (Motor) |
| `scripts/cleanup_keep_latest_14_jobs.py` (+ `.mongosh.js`) | Prune active jobs to newest 14 (preview/backup first) |
| `services/db/*.sql` | **Legacy** PostgreSQL schema (reference only) |

---

## 6. Workforce Lineage (schema-in-practice)

Workforce collections (`organizations`, `divisions`, `units`, `departments`, `employees`) wrap
records in a `LineageEnvelope` containing:
- `tenant_id` scoping
- audit/write hooks (`write_workforce_audit` → `audit_logs`)
- lifecycle transition metadata (e.g. `transition_type=employee_promotion`)

Hierarchy: **organization → divisions → units → departments → employees**. Lifecycle states and
transitions are defined in `app/workforce_common.py`.

---

## 7. Backup & Restore (operational)

- Atlas managed backups are the primary restore path (see `12_OPERATIONS_RUNBOOK.md`).
- Job-cleanup scripts create backups in the `job_cleanup_backups` collection before pruning.
- No local Mongo is required for normal operation; all data is remote (Atlas).

---

## 8. Verification Notes (2026-08-14)

- `backend/.env` contains a valid Atlas connection URI and `MONGODB_DB_NAME=bhiv_hr` (presence
  verified; value not printed).
- Gateway connects via Motor (async) — confirmed by successful local `/health` and app import.
- Collection list cross-checked against service source imports.

---

## 9. Next

→ `09_FRONTEND_REFERENCE.md`.
