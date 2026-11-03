"""Tenant hierarchy + admin access tests (offline, no MongoDB required).

Proves the account-isolation model end to end:

* platform -> key_account (SVA) -> account (SVA's clients) subtree visibility
* a key account sees exactly its own subset — never a sibling or rival tenant
* an account sees only itself, even when siblings exist
* scope resolution / filters are byte-identical to pre-tenancy behaviour when
  no tenants are configured (existing deployments unaffected)
* admin login bootstrap, token role, and route guards (admin vs client)
* key-account sub-client management rejects non-key accounts (403)

All DB access is served by a small in-memory fake so the real router logic
(tenancy_routes) runs unchanged.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

GATEWAY_ROOT = Path(__file__).resolve().parents[2] / "services" / "gateway"
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

# Secrets must exist before jwt_auth is imported by any test module.
os.environ.setdefault("JWT_SECRET_KEY", "tenancy-test-jwt-secret-0123456789abcdef")
os.environ.setdefault("CANDIDATE_JWT_SECRET_KEY", "tenancy-test-candidate-secret-012345678")
os.environ.setdefault("PLATFORM_ADMIN_EMAIL", "root@sampada.test")
os.environ.setdefault("PLATFORM_ADMIN_PASSWORD", "AdminPassw0rd!")

import jwt as pyjwt  # noqa: E402
import pytest  # noqa: E402

from app import tenant_registry  # noqa: E402
from app.control_center_governance import (  # noqa: E402
    _audit_scope_filter,
    _client_all_job_ids_for_dashboard,
    resolve_policy_scope,
)
from app.workforce_common import workforce_scope_filter  # noqa: E402

pytestmark = pytest.mark.e2e_unit


# ── in-memory MongoDB fake ────────────────────────────────────────────────────

def _dig(doc: Dict[str, Any], key: str) -> Any:
    value: Any = doc
    for part in key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _match(doc: Dict[str, Any], flt: Optional[Dict[str, Any]]) -> bool:
    if not flt:
        return True
    for key, expected in flt.items():
        if key == "$or":
            if not any(_match(doc, sub) for sub in expected):
                return False
            continue
        value = _dig(doc, key)
        if isinstance(expected, dict) and any(str(k).startswith("$") for k in expected):
            for op, operand in expected.items():
                if op == "$ne":
                    if value == operand:
                        return False
                elif op == "$in":
                    if isinstance(value, list):
                        if not any(v in operand for v in value):
                            return False
                    elif value not in operand:
                        return False
                elif op == "$regex":
                    import re as _re

                    flags = (
                        _re.I
                        if "i" in str((expected.get("$options") or "")).lower()
                        else 0
                    )
                    if not _re.search(str(operand), str(value or ""), flags):
                        return False
                elif op == "$options":
                    continue
                else:
                    raise AssertionError(f"fake db: unsupported operator {op}")
            continue
        if isinstance(value, list):
            if expected not in value:
                return False
        elif value != expected:
            return False
    return True


class _Cursor:
    def __init__(self, docs: List[Dict[str, Any]]):
        self._docs = list(docs)

    def limit(self, n: int):
        self._docs = self._docs[:n]
        return self

    def sort(self, *args, **kwargs):
        return self

    async def to_list(self, length: Optional[int] = None):
        return list(self._docs if length is None else self._docs[:length])


class FakeCollection:
    def __init__(self, name: str):
        self.name = name
        self.docs: List[Dict[str, Any]] = []

    async def create_index(self, *args, **kwargs):
        return None

    def find(self, flt=None, proj=None):
        return _Cursor([d for d in self.docs if _match(d, flt or {})])

    async def find_one(self, flt, proj=None):
        for doc in self.docs:
            if _match(doc, flt or {}):
                return doc
        return None

    async def insert_one(self, doc):
        # pymongo mutates the passed document in-place (adds _id) — mimic that
        doc.setdefault("_id", f"{self.name}_{uuid.uuid4().hex[:10]}")
        self.docs.append(doc)
        return MagicMock(inserted_id=doc["_id"])

    async def update_one(self, flt, update, **kwargs):
        for doc in self.docs:
            if _match(doc, flt or {}):
                if "$set" in update:
                    doc.update(update["$set"])
                for key, value in (update.get("$addToSet") or {}).items():
                    current = doc.setdefault(key, [])
                    if value not in current:
                        current.append(value)
                for key, value in (update.get("$pull") or {}).items():
                    if isinstance(doc.get(key), list):
                        doc[key] = [v for v in doc[key] if v != value]
                return MagicMock(matched_count=1, modified_count=1)
        return MagicMock(matched_count=0, modified_count=0)


class FakeDB:
    def __init__(self):
        self.tenants = FakeCollection("tenants")
        self.clients = FakeCollection("clients")
        self.platform_admins = FakeCollection("platform_admins")
        self.audit_logs = FakeCollection("audit_logs")
        self.jobs = FakeCollection("jobs")
        self.client_connected_recruiter = FakeCollection("client_connected_recruiter")


# ── hierarchy fixtures ────────────────────────────────────────────────────────

NOW = datetime.now(timezone.utc)

HIERARCHY_DOCS: List[Dict[str, Any]] = [
    {
        "tenant_id": "sva",
        "name": "SVA HR",
        "level": "key_account",
        "parent_tenant_id": "platform",
        "path": "/platform/sva/",
        "client_ids": ["sva_cp"],
        "status": "active",
        "created_at": NOW,
    },
    {
        "tenant_id": "sva-alpha",
        "name": "Alpha Ltd",
        "level": "account",
        "parent_tenant_id": "sva",
        "path": "/platform/sva/sva-alpha/",
        "client_ids": ["alpha_cp"],
        "status": "active",
        "created_at": NOW,
    },
    {
        "tenant_id": "sva-beta",
        "name": "Beta GmbH",
        "level": "account",
        "parent_tenant_id": "sva",
        "path": "/platform/sva/sva-beta/",
        "client_ids": ["beta_cp"],
        "status": "active",
        "created_at": NOW,
    },
    {
        "tenant_id": "rival",
        "name": "Rival Corp",
        "level": "key_account",
        "parent_tenant_id": "platform",
        "path": "/platform/rival/",
        "client_ids": ["rival_cp"],
        "status": "active",
        "created_at": NOW,
    },
    {
        "tenant_id": "rival-x",
        "name": "Rival Sub",
        "level": "account",
        "parent_tenant_id": "rival",
        "path": "/platform/rival/rival-x/",
        "client_ids": ["rivalx_cp"],
        "status": "active",
        "created_at": NOW,
    },
    # Invalid level — must be skipped, never crash scope resolution
    {"tenant_id": "broken", "level": "nope", "client_ids": ["broken_cp"]},
]


@pytest.fixture
def hierarchy():
    snapshot = tenant_registry.build_snapshot(HIERARCHY_DOCS)
    tenant_registry.set_snapshot(snapshot)
    yield snapshot
    tenant_registry.set_snapshot(tenant_registry.build_snapshot([]))


@pytest.fixture
def empty_hierarchy():
    tenant_registry.set_snapshot(tenant_registry.build_snapshot([]))
    yield
    tenant_registry.set_snapshot(tenant_registry.build_snapshot([]))


# ── registry / subtree visibility ─────────────────────────────────────────────

def test_key_account_sees_own_subtree(hierarchy):
    assert hierarchy.visible_client_ids("sva_cp") == ["sva_cp", "alpha_cp", "beta_cp"]


def test_account_sees_only_itself(hierarchy):
    assert hierarchy.visible_client_ids("alpha_cp") == ["alpha_cp"]
    assert hierarchy.visible_tenant_ids("alpha_cp") == ["alpha_cp"]


def test_rival_company_subset_is_disjoint(hierarchy):
    sva_set = set(hierarchy.visible_client_ids("sva_cp"))
    rival_set = set(hierarchy.visible_client_ids("rival_cp"))
    assert sva_set & rival_set == set()
    # sibling account cannot reach its nephew or the rival
    assert "beta_cp" not in hierarchy.visible_client_ids("alpha_cp")
    assert "rival_cp" not in hierarchy.visible_client_ids("sva_cp")


def test_unlinked_client_falls_back_to_single_tenant(hierarchy):
    assert hierarchy.visible_client_ids("unknown_cp") == ["unknown_cp"]


def test_invalid_documents_are_skipped(hierarchy):
    assert "broken" not in hierarchy.nodes
    # ...and the broken doc's client id must not inherit visibility anywhere
    assert "broken_cp" not in hierarchy.visible_client_ids("sva_cp")


def test_tree_shape(hierarchy):
    tree = hierarchy.to_tree()
    assert [n["tenant_id"] for n in tree] == ["sva", "rival"]
    sva = tree[0]
    assert sva["level"] == "key_account"
    assert [c["tenant_id"] for c in sva["children"]] == ["sva-alpha", "sva-beta"]


# ── policy scope resolution (backward compatible) ─────────────────────────────

def test_resolve_scope_key_account_expands(hierarchy):
    scope = resolve_policy_scope({"type": "jwt_token", "role": "client", "user_id": "sva_cp"})
    assert scope["scope"] == "client"
    assert scope["scope_label"] == "client:sva_cp"  # label unchanged
    assert scope["tenant_id"] == "sva_cp"  # write key unchanged
    assert set(scope["visible_client_ids"]) == {"sva_cp", "alpha_cp", "beta_cp"}
    assert scope["tenant_kind"] == "key_account"
    assert scope["root_tenant_id"] == "sva"


def test_resolve_scope_account_is_isolated(hierarchy):
    scope = resolve_policy_scope({"type": "jwt_token", "role": "client", "user_id": "alpha_cp"})
    assert scope["visible_client_ids"] == ["alpha_cp"]
    assert scope["tenant_kind"] == "account"
    assert "beta_cp" not in scope["visible_client_ids"]


def test_resolve_scope_without_tenants_is_unchanged(empty_hierarchy):
    scope = resolve_policy_scope({"type": "jwt_token", "role": "client", "user_id": "LEGACY"})
    assert scope["scope"] == "client"
    assert scope["tenant_id"] == "LEGACY"
    assert scope["visible_client_ids"] == ["LEGACY"]
    assert scope["visible_tenant_ids"] == ["LEGACY"]
    assert scope["tenant_kind"] is None


def test_resolve_scope_api_key_stays_platform():
    scope = resolve_policy_scope({"type": "api_key"})
    assert scope["scope"] == "platform"
    assert "visible_client_ids" not in scope


# ── workforce isolation filter ────────────────────────────────────────────────

def test_workforce_filter_key_account_includes_subtree(hierarchy):
    scope = resolve_policy_scope({"type": "jwt_token", "role": "client", "user_id": "sva_cp"})
    filt = workforce_scope_filter(scope)
    assert set(filt["tenant_id"]["$in"]) == {"sva_cp", "alpha_cp", "beta_cp"}
    assert "rival_cp" not in filt["tenant_id"]["$in"]
    assert "rivalx_cp" not in filt["tenant_id"]["$in"]


def test_workforce_filter_single_tenant_stays_equality(hierarchy):
    scope = resolve_policy_scope({"type": "jwt_token", "role": "client", "user_id": "alpha_cp"})
    # single-member sets keep the exact legacy shape ({"tenant_id": "x"})
    assert workforce_scope_filter(scope) == {"tenant_id": "alpha_cp"}


def test_workforce_filter_raw_scope_unchanged():
    assert workforce_scope_filter({"scope": "client", "user_id": "TENANT_A"}) == {
        "tenant_id": "TENANT_A"
    }


def test_workforce_filter_platform_bypass():
    assert workforce_scope_filter({"scope": "platform"}) == {}


# ── job visibility expansion ──────────────────────────────────────────────────

def _jobs_db() -> FakeDB:
    db = FakeDB()
    db.jobs.docs = [
        {"_id": "job_sva", "client_id": "sva_cp", "status": "active"},
        {"_id": "job_alpha", "client_id": "alpha_cp", "status": "active"},
        {"_id": "job_beta", "client_id": "beta_cp", "status": "active"},
        {"_id": "job_rival", "client_id": "rival_cp", "status": "active"},
        {"_id": "job_orphan", "client_id": "unknown_cp", "status": "active"},
    ]
    return db


def test_key_account_job_ids_include_subtree(hierarchy):
    db = _jobs_db()
    ids = asyncio.run(_client_all_job_ids_for_dashboard(db, "sva_cp"))
    assert set(ids) == {"job_sva", "job_alpha", "job_beta"}
    assert "job_rival" not in ids


def test_account_job_ids_stay_isolated(hierarchy):
    db = _jobs_db()
    ids = asyncio.run(_client_all_job_ids_for_dashboard(db, "alpha_cp"))
    assert ids == ["job_alpha"]


def test_job_ids_without_tenancy_unchanged(empty_hierarchy):
    db = _jobs_db()
    ids = asyncio.run(_client_all_job_ids_for_dashboard(db, "unknown_cp"))
    assert ids == ["job_orphan"]


# ── audit visibility ──────────────────────────────────────────────────────────

def test_audit_filter_single_tenant_matches_legacy_shape():
    scope = resolve_policy_scope({"type": "jwt_token", "role": "client", "user_id": "X"})
    filt = _audit_scope_filter(scope)
    assert filt["$or"] == [
        {"actor.user_id": "X"},
        {"actor.role": "client"},
        {"context.client_id": "X"},
        {"context.scope_label": "client:X"},
    ]


def test_audit_filter_key_account_widens_to_subtree(hierarchy):
    scope = resolve_policy_scope({"type": "jwt_token", "role": "client", "user_id": "sva_cp"})
    filt = _audit_scope_filter(scope)
    or_clauses = filt["$or"]
    assert {"actor.user_id": {"$in": ["sva_cp", "alpha_cp", "beta_cp"]}} in or_clauses
    assert {"context.client_id": {"$in": ["sva_cp", "alpha_cp", "beta_cp"]}} in or_clauses
    assert {
        "context.scope_label": {"$in": ["client:sva_cp", "client:alpha_cp", "client:beta_cp"]}
    } in or_clauses


# ── admin login + tenancy API (real router, fake DB) ─────────────────────────

def _client_token(user_id: str, role: str = "client") -> str:
    return pyjwt.encode(
        {
            "sub": user_id,
            "user_id": user_id,
            "client_id": user_id,
            "role": role,
            "exp": int(datetime.now(timezone.utc).timestamp()) + 3600,
        },
        os.environ["JWT_SECRET_KEY"],
        algorithm="HS256",
    )


@pytest.fixture
def api(monkeypatch):
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from routes import tenancy_routes

    monkeypatch.setattr(
        tenancy_routes, "get_mongo_db", AsyncMock(return_value=FakeDB()), raising=False
    )
    db = tenancy_routes.get_mongo_db.return_value
    asyncio.run(tenant_registry.refresh(db))

    app = FastAPI()
    app.include_router(tenancy_routes.router)
    client = TestClient(app)
    yield client, db, tenancy_routes
    tenant_registry.set_snapshot(tenant_registry.build_snapshot([]))


def _admin_headers(client) -> Dict[str, str]:
    response = client.post(
        "/v1/admin/login",
        json={
            "email": os.environ["PLATFORM_ADMIN_EMAIL"],
            "password": os.environ["PLATFORM_ADMIN_PASSWORD"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True, body
    return {"Authorization": f"Bearer {body['access_token']}"}


def test_admin_login_bootstrap_and_token(api):
    client, db, _ = api
    response = client.post(
        "/v1/admin/login",
        json={
            "email": os.environ["PLATFORM_ADMIN_EMAIL"],
            "password": os.environ["PLATFORM_ADMIN_PASSWORD"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["role"] == "admin"
    assert len(db.platform_admins.docs) == 1  # bootstrapped on first login

    payload = pyjwt.decode(
        body["access_token"], os.environ["JWT_SECRET_KEY"], algorithms=["HS256"]
    )
    assert payload["role"] == "admin"
    assert payload["tenant_kind"] == "platform"

    # Wrong password → rejected, no duplicate admin
    bad = client.post(
        "/v1/admin/login",
        json={
            "email": os.environ["PLATFORM_ADMIN_EMAIL"],
            "password": "definitely-wrong",
        },
    )
    assert bad.status_code == 200 and bad.json()["success"] is False
    assert len(db.platform_admins.docs) == 1

    # Second login uses the stored bcrypt hash (env no longer required)
    again = client.post(
        "/v1/admin/login",
        json={
            "email": os.environ["PLATFORM_ADMIN_EMAIL"],
            "password": os.environ["PLATFORM_ADMIN_PASSWORD"],
        },
    )
    assert again.json()["success"] is True

    # Unknown account never bootstraps
    unknown = client.post(
        "/v1/admin/login", json={"email": "stranger@elsewhere.test", "password": "x"}
    )
    assert unknown.json()["success"] is False
    assert len(db.platform_admins.docs) == 1


def test_admin_routes_reject_non_admin_tokens(api):
    client, _, _ = api
    # Anonymous → 401
    assert client.get("/v1/admin/tenants").status_code == 401
    # Client-role token (e.g. SVA itself) → 403: hierarchy admin is platform-only
    sva = {"Authorization": f"Bearer {_client_token('sva_cp')}"}
    assert client.get("/v1/admin/tenants", headers=sva).status_code == 403
    assert (
        client.post(
            "/v1/admin/tenants",
            json={"name": "Hacker Inc", "level": "key_account"},
            headers=sva,
        ).status_code
        == 403
    )
    # Admin token → 200
    admin = _admin_headers(client)
    assert client.get("/v1/admin/tenants", headers=admin).status_code == 200


def test_admin_onboards_key_account_and_links_shared_cp_details(api):
    client, db, _ = api
    admin = _admin_headers(client)

    # SVA's shared Client-Portal account already exists
    db.clients.docs.append(
        {
            "client_id": "sva_cp",
            "company_name": "SVA",
            "email": "sva@shared.test",
            "status": "active",
        }
    )

    created = client.post(
        "/v1/admin/tenants",
        json={
            "name": "SVA",
            "level": "key_account",
            "tenant_id": "sva",
            "client_id": "sva_cp",
        },
        headers=admin,
    )
    assert created.status_code == 201, created.text
    assert created.json()["tenant"]["path"] == "/platform/sva/"

    # Registry refreshed eagerly: SVA now resolves as key account
    info = client.get("/v1/tenant/info", headers={"Authorization": f"Bearer {_client_token('sva_cp')}"})
    assert info.status_code == 200
    info_body = info.json()
    assert info_body["linked"] is True
    assert info_body["is_key_account"] is True
    assert info_body["tenant_id"] == "sva"

    # Duplicate tenant id → 409
    dup = client.post(
        "/v1/admin/tenants",
        json={"name": "SVA again", "level": "key_account", "tenant_id": "sva"},
        headers=admin,
    )
    assert dup.status_code == 409

    # Linking the same CP account to a second tenant is refused (isolation integrity)
    rival = client.post(
        "/v1/admin/tenants",
        json={"name": "Rival", "level": "key_account", "tenant_id": "rival"},
        headers=admin,
    )
    assert rival.status_code == 201
    steal = client.post(
        "/v1/admin/tenants/sva/link-client",
        json={"client_id": "sva_cp"},
        headers=admin,
    )
    # already linked to sva → same-tenant link is a no-op success...
    assert steal.status_code == 200
    steal2 = client.post(
        "/v1/admin/tenants/rival/link-client",
        json={"client_id": "sva_cp"},
        headers=admin,
    )
    # ...but a *different* tenant may not adopt it
    assert steal2.status_code == 409

    # Accounts may only be created under a key account
    bad_parent = client.post(
        "/v1/admin/tenants",
        json={"name": "Orphan", "level": "account", "parent_tenant_id": "platform"},
        headers=admin,
    )
    assert bad_parent.status_code == 400


def test_admin_creates_account_with_provisioned_login(api):
    client, db, _ = api
    admin = _admin_headers(client)
    client.post(
        "/v1/admin/tenants",
        json={"name": "SVA", "level": "key_account", "tenant_id": "sva"},
        headers=admin,
    )
    created = client.post(
        "/v1/admin/tenants",
        json={
            "name": "Alpha Ltd",
            "level": "account",
            "parent_tenant_id": "sva",
            "provision_client": {"email": "alpha@sva.test", "company_name": "Alpha Ltd"},
        },
        headers=admin,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    account = body["client_account"]
    assert account["email"] == "alpha@sva.test"
    assert account["password"]  # auto-generated, returned once
    assert body["tenant"]["parent_tenant_id"] == "sva"
    # The provisioned login really exists in the clients collection
    assert any(c["email"] == "alpha@sva.test" for c in db.clients.docs)


def test_status_update_suspends_and_restores(api):
    client, _, _ = api
    admin = _admin_headers(client)
    client.post(
        "/v1/admin/tenants",
        json={"name": "SVA", "level": "key_account", "tenant_id": "sva"},
        headers=admin,
    )
    updated = client.patch(
        "/v1/admin/tenants/sva",
        json={"status": "suspended"},
        headers=admin,
    )
    assert updated.status_code == 200
    listing = client.get("/v1/admin/tenants", headers=admin).json()
    sva = next(t for t in listing["tenants"] if t["tenant_id"] == "sva")
    assert sva["status"] == "suspended"

    restored = client.patch(
        "/v1/admin/tenants/sva", json={"status": "active"}, headers=admin
    )
    assert restored.status_code == 200
    listing = client.get("/v1/admin/tenants", headers=admin).json()
    sva = next(t for t in listing["tenants"] if t["tenant_id"] == "sva")
    assert sva["status"] == "active"


def test_key_account_manages_only_its_own_subset(api, hierarchy):
    client, db, _ = api
    db.tenants.docs.extend([dict(d) for d in HIERARCHY_DOCS if d.get("level") in ("key_account", "account")])
    asyncio.run(tenant_registry.refresh(db))

    sva_headers = {"Authorization": f"Bearer {_client_token('sva_cp')}"}
    alpha_headers = {"Authorization": f"Bearer {_client_token('alpha_cp')}"}
    rival_headers = {"Authorization": f"Bearer {_client_token('rival_cp')}"}
    stranger_headers = {"Authorization": f"Bearer {_client_token('stranger_cp')}"}

    # SVA lists its subset: both children, nothing from the rival
    listing = client.get("/v1/tenant/sub-clients", headers=sva_headers)
    assert listing.status_code == 200
    body = listing.json()
    assert body["tenant"]["tenant_id"] == "sva"
    assert {s["tenant_id"] for s in body["sub_clients"]} == {"sva-alpha", "sva-beta"}
    assert set(body["visible_client_ids"]) == {"sva_cp", "alpha_cp", "beta_cp"}

    # Rival key account sees only its own subset
    rival_listing = client.get("/v1/tenant/sub-clients", headers=rival_headers)
    assert rival_listing.status_code == 200
    assert {s["tenant_id"] for s in rival_listing.json()["sub_clients"]} == {"rival-x"}

    # A plain account inside the hierarchy cannot manage sub-clients (403)
    assert client.get("/v1/tenant/sub-clients", headers=alpha_headers).status_code == 403
    # An unlinked client cannot either (403)
    assert (
        client.get("/v1/tenant/sub-clients", headers=stranger_headers).status_code == 403
    )
    # ...and never for creating
    denied = client.post(
        "/v1/tenant/sub-clients",
        json={"name": "Sneaky", "email": "sneaky@x.test"},
        headers=alpha_headers,
    )
    assert denied.status_code == 403

    # SVA creates a client inside its own subset
    created = client.post(
        "/v1/tenant/sub-clients",
        json={"name": "Gamma LLC", "email": "gamma@sva.test"},
        headers=sva_headers,
    )
    assert created.status_code == 201, created.text
    created_body = created.json()
    assert created_body["tenant"]["parent_tenant_id"] == "sva"
    assert created_body["tenant"]["level"] == "account"
    child_client_id = created_body["client_account"]["client_id"]

    # New child is visible to SVA...
    refreshed = client.get("/v1/tenant/info", headers=sva_headers).json()
    assert child_client_id in refreshed["visible_client_ids"]
    # ...but still invisible to a sibling account and to the rival
    alpha_info = client.get("/v1/tenant/info", headers=alpha_headers).json()
    assert child_client_id not in alpha_info["visible_client_ids"]
    rival_info = client.get("/v1/tenant/info", headers=rival_headers).json()
    assert child_client_id not in rival_info["visible_client_ids"]


def test_end_to_end_http_hierarchy_through_real_gateway(monkeypatch):
    """Full over-HTTP flow against the real gateway app (not a mini app).

    admin login -> onboard SVA (key account, linked shared CP account) ->
    SVA client login -> SVA creates a sub-client -> sub-client login ->
    isolation verified (tenant info + job visibility + admin route guard).
    This also exercises the registry-refresh middleware on every request.
    """
    import bcrypt

    main_module = pytest.importorskip("app.main")
    import app.database as database_module
    from fastapi.testclient import TestClient
    from routes import tenancy_routes

    db = FakeDB()
    monkeypatch.setattr(main_module, "get_mongo_db", AsyncMock(return_value=db))
    monkeypatch.setattr(tenancy_routes, "get_mongo_db", AsyncMock(return_value=db))
    monkeypatch.setattr(database_module, "get_mongo_db", AsyncMock(return_value=db))
    tenant_registry.set_snapshot(tenant_registry.build_snapshot([]))
    asyncio.run(tenant_registry.refresh(db))

    try:
        client = TestClient(main_module.app)

        # SVA's shared Client-Portal credentials already exist
        db.clients.docs.append(
            {
                "client_id": "sva_cp",
                "company_name": "SVA Services",
                "email": "sva@shared.test",
                "password_hash": bcrypt.hashpw(b"SvaPassw0rd!", bcrypt.gensalt()).decode(),
                "status": "active",
                "failed_login_attempts": 0,
                "locked_until": None,
            }
        )

        admin = _admin_headers(client)

        onboard = client.post(
            "/v1/admin/tenants",
            json={
                "name": "SVA",
                "level": "key_account",
                "tenant_id": "sva",
                "client_id": "sva_cp",
            },
            headers=admin,
        )
        assert onboard.status_code == 201, onboard.text

        # Rival company (separate key account) with its own client
        rival = client.post(
            "/v1/admin/tenants",
            json={
                "name": "Rival Corp",
                "level": "key_account",
                "tenant_id": "rival",
                "provision_client": {"email": "rival@other.test", "company_name": "Rival Corp"},
            },
            headers=admin,
        )
        assert rival.status_code == 201, rival.text
        rival_client_id = rival.json()["client_account"]["client_id"]

        # SVA signs in through the normal client portal with its shared CP details
        login = client.post(
            "/v1/client/login", json={"email": "sva@shared.test", "password": "SvaPassw0rd!"}
        )
        assert login.status_code == 200
        assert login.json()["success"] is True, login.text
        sva_token = login.json()["access_token"]
        sva_headers = {"Authorization": f"Bearer {sva_token}"}

        # Hierarchy placement is resolved from the JWT via the registry middleware
        info = client.get("/v1/tenant/info", headers=sva_headers)
        assert info.status_code == 200, info.text
        assert info.json()["is_key_account"] is True
        assert info.json()["tenant_id"] == "sva"

        # SVA is a client, never a platform admin
        assert client.get("/v1/admin/tenants", headers=sva_headers).status_code == 403

        # SVA creates a client inside its own subset
        created = client.post(
            "/v1/tenant/sub-clients",
            json={"name": "Alpha Ltd", "email": "ops@alpha.test"},
            headers=sva_headers,
        )
        assert created.status_code == 201, created.text
        created_body = created.json()
        child_client_id = created_body["client_account"]["client_id"]
        child_password = created_body["client_account"]["password"]

        # The new client signs in and sees only its own slice
        child_login = client.post(
            "/v1/client/login",
            json={"email": "ops@alpha.test", "password": child_password},
        )
        assert child_login.json()["success"] is True, child_login.text
        child_headers = {"Authorization": f"Bearer {child_login.json()['access_token']}"}

        child_info = client.get("/v1/tenant/info", headers=child_headers).json()
        assert child_info["linked"] is True
        assert child_info["is_key_account"] is False
        assert child_info["visible_client_ids"] == [child_client_id]
        assert client.get("/v1/tenant/sub-clients", headers=child_headers).status_code == 403

        # Job visibility: SVA sees its own + sub-client jobs, never the rival's
        db.jobs.docs.extend(
            [
                {"_id": "job_sva", "client_id": "sva_cp", "status": "active"},
                {"_id": "job_child", "client_id": child_client_id, "status": "active"},
                {"_id": "job_rival", "client_id": rival_client_id, "status": "active"},
            ]
        )
        sva_jobs = asyncio.run(main_module._client_job_ids_for_dashboard(db, "sva_cp"))
        assert set(sva_jobs) == {"job_sva", "job_child"}
        assert "job_rival" not in sva_jobs

        child_jobs = asyncio.run(
            main_module._client_job_ids_for_dashboard(db, child_client_id)
        )
        assert child_jobs == ["job_child"]
    finally:
        tenant_registry.set_snapshot(tenant_registry.build_snapshot([]))


def test_tenant_info_for_plain_and_unlinked_clients(api, hierarchy):
    client, _, _ = api
    # Linked account reports its level
    alpha = client.get(
        "/v1/tenant/info", headers={"Authorization": f"Bearer {_client_token('alpha_cp')}"}
    ).json()
    assert alpha["linked"] is True
    assert alpha["is_key_account"] is False
    assert alpha["visible_client_ids"] == ["alpha_cp"]

    # Unlinked client keeps legacy single-tenant behaviour
    stranger = client.get(
        "/v1/tenant/info", headers={"Authorization": f"Bearer {_client_token('stranger_cp')}"}
    ).json()
    assert stranger["linked"] is False
    assert stranger["visible_client_ids"] == ["stranger_cp"]

    # Platform admin sees platform scope
    admin = _admin_headers(client)
    platform = client.get("/v1/tenant/info", headers=admin).json()
    assert platform["scope"] == "platform"
    assert platform["level"] == "platform"


# ── admin client discovery (GET /v1/admin/clients) ───────────────────────────

def _seed_clients(db) -> None:
    db.clients.docs.extend(
        [
            {
                "client_id": "sva_cp",
                "company_name": "SVA HR",
                "email": "ops@sva.test",
                "status": "active",
                "client_code": "SVA001",
            },
            {
                "client_id": "alpha_cp",
                "company_name": "Alpha Ltd",
                "email": "hr@alpha.test",
                "status": "active",
                "client_code": "ALP002",
            },
            {
                "client_id": "lonely_cp",
                "company_name": "Lonely Corp",
                "email": "info@lonely.test",
                "status": "active",
                "client_code": "LON003",
            },
        ]
    )


def test_admin_list_clients_maps_linked_tenants(api):
    client, db, _ = api
    db.tenants.docs.extend(
        [dict(d) for d in HIERARCHY_DOCS if d.get("level") in ("key_account", "account")]
    )
    _seed_clients(db)
    admin = _admin_headers(client)

    resp = client.get("/v1/admin/clients", headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["count"] == 3
    by_id = {c["client_id"]: c for c in body["clients"]}
    # linked accounts point at the tenant that holds them...
    assert by_id["sva_cp"]["linked_tenant_id"] == "sva"
    assert by_id["alpha_cp"]["linked_tenant_id"] == "sva-alpha"
    # ...an account with no tenant is null (the discovery case)
    assert by_id["lonely_cp"]["linked_tenant_id"] is None
    assert by_id["sva_cp"]["company_name"] == "SVA HR"
    assert by_id["sva_cp"]["email"] == "ops@sva.test"


def test_admin_list_clients_search_filters(api):
    client, db, _ = api
    _seed_clients(db)
    admin = _admin_headers(client)

    resp = client.get(
        "/v1/admin/clients", params={"search": "alpha"}, headers=admin
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 1
    assert body["clients"][0]["client_id"] == "alpha_cp"

    # case-insensitive match over email as well
    resp = client.get(
        "/v1/admin/clients", params={"search": "LONELY"}, headers=admin
    )
    assert resp.json()["count"] == 1
    assert resp.json()["clients"][0]["client_id"] == "lonely_cp"

    # no match → empty list, not an error
    resp = client.get(
        "/v1/admin/clients", params={"search": "zzz-none"}, headers=admin
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_admin_list_clients_rejects_non_admin(api):
    client, _, _ = api
    # anonymous → 401
    assert client.get("/v1/admin/clients").status_code == 401
    # client-role token → 403 (platform-only surface)
    sva = {"Authorization": f"Bearer {_client_token('sva_cp')}"}
    assert client.get("/v1/admin/clients", headers=sva).status_code == 403
    # admin → 200
    admin = _admin_headers(client)
    assert client.get("/v1/admin/clients", headers=admin).status_code == 200


# ── tenant-provisioned client login (real gateway app) ───────────────────────

def test_client_login_normalizes_email_and_rejects_bad_password(monkeypatch):
    """A tenant provisions a CP login with an email + password; the client must
    be able to sign in with that email in any casing/whitespace, and wrong
    passwords must keep failing. Also covers legacy mixed-case stored emails."""
    import bcrypt

    main_module = pytest.importorskip("app.main")
    import app.database as database_module
    from fastapi.testclient import TestClient
    from routes import tenancy_routes

    db = FakeDB()
    monkeypatch.setattr(main_module, "get_mongo_db", AsyncMock(return_value=db))
    monkeypatch.setattr(tenancy_routes, "get_mongo_db", AsyncMock(return_value=db))
    monkeypatch.setattr(database_module, "get_mongo_db", AsyncMock(return_value=db))
    tenant_registry.set_snapshot(tenant_registry.build_snapshot([]))
    asyncio.run(tenant_registry.refresh(db))

    try:
        client = TestClient(main_module.app)
        admin = _admin_headers(client)

        # Tenant provisions a client login — email typed with mixed case
        created = client.post(
            "/v1/admin/tenants",
            json={
                "name": "Case Corp",
                "level": "key_account",
                "tenant_id": "case-corp",
                "provision_client": {
                    "email": "Ops@Case.TEST",
                    "company_name": "Case Corp",
                    "password": "CasePass123!",
                },
            },
            headers=admin,
        )
        assert created.status_code == 201, created.text
        stored = next(
            c for c in db.clients.docs if c.get("company_name") == "Case Corp"
        )
        assert stored["email"] == "ops@case.test"  # normalized on write

        # Email exactly as typed (mixed case) must work
        login = client.post(
            "/v1/client/login",
            json={"email": "Ops@Case.TEST", "password": "CasePass123!"},
        )
        assert login.status_code == 200
        assert login.json()["success"] is True, login.text
        body = login.json()
        assert body["company_name"] == "Case Corp"
        claims = pyjwt.decode(
            body["access_token"], os.environ["JWT_SECRET_KEY"], algorithms=["HS256"]
        )
        assert claims["role"] == "client"
        assert claims["client_id"] == stored["client_id"]
        assert claims["email"] == "ops@case.test"

        # Whitespace + different casing resolves to the same account
        login2 = client.post(
            "/v1/client/login",
            json={"email": "  OPS@CASE.test  ", "password": "CasePass123!"},
        )
        assert login2.json()["success"] is True, login2.text

        # Wrong password is still rejected
        rejected = client.post(
            "/v1/client/login",
            json={"email": "ops@case.test", "password": "WrongPass999!"},
        )
        assert rejected.status_code == 200
        assert rejected.json()["success"] is False

        # Legacy/seed docs stored with mixed-case email match case-insensitively
        db.clients.docs.append(
            {
                "client_id": "legacy_cp",
                "company_name": "Legacy Co",
                "email": "Mixed@Case.Test",
                "password_hash": bcrypt.hashpw(
                    b"LegacyPass1!", bcrypt.gensalt()
                ).decode("utf-8"),
                "status": "active",
                "failed_login_attempts": 0,
                "locked_until": None,
            }
        )
        legacy = client.post(
            "/v1/client/login",
            json={"email": "mixed@case.test", "password": "LegacyPass1!"},
        )
        assert legacy.json()["success"] is True, legacy.text
    finally:
        tenant_registry.set_snapshot(tenant_registry.build_snapshot([]))
