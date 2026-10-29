# 11 — Deployment

**Status:** ✅ Verified (live VM 2026-08-14; configs cross-checked)
**Owner:** Shashank Mishra

> Docker, GitHub Actions CI/CD, VM path-based routing, Render backup, and Vercel frontend.
> Read after `10_TESTING_AND_EVIDENCE.md`.

---

## 1. Production Topology (Verified 2026-08-14)

```text
VM (primary) ── https://sampada.blackholeinfiverse.com
   ├── /              → Frontend (container port 3000, host 3004→3000)
   ├── /gateway       → Gateway  (container 8000, host 8003→8000)
   ├── /agent         → Agent    (container 9000, host 9002→9000)
   └── /langgraph     → LangGraph (container 9001, host 9003→9001)

Render (backup failover)  ── bhiv-hr-gateway-*.onrender.com etc. (subdomain names have changed)
Vercel (alternate frontend) ── infiverse-hr.vercel.app
MongoDB Atlas              ── db: bhiv_hr
```

### Live health on the VM (2026-08-14)

| Endpoint | Status |
|----------|--------|
| `https://sampada.blackholeinfiverse.com/` | 200 |
| `/gateway/health` (v4.2.0) | 200 |
| `/agent/health` (v3.0.0) | 200 |
| `/langgraph/health` | 200 |

---

## 2. Docker Compose

### Working production compose — `backend/docker-compose.production.yml` (272 lines)

- Version 4.3.0 "MongoDB Atlas Edition"; reads `backend/.env`; no local volumes needed.
- Services: `gateway` (8000), `agent` (9000), `langgraph` (9001) — with healthchecks, resource
  limits (gateway 512M/0.5 CPU), `extra_hosts: host.docker.internal` (workflow bridge).
- Internal URLs: `AGENT_SERVICE_URL=http://agent:9000`, `LANGGRAPH_SERVICE_URL=http://langgraph:9001`,
  `GATEWAY_SERVICE_URL=http://gateway:8000`.
- Gateway workflow URL: `WORKFLOW_API_BASE_URL=${WORKFLOW_API_BASE_URL_DOCKER:-http://host.docker.internal:5000/api}`.
- Streamlit portals are **commented out** ("STREAMLIT PORTALS (LEGACY)").
- Network: `bhiv-network` (bridge).

Run:

```powershell
cd backend
docker compose -f docker-compose.production.yml up --build -d
docker compose -f docker-compose.production.yml logs -f gateway agent langgraph
docker compose -f docker-compose.production.yml down
```

### Legacy compose — root `docker-compose.production.yml` (DO NOT USE)

- References images `bhiv/hr-gateway|hr-agent|hr-lang-graph|hr-frontend:latest`.
- References `backend/gateway.env`, `backend/agent.env`, `backend/langgraph.env` — **these files do
  not exist** (only `backend/.env` exists). Keep as a historical template only.

---

## 3. CI/CD — GitHub Actions (`.github/workflows/deploy.yml`, 371 lines)

4 jobs:

| Job | Purpose |
|-----|---------|
| `validate` | Validate compose config with stub envs |
| `build` | Build & push `bhiv/hr-gateway`, `hr-agent`, `hr-lang-graph`, `hr-frontend` tagged `<sha>:latest` |
| `deploy` | SSH to VM → Docker Hub login → pull → `up -d` → 12-retry health loop on gateway/agent/langgraph/frontend (3004) → 168 h image prune → `docs/RELEASE_HISTORY.md` update; state persisted at `/var/tmp/SAMPADA/` |
| `rollback` | On failure: grep last `SUCCESS` SHA from Release History → redeploy → record `ROLLBACK_SUCCESS` / `ROLLBACK_FAILED` |

---

## 4. VM Path-Based Routing

- Reverse proxy maps paths to container ports (3004→3000, 8003→8000, 9002→9000, 9003→9001).
- The frontend is built with `VITE_API_BASE_URL` pointing at
  `https://sampada.blackholeinfiverse.com/gateway` for the primary VM deploy.

