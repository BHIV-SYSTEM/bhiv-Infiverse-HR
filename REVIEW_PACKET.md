# REVIEW_PACKET.md — BHIV vNEXT Team 2 (Sampada / Infiverse-HR side)

**Task:** P0 Team 2 delivery — Pravesh SSO integration, canonical candidate identity +
institute/cohort + Abhiruchi intake + HackaVerse enrolment publishing, and the
Editable CV Builder on Candidate and Recruiter pages.
**Owner:** Ashmit and team (Ashmit — delivery lead; Pratik — Sampada test/validation;
Samsoorya — DevOps; Rudra/Sanskar — shared contracts).
**Status:** VERIFIED LIVE locally (see evidence). Prepared 2026-10-26.

---

## 0. Repository / branch / commit

| Item | Value |
|---|---|
| Repository | `BHIV-SYSTEM/bhiv-Infiverse-HR` |
| Branch | `main` |
| Base commit | `6240528` ("fixing add_header X-Frame-Options DENY") |
| Changes | uncommitted working tree (files listed in §7) — commit on review approval |
| Counterpart repo | `BHIV-Engineering-Exchange/bhiv-Hackaverse` @ `fc4e610` (see its own packet) |

---

## 1. Pravesh (blackhole_auth) read-only SSO bridge

**What it is:** Pravesh is the company auth system (`blackhole_auth` repo, prod origin
`https://pravesh.blackholeinfiverse.com`). It sets an httpOnly `blackhole_token`
cookie — an HS256 JWT carrying `{user_id, email, tenant_id, roles[], permissions[],
allowedApps[]}`. The reference integration is the Express wrapper
`blackhole_auth/backend/src/middleware/blackholeAuth.js`; this is its gateway equivalent.

**Files**
- `backend/services/gateway/pravesh_bridge.py` — verification, app-slug authorisation,
  role mapping, read-only identity mapping
- `backend/services/gateway/routes/pravesh_routes.py` — `GET /api/v1/pravesh/status`,
  `GET /api/v1/pravesh/session`
- `backend/services/gateway/jwt_auth.py` — `get_auth()` (and the role-specific wrappers
  + `get_optional_auth`) accept the Pravesh cookie/header **only when no Authorization
  header is present**; Bearer/API-key paths are untouched
- `backend/services/gateway/app/main.py` — router registration (try/except, optional)

**Hard rules implemented**
- Disabled entirely unless `PRAVESH_JWT_SECRET` is set → zero behaviour change by default.
- Identity is namespaced `pravesh:<user_id>`; a local account is **never** adopted.
- Email is a linking attribute only: `/pravesh/session` reports
  `link_status` (`candidate_match` / `ambiguous_match` / `no_local_account` / …) and
  `merge_policy: email_is_linking_attribute_only`. Nothing is ever merged or created.
- Token without `sampada` in `allowedApps` → 401/403 (configurable, default enforced).
- `alg=none`, bad signature, expired, missing `user_id` → rejected.
- Unknown Pravesh roles map to `candidate` (least privilege), never to admin.

**Env:** `PRAVESH_JWT_SECRET`, `PRAVESH_APP_SLUG` (default `sampada`), `PRAVESH_COOKIE_NAME`,
`PRAVESH_REQUIRE_APP_CLAIM`, `PRAVESH_HEADER_NAME` (API clients send `X-Pravesh-Token`).

---

## 2. Canonical candidate identity, institute/cohort, Abhiruchi intake, enrolment

**Files:** `backend/services/gateway/app/identity/{models,service,routes}.py`
(prefix `/api/v1/identity`).

| Endpoint | Auth | Behaviour |
|---|---|---|
| `POST /identity/abhiruchi/intake` | API key (service-to-service) | Idempotent handoff. Reuse order: authoritative `canonical_candidate_id` → same `(source_system, source_reference)` → else **new** Sampada-local record. Replays never duplicate. |
| `GET /identity/candidate/{id}` | gateway auth | Read-only lookup by local **or** canonical id. |
| `GET /identity/institutes` | gateway auth | `MSSU, DYP-A, SIT-S, SIPS-S, VJTI` (+ alias `RTMSSU → MSSU`). |
| `POST /identity/enrolment/publish` | gateway auth | Builds the versioned envelope; POSTs to HackaVerse when `HACKAVERSE_ENROLMENT_URL` is set. Delivery is reported honestly: `LIVE` / `SIMULATED` / `FAILED` — SIMULATED is never presented as LIVE. |
| `GET /identity/enrolment/{key}` | gateway auth | Retrieve a stored envelope. |

