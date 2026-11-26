# 07 — Authentication & Security

**Status:** ✅ Verified (2026-08-14)
**Owner:** Shashank Mishra

> The triple-layer authentication model, role-based access, 2FA, password policy, and the input /
> transport security controls. Read after `06_API_REFERENCE.md`.

---

## 1. Authentication Model (Gateway `jwt_auth.py`)

The gateway uses a **triple-layer** authentication scheme resolved by `get_auth`:

| Layer | Credential | Signing secret | Implied role |
|-------|-----------|----------------|--------------|
| 1 — API key | `Authorization: Bearer <API_KEY_SECRET>` | `API_KEY_SECRET` | `admin` |
| 2 — Candidate JWT | `Authorization: Bearer <JWT>` | `CANDIDATE_JWT_SECRET_KEY` (HS256) | `candidate` / `recruiter` |
| 3 — Client JWT | `Authorization: Bearer <JWT>` | `JWT_SECRET_KEY` (HS256) | `client` / `hr` |

Role guards (imported from `dependencies.py`):
- `get_candidate_auth`, `get_recruiter_auth`, `get_client_auth`, `get_admin_auth`,
  `get_optional_auth`, `require_role(...)`.
- `get_service_auth` (in `jwt_auth.py`): API-key-only guard for internal
  operations endpoints (service `api_key` or platform-admin JWT rejected — see 8.3/8.4).
- Audience-validation fallbacks included for token compatibility.

### Client vs candidate login flows (verified)

| Portal | Endpoint | Response |
|--------|----------|----------|
| Client | `POST /v1/client/login` | `access_token` (client JWT) |
| Candidate / Recruiter | `POST /v1/candidate/login` | JWT with role; recruiter registrations carry a `role` field |

Live check: both endpoints reachable; invalid credentials → `{"success":false,"error":"Invalid credentials"}`
(HTTP 200 with error body).

---

## 2. Frontend Auth Flow (`frontend/src/context/AuthContext.tsx`, `services/authService.ts`)

```text
AuthPage.tsx ─▶ AuthContext.signIn/signUp ─▶ authService.login/register
                                                │
                          /v1/client/login  or  /v1/candidate/login
                                                ▼
                                     token + user_data stored
                                                ▼
                     axios interceptor adds  Authorization: Bearer <token>
```

- **Storage**: sessionStorage-first, localStorage fallback. Keys: `auth_token`, `user_data`,
  `user_role`, `user_email`, `user_name`, `isAuthenticated`, `candidate_id`,
  `backend_candidate_id`, `client_id`, `candidate_profile_data`.
- **Restore**: `AuthContext` decodes the JWT payload (`atob(token.split('.')[1])`), checks `exp`,
  derives role with priority **JWT payload → user_data → user_role → default `candidate`**.
- **Route protection**: `ProtectedRoute` (roles `candidate|recruiter|client|admin`) gates pages;
  `PublicRoute` redirects logged-in users away from `/auth`. Wrong-role access redirects to the
  correct dashboard.
- **Logout**: clears all keys and the `Authorization` header.

---

## 3. Role-Based Access (frontend route gates)

| Role | Routes (verified in `App.tsx`) |
|------|--------------------------------|
| `candidate` | `/candidate/dashboard`, `/profile`, `/jobs`, `/applied-jobs`, `/interviews`, `/tasks`, `/tasks/:taskId`, `/feedback` |
| `recruiter` | `/recruiter` (+ create-job, upload-candidates, jobs, candidate-search, screening, applicants/:jobId, schedule-interview, values-assessment, feedback/:candidateId, export-reports, client-jobs, batch-operations, automation, reports) |
| `client` | `/client` (+ dashboard, jobs, candidates, matches, live-monitoring, reports, ndas) |
| `client\|recruiter\|admin` | `/control` (Control Center) |

---

## 4. 2FA (TOTP)

Endpoints: `/v1/auth/2fa/*` — setup, verify, login, status, disable, backup-codes, test-token, QR.
- QR code generation via `qrcode[pil]`; OTP verification via `pyotp`.
- Backup codes are stored/validated server-side.