---

## 5. Render (backup failover)

| Service | Historic URLs (subdomains have changed) |
|---------|------------------------------------------|
| Gateway | `https://bhiv-hr-gateway-*.onrender.com` |
| Agent | `https://bhiv-hr-agent-*.onrender.com` |
| LangGraph | `https://bhiv-hr-langgraph-*.onrender.com` |

- Render instances sleep when idle → first request may 503 until cold start (observed in prior
  documented health checks). Use the VM as the verified primary.
- Check current Render service names in the Render dashboard; update `VITE_API_BASE_URL` in the
  Vercel/VM env to match the active target.

---

## 6. Vercel Frontend

`frontend/vercel.json`: build `npm run build`, output `dist`, SPA rewrite `/(.*)` → `/index.html`,
immutable Cache-Control on `/assets/*`.

### Required env vars (Vercel)

| Var | Production example |
|-----|--------------------|
| `VITE_API_BASE_URL` | `https://sampada.blackholeinfiverse.com/gateway` |
| `VITE_AGENT_SERVICE_URL` | `https://sampada.blackholeinfiverse.com/agent` |
| `VITE_LANGGRAPH_SERVICE_URL` | `https://sampada.blackholeinfiverse.com/langgraph` |
| `VITE_ENABLE_CONTROL_CENTER` | `true` |
| `VITE_ENABLE_GOVERNANCE` | `true` |
| `VITE_PRAVESH_URL` | `https://pravesh.blackholeinfiverse.com` |
| `VITE_API_KEY` | Must match Render/VM `API_KEY_SECRET` |

Troubleshooting: SPA 404-on-refresh → ensure the rewrite rule is applied; env changes require a new
build.

---

## 7. Environment Strategy

- **One `.env` per environment**; secrets only in env (never committed). Root `.gitignore` covers
  `.env`, `.env.local`, `.env.production`.
- `ENVIRONMENT` variable (`development` vs `production`) is read by services for logging/feature
  behaviour.
- `CORS_ORIGINS` must include every frontend origin:
  `http://localhost:3000,http://localhost:5173,https://sampada.blackholeinfiverse.com,https://infiverse-hr.vercel.app`.

---

## 8. Deploy / Rollback Quick Reference

### Deploy (CI)
1. Push to `main` → GitHub Actions `validate` → `build` → `deploy`.
2. Deploy job SSHes to the VM, pulls images, `up -d`, health-checks all four services.

### Manual re-deploy (VM)
```powershell
# on the VM
docker compose -f /path/to/backend/docker-compose.production.yml pull
docker compose -f /path/to/backend/docker-compose.production.yml up -d
```

### Rollback
- Automatic: CI `rollback` job redeploys the last `SUCCESS` image SHA recorded in
  `docs/RELEASE_HISTORY.md`.
- Manual: `docker compose up -d <service>@<previous-image-tag>` from the Release History.
- Render failover: point frontend `VITE_API_BASE_URL` at the Render gateway URL.

---

## 9. Release History

- Located at `docs/RELEASE_HISTORY.md` on the VM (`~/SAMPADA/docs/RELEASE_HISTORY.md`) and updated
  by the CI deploy job. (Note: `docs/RELEASE_HISTORY.md` is not currently present in the repo —
  it is generated/maintained on the VM.)

---

## 10. Deployment Gotchas

1. **Do not use the root `docker-compose.production.yml`** — use `backend/docker-compose.production.yml`.
2. Render URLs change across doc versions — always confirm from the Render dashboard.
3. `WORKFLOW_API_BASE_URL_DOCKER` (`host.docker.internal`) is required only for the workflow bridge
   to a locally-hosted Complete-Infiverse server.
4. Frontend chunk-size warning (>500 kB) is cosmetic; code-splitting is a recommended future task.

---

## 11. Next

→ `12_OPERATIONS_RUNBOOK.md`.
