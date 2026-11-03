# 17 — Multi-Tenant Hierarchy & Admin Access

**Status:** ✅ Implemented & verified 2026-11-03 (offline test suite + end-to-end HTTP test against the
real gateway app + frontend production build)
**Scope:** Additive — no existing feature, endpoint, role, or data shape was changed.

> Read after `07_AUTHENTICATION_AND_SECURITY.md` and `08_DATABASE.md`. This document defines the
> account hierarchy (platform → key account → client), the isolation rules, the admin login link,
> and the exact onboarding flow for **SVA** (key account holder).

---

## 1. The hierarchy

```text
platform  (BHIV / Sampada operators — JWT role=admin)
  │
  ├── key_account: SVA            ← the HR company, our client (role=client, linked CP account)
  │     ├── account: SVA's client A   ← sees only its own subset
  │     └── account: SVA's client B   ← sees only its own subset
  │
  └── key_account: <any other company>
        └── account: ...
```

* **platform** is a virtual root. It is not a `tenants` document; key accounts use
  `parent_tenant_id = "platform"`.
* **key account** = the contractual account holder (SVA). Linked to one or more existing
  Client-Portal logins (their shared CP details) or provisioned fresh.
* **account** = a client *inside* the key account's tenanted subset. Depth is fixed at one level:
  accounts are always created directly under a key account.

## 2. Who logs in where

