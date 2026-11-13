# 09 — Frontend Reference

**Status:** ✅ Verified (2026-08-14 — `npm run build` passed)
**Owner:** Shashank Mishra

> The React + Vite + TypeScript SPA ("Sampada — HR Recruitment System"). Read after
> `08_DATABASE.md`.

---

## 1. Stack & Tooling (Verified — `package.json`, configs)

| Item | Version / Detail |
|------|------------------|
| React | 18.2 |
| TypeScript | 5.3 (strict, ES2020, `jsx: react-jsx`, `noEmit`) |
| Vite | 7.3 (dev server `host:true port:3000`) |
| Routing | `react-router-dom` 6.20 |
| HTTP | `axios` 1.6 (auth interceptors) |
| Styling | Tailwind CSS 3.3 + `tailwindcss-animate`, shadcn-style HSL tokens, `framer-motion` |
| Other | `react-hot-toast`, `xlsx` (batch uploads) |
| Build | `npm run build` = `tsc && vite build` (verified ✅ 151 modules, 57.96 s) |
| Entry | `src/main.tsx` (StrictMode + ErrorBoundary) |

---

## 2. Route Tree (Authoritative — `src/App.tsx`)

> `src/routes.tsx` is a **redundant, unused** legacy route set (kept for reference only). The
> authoritative tree is in `App.tsx`.

| Route | Access | Page |
|-------|--------|------|
| `/` | public | Redirect → `/auth` |
| `/auth` | public (`PublicRoute`) | `AuthPage` (`?mode=login|signup`) |
| `/control` | `ProtectedRoute` roles `client\|recruiter\|admin` | `ControlCenter` |
| `/candidate/*` | `candidate` | `CandidateLayout`: `dashboard`, `profile`, `jobs`, `applied-jobs`, `interviews`, `tasks`, `tasks/:taskId`, `feedback` |
| `/recruiter/*` | `recruiter` | `RecruiterLayout`: `create-job`, `upload-candidates`, `jobs`→Dashboard, `candidate-search`, `screening`→ApplicantsMatching, `applicants/:jobId`, `schedule-interview`, `values-assessment`, `feedback/:candidateId`, `export-reports`, `client-jobs`, `batch-operations`, `automation`, `reports`→ExportReports |
| `/client/*` | `client` | `ClientLayout`: `dashboard`, `jobs`, `candidates`, `matches`, `live-monitoring`, `reports`, `ndas` |
| `/invite/:token` | public (single-use invite token) | `InviteSignupPage` — redeem a tenant share link, auto sign-in as `client` |
| `*` | fallback | → `/auth` |

Protected-route behaviour: wrong-role access redirects to the correct dashboard; a loading gate
shows while auth state restores.

---

## 3. Pages (`src/pages/`)

- **auth**: `AuthPage.tsx` (login/signup; role config redirects candidate→`/candidate/dashboard`,
  recruiter→`/recruiter`, client→`/client`). `InviteSignupPage.tsx` (redeems `/invite/:token`
  share links → auto client sign-in). `HRAuth.tsx` is empty (0 bytes).
- **candidate**: `Dashboard`, `Profile`, `JobSearch`, `AppliedJobs` (status + document upload
  resume/nda), `Interviews`, `Tasks` (workflow bridge task list), `TaskDetail` (individual task detail & submission form), `Feedback`, `candidateTasksTypes.ts` (TypeScript type definitions).
- **recruiter**: `Dashboard`, `JobCreation`, `ApplicantsMatching`, `FeedbackForm`,
  `AutomationPanel`, `CandidateSearch`, `BatchUpload` (wrapper), `BatchOperations`,
  `InterviewScheduling`, `ExportReports`, `ClientJobsMonitor`, `ValuesAssessment`.
- **client**: `ClientDashboard`, `ClientJobPosting`, `ClientCandidates`, `MatchResults`,
  `ClientReports`, `ShortlistReview`, `LiveRecruiterMonitoring`, `NdaApprovalsPage`
  (tenant-scoped NDA review at `/client/ndas`; the central-admin NDA page was removed —
  see `07_AUTHENTICATION_AND_SECURITY.md` §8.3).
- **control**: `ControlCenter.tsx` — executive/hiring/workforce/growth/org/governance/replay zones.
- **hr**: `HRDashboard`, `CandidateManagement`, `AIMatching` — **all empty (0 bytes)**.
- `RoleSelection.tsx` — 3 role cards → `/auth?mode=signup`.

---

## 4. API Layer (`src/services/api.ts` — 2717 lines)

- One axios client; `API_BASE_URL` from `VITE_API_BASE_URL` (default `http://localhost:8000`).
- `resolveServiceBaseUrl()` forces local ports (8000/9000/9001) in Vite dev on localhost even when
  `.env` holds Render URLs.
