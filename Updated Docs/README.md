# 🌌 INFIVERSE-HR — Updated Documentation (Single Source of Truth)

**Status:** ✅ Verified 2026-08-14 (live VM + local tests)
**Owner:** Shashank Mishra
**System Owner:** Soham Kotkar & Vijay Dhawan (Sampada)

> This folder is the **single source of truth** for the INFIVERSE-HR (Sampada / BHIV) platform.
> All content was verified against the working codebase and the live production VM on 2026-08-14.
> Read the documents **in order** — the path is strictly linear with no circular references.

---

## 📚 Master Index (read in this order)

| # | Document | Purpose |
|---|----------|---------|
| 00 | [00_VERIFICATION_REPORT.md](00_VERIFICATION_REPORT.md) | What was tested (live + local) and the results |
| 01 | [01_PROJECT_OVERVIEW.md](01_PROJECT_OVERVIEW.md) | Product, portals, roles, scale, tech stack |
| 02 | [02_REPOSITORY_STRUCTURE.md](02_REPOSITORY_STRUCTURE.md) | Full verified folder map |
| 03 | [03_ARCHITECTURE.md](03_ARCHITECTURE.md) | Services, ports, data flows |
| 04 | [04_SETUP_AND_RUN.md](04_SETUP_AND_RUN.md) | Local setup + run steps (15-minute start) |
| 05 | [05_BACKEND_REFERENCE.md](05_BACKEND_REFERENCE.md) | Gateway / Agent / LangGraph internals, env inventory |
| 06 | [06_API_REFERENCE.md](06_API_REFERENCE.md) | Live endpoint inventory (**204 operations**) |
| 07 | [07_AUTHENTICATION_AND_SECURITY.md](07_AUTHENTICATION_AND_SECURITY.md) | Triple-auth, 2FA, RBAC, security controls |
| 08 | [08_DATABASE.md](08_DATABASE.md) | MongoDB collections, schemas, seed, migrations |
| 09 | [09_FRONTEND_REFERENCE.md](09_FRONTEND_REFERENCE.md) | Routes, pages, services, contexts, auth flow |
| 10 | [10_TESTING_AND_EVIDENCE.md](10_TESTING_AND_EVIDENCE.md) | Test suites + `evidence/` artifacts |
| 11 | [11_DEPLOYMENT.md](11_DEPLOYMENT.md) | Docker, CI/CD, VM / Render / Vercel |
| 12 | [12_OPERATIONS_RUNBOOK.md](12_OPERATIONS_RUNBOOK.md) | Monitoring, health checks, rollback |
| 13 | [13_GOVERNANCE_CONTROL_CENTER.md](13_GOVERNANCE_CONTROL_CENTER.md) | Governance, workforce OS, control center, SETU |
| 14 | [14_SCOPE_SPRINTS_VANA.md](14_SCOPE_SPRINTS_VANA.md) | SETU ecosystem, sprints, VANA, partners |
| 15 | [15_KNOWN_ISSUES_ARCHIVE_INDEX.md](15_KNOWN_ISSUES_ARCHIVE_INDEX.md) | Gaps/tech debt + archive map |
| 16 | [16_ECOSYSTEM_INTEGRATION_REFERENCE.md](16_ECOSYSTEM_INTEGRATION_REFERENCE.md) | Ecosystem integration deep-dive (9 repos, data flows, deployment) |
| 17 | [17_TENANT_HIERARCHY_AND_ADMIN_ACCESS.md](17_TENANT_HIERARCHY_AND_ADMIN_ACCESS.md) | Multi-tenant hierarchy (platform → key account → client), isolation rules, admin login link, SVA onboarding |
| REF | [BHIV_ECOSYSTEM_RUNTIME_CONVERGENCE_AUDIT.md](BHIV_ECOSYSTEM_RUNTIME_CONVERGENCE_AUDIT.md) | Full runtime convergence audit — live verification, gaps, AI tools, next steps |

**Start here → [00_VERIFICATION_REPORT.md](00_VERIFICATION_REPORT.md)**

---

## ⚡ 60-Second Orientation

- **What**: Enterprise AI-enabled multi-tenant HR recruitment + workforce-intelligence platform.
- **Backend**: 3 FastAPI microservices — Gateway `:8000` (172 ops), Agent `:9000` (AI matching),
  LangGraph `:9001` (workflows/notifications/RL). **204 total live API operations.**
- **Frontend**: React 18 + Vite + TypeScript SPA on `:3000` with candidate / recruiter / client /
  control-center portals.
- **Database**: MongoDB Atlas (`bhiv_hr`), 33 collections.
- **Ecosystem**: 9 integrated partner repos (Artha, ai-crm, Karma-Tracker, Prana, bucket,
  bhiv-registry, bhiv-intelligence-samachar, bhiv-SVACS, workflow-blackhole). Full profiles in
  `16_ECOSYSTEM_INTEGRATION_REFERENCE.md`.
- **Live production**: `https://sampada.blackholeinfiverse.com` — all services healthy (verified
  2026-08-14).

---

## 🧭 For New Developers

1. Read `00` → `15` in order (linear path, no skipping).
2. Follow `04_SETUP_AND_RUN.md` to start locally (verified: gateway starts in ~7 s, frontend build
   passes).
3. Use `06_API_REFERENCE.md` + `http://localhost:8000/docs` as your API truth.
4. Check `15_KNOWN_ISSUES_ARCHIVE_INDEX.md` before touching code that touches flagged areas.
5. For ecosystem context (partner repos, live state, gaps), see `16` and `BHIV_ECOSYSTEM_RUNTIME_CONVERGENCE_AUDIT.md`.

---

## 🗄️ Archived Documentation

All superseded documentation is preserved (non-destructively) in **[`archived/`](archived/)**
(131 files):

| Archived source | Copy location |
|-----------------|---------------|
| Root docs (README, QUICK_START, state, review, VANA, …) | [`archived/root/`](archived/root/) |
| Governance / control-center / Task19 docs | [`archived/docs/`](archived/docs/) |
| Handover package (00–13) | [`archived/handover/`](archived/handover/) |
| Backend docs | [`archived/backend-docs/`](archived/backend-docs/) |
| Backend handover | [`archived/backend-handover/`](archived/backend-handover/) |
| Frontend docs | [`archived/frontend/`](archived/frontend/) |

The obsolete source paths are added to the root `.gitignore` so they stop being tracked while
remaining on disk. **Nothing was deleted.**

---

## 🔗 Repository Roots

- Root readme: [`../README.md`](../README.md) (updated to point here)
- Code: [`../backend/`](../backend/), [`../frontend/`](../frontend/)
- Proof artifacts: [`../evidence/`](../evidence/) (live — referenced, not archived)

---

## 🔒 Constitutional Boundaries

- All dashboard / control-center / governance surfaces are **read-only observability** — no
  parallel execution authority.
- Escalation authority, schema mutations, security overrides, and final prioritization remain with
  System Owners **Soham Kotkar & Vijay Dhawan** (Sampada).
- Never commit secrets. Never delete files — archive instead.