---

## 5. Password Policy Engine

- `POST /v1/auth/password/validate` → strength score 0–100 + feedback list.
- `GET /v1/auth/password/policy` → `min_length` etc.
- Weak-password rejections are part of the security test suite.

---

## 6. Input & Transport Security

| Control | Implementation |
|---------|----------------|
| XSS / SQLi blocking | `POST /v1/security/test-input-validation` + gateway input validation |
| Email validation | `POST /v1/security/validate-email`, `test-email-validation` |
| Phone validation | `POST /v1/security/validate-phone`, `test-phone-validation` |
| CSP | violation reporting `POST /v1/security/csp-report`, view `GET /v1/security/csp-violations`, policies `GET /v1/security/csp-policies` |
| Security headers | `GET /v1/security/test-headers` (+ legacy variant) |
| Rate limiting | `GET /v1/security/rate-limit-status`; `rate_limits` collection with TTL expiry |
| Blocked IPs | `GET /v1/security/blocked-ips` |
| Penetration test helpers | `POST /v1/security/penetration-test`, `GET /v1/security/test-auth` |
| Prometheus metrics | `GET /metrics` (no PII) |

---

## 7. Multi-Tenant Isolation

- Client-scoped access rules enforced in the gateway domain handlers.
- Workforce documents carry `tenant_id` scoping via the `LineageEnvelope`
  (`app/lineage_envelope.py`) plus `write_workforce_audit` hooks.
- Tenant isolation verification suites: `backend/tests/gateway/test_tenant_isolation_workforce.py`.

---

## 8. Security Hygiene (do not violate)

- **Never commit** `.env` files or any real secret to the repository. `.gitignore` covers
  `.env`, `.env.local`, `.env.production`.
- Do not log tokens, passwords, or PII. `LOG_FORMAT=json` is the production logging standard.
- Do not weaken the auth guards (`get_auth`, role guards) to make testing easier.
- Secrets live in the owner's secure channel (see archived
  `handover/11_CREDENTIALS_REGISTER.md`); the repository stores only placeholders.

### 8.1 Pravesh SSO — Abhiruchi registry fallback

`POST /api/v1/pravesh/exchange` resolves the canonical candidate id in this order:
Pravesh identity → Sampada identity store (quarantined records excluded) →
**live Abhiruchi students registry** (last resort, availability-dependent).

- `ABHIRUCHI_STUDENTS_URL` (gateway env): registry URL; default
  `https://abhiruchi.blackholeinfiverse.com/api/students`; **empty string disables
  the fallback** (session then falls back to the namespaced `pravesh:<id>` identity).
- Lookup rules: **5 s timeout**, **5-minute per-email cache**
  (`routes/pravesh_routes.py::_abhiruchi_candidate_id`), every failure is swallowed
  (fail soft — SSO still succeeds).
- Frontend mirror: `VITE_ABHIRUCHI_STUDENTS_URL` (frontend `.env`), same default,
  empty disables, 5 s `AbortSignal.timeout` (`pages/candidate/Profile.tsx`).
- The registry is a *linking attribute source only* — it never merges, overwrites
  or creates identity data.

### 8.2 Identity quarantine (P1-6)

When an authoritative replay supplies a **different** canonical candidate id
than the one already stored, the identity record is frozen instead of being
silently resolved:

- Record `canonical_id_status` becomes **`QUARANTINED`** (the stored id is
  never overwritten), an `identity.ambiguous.v1` event is emitted with honest
  delivery status (`SIMULATED` unless `IDENTITY_AMBIGUOUS_URL` is configured),
  and an `identity_quarantine` document is opened.
- `POST /api/v1/identity/enrolment/publish` returns **409** while quarantined;
  Pravesh SSO canonical-id resolution skips quarantined records.
- Service/admin endpoints:
  `GET /api/v1/identity/quarantine` (list, default `status=open`) and
  `POST /api/v1/identity/quarantine/{id}/resolve` with
  `{"resolution": "released" | "confirmed", "note": ...}` — `released`
  restores the previous canonical status and unblocks publishing;
  `confirmed` keeps the record quarantined for data remediation.

