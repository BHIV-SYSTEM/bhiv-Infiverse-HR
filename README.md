# 🌌 INFIVERSE-HR (Sampada / BHIV)

Enterprise AI-enabled HR recruitment platform with dedicated portals for Candidates, Recruiters, and
Clients. Backend is a Python FastAPI microservice suite; frontend is React + Vite + TypeScript.

> ## 📖 Documentation
>
> **The primary documentation is the [Updated Docs](./Updated%20Docs/README.md) folder — the
> Single Source of Truth for this platform.** It was fully verified against the working codebase and
> the live production VM on **2026-08-14**.
>
> Start at the [master index](./Updated%20Docs/README.md), then read documents `00` → `15` in order.

---

## 🧭 Quick Orientation

| Layer | Detail |
|-------|--------|
| **Gateway** | FastAPI `:8000` — auth, jobs, candidates, applications, notifications, workforce governance, control center, workflow bridge |
| **Agent** | FastAPI `:9000` — AI candidate-job semantic matching |
| **LangGraph** | FastAPI `:9001` — workflow automation, RL engine, Email/WhatsApp/Telegram notifications |
| **Frontend** | React 18 + Vite + TS, dev `:3000` — Candidate / Recruiter / Client / Control Center portals |
| **Database** | MongoDB Atlas (`bhiv_hr`) |
| **Live API surface** | **204 operations** (gateway 172 · agent 6 · langgraph 26) — verified from live OpenAPI |

### Local URLs

- Frontend: `http://localhost:3000`
- Gateway: `http://localhost:8000` (`/docs`, `/health`)
- Agent: `http://localhost:9000` (`/docs`, `/health`)
- LangGraph: `http://localhost:9001` (`/docs`, `/health`)

### Production (verified healthy 2026-08-14)

- `https://sampada.blackholeinfiverse.com` — frontend
- `https://sampada.blackholeinfiverse.com/gateway/health` — gateway v4.2.0
- `https://sampada.blackholeinfiverse.com/agent/health` — agent v3.0.0
- `https://sampada.blackholeinfiverse.com/langgraph/health` — langgraph

---

## 🗺️ Where to Look

| Need | Go to |
|------|-------|
| First-day onboarding | `Updated Docs/00_VERIFICATION_REPORT.md` → `04_SETUP_AND_RUN.md` |
| API reference | `Updated Docs/06_API_REFERENCE.md` (+ `http://localhost:8000/docs`) |
| Architecture | `Updated Docs/03_ARCHITECTURE.md` |
| Database | `Updated Docs/08_DATABASE.md` |
| Frontend | `Updated Docs/09_FRONTEND_REFERENCE.md` |
| Deployment / ops | `Updated Docs/11_DEPLOYMENT.md`, `12_OPERATIONS_RUNBOOK.md` |
| Governance / control center | `Updated Docs/13_GOVERNANCE_CONTROL_CENTER.md` |
| Known issues | `Updated Docs/15_KNOWN_ISSUES_ARCHIVE_INDEX.md` |
| Archived (superseded) docs | `Updated Docs/archived/` |

---

## 🏃 Local Startup (summary)

```powershell
# Backend
cd backend
copy .env.example .env
setup_venv.bat          # first time only
run_with_venv.bat       # starts gateway:8000, agent:9000, langgraph:9001
python run_services.py       


# Frontend (separate terminal)
cd frontend
copy .env.example .env
npm install
npm run dev             # http://localhost:3000
```

Full step-by-step instructions, environment variables, Docker, and troubleshooting:
`Updated Docs/04_SETUP_AND_RUN.md`.

---

## 🧪 Testing

- `pytest backend/tests/gateway/test_gateway_imports.py backend/tests/gateway/test_workforce_lifecycle.py` — verified 5/5 pass.
- `cd frontend && npm run build` — verified production build (`tsc && vite build`) passes.
- Full suite inventory: `Updated Docs/10_TESTING_AND_EVIDENCE.md`.
- Live verification results: `Updated Docs/00_VERIFICATION_REPORT.md`.

---

## 🔒 Constitutional Alignment Rules

As a developer working on the **Sampada** scope:

- **Visibility Only**: All dashboard and intelligence features are strictly read-only on execution
  authority. You must not introduce parallel orchestration frameworks or state-mutating handlers.
- **System Boundaries**: Escalation authority, database schema mutations, security authorization
  overrides, and final prioritization remain with System Owner **Rishabh Yadav**.
- **Non-Destructive**: Archive rather than delete. Never commit secrets.

---

## 📦 Ecosystem

Partner repos (`ai-crm`, `Artha`, `Prana`, `Karma-Tracker`, `bhiv-registry`, `bhiv-SVACS`,
`bhiv-intelligence-samachar`, `bucket`, `workflow-blackhole`) are gitignored integration
references. See `Updated Docs/14_SCOPE_SPRINTS_VANA.md` and the archived
`ECOSYSTEM_REPOSITORY_MAP.md` for the ecosystem map.