- 30 s timeout (Render cold-start); response interceptor logs 401/502 (quiet for
  `/v1/candidate/workflow`).
- Request interceptor injects `Authorization: Bearer <token>`.
- Notification posts use 150 s timeout + 503/504 retry helper `postNotificationWithRetry`.
- Notification polling every 20 seconds + visibility change listener for real-time bell updates.
- `CustomEvent('portal-notifications-updated')` for cross-component notification sync.

### Endpoint groups used by the frontend

| Group | Paths |
|-------|-------|
| Health / metrics | `/health`, `/health/detailed`, `/metrics/dashboard` |
| Control center | `/v1/control-center/audit-events`, `-replay`, `-dashboard-aggregates` |
| Workforce governance | `/v1/workforce/organizations`, `.../hierarchy`, `/v1/policies/definitions`, `/v1/governance/challenges`, `/v1/decisions`, `/v1/setu/signals`, `/v1/workforce/trace-replay` |
| Auth | `/v1/candidate/login\|register`, `/v1/client/login\|register` |
| Jobs | `/v1/jobs` CRUD + autocomplete endpoints |
| Applications/documents | `/v1/candidate/apply`, `/v1/candidate/applications/:id`, `.../documents/:type`, client/recruiter variants |
| Candidate profile | `/v1/candidate/profile/:id` |
| Matching | `/v1/match/:jobId/top`, `/v1/match/batch` (90 s timeout) |
| Interviews/feedback/tasks | `/v1/interviews`, `/v1/feedback`, `/v1/tasks` (⚠️ not in live OpenAPI — see 15) |
| Workflow bridge | `/v1/candidate/workflow-tasks` (+ `/submit`), `/v1/candidate/workflow-link[-status]` |
| Offers | `/v1/offers` |
| Client/recruiter connection | `/v1/client/profile`, `/v1/client/by-connection/:id`, `/v1/client/connected-recruiter`, `/v1/recruiter/confirm-connection`, `/disconnect`, `/current-connection`, `/v1/connection/health-check`, SSE `connection-events` |
| Notifications | `/v1/notifications/*`, `/v1/portal/notifications` + read/read-all |
| Actions | `/v1/jobs/:id/shortlist\|reject` |
| Stats | `/v1/candidate/stats/:id`, `/v1/client/stats`, `/v1/recruiter/stats` |
| Analytics | `/v1/analytics/skills`, `/v1/analytics/funnel` (⚠️ not in live OpenAPI — see 15) |
| Candidates | `/v1/candidates`, `?search`, `/search`, `/parse-pdf`, `/check-duplicates`, `/bulk` |

---

## 5. Auth Service (`src/services/authService.ts` — 471 lines)

- Singleton class; `API_BASE_URL` from `VITE_API_BASE_URL || 'http://localhost:8000'`.
- `login(role)` → client uses `/v1/client/login`; recruiter/candidate use `/v1/candidate/login`
  (auto-detect fallback tries client then candidate).
- `register` → client branch (`/v1/client/register` with generated `client_id`) vs
  recruiter/candidate branch (`/v1/candidate/register` with `role`).
- `isAuthenticated()` decodes JWT `exp`.
- Token set/remove wired onto `axios.defaults.headers.common`.

---

## 6. Contexts (`src/context/`)

| Context | File | Responsibility |
|---------|------|----------------|
| `AuthContext` | `AuthContext.tsx` | `user`, `loading`, `signIn/signUp/signOut`, `userRole`, `userName`; restore + role resolution. Per-tab isolation via `sessionStorage`. |
| `ThemeContext` | `ThemeContext.tsx` | `localStorage 'bhiv-theme'` (default light); toggles `.dark` class on `<html>`. |
| `SidebarContext` | `SidebarContext.tsx` | Sidebar collapsed/expanded state; mobile menu open/close. |
| `RecruiterConnectionContext` | `RecruiterConnectionContext.tsx` | 24-hex `connectionId` + company; status `none\|connected\|invalid`; persisted under `RECRUITER_LAST_CONNECTION_KEY`; restores from DB; **bidirectional SSE** streams (`/v1/client/connection-events`, `/v1/recruiter/connection-events`); 30 s health check auto-disconnect. Wrapped by `RecruiterLayout`. |
| `CandidateTasksContext` | `CandidateTasksContext.tsx` | Candidate workflow task list from `/v1/candidate/workflow-tasks`; `upsertSubmission` flips `Pending → In Progress`. Wrapped by `CandidateLayout`. |

### Context Provider Hierarchy

