# 10 — Testing & Evidence

**Status:** ✅ Verified (2026-08-14 — sample suites run)
**Owner:** Shashank Mishra

> Every test suite, how to run it, and where proof artifacts live. Read after
> `09_FRONTEND_REFERENCE.md`.

---

## 1. Test Inventory (`backend/tests/`)

| Directory | Test files | Covers |
|-----------|-----------|--------|
| `agent/` | 8 | AI matching endpoints |
| `api/` | 15 | Endpoint suites (comprehensive, 2FA, password, portals) |
| `control_center/` | 6 | Control-center e2e (has its own `pytest.ini`) |
| `cv/` | 1 | CV export gate integration |
| `database/` | 3 | DB connectivity + portal DB tests |
| `deployment/` | 5 | Deployment/health checks |
| `e2e/control_center/` | — | E2E runner with fixtures (`e2e_config`, `e2e_reporter`, `env_preflight`, `live_services`) |
| `fixes/` | 3 | Rectification/regression |
| `framework/` | 7 | Test framework helpers |
| `gateway/` | 12 | Gateway imports, routes, workforce, tenant isolation, tenant hierarchy + admin + invites |
| `identity/` | 1 | Identity/quarantine regression |
| `integration/` | 4 | Cross-service flows |
| `langgraph/` | 15 | LangGraph workflows, RL, notifications |
| `misc/` | 34 | Assorted endpoint tests |
| `nda/` | 2 | NDA gate verification + tenancy isolation (tenant queue/review, auto-decision, service-key access); unowned NEEDS_REVIEW never auto-rejected |
| `pravesh/` | 1 | Pravesh SSO bridge |
| `rl_integration/` | 6 | RL engine |
| `scripts/` | 1 | Candidate-linking script |
| `security/` | 9 | Auth, 2FA, password, validation |
| `validation/` | 10 | Localhost endpoint validation |
| `verification/` | 2 | KYC honest verdicts (21) + contact-verification OTP rules (18) |
| `workflows/` | 1 | Workflow tests |

---

## 2. Main Runners

### Comprehensive endpoint tests (`backend/tests/comprehensive_endpoint_tests.py`)
8 phases:
1. Health
2. Core API
3. Auth/security (15 checks)
4. Business workflow (13 checks)
5. AI matching (6 checks)
6. LangGraph (5 checks)
7. Integration (2 checks)
8. Portal accessibility (3 checks)

```powershell
cd backend
.\venv\Scripts\python.exe tests\comprehensive_endpoint_tests.py
```

### Pytest (self-contained suites)

```powershell
cd backend
.\venv\Scripts\python.exe -m pytest tests\gateway\test_gateway_imports.py tests\gateway\test_workforce_lifecycle.py -v
```

**Verified 2026-08-14:** the above → **5 passed** in 2.35 s.

### Tenancy / NDA / KYC / contact-OTP combined verification run (2026-11-12)

```powershell
# from the repository root
.\backend\venv\Scripts\python.exe -m pytest backend/tests/identity backend/tests/nda `
  backend/tests/gateway backend/tests/scripts backend/tests/cv backend/tests/pravesh `
  backend/tests/verification -q
```

**Result: 272 passed, 2 skipped, 0 failed.** Highlights: `nda/` **55** (incl. 16 tenancy
isolation tests + middle-name/email-alias identity matches), `verification/` **39**
(KYC 21 + contact OTP 18: eligibility, rate-limit, expiry, honest statuses),
`gateway/test_tenant_hierarchy_admin.py`
**36** (incl. 5 invite-share-link tests). The hierarchy suite now re-syncs `jwt_auth`
secrets at module import so it passes regardless of pytest argument/collection order
(previously it only passed when collected before other suites).

Frontend: `cd frontend && npm run build` → passes (`tsc && vite build`).

### E2E control-center suites

