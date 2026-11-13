# 03 — Architecture

**Status:** ✅ Verified (2026-08-14)
**Owner:** Shashank Mishra

> Service interaction, ports, and data-flow map for the INFIVERSE-HR platform. Read after
> `02_REPOSITORY_STRUCTURE.md`.

---

## 1. High-Level View

```text
                        ┌─────────────────────────────┐
  Browser               │  Frontend (React + Vite)     │
  ─────────▶ :3000      │  sampada.blackholeinfiverse  │
                        └──────────────┬──────────────┘
                                       │ axios (VITE_API_BASE_URL)
                                       ▼
                        ┌─────────────────────────────┐
                        │  Gateway  :8000  (FastAPI)   │
                        │  triple-auth · validation ·  │
                        │  tenant isolation · metrics  │
                        └──────┬──────┬──────┬────────┘
                               │      │      │
                 AGENT_SERVICE_URL  LANGGRAPH_SERVICE_URL
                               │      │      │
                     ┌─────────▼─┐  ┌─▼─────────────┐
                     │ Agent :9000│  │ LangGraph :9001 │
                     │ AI matching│  │ workflows · RL ·│
                     │            │  │ notifications    │
                     └─────┬──────┘  └────┬───────────┘
                           │              │
                           ▼              ▼
                     ┌─────────────────────────────┐
                     │   MongoDB Atlas  db: bhiv_hr │
                     └─────────────────────────────┘

  External:  Complete-Infiverse (workflow bridge, WORKFLOW_API_BASE_URL)
             SETU partners (POST /v1/setu/signals/{signal_type})
             Email / WhatsApp (Twilio) / Telegram / Gemini (LangGraph side)
```

---

## 2. Service Ports & Entry Points

| Service | Port | Entry (uvicorn) | Health endpoint |
|---------|------|-----------------|-----------------|
| Gateway | 8000 | `app.main:app` | `GET /health` |
| Agent | 9000 | `app:app` | `GET /health` |
| LangGraph | 9001 | `app.main:app` | `GET /health` |
| Portal (legacy) | 8501 | Streamlit `app.py` | `/_stcore/health` |
| Client portal (legacy) | 8502 | Streamlit | `/_stcore/health` |
| Candidate portal (legacy) | 8503 | Streamlit | `/_stcore/health` |
| Frontend (dev) | 3000 | Vite dev server | `GET /` (HTML) |

Internal service URLs (from `backend/.env.example`):
`GATEWAY_SERVICE_URL=http://localhost:8000`, `AGENT_SERVICE_URL=http://localhost:9000`,
`LANGGRAPH_SERVICE_URL=http://localhost:9001`.

---

## 3. The Gateway (Port 8000) — Core Responsibilities

The gateway is the **only** public API surface for the frontend. Verified responsibilities:

1. **Authentication (triple-layer)** — `jwt_auth.py` `get_auth`:
   - API key (`Authorization: Bearer <API_KEY_SECRET>`) → admin role.
   - Candidate JWT (`CANDIDATE_JWT_SECRET_KEY`, HS256) → candidate/recruiter roles.
   - Client JWT (`JWT_SECRET_KEY`) → client/HR roles.
   - Role guards: `get_candidate_auth`, `get_recruiter_auth`, `get_client_auth`,
     `get_admin_auth`, `get_optional_auth`, `require_role(...)`, plus the
     service-only `get_service_auth` (API key; used by NDA admin ops + quarantine).
2. **Input validation** — XSS/SQLi blocking; email/phone validation; password policy; CSP
   reporting; rate limiting; blocked-IP tracking (see `07_AUTHENTICATION_AND_SECURITY.md`).
3. **Multi-tenant isolation** — client-scoped data access rules.
4. **Domain APIs** — jobs, candidates, applications, interviews, offers, feedback, documents,
   portal notifications, client/recruiter stats.
5. **Integration proxies** — Agent (matching), LangGraph (workflows/RL), Complete-Infiverse
   workflow bridge (`workflow_proxy.py`), AI (`routes/ai_integration.py`).
6. **Workforce governance** — orgs/divisions/units/departments/employees, policies, governance
   challenges/reviews/overrides, decisions, SETU signals (`workforce_governance_routes.py`).
7. **Control center** — audit events, replay, dashboard aggregates.
8. **Monitoring** — Prometheus `/metrics`, `/health/detailed`, `/metrics/dashboard`.

### Gateway router mounting (verified)

