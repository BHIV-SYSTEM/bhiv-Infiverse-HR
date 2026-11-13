# 02 — Repository Structure

**Status:** ✅ Verified (2026-08-14)
**Owner:** Shashank Mishra

> Complete, verified map of the `INFIVERSE-HR-PLATFORM` repository. Read this after
> `01_PROJECT_OVERVIEW.md`. Every path listed here exists on disk (verified during the audit).

---

## 1. Repository Root

```text
INFIVERSE-HR-PLATFORM/
├── README.md                       # Root readme (points to "Updated Docs" as primary reference)
├── QUICK_START.md                  # Legacy quick-start (archived → archived/root/)
├── Handover.md                     # Legacy handover entry (archived)
├── REVIEW_PACKET.md                # Legacy review packet (archived)
├── SAMPADA_CURRENT_STATE.md        # Legacy state doc (archived)
├── ECOSYSTEM_REPOSITORY_MAP.md     # SETU partner repo map (archived)
├── CONTRIBUTION_LOG.md             # Contribution history (archived)
├── PARTNER_SETU_LIVE_RUNBOOK.md    # Partner SETU closeout runbook (archived)
├── VANA_REUSABILITY_SAMPADA.md     # Vana reusability audit — Sampada (archived)
├── VANA_REUSABILITY_SAMACHAR.md    # Vana reusability audit — Samachar (archived)
├── Updated Docs/                   # ⭐ THIS documentation set (single source of truth)
│   ├── README.md                   # Master index
│   ├── 00_VERIFICATION_REPORT.md
│   ├── 01_PROJECT_OVERVIEW.md
│   ├── ...  (02–15)
│   └── archived/                   # Copies of all superseded docs
├── backend/                        # FastAPI microservices + tools
├── frontend/                       # React + Vite + TS SPA
├── docs/                           # Legacy governance/control-center docs (archived)
├── handover/                       # Legacy handover package (archived)
├── evidence/                       # Verification proof artifacts (still live)
├── tests/                          # Root test file (test_schemas.py) — real suites live in backend/tests/
├── scripts/                        # Deployment/cleanup scripts
├── local-data/                     # Local persistent data (Postgres ignored)
├── .github/workflows/deploy.yml    # CI/CD: build → VM deploy → rollback
├── docker-compose.production.yml   # Legacy compose template (images bhiv/hr-*)
├── docker-compose.production.template.yml
├── run_project.ps1                 # Interactive launcher (backend + frontend)
├── run_project.bat                 # Windows launcher
├── START_BACKEND.ps1               # Backend-only launcher
├── START_FRONTEND.ps1              # Frontend-only launcher
├── package-lock.json               # Root lockfile (frontend deps at frontend/package-lock.json)
└── .gitignore                      # Ignore rules (incl. archived-doc entries appended 2026-08-14)
```

**Gitignored folders present on disk (partner/external repos — see `16_ECOSYSTEM_INTEGRATION_REFERENCE.md` for full profiles):**
`ai-crm/`, `Artha/`, `Prana/`, `Karma-Tracker/`, `bhiv-registry/`, `bhiv-SVACS/`,
`bhiv-intelligence-samachar/`, `bucket/`, `workflow-blackhole/`, plus `local-data/postgres/`,
`Complete-Infiverse/`.

### Integrated Repository Summary

| Repo | Stack | Port(s) | Role |
|------|-------|---------|------|
| `Artha/` | Node.js/Express/MongoDB/Redis + React + Python | 5000 | India-compliant accounting (GST/TDS, HMAC ledger) |
| `ai-crm/` | Node.js/Express/MongoDB + Python/FastAPI + React | 8001/8002 | Logistics/inventory CRM |
| `Karma-Tracker/` | Python/FastAPI + MongoDB + NumPy | 8003/8030 | Karma scoring (passive mode) |
| `Prana/` | Vanilla JS (browser-only) | — | Cognitive state capture |
| `bucket/` | Python/FastAPI + MongoDB + Redis | 8001/8010 | Append-only immutable storage |
| `bhiv-registry/` | Python/FastAPI + PostgreSQL | 8020 | Dataset metadata registry (MDU) |
| `bhiv-intelligence-samachar/` | Python/FastAPI + Next.js 14 | 8000/3000 | News AI platform |
| `bhiv-SVACS/` | Python/FastAPI + PyTorch + React | 8000/5173 | Maritime vessel classification |
| `workflow-blackhole/` | Node.js/Express/MongoDB/Socket.IO + React 19 | 5000/80 | Workforce management + Docker orchestration hub |