```powershell
cd backend\tests\e2e\control_center
pytest --asyncio-mode=auto
```
Markers: `e2e`, `e2e_unit`. Fixtures: `e2e_config`, `e2e_reporter`, `env_preflight`,
`live_services`.

### Legacy standalone runners (for reference)
- `backend/handover/test_all_endpoints.py`
- `backend/runtime-core/test/test_all_endpoints.py` (49 scenarios)
- `backend/runtime-core/test_suite/` (9 files)

---

## 3. Frontend Verification

| Check | Command | Result (2026-08-14) |
|-------|---------|----------------------|
| Type-check + production build | `npm run build` (`tsc && vite build`) | ✅ 151 modules |
| Dev server | `npm run dev` | ✅ (port 3000) |

---

## 4. Verification Scripts (`root` / `evidence/`)

The following are **legacy** evidence-harness references from `README.md` (they point to a stale
`C:\Users\Shani\.gemini\...` scratch path and should **not** be re-run as-is):

- `run_convergence_evidence.js` — full E2E flow → writes `evidence/`.
- `test_failure_simulations.js` — XSS/SQLi/weak-password/bad-TOTP/CSP checks.
- `evidence/replay/replay_script.js` — deterministic state reconstruction.

> Live evidence collection is now performed through the pytest suites and the live-VM smoke checks
> recorded in `00_VERIFICATION_REPORT.md`. The `evidence/` folder holds the accumulated artifacts
> (see section 6).

---

## 5. Manual Live Smoke Checklist (production)

1. `GET /gateway/health` → 200.
2. `GET /agent/health` → 200.
3. `GET /langgraph/health` → 200.
4. `GET /` (frontend) → 200.
5. Login as candidate → protected pages load.
6. `GET /gateway/v1/jobs` → 200 with data.
7. Open `/gateway/docs` → Swagger renders.
8. Confirm a protected route (e.g. `/gateway/v1/candidates/stats`) returns 401 unauthenticated.

> Performed 2026-08-14: all passed (see `00_VERIFICATION_REPORT.md`).

---

## 6. Evidence Artifacts (`evidence/`)

| Folder | Contents |
|--------|----------|
| `boundaries/` | Visibility-boundary verification |
| `entry-points/` | Token templates + curl tests |
| `failure/` | Vulnerability blocks + failure logs |
| `general/` | Unified verification summaries |
| `ownership/` | Responsibility matrix |
| `replay/` | Chronological state reconstruction |
| `trace-continuity/` | Correlation-ID request logs |
| `live_workforce_governance_setu/` | Live WO/GE/SETU runtime captures (Tier 2) |
| `phase_iv_production_validation/` | Phase IV validation |
| `phase_iv_tier1/` | Phase IV tier-1 evidence |
| `workforce_runtime/` | Workforce runtime validation |

Handover evidence (archived copy): `Updated Docs/archived/handover/evidence/` contains
`INDEX.md`, `health_checks_2026-08-08.md`, `audit-2026-08-10.md`, `health-checks/`.

---

## 7. Running Tests That Need a Live DB / Services

- Suites that hit endpoints expect the services running (local or live VM) with a valid
  `backend/.env`. Set `WORKFLOW_API_BASE_URL` if testing the task-bridge flows.
- AI-matching suites download HF models on first run (set `HF_TOKEN`).
- Notification suites need Twilio/Telegram/Gmail credentials — use the `test/` endpoints to avoid
  sending real messages.

---

## 8. Known Test-Infrastructure Notes

- `test_gateway_imports.py` returns values instead of asserting (PytestReturnNotNoneWarning) —
  harmless, but worth refactoring.
- `pytest.ini` only exists under `tests/e2e/control_center/`; run suites from the `backend/` root
  or that folder as appropriate.
- `PytestUnknownMarkWarning` for `e2e_unit` outside the e2e folder is expected.

---

## 9. Next

→ `11_DEPLOYMENT.md`.
