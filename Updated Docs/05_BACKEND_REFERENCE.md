# 05 — Backend Reference

**Status:** ✅ Verified (2026-08-14 — gateway import + startup + unit tests passed)
**Owner:** Shashank Mishra

> Deep dive into each backend service. Read after `04_SETUP_AND_RUN.md`.

---

## 1. Service Matrix (Verified)

| Service | Path | Port | Entry | Language/Framework |
|---------|------|------|-------|--------------------|
| Gateway | `backend/services/gateway` | 8000 | `uvicorn app.main:app` | Python 3.12 / FastAPI / Motor |
| Agent | `backend/services/agent` | 9000 | `uvicorn app:app` | Python / FastAPI / PyMongo / sentence-transformers |
| LangGraph | `backend/services/langgraph` | 9001 | `uvicorn app.main:app` | Python / FastAPI / langgraph / PyMongo |
| Portal (legacy) | `backend/services/portal` | 8501 | Streamlit | Python / Streamlit |
| Client portal (legacy) | `backend/services/client_portal` | 8502 | Streamlit | Python / Streamlit |
| Candidate portal (legacy) | `backend/services/candidate_portal` | 8503 | Streamlit | Python / Streamlit |

---

## 2. Gateway (`services/gateway`)

### Runtime facts (verified)

- Default port `8000`; OpenAPI at `/docs`; health at `/health`.
- `app/main.py` — 6547 lines, **112 route decorators**, 5 mounted routers.
- App import test: **121 routes registered** (2026-08-14); live OpenAPI: **172 operations**.
- DB: Motor async singleton (`app/database.py`, pool max 10 / min 2).

### Core modules

| Module | Responsibility |
|--------|----------------|
| `config.py` | Requires `DATABASE_URL`, `API_KEY_SECRET`, `JWT_SECRET_KEY`, `CANDIDATE_JWT_SECRET_KEY`, `AGENT_SERVICE_URL`, `LANGGRAPH_SERVICE_URL` |
| `jwt_auth.py` | Triple-layer auth (API key / candidate JWT / client JWT), role guards, audience validation, service-only guard `get_service_auth` |
| `dependencies.py` | Re-exports auth dependencies for routers |
| `routes/tenancy_routes.py` | Platform-admin login, tenant hierarchy CRUD, key-account sub-client management, single-use invite share links |
| `app/nda.py` | Candidate NDA gate: official template render, PyPDF2 auto-verification, tenant-company review, enforcement helper |
| `app/verification.py` | KYC identity verification: PDF text extraction + name match → honest VERIFIED/REJECTED/UNVERIFIABLE verdicts (legacy on Profile) |
| `app/contact_verification.py` | Contact verification: email OTP for independent candidates (Gmail SMTP; hired/placed → hidden) |
| `monitoring.py` | Prometheus metrics |
| `workflow_proxy.py` | Complete-Infiverse bridge (`/v1/candidate/workflow-*`, token refresh) |
| `langgraph_integration.py` | Workflow/notification proxy router |
| `routes/ai_integration.py` | AI router (`/api/v1`) |
| `routes/rl_routes.py` | RL router (`/api/v1`) |
| `routes/workforce_governance_routes.py` | 45 workforce/governance/SETU routes |
| `control_center_governance.py` | Policy-scope resolution (462 lines) |
| `decision_ledger.py`, `decision_workflow.py`, `policy_engine.py` | Governance engine |
| `setu_participation.py` | SETU signal dispatch |
| `lineage_envelope.py` | Lineage/trace envelope for workforce docs |
| `workforce_common.py`, `workforce_lifecycle.py`, `workforce_runtime.py` | Org/division/unit/department/employee models & lifecycle |

### DB helpers / migrations

- `create_mongodb_indexes.py`, `migrate_mongodb_schema.py`, `db_helpers.py` (ObjectId helpers).

### Requirements (key pins)

`fastapi<0.120`, `uvicorn<0.30`, `starlette<0.40`, `pydantic<3.0`, `motor>=3.3.0`,
`python-jose[cryptography]`, `passlib[bcrypt]`, `pyotp`, `qrcode[pil]`, `python-multipart`,
`bcrypt<5`, `PyJWT<3`, `cryptography<45`, `prometheus-client`, `psutil`, `requests`,
`httpx<0.28`, `redis<6`, `PyPDF2<4.0`.

---

## 3. Agent (`services/agent`)