---

## 2. `backend/` — FastAPI Microservices

```text
backend/
├── .env                          # Configured environment (gitignored — never commit)
├── .env.example                  # 110-line template — full variable inventory in 05_BACKEND_REFERENCE.md
├── requirements.txt              # Top-level requirements
├── run_services.py               # Launcher: gateway:8000, agent:9000, langgraph:9001
├── run_test_simple.py
├── run_with_venv.bat             # Activates venv & runs run_services.py
├── setup_venv.bat                # Creates Windows venv
├── seed_mongodb.py               # Seeds bhiv_hr database (jobs, candidates, clients, ...)
├── verify_hf_token.py            # HuggingFace token check
├── docker-compose.production.yml # ⭐ Working production compose (Atlas edition, 272 lines)
├── services/
│   ├── gateway/                  # Port 8000 — main API (app/main.py: 6547 lines)
│   ├── agent/                    # Port 9000 — AI matching (app.py)
│   ├── langgraph/                # Port 9001 — workflows + notifications (app/main.py: 1201 lines)
│   ├── portal/                   # Streamlit HR dashboard (LEGACY) port 8501
│   ├── client_portal/            # Streamlit client portal (LEGACY) port 8502
│   ├── candidate_portal/         # Streamlit candidate portal (LEGACY) port 8503
│   └── db/                       # Legacy PostgreSQL schema/migrations (reference only)
├── runtime-core/                 # LEGACY "Sovereign Application Runtime" (SAR v1.0.0) — reference only
├── assets/                       # Static assets
├── docs/                         # Legacy backend docs (archived → Updated Docs/archived/backend-docs/)
├── handover/                     # Legacy backend handover (archived → archived/backend-handover/)
├── scripts/                      # Cleanup/migration scripts
├── tests/                        # ⭐ pytest suites (inventory in 10_TESTING_AND_EVIDENCE.md)
└── tools/                        # Analysis, data, database, fixes, monitoring, security, utilities
```

### `backend/services/gateway/` detail

```text
gateway/
├── app/
│   ├── main.py                   # 112 route decorators + 5 mounted routers
│   ├── database.py               # Motor async (pool max 10 / min 2)
│   ├── db_helpers.py             # ObjectId helpers
│   ├── control_center_governance.py
│   ├── decision_ledger.py
│   ├── decision_workflow.py
│   ├── policy_engine.py
│   ├── setu_participation.py
│   ├── lineage_envelope.py
│   ├── workforce_common.py
│   ├── workforce_lifecycle.py
│   ├── workforce_runtime.py      # orgs/divisions/units/departments/employees models
│   ├── nda.py                    # 1111 lines — NDA gate (template, verify, tenant review)
│   └── verification.py           # 325 lines — KYC identity verification (text + name match)
├── routes/
│   ├── ai_integration.py         # /api/v1 AI router
│   ├── rl_routes.py              # /api/v1 RL router
│   ├── tenancy_routes.py         # 1098 lines — admin login, tenant hierarchy, invites
│   └── workforce_governance_routes.py  # 45 workforce routes
├── config.py                     # Requires DATABASE_URL, API_KEY_SECRET, JWT secrets, service URLs
├── dependencies.py               # Re-exports auth deps
├── jwt_auth.py                   # 393 lines — triple-auth model + get_service_auth
├── langgraph_integration.py      # LangGraph router
├── monitoring.py                 # Prometheus metrics
├── workflow_proxy.py             # 676 lines — Complete-Infiverse bridge
├── create_mongodb_indexes.py
├── migrate_mongodb_schema.py
└── requirements.txt              # fastapi<0.120, uvicorn<0.30, motor, pydantic, python-jose, pyotp, ...
```

---

## 3. `frontend/` — React + Vite SPA