| Who | Login surface | Role in JWT | Sees |
|-----|---------------|-------------|------|
| Platform admin (BHIV/Sampada ops) | **`/admin/login`** (the admin link) | `admin` | Everything (platform scope) |
| SVA (key account) | regular client portal `/auth` with the shared CP details | `client` | Its own data **+ every client inside its subset** |
| SVA's clients (accounts) | regular client portal `/auth` | `client` | Only their own data |
| Other companies | regular client portal `/auth` | `client` | Only their own data (never SVA's) |
| Recruiters / candidates | unchanged | `recruiter` / `candidate` | unchanged |

**Admin link:** point the browser at `<frontend-origin>/admin/login` (e.g.
`https://sampada.sva.example/admin/login` on the custom domain). `/admin` redirects there.
After sign-in the admin lands on `/admin/tenants`; the Control Center remains at `/control`.

## 3. Backend components

| File | What it adds |
|------|--------------|
| `backend/services/gateway/app/tenant_registry.py` | In-process snapshot of the `tenants` collection: subtree resolution, visible-id sets, TTL refresh (30 s), failure-safe fallback to single-tenant behaviour |
| `backend/services/gateway/routes/tenancy_routes.py` | Admin login + tenant CRUD + key-account sub-client management |
| `backend/services/gateway/app/control_center_governance.py` | `resolve_policy_scope()` now attaches `visible_client_ids` / `visible_tenant_ids` / `tenant_kind`; job + audit scoping expand over a key account's subtree |
| `backend/services/gateway/app/workforce_common.py` | `workforce_scope_filter()` accepts the visible set (single id → identical legacy filter) |
| `backend/services/gateway/app/main.py` | Registry-refresh middleware + subtree expansion in `_client_job_ids_for_dashboard` / `_client_all_job_ids_for_dashboard` (covers every client-portal endpoint) + router include |
| `frontend/src/pages/auth/AdminLoginPage.tsx` | The admin login page (`/admin/login`) |
| `frontend/src/pages/admin/AdminTenantsPage.tsx` | Platform admin hierarchy UI (`/admin/tenants`) |
| `frontend/src/pages/client/SubClients.tsx` | Key-account sub-client management (`/client/sub-clients`) + sidebar item (key accounts only) |

### API surface

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/v1/admin/login` | none | Platform admin sign-in (bootstraps from env on first use) |
| GET | `/v1/admin/tenants` | admin | Full hierarchy + linked CP accounts + counts |
| GET | `/v1/admin/clients` | admin | Every CP account with its `client_id` + linked tenant (`?search=` filters) |
| POST | `/v1/admin/tenants` | admin | Create key account / account; link or provision a CP login |
| PATCH | `/v1/admin/tenants/{tenant_id}` | admin | Rename / suspend / activate |
| POST | `/v1/admin/tenants/{tenant_id}/link-client` | admin | Adopt an existing CP account into a tenant |
| POST | `/v1/admin/tenants/{tenant_id}/unlink-client` | admin | Remove a CP account from a tenant |
| GET | `/v1/tenant/info` | client | Caller's placement: linked, level, key-account flag, visible ids |
| GET | `/v1/tenant/sub-clients` | key account | The clients inside the caller's subset |
| POST | `/v1/tenant/sub-clients` | key account | Create a client inside the caller's subset (+ CP login) |

Every mutation writes a `control_center` audit event (`tenant_create`, `tenant_link_client`, …) so
the change is visible in the Control Center timeline.

## 4. Isolation rules

Visibility is the **set of Client-Portal ids in the caller's subtree** (all existing data is keyed by
`client_id` / `tenant_id` = client id, so nothing had to be migrated):

| Caller | Effective filter |
|--------|------------------|
| Platform admin / API key | no filter (everything) |
| Key account (SVA) | `{tenant_id: {$in: [own id + every linked client id in its subtree]}}` |
| Account (SVA's client) | `{tenant_id: "<own id>"}` — byte-identical to pre-tenancy |
| Unlinked/legacy client | `{tenant_id: "<own id>"}` — byte-identical to pre-tenancy |

Guarantees enforced by the code:

1. A client can never be linked to two tenants (`409` on link) — one account can never appear in
   two companies' subsets.
2. Sub-client creation always uses the caller's own key-account node as parent — a key account
   cannot create accounts outside its own boundary.
3. Non-key accounts (plain clients) receive `403` from `/v1/tenant/sub-clients*`.
4. Client-role tokens receive `403` from every `/v1/admin/*` route; anonymous requests get `401`.
5. If tenancy is not configured (empty `tenants` collection or database unavailable), the registry
   degrades to `[own client id]` — the pre-tenancy behaviour, so existing deployments are unaffected.

## 5. Configuration

Add to the gateway environment (`backend/.env` locally; deployment secrets in production):

```ini
# Platform admin bootstrap — first successful login creates the bcrypt-hashed
# admin in the platform_admins collection. Leave empty to disable bootstrap.
PLATFORM_ADMIN_EMAIL=admin@your-domain.example
PLATFORM_ADMIN_PASSWORD=<strong-password>

# Already required by the gateway (unchanged); the admin JWT is signed with this key
JWT_SECRET_KEY=<existing-secret>
```

No other variable is needed. The admin link works on the existing domain; the **custom domain is a
DNS/CORS/deployment task** (Alay) — add the new origin to `CORS_ORIGINS` (comma-separated) so the
frontend on the custom domain can call the gateway.

> Rotate `PLATFORM_ADMIN_PASSWORD` by changing the env value **and** deleting the
> `platform_admins` document (or changing the password through a fresh bootstrap after removing the
> old document). The env value is only used for the initial bootstrap.

## 6. SVA onboarding runbook

1. **Deploy** the gateway with `PLATFORM_ADMIN_EMAIL` / `PLATFORM_ADMIN_PASSWORD` set; deploy the
   frontend.
2. **Admin link** → open `<origin>/admin/login`, sign in with the platform admin credentials.
3. **Create the key account** for SVA:
   * Click **+ New key account**, enter `SVA`.
   * Choose **Link existing CP account** and paste the `client_id` from the CP details SVA shared
     (or **Provision new CP login** with their email to create one).
   * Submit → SVA appears in the tree with its linked CP account.
4. (Optional) Set `tenant_id` explicitly (e.g. `sva`) if you want stable URLs/reporting keys.
5. **SVA adds its clients** — SVA signs in through the normal client portal with the shared CP
   details, opens **Sub-Clients** in the sidebar, and creates each of its clients (name + login
   email; a password is auto-generated and shown once with `client_id` / `connection_id` to share).
   Platform admins can do the same from `/admin/tenants` → **+ Add client**.
6. **Verify isolation** with the admin page: create a second key account (e.g. a test company) and
   confirm SVA's list never shows its clients, and vice-versa.

## 7. Data model — `tenants`

```jsonc
{
  "tenant_id": "sva",                    // unique slug (2-64: a-z 0-9 - _)
  "name": "SVA",
  "level": "key_account",                // "key_account" | "account"
  "parent_tenant_id": "platform",        // key accounts: "platform"; accounts: their key account
  "path": "/platform/sva/",              // materialized path (accounts append their slug)
  "client_ids": ["sva_cp"],              // linked Client-Portal accounts (clients.client_id)
  "status": "active",                    // "active" | "suspended" (management metadata)
  "created_at": "...",
  "updated_at": "..."
}
```

`platform_admins`: `{ email, password_hash, failed_login_attempts, locked_until, created_at, last_login }`
(indexed unique on `email`; lockout after 5 failed attempts for 30 minutes, mirroring client login).

Suspending a tenant is management metadata only. To actually block a tenant's login, set the linked
`clients` document's `status` to anything other than `active` (the existing client-login check).

## 8. Verification evidence (2026-11-03)

| Check | Command | Result |
|-------|---------|--------|
| New hierarchy/admin suite | `pytest backend/tests/gateway/test_tenant_hierarchy_admin.py` | **27 passed** |
| Whole gateway suite (regression) | `pytest backend/tests/gateway` | **60 passed, 2 skipped, 0 failed** |
| Real app route registration | OpenAPI introspection | all 6 tenancy paths present (180 total paths) |
| Frontend typecheck + build | `cd frontend && npm run build` | **passes** (`tsc && vite build`) |

The new suite covers: subtree visibility, sibling/rival isolation, legacy (no-tenancy) filter
equality, workforce/audit/job scoping, admin login bootstrap + lockout behaviour, admin-vs-client
route guards, double-link rejection, tenant status changes, and a full over-HTTP flow
(admin login → onboard SVA → SVA client login → SVA creates a sub-client → sub-client login →
isolation verified through the real `app.main` gateway including the registry middleware).

## 9. Deployment notes

* The registry is an **in-process cache with a 30 s TTL**, refreshed eagerly after every tenant
  mutation. Multiple gateway replicas are safe (each refreshes independently); no shared state.
* The admin JWT uses the existing `JWT_SECRET_KEY`; admin sessions last 24 h like client sessions.
* Custom domain: add the origin to `CORS_ORIGINS` and point DNS/proxy — no tenant-specific config is
  required for a domain change.
* No secrets belong in the repository. `PLATFORM_ADMIN_*` go in the deployment secret store only.