```
<ErrorBoundary>
  <AuthProvider>
    <ThemeProvider>
      <BrowserRouter>
        <Routes>
          <CandidateLayout>   <!-- adds SidebarProvider + CandidateTasksProvider -->
          <RecruiterLayout>   <!-- adds SidebarProvider + RecruiterConnectionProvider -->
          <ClientLayout>      <!-- adds SidebarProvider only -->
        </Routes>
      </BrowserRouter>
    </ThemeProvider>
  </AuthProvider>
</ErrorBoundary>
```

### Auth Storage Strategy (`src/utils/authStorage.ts`)

- Per-tab isolation via `sessionStorage` (not `localStorage`) — allows multiple roles logged in simultaneously across browser tabs
- Fallback to `localStorage` if `sessionStorage` unavailable
- All auth keys: `auth_token`, `user_data`, `user_role`, `user_email`, `user_name`, `isAuthenticated`, `candidate_id`, `backend_candidate_id`, `client_id`, `candidate_profile_data`

---

## 7. Components (`src/components/`)

- **Root**: `ApiStatus` (30 s `/health` polling), `AutocompleteSearch`, `BlobLoadingOverlay`,
  `FormInput`, `Loading`, `Navbar` (public), `ProtectedRoute` + `PublicRoute`,
  `SalaryRangeInput`, `Sidebar` (legacy), `SplashScreen`, `StatsCard` (deprecated adapter),
  `Table`.
- **cards/**: `ExecutiveMetricCard`, `TelemetryCard`, `ReplayCard`, `GovernanceCard`,
  `TimelineCard`, `AlertCard`, `EscalationCard`, `MapCard` — read-only observability surfaces
  ("observability not execution authority"). Shared types in `types.ts`, shared styles in
  `cardStyles.ts`.
- **layouts/**: `CandidateLayout` (SidebarProvider + CandidateTasksProvider + RoleNavbar + CandidateSidebar), `RecruiterLayout` (SidebarProvider + RecruiterConnectionProvider + RoleNavbar + RecruiterSidebar), `ClientLayout` (SidebarProvider + RoleNavbar + ClientSidebar).
- **sidebars/**: `CandidateSidebar` (7 nav items + API health + user info + logout), `RecruiterSidebar` (11 nav items + connection status block + API health + user info + logout), `ClientSidebar` (6 nav items + connected recruiter status via SSE + API health + user info + logout).
- **navbars/**: `RoleNavbar` (theme toggle, portal-notification bell polling every 20s with mark-read, user profile, logout).
- **recruiter/**: `BulkCandidateUploadPanel` (858 lines — CSV/XLSX/PDF parse via `xlsx`, editable
  preview, duplicate check, per-job bulk upload).
- **config/**: `notifications.config.ts` — validation regexes, `FILTER_CONFIG` (min match 70,
  new-applicant window 7 days, max 100, bulk warning 20), `CANDIDATE_STATUS` enum,
  `NOTIFICATION_TYPES`.

---

## 8. Environment Variables (`frontend/.env.example`)

| Var | Local default | Notes |
|-----|---------------|-------|
| `VITE_API_BASE_URL` | `http://localhost:8000` | Gateway base URL |
| `VITE_AGENT_SERVICE_URL` | `http://localhost:9000` | Agent (dev only) |
| `VITE_LANGGRAPH_SERVICE_URL` | `http://localhost:9001` | LangGraph (dev only) |
| `VITE_ENABLE_CONTROL_CENTER` | `true` | Show Control Center route |
| `VITE_ENABLE_GOVERNANCE` | `true` | Show governance surfaces |
| Supabase URL/anon key | optional | unused by core flows |

`src/vite-env.d.ts` types all `VITE_*` vars above.

---

## 9. Deployment Config

- **`vercel.json`**: build `npm run build`, output `dist`, framework vite, SPA rewrite
  `/(.*)` → `/index.html`, 1-year immutable Cache-Control on `/assets/*`.
- **`Dockerfile`**: node:20-alpine builder (`npm ci` + `npm run build`); prod serves `dist` on
  port 3000 with `serve` as non-root user `frontend`.

---

## 10. Known Frontend Gaps (see `15_KNOWN_ISSUES_ARCHIVE_INDEX.md`)

1. `src/routes.tsx` unused/redundant (use `App.tsx`).
2. Empty stub pages: `pages/hr/*` (3 files), `pages/auth/HRAuth.tsx`.
3. Frontend references a few paths absent from the live OpenAPI (`/v1/tasks`,
   `/v1/analytics/*`, `/v1/client/shortlist`, `/v1/client/review/:candidateId`).

---

## 11. Verification (2026-08-14)

- `npm run build` (`tsc && vite build`) ✅ — 151 modules, chunk-size warnings only.

---

## 12. Next

→ `10_TESTING_AND_EVIDENCE.md`.