```text
frontend/
├── package.json                  # react 18.2, vite 7.3, TS 5.3, axios, react-router-dom, xlsx
├── vite.config.ts                # dev server host:true port:3000
├── tsconfig.json                 # strict, ES2020, jsx react-jsx
├── tailwind.config.js            # dark mode, shadcn-style tokens
├── postcss.config.js
├── vercel.json                   # SPA rewrite + build config
├── Dockerfile                    # node:20-alpine build → serve on 3000
├── .env / .env.example           # VITE_* variables (gitignored)
├── index.html                    # "Sampada - HR Recruitment System"
└── src/
    ├── main.tsx                  # StrictMode + ErrorBoundary entry
    ├── App.tsx                   # ⭐ Route tree (authoritative) — see 09_FRONTEND_REFERENCE.md
    ├── routes.tsx                # Redundant/unused legacy route set (kept for reference)
    ├── ErrorBoundary.tsx
    ├── pages/                    # auth/, candidate/, recruiter/, client/, control/, hr/
    ├── components/               # layouts/, sidebars/, navbars/, cards/, recruiter/, config/
    ├── context/                  # AuthContext, ThemeContext, SidebarContext, RecruiterConnectionContext, CandidateTasksContext
    ├── services/                 # api.ts (axios client, 2717 lines), authService.ts
    ├── config/                   # notifications.config.ts
    └── utils/
```

---

## 4. Documentation Locations (before this update)

| Location | Content | Status now |
|----------|---------|------------|
| `docs/` (30 files) | Governance models, control-center, Task19, SETU, workforce | Archived → `Updated Docs/archived/docs/` |
| `handover/` (17 files) | 00–13 handover deliverables | Archived → `Updated Docs/archived/handover/` |
| `backend/docs/` (8 subfolders) | Backend API/database/security/testing docs | Archived → `Updated Docs/archived/backend-docs/` |
| `backend/handover/` (21 files) | Legacy backend handover | Archived → `Updated Docs/archived/backend-handover/` |
| `frontend/*.md` (3 files) | Frontend README, auth structure, Vercel deploy | Archived → `Updated Docs/archived/frontend/` |
| Root `*.md` (10 files) | README, quick start, state, review, VANA, etc. | Archived → `Updated Docs/archived/root/` |
| `evidence/` | Proof artifacts (11 subdirs) | **Live** — referenced, not archived |
| `Updated Docs/` (17→18 files) | Master docs 00–15 + **16_ECOSYSTEM_INTEGRATION_REFERENCE** | **Single source of truth** |

---

## 5. `evidence/` — Verification Artifacts

```text
evidence/
├── boundaries/                   # Visibility boundary verification
├── entry-points/                 # Token templates + curl tests
├── failure/                      # Vulnerability blocks + failure logs
├── general/                      # Unified verification summaries
├── ownership/                    # Responsibility matrix
├── replay/                       # Chronological state reconstruction
├── trace-continuity/             # Correlation-ID request logs
├── live_workforce_governance_setu/
├── phase_iv_production_validation/
├── phase_iv_tier1/
└── workforce_runtime/
```

> These are live proof artifacts produced by the test harnesses in `10_TESTING_AND_EVIDENCE.md`.
> They remain tracked and referenced; they are documentation *evidence*, not outdated docs.

---

## 6. `scripts/` & `backend/tools/`

**`scripts/` (root):**
- `cleanup_keep_latest_14_jobs.py` + `.mongosh.js` — prune active jobs to the 14 most recent.
- `migrate_interview_dates.py` — string `interview_date` → datetime migration (Motor).
- `local-deploy.cmd` — 5-step Docker deploy.
- `setup_insightflow_postgres.ps1` — One-time PostgreSQL setup for bhiv-registry (creates `bhiv_registry` database).
- `start_all_ecosystem_services.ps1` — Starts ALL 9 ecosystem services as hidden PowerShell processes (gateway :8000, frontend :3000, Artha :5000, Niyantran :5001, ai-crm Python :8001, ai-crm Node :8002, Bucket :8010, InsightFlow :8020, Karma :8030).
- `start_all_ecosystem_services.cmd` — CMD wrapper for the PowerShell ecosystem launcher.

**`backend/tools/`** — 10 categories of helper scripts: `analysis/`, `data/`, `database/`,
`fixes/`, `monitoring/`, `portal/`, `security/`, `utilities/` (13 scripts), plus
`requirements.txt` and `setup_advanced_tools.py`.

---

## 7. Conventions Used Across the Codebase

- Backend config: one `config.py` per service; env via `python-dotenv`; secrets only from env, never
  hardcoded.
- MongoDB: Motor (async) in gateway; PyMongo (sync) in agent/langgraph.
- Frontend: API calls go through the axios client in `src/services/api.ts`; auth state via
  `AuthContext`; route protection via `ProtectedRoute`.
- Testing: pytest; `backend/tests/e2e/control_center/pytest.ini` defines `e2e`/`e2e_unit` markers.
- Docs: title + Status/Owner/Last-updated header block; numbered sections; tables for inventories.

---

## 8. Next

→ `03_ARCHITECTURE.md` — service interactions, ports, and data flows.