**Identity rules enforced (PDF §1/§5)**
- Sampada **never invents** a canonical candidate id: absent → `canonical_id_status: "UNKNOWN"`.
- A conflicting canonical id is never applied over an existing one (warning instead).
- Same email + institute on two records → both flagged `possible_duplicate_of`, **never merged**.
- No new lifecycle statuses: the source's own status is passed through or stays `null`.
- Every intake stores its `trace_id` and all `source_references`.

**Contract v1.0 (`candidate.enrolment.v1`)** — agreed shape, must be version-bumped only
with Rudra/Sanskar:

```json
{
  "schema_version": "1.0",
  "event": "candidate.enrolment.v1",
  "source_system": "sampada",
  "trace_id": "trace-team2-smoke-1",
  "idempotency_key": "sd_<local_id>:<cohort_id>",
  "sent_at": "2026-10-26T…",
  "candidate": {
    "canonical_candidate_id": null,
    "canonical_id_status": "UNKNOWN",
    "sampada_local_id": "sd_e2b0340949fc4947",
    "email": "student@example.com",
    "institute_code": "MSSU",
    "cohort_id": "MSSU-2026-B1",
    "lifecycle_status": null
  },
  "identity_policy": "email_is_linking_attribute_only"
}
```

HackaVerse rejects unknown `schema_version`, unknown `event`, and non-agreed
`institute_code` with **422** (contract protection).

---

## 3. Editable CV Builder (Candidate + Recruiter pages)

**Backend** `backend/services/gateway/app/cv/` (prefix `/api/v1/cv`):

| Endpoint | Purpose |
|---|---|
| `GET /cv/templates` | The three editable templates (one shared content model). |
| `GET /cv/prefill?candidate_id=` | Authorised profile → CV fields **with provenance** (`source: profile/auth/none`, `verified`). Missing fields return **questions, not guesses**. Cross-user access → 403. |
| `GET/POST /cv/drafts`, `GET/PUT /cv/drafts/{id}` | Save/reopen drafts; strictly scoped to the signed-in user (foreign draft → 404). Template switch keeps content. Editing after approval revokes approval. |
| `POST /cv/drafts/{id}/approve` | Explicit approval, requires the exact phrase `I have reviewed this CV` (422 otherwise). |
| `POST /cv/suggest` | **Bounded** AI assistance: whitespace/sentence-capitalisation proposals only, returned for review, never persisted server-side, wording never re-worded (word-identity verified in tests). Empty input → no proposals. |
| `POST /cv/assemble` | Preview HTML per template + honest validation (`summary_blank`, `whitespace`, …). Blank stays blank. |
| `POST /cv/export` | **403 until approved.** Returns print-ready standalone HTML (browser print → PDF; no new dependency added). |

**Frontend** (`frontend/src/pages/candidate/CvBuilder.tsx`, shared by both roles):
- Candidate entry: **“Build My CV”** button on `/candidate/profile`
  (`frontend/src/pages/candidate/Profile.tsx`) → route `/candidate/cv-builder`
- Recruiter entry: **“CV Builder”** nav item
  (`frontend/src/components/sidebars/RecruiterSidebar.tsx`) → route `/recruiter/cv-builder`
- Routes registered in `frontend/src/routes.tsx`
- Page provides: provenance badges, missing-information question panel, 3-template
  switcher, section add/remove/reorder/rename + entry editing, per-entry
  “Suggest grammar fix” with Accept/Discard review UI, save/load draft, preview iframe,
  approve-then-export flow. Recruiter copy states explicitly that only their own CV is
  reachable — enforced server-side, not just in the UI.

---

## 4. Evidence

| Evidence | File / command |
|---|---|
| Backend tests (this repo): **65 passed** (CV 19, Pravesh 25, identity 21) | `evidence_packet/team2_cv_pravesh_identity_tests.txt` — re-run: `python -m pytest backend/tests/cv backend/tests/pravesh backend/tests/identity -q` |
| Existing gateway regression: **3 passed** | `evidence_packet/team2_gateway_regression.txt` |
| Frontend `tsc --noEmit` **PASS** + `vite build` **PASS** | `evidence_packet/team2_frontend_build.txt` |
| Runtime end-to-end smoke (both servers) | `evidence_packet/team2_runtime_smoke.sh` → `evidence_packet/team2_runtime_smoke.txt` |
| Counterpart tests (HackaVerse): **35 passed** | `../bhiv-Hackaverse-main/evidence_packet/team2/hackaverse_bridge_tests.txt` |

Key runtime results (from the smoke log):
- Pravesh session: valid cookie **200** (both apps), no cookie **401**, wrong `allowedApps` **403**.
- CV: export before approval **403** → wrong approval phrase **422** → exact phrase **200** →
  export **200** (`print_ready: true`); foreign draft **404**.