### Runtime facts (verified)

- Port `9000`; `app.py` entry; 6 endpoints (live OpenAPI confirmed).
- Semantic matching via sentence-transformers; HF token via `HF_TOKEN`.

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Root info |
| GET | `/health` | Health (version 3.0.0 live) |
| GET | `/test-db` | DB connectivity check |
| POST | `/match` | Score one candidate-job pair |
| POST | `/batch-match` | Batch scoring |
| GET | `/analyze/{candidate_id}` | Candidate analysis |

### Requirements (key pins)

`sentence-transformers`, `scikit-learn<1.6`, `numpy<2.0`, `torch<2.3`, `transformers<5.0`,
`pymongo`, `dnspython`, `PyJWT`, `prometheus-client`, `python-dotenv`.

---

## 4. LangGraph (`services/langgraph`)

### Runtime facts (verified)

- Port `9001`; `app/main.py` (1201 lines); 26 live operations (GET 10, POST 16).
- Workflow engine: LangGraph `StateGraph` (`graphs.py`) with 4 nodes; `CandidateApplicationState`
  TypedDict (`state.py`).
- Agents: 4 agents in `agents.py` (uses Gemini via `google-generativeai` / `langchain-google-genai`).
- Notifications: `communication.py` (791 lines) — Twilio WhatsApp, Telegram, Gmail.
- Persistence: `mongodb_tracker.py` (`workflows` collection + in-memory fallback),
  `mongodb_checkpointer.py`.
- RL: `rl_engine.py`, `rl_database.py`, `rl_performance_monitor.py`, `rl_integration/`
  (decision_engine.py, ml_models.py, mongodb_adapter.py, rl_endpoints.py).

### RL Engine Details (verified in `app/rl_engine.py`, `app/rl_database.py`)

- `RLEngine`: feature weight learning — skill_match (50 pts), experience (30 pts), education
  (20 pts), RL adjustment (+/-10). Decision thresholds: >=75 shortlist, 50-74 review, <50 reject.
- `FeedbackProcessor`: outcome-based reward signals — hired=1.0, shortlisted=0.5, rejected=-0.3,
  withdrawn=-0.5.
- `RLPerformanceMonitor`: in-memory windowed metrics (prediction time, accuracy, reward trends).
- Persistent storage: `rl_predictions`, `rl_feedback`, `rl_training_data`, `rl_model_performance`
  MongoDB collections.

### Communication Channels (`app/communication.py` — 791 lines)

- `CommunicationManager` class supporting:
  - **Email** — Gmail SMTP via `smtplib`.
  - **WhatsApp** — Twilio API (`+14155238886` sandbox).
  - **Telegram** — Bot API via `python-telegram-bot`.
- SSE (Server-Sent Events) for real-time client/recruiter connection status (in-memory event queues).
- WebSocket support for workflow updates.

### Persistence

- Custom `MongoDBSaver` (`app/mongodb_checkpointer.py`) replacing PostgresSaver for LangGraph
  state persistence. Stores checkpoints in `langgraph_checkpoints` collection.

### Endpoint groups (verified)

- Workflow: `POST /workflows/application/start`, `GET /workflows/{id}/status`,
  `POST /workflows/{id}/resume`, `WS /ws/{id}`, `GET /workflows`, `GET /workflows/stats`.
- Automation/notifications: send, test email/whatsapp/telegram/whatsapp-buttons/sequence,
  bulk, preview, whatsapp webhook, `POST /automation/workflows/trigger`.
- RL router (`/rl`, from `rl_integration/rl_endpoints.py`): predict, feedback, analytics,
  performance/{model_version}, history/{candidate_id}, retrain, start-monitoring.
- Misc: `GET /test-integration`, `GET /health`.

### Requirements (key pins)

`langgraph>=0.2.0`, `langchain>=0.2.0`, `langchain-google-genai`, `fastapi`, `uvicorn`, `httpx`,
`twilio>=8.0.0`, `python-telegram-bot>=20.0`, `google-generativeai`, `transformers`, `torch`,
`scikit-learn`, `numpy`, `psutil`, `pymongo`, `pytest`, `pytest-asyncio`, `pydantic>=2.0`,
`pydantic-settings`.

---

## 5. Legacy Streamlit Portals (`portal`, `client_portal`, `candidate_portal`)