### 8.3 Candidate NDA gate (tenant-scoped review)

New candidates are walled behind a verified NDA (official template → sign +
stamp → upload → auto-verify → **hiring-company review**):

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /v1/nda/template` | any gateway auth | Official print-ready HTML NDA (from `NEW_BHIV_NDA.pdf`, clauses 1–9) |
| `GET /v1/nda/status` | candidate | Status + verification evidence |
| `POST /v1/nda/upload` | candidate | PDF upload, PyPDF2 auto-verification |
| `GET /v1/nda/document` | api_key / own tenant company / own | Download the on-file NDA |
| `GET /v1/nda/tenant/queue` | client (tenant) | Own-tenant subset awaiting review |
| `POST /v1/nda/tenant/{candidate_id}/review` | client (tenant) | `VERIFIED` / `REJECTED` decision |
| `GET /v1/nda/admin/queue`, `POST /v1/nda/admin/{id}/review` | service (`api_key`) | Ops/test automation only |

- **Central admin has zero NDA access** (tenancy audit): platform-admin JWTs and
  anonymous requests get **401** from `/v1/nda/admin/*`, and **403** from
  document/tenant endpoints. Review decisions taken via tenant JWT are stamped
  `tenant_override` (`admin_override` only via service `api_key`).
- Review UI: **`/client/ndas`** ("NDA Approvals" in the company sidebar, role
  `client`) — own-tenant queue with candidate names, PDF viewer, approve/reject.
- Verdicts: `VERIFIED` (parses + signature/stamp marker + identity match),
  `NEEDS_REVIEW` (scans or unconfirmed text), `REJECTED` (not a usable PDF).
  Reasons are machine-readable in `verification.reasons`.
- **No auto-reject for unowned candidates**: `NEEDS_REVIEW` is **kept** for
  both owned and unowned candidates (reason `unowned_candidate_needs_review`
  when unowned). Correct-but-unverifiable files (scans, image signatures,
  name/email variants) are never auto-`REJECTED` — ops reviews the service
  queue (`GET /v1/nda/admin/queue`, api_key) or the candidate re-uploads a
  clearer text-based PDF. `REJECTED` is only for unusable files (empty,
  non-PDF, encrypted, unparsable).
- **Company routing**: the NDA is **platform-level** (BlackHole Infiverse
  LLP), not per-company. A candidate's submission appears in the review
  queue of **every company whose job they applied to** (tenant subset via
  `job_applications` → `jobs.client_id`). There is no cross-tenant leak —
  XYZ only sees it if the candidate also applied to an XYZ job — but one
  NDA can be visible to multiple companies (first reviewer wins).
- **Enforcement** (`enforce_nda_gate`, 403 `NDA_NOT_VERIFIED`):
  `POST /v1/candidate/apply`, `PUT /v1/candidate/profile/{id}`,
  `POST /v1/candidate/applications/{id}/documents/{type}` (except the `nda`
  upload itself) and `POST /api/v1/cv/export`. Frontend wall:
  `components/NdaGate.tsx` around the whole `/candidate` tree.
- **Bypasses**: non-candidate roles, demo guest sessions (`guest:*`),
  `NDA_GATE_ENABLED=false` (kill switch), and unconfigured-database dev mode.

### 8.4 Tenant invite share links

The central admin provisions company logins with **single-use, expiring share
links** (accounts-only console — no candidate or NDA data crosses tenants):

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /v1/admin/tenants/{tenant_id}/invites` | admin | Create invite (default TTL **168 h**, clamp 1–720 h) |
| `GET /v1/admin/tenants/{tenant_id}/invites` | admin | List invites (status, never passwords) |
| `POST /v1/admin/tenants/{tenant_id}/invites/{token}/revoke` | admin | Revoke an unused invite |
| `GET /v1/invite/{token}` | none (token) | Invite info — **404** unknown, **410** expired/revoked/used |
| `POST /v1/invite/{token}/register` | none (token) | Register the company account — atomic single-use claim |

- Registration claims the token atomically before provisioning; failed
  provisioning rolls the claim back so the link is only burned on success
  (except an existing-account **409**, which does not burn the link).
- Frontend: `/invite/:token` (`InviteSignupPage.tsx`) → auto sign-in as `client`;
  share/revoke controls live in **`/admin/tenants`** per tenant key account.
- Audit events: `tenant_invite_create`, `tenant_invite_revoke`,
  `tenant_invite_register`.

### 8.5 Contact verification + KYC (Workstream C)

**Profile card (current): contact verification via email OTP** — independent
candidates only. Hired / placed candidates (accepted/hired offer or owning
client) get `eligible: false` and the card is hidden entirely.

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /v1/verification/contact/status` | candidate | Eligibility + status + masked email |
| `POST /v1/verification/contact/otp/send` | candidate | 6-digit code to profile email (1/60s, 5/hour) |
| `POST /v1/verification/contact/otp/verify` | candidate | Match code → `CONTACT_VERIFIED` |

- `app/contact_verification.py`: independent = no owning client
  (`_candidate_owner_client_ids` empty) **and** no offer in
  `accepted`/`hired`. Codes are SHA-256 hashed, 10-minute expiry, max 5
  attempts; never returned by the API after send.
- Transport: free **Gmail SMTP** (`GMAIL_EMAIL` + `GMAIL_APP_PASSWORD` in
  `backend/.env`). Unconfigured → **503**, never a fake success.
- Honest statuses: `NOT_VERIFIED` → `CONTACT_VERIFIED` (method `email-otp`).
  This is **not** an ID-document check.
- Frontend: Contact Verification card on **`/profile`**
  (`pages/candidate/Profile.tsx`) — Send code + 6-digit input; hidden when
  not eligible.

**Legacy KYC document routes** (`app/verification.py`) remain mounted for
tests/history (soft-retire) but the Profile card no longer calls them:

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /v1/verification/kyc/document` | candidate | PDF/JPEG/PNG ≤ 5 MB upload |
| `GET /v1/verification/kyc/status` | candidate | Current verdict (default `NOT_SUBMITTED`) |

- `app/verification.py`: PyPDF2 text extraction (503 if PyPDF2 unavailable —
  never a fake `REJECTED`) + normalized name match against the profile.
- Verdicts: `VERIFIED` (text + name matched), `REJECTED` (name mismatch /
  unparsable / encrypted / unsupported), `UNVERIFIABLE` (image-only upload or
  scanned PDF with no extractable text → manual review). Reasons are
  machine-readable in `verification.reasons`.
- Result is persisted on the candidates document as `kyc` (missing profile → 404).

---

## 9. Verification Evidence (2026-08-14; extended 2026-11-12)

- Unauthenticated protected endpoints returned **401** (candidate stats, langgraph workflows).
- Invalid-credential logins returned `success:false` (auth guards functional).
- Security test suites exist under `backend/tests/security/` (9 files) — see `10_TESTING_AND_EVIDENCE.md`.
- **2026-11-12** (tenancy/NDA/KYC work): combined run of
  `backend/tests/{identity,nda,gateway,scripts,cv,pravesh,verification}` →
  **252 passed, 2 skipped, 0 failed** (`nda/` 53, `verification/` 21,
  `test_tenant_hierarchy_admin.py` 36 incl. 5 invite tests); frontend
  `npm run build` passes. Central-admin NDA rejection (401/403), invite
  single-use/expiry behaviour, and KYC honest-verdict rules each have
  dedicated tests (`nda/test_nda_tenancy.py`, `gateway/test_tenant_hierarchy_admin.py`,
  `verification/test_kyc_verification.py`).
- **2026-11-12** (contact verification + NDA false-reject fix): combined run
  → **272 passed, 2 skipped, 0 failed** (`nda/` 55 incl. middle-name/email
  identity matches + no-auto-reject for unowned NEEDS_REVIEW;
  `verification/` 39 = KYC 21 + contact OTP 18).
  Frontend `npm run build` green.

---

## 10. Next

→ `08_DATABASE.md`.