- Identity: intake `created` → identical retry `reused` (same `sampada_local_id`, no duplicate);
  `RTMSSU` normalised to `MSSU`; absent canonical id reported `UNKNOWN`.
- Enrolment: publish → HackaVerse **LIVE 200**; repeat publish → HackaVerse **ingest_count: 2**
  on one `idempotency_key` (no duplicate cohort records); contract violations → **422**.

---

## 5. Gap ownership assignment (response to Pratik's audit)

| Gap from the audit | Owner | Notes |
|---|---|---|
| Pravesh SSO integration (bridge code, both repos) | **Ashmit** | ✅ delivered in this packet + HackaVerse packet |
| Canonical candidate-ID source & local-ID mapping rules | **Rudra + Sanskar decide, Ashmit implements** | We must not define the shared contract alone (PDF §5). Implementation already preserves local IDs and keeps `UNKNOWN` until the source supplies a canonical id. |
| Institute/cohort support (codes, cohort field) | **Ashmit** | ✅ in Sampada (intake/publish) + HackaVerse (ingest) |
| Abhiruchi intake handoff testing (idempotency, duplicates, retries) | **Pratik** | Code done; Pratik owns validation against real Abhiruchi payloads + regression of the existing Sampada → Niyantran task flow |
| HackaVerse enrolment publishing/consumption | **Ashmit (+ Ansh for any frontend cohort UI)** | ✅ API-level LIVE and verified |
| Shared contract sign-off & joint integration test | **Rudra / Sanskar** | Packet §2 is the proposal; do not merge dependent changes before agreement |
| Deployment, env/secrets, CORS/credentials enablement | **Samsoorya** | See blockers below |
| Environment roster | Ashmit | Soham Kotkar / Vijay Dhawan are **not** active owners (per PDF §6) |

## 6. Known issues / blockers

1. **Pravesh `allowedApps` slugs** — the Pravesh dev login seeds
   `sampada` but **not `hackaverse`**; the Pravesh admin must add `hackaverse`, and both
   apps need `PRAVESH_JWT_SECRET` (= blackhole_auth `JWT_SECRET`) in their deploy env.
   Until then the bridge stays safely inert/403.
2. **Gateway CORS is `allow_credentials=False`** — browsers will not send the Pravesh
   cookie cross-origin until Samsoorya enables credentialed CORS for the intended origins.
   Service-to-service and header (`X-Pravesh-Token`) paths are unaffected.
3. **Local runs fall back to an in-memory store** when `DATABASE_URL`/`MONGODB_URI` is
   unset; every response reports `storage_mode` (`memory` vs `mongo`) so this can never be
   mistaken for durable storage. Production must set the Mongo URI.
4. **CV prefill** only fills what the authorised candidate record contains; there is no
   education institution/employer/date data in the current profile schema, so those stay
   blank + questioned by design (no invention).
5. **Export format** is print-ready HTML (print → PDF from the browser). A PDF library was
   not added, per "no new major dependency without approval".
6. `frontend/package-lock.json` changed as a side effect of installing dependencies for the
   build check; review before committing.

## 7. Files changed (this repo)

Modified: `backend/services/gateway/app/main.py`, `backend/services/gateway/jwt_auth.py`,
`frontend/src/routes.tsx`, `frontend/src/pages/candidate/Profile.tsx`,
`frontend/src/components/sidebars/RecruiterSidebar.tsx`, `frontend/package-lock.json`.
Added: `backend/services/gateway/pravesh_bridge.py`, `backend/services/gateway/routes/pravesh_routes.py`,
`backend/services/gateway/app/identity/`, `backend/services/gateway/app/cv/`,
`backend/tests/{pravesh,identity,cv}/`, `frontend/src/pages/candidate/CvBuilder.tsx`,
`evidence_packet/`, this file.

**Rollback:** all new modules are self-contained; `main.py`/`jwt_auth.py` edits revert with
`git restore` on those three files. With `PRAVESH_JWT_SECRET` unset the bridge is inert even
if left in place. Frontend entries are one import + three route lines + one nav item.
No data migrations were performed; nothing existing was deleted or re-keyed.

## 8. Handover

- Set env on deploy: `PRAVESH_JWT_SECRET`, `PRAVESH_APP_SLUG=sampada`,
  `HACKAVERSE_ENROLMENT_URL`, `HACKAVERSE_ENROLMENT_API_KEY`, and a real `DATABASE_URL`.
- Get `hackaverse` into Pravesh `allowedApps` and agree contract v1.0 with Rudra/Sanskar
  before dependent merges.
- Suggested next: wire the CV Builder draft list into the candidate dashboard, and show
  cohort membership on the HackaVerse frontend once enrolment data is flowing.