- **Marked LEGACY** in `backend/docker-compose.production.yml` (commented out).
- `portal` (8501): HR dashboard, `app.py` 1929 lines.
- `client_portal` (8502): client portal, auth **disabled** (`DEMO_CLIENT`).
- `candidate_portal` (8503): candidate login/register portal.
- The SPA frontend (`frontend/`) is the supported UI; these Streamlit apps are retained for
  reference/history only.

---

## 6. `runtime-core` (Legacy SAR)

- README marks it **legacy reference only**. Production is `backend/` + MongoDB Atlas.
- FastAPI "Sovereign Application Runtime" v1.0.0 with 5 routers (auth, tenancy, role_enforcement,
  audit_logging, workflow) + 4 middlewares; `docker-compose.yml` with `mongo:6.0`.
- Not part of the active deployment.

---

## 7. Environment Variables (from `backend/.env.example`, 110 lines — verified)

| Section | Variables |
|---------|-----------|
| DATABASE | `DATABASE_URL`, `MONGODB_URI`, `MONGODB_DB_NAME` |
| AUTH / SECURITY | `API_KEY_SECRET`, `JWT_SECRET_KEY`, `CANDIDATE_JWT_SECRET_KEY`, `GATEWAY_SECRET_KEY` |
| SERVICE URLS | `GATEWAY_SERVICE_URL`, `AGENT_SERVICE_URL`, `LANGGRAPH_SERVICE_URL`, `PORTAL_SERVICE_URL`, `CLIENT_PORTAL_SERVICE_URL`, `CANDIDATE_PORTAL_SERVICE_URL` |
| CORS | `CORS_ORIGINS` (localhost:3000, localhost:5173, sampada.blackholeinfiverse.com, infiverse-hr.vercel.app) |
| FEATURE FLAGS | `ENABLE_SEMANTIC`, `ENABLE_AUTO_SYNC`, `ENABLE_VALUES_ASSESSMENT`, `ENABLE_LEARNING_ENGINE` |
| PERFORMANCE | `MAX_CANDIDATES_PER_REQUEST=50`, `AI_MATCHING_TIMEOUT=15`, `AGENT_MATCH_TIMEOUT=60`, `DATABASE_POOL_SIZE=10` |
| PORTS | `GATEWAY_PORT=8000`, `AGENT_PORT=9000`, `LANGGRAPH_PORT=9001`, `PORTAL_PORT=8501`, `CLIENT_PORTAL_PORT=8502`, `CANDIDATE_PORTAL_PORT=8503` |
| HUGGING FACE | `HF_TOKEN`, `HF_HUB_DISABLE_SYMLINKS_WARNING`, `HF_HUB_DISABLE_TELEMETRY`, `HF_HUB_DISABLE_PROGRESS_BARS`, `TRANSFORMERS_VERBOSITY`, `TOKENIZERS_PARALLELISM` |
| RUNTIME / LOGGING | `LOG_LEVEL`, `LOG_FORMAT=json`, `ENVIRONMENT`, `OBSERVABILITY_ENABLED`, `PYTHON_VERSION=3.12.7` |
| COMMUNICATION | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_NUMBER`, `GMAIL_EMAIL`, `RECOVERY_CODE`, `GMAIL_APP_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `TELEGRAM_ADMIN_CHAT_ID` |
| GEMINI | `GEMINI_API_KEY`, `GEMINI_MODEL` |
| RESUME | `RESUME_KEYWORDS_URL` |
| WORKFLOW BRIDGE | `WORKFLOW_API_BASE_URL=http://127.0.0.1:5000/api`, `WORKFLOW_API_URL`, `WORKFLOW_BRIDGE_EMAIL`, `WORKFLOW_BRIDGE_PASSWORD`, `WORKFLOW_USER_PASSWORD`, `WORKFLOW_API_BASE_URL_DOCKER=http://host.docker.internal:5000/api`, `WORKFLOW_TOKEN_REFRESH_SECONDS=43200` |

---

## 8. Verification Evidence (2026-08-14)

- Gateway app import: ✅ 121 routes.
- Gateway local startup: ✅ `/health` 200 v4.2.0 in ~7.4 s.
- `pytest backend/tests/gateway/test_gateway_imports.py backend/tests/gateway/test_workforce_lifecycle.py` → ✅ 5 passed.
- Live VM health parity: gateway 4.2.0, agent 3.0.0, langgraph healthy.

---

## 9. Next

→ `06_API_REFERENCE.md` — the full verified endpoint inventory.