| Router | Prefix | Backing module | Endpoints |
|--------|--------|----------------|-----------|
| `ai_router` | `/api/v1` | `routes/ai_integration.py` | test-communication, gemini/analyze |
| `langgraph_router` | `/api/v1` | `langgraph_integration.py` | workflow trigger/status/list, webhooks |
| `rl_router` | `/api/v1` | `routes/rl_routes.py` | rl predict/feedback/analytics/performance |
| `workflow_proxy_router` | (none) | `workflow_proxy.py` | `/v1/candidate/workflow-*` bridge |
| `workforce_governance_router` | (none) | `routes/workforce_governance_routes.py` | 45 routes |

---

## 4. The Agent (Port 9000) — AI Matching

- Endpoints: `GET /`, `GET /health`, `GET /test-db`, `POST /match`, `POST /batch-match`,
  `GET /analyze/{candidate_id}`.
- Model: sentence-transformers semantic embeddings; scores candidate-job similarity.
- Uses PyMongo (sync); HF token via `HF_TOKEN`.
- Invoked by gateway at `AGENT_SERVICE_URL` (frontend calls it only in dev via
  `VITE_AGENT_SERVICE_URL`; in production all matching goes through the gateway proxy).

---

## 5. The LangGraph (Port 9001) — Workflows & Notifications

- Endpoints: 26 operations — workflow lifecycle (`POST /workflows/application/start`,
  `GET /workflows/{id}/status`, `POST /workflows/{id}/resume`, `WS /ws/{id}`, `GET /workflows`),
  automation/notifications (send, test email/whatsapp/telegram/buttons/sequence, bulk, preview,
  whatsapp webhook, stats), RL (`/rl/*`: predict, feedback, analytics, performance, history,
  retrain, start-monitoring).
- Agents (4) in `agents.py`; state machine in `graphs.py` (`CandidateApplicationState`);
  communication via `communication.py` (Twilio WhatsApp, Telegram, Gmail).
- Persistence: `mongodb_tracker.py` (`workflows` collection, in-memory fallback),
  `mongodb_checkpointer.py`, `rl_database.py`.
- Governance advisory returned on `/health`.

---

## 6. Data Layer

- **Primary store**: MongoDB Atlas, database `bhiv_hr`.
- **Gateway**: Motor async client (pool `maxPoolSize=10`, `minPoolSize=2`) — `app/database.py`.
- **Agent / LangGraph**: PyMongo sync clients.
- **34 collections** extracted from source (full schema in `08_DATABASE.md`):
  `application_documents, audit_logs, candidates, challenges, client_connected_recruiter,
  clients, decisions, departments, divisions, employees, feedback, interviews, job_applications,
  jobs, matching_cache, notification_logs, offers, organizations, policy_definitions,
  policy_evaluations, policy_overrides, policy_registry, portal_notifications, reviews,
  rl_feedback, rl_model_performance, rl_predictions, rl_training_data, schema_version,
  setu_signals, units, workflow_overrides, workflows`.
- **Migration note**: PostgreSQL → MongoDB Atlas completed 2026-01-22. PostgreSQL references in
  `services/db/` and `runtime-core/` are legacy only.

### Workforce lineage envelope

Workforce documents (organizations, divisions, units, departments, employees) wrap data in a
`LineageEnvelope` (`app/lineage_envelope.py`) containing tenant_id scoping and write-audit hooks;
lifecycle state transitions are defined in `app/workforce_common.py`.

---

## 7. Frontend ↔ Backend Flow (production)

1. User loads `https://sampada.blackholeinfiverse.com` → Vercel/VM frontend.
2. Frontend axios client uses `VITE_API_BASE_URL` → `https://sampada.blackholeinfiverse.com/gateway`.
3. Auth: `AuthContext` → `authService.login/register` → gateway `/v1/client/login` or
   `/v1/candidate/login` → JWT stored (sessionStorage-first, localStorage fallback).
4. Every subsequent request carries `Authorization: Bearer <token>` via axios interceptor.
5. Protected frontend routes gate on role via `ProtectedRoute` (roles candidate/recruiter/client).

> In local dev, `src/services/api.ts` `resolveServiceBaseUrl()` forces local ports
> (8000/9000/9001) even if `.env` holds Render URLs.

---

## 8. Cross-Cutting Concerns

- **Observability**: Prometheus `/metrics` on gateway; health/detailed dashboards; audit_logs;
  correlation IDs in agent health responses.
- **Security**: see `07_AUTHENTICATION_AND_SECURITY.md`.
- **Config**: one `.env` per environment; secrets never committed (see `04_SETUP_AND_RUN.md` and
  `11_DEPLOYMENT.md`).
- **Isolation**: tenant/client isolation enforced at gateway layer; workforce/tenant scoping via
  lineage envelopes.

---

## 9. Next

→ `04_SETUP_AND_RUN.md` — how to run the whole stack locally.
