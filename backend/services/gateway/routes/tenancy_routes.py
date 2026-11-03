"""Multi-tenant hierarchy API (platform -> key account -> account).

Provides:
* ``POST /v1/admin/login``            - platform admin sign-in (the admin link)
* ``/v1/admin/tenants*``              - platform admin tenant hierarchy CRUD
* ``GET  /v1/tenant/info``            - a client's own tenant placement
* ``GET/POST /v1/tenant/sub-clients`` - key-account (e.g. SVA) management of
  the clients inside its own tenanted subset

Isolation guarantees:
* Admin endpoints require role ``admin`` (platform scope).
* Key-account endpoints require the caller's linked tenant node to be a
  ``key_account``; a plain client or a sibling tenant gets 403, and mutations
  are always written under the caller's own subtree (parent = caller node).
* Linking an existing Client-Portal account to two tenants at once is
  rejected, so one account can never appear in two companies' subsets.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import tenant_registry
from app.database import get_mongo_db
from jwt_auth import get_admin_auth, get_auth

router = APIRouter(tags=["Tenancy"])

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_TTL_SECONDS = 86400  # 24h, same as client login
ADMIN_LOCK_THRESHOLD = 5
ADMIN_LOCK_MINUTES = 30


# ── helpers ───────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return slug


def _valid_tenant_id(tenant_id: str) -> bool:
    return bool(tenant_registry.TENANT_ID_PATTERN.match(tenant_id or ""))


def _unique_tenant_id(base: str, existing: set) -> str:
    root = _slugify(base)[:48].strip("-") or "tenant"
    candidate = root
    counter = 1
    while candidate in existing or not _valid_tenant_id(candidate):
        candidate = f"{root[:56]}-{counter}"
        counter += 1
        if counter > 500:  # pragma: no cover - pathological input
            candidate = f"{root[:50]}-{secrets.token_hex(3)}"
            break
    return candidate


async def _ensure_indexes(db) -> None:
    try:
        await db.tenants.create_index([("tenant_id", 1)], unique=True)
        await db.tenants.create_index([("client_ids", 1)])
        await db.platform_admins.create_index([("email", 1)], unique=True)
    except Exception as exc:  # indexes are an optimization, never a blocker
        import logging

        logging.getLogger(__name__).debug("[tenancy] index creation skipped: %s", exc)


async def _all_linked_client_ids(db) -> set:
    cursor = db.tenants.find({}, {"client_ids": 1})
    docs = await cursor.to_list(length=1000)
    linked: set = set()
    for doc in docs:
        raw = doc.get("client_ids") or []
        if isinstance(raw, str):
            raw = [raw]
        linked.update(str(c) for c in raw if c)
    return linked


async def _client_accounts_for(db, client_ids: List[str]) -> List[Dict[str, Any]]:
    if not client_ids:
        return []
    cursor = db.clients.find(
        {"client_id": {"$in": client_ids}},
        {"client_id": 1, "company_name": 1, "email": 1, "status": 1},
    )
    docs = await cursor.to_list(length=len(client_ids))
    return [
        {
            "client_id": str(d.get("client_id")),
            "company_name": str(d.get("company_name") or ""),
            "email": str(d.get("email") or ""),
            "status": str(d.get("status") or "active"),
        }
        for d in docs
    ]


async def _audit(
    db,
    *,
    action: str,
    outcome: str,
    auth: Dict[str, Any],
    detail: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        ctx = dict(context or {})
        ctx.setdefault("source_system", "gateway")
        ctx.setdefault("service", "tenancy")
        await db.audit_logs.insert_one(
            {
                "event_type": "control_center",
                "action": action,
                "outcome": outcome,
                "detail": detail,
                "correlation_id": str(uuid.uuid4()),
                "context": ctx,
                "actor": {
                    "user_id": auth.get("user_id"),
                    "role": auth.get("role"),
                    "auth_type": auth.get("type"),
                },
                "created_at": _now(),
            }
        )
    except Exception:  # audit must never break the mutation
        pass


async def _refresh_registry(db) -> None:
    """Eagerly reload the hierarchy after a mutation (no TTL wait)."""
    try:
        await tenant_registry.refresh(db)
    except Exception:
        tenant_registry.invalidate()


def _client_id_of(auth: Dict[str, Any]) -> str:
    return str(auth.get("user_id") or "").strip()


def _require_client_role(auth: Dict[str, Any]) -> str:
    if auth.get("type") != "jwt_token" or auth.get("role") != "client":
        raise HTTPException(status_code=403, detail="This endpoint is only available for clients")
    client_id = _client_id_of(auth)
    if not client_id:
        raise HTTPException(status_code=401, detail="Client not identified")
    return client_id


def _key_account_of(auth: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the caller's key-account node or raise 403."""
    client_id = _require_client_role(auth)
    node = tenant_registry.node_for_client(client_id)
    if node is None or not node.is_key_account:
        raise HTTPException(
            status_code=403,
            detail="Key account access required",
        )
    return {"client_id": client_id, "node": node}


# ── pydantic contracts ───────────────────────────────────────────────────────

class AdminLogin(BaseModel):
    email: str
    password: str


class ClientProvision(BaseModel):
    """Optionally provision a Client-Portal login for a new tenant."""

    email: str
    password: Optional[str] = None
    company_name: Optional[str] = None


class AdminTenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    level: str  # "key_account" | "account"
    tenant_id: Optional[str] = None
    parent_tenant_id: Optional[str] = None
    client_id: Optional[str] = None  # link an existing Client-Portal account
    provision_client: Optional[ClientProvision] = None


class AdminTenantUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    status: Optional[str] = None  # "active" | "suspended"


class LinkClient(BaseModel):
    client_id: str = Field(min_length=1, max_length=128)


class SubClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str
    password: Optional[str] = None
    company_name: Optional[str] = None


# ── platform admin: sign-in ──────────────────────────────────────────────────

@router.post("/v1/admin/login")
async def admin_login(body: AdminLogin):
    """Platform admin sign-in (the shared admin link lands on /admin/login).

    First successful login bootstraps the admin from PLATFORM_ADMIN_EMAIL /
    PLATFORM_ADMIN_PASSWORD when the collection is empty; later logins use the
    stored bcrypt hash. Failures mirror the client-login lockout policy.
    """
    try:
        email = str(body.email or "").strip().lower()
        if not email:
            return {"success": False, "error": "Email is required"}
        db = await get_mongo_db()
        await _ensure_indexes(db)

        admin = await db.platform_admins.find_one({"email": email})

        if not admin:
            env_email = (os.getenv("PLATFORM_ADMIN_EMAIL") or "").strip().lower()
            env_password = os.getenv("PLATFORM_ADMIN_PASSWORD") or ""
            bootstrapped = False
            email_ok = False
            password_ok = False
            if env_email and env_password:
                email_ok = secrets.compare_digest(email, env_email)
                password_ok = secrets.compare_digest(str(body.password or ""), env_password)
                if email_ok and password_ok:
                    admin = {
                        "email": email,
                        "password_hash": bcrypt.hashpw(
                            body.password.encode("utf-8"), bcrypt.gensalt()
                        ).decode("utf-8"),
                        "failed_login_attempts": 0,
                        "locked_until": None,
                        "created_at": _now(),
                        "last_login": None,
                    }
                    inserted = await db.platform_admins.insert_one(admin)
                    admin.setdefault("_id", getattr(inserted, "inserted_id", None))
                    bootstrapped = True
            if not bootstrapped:
                # Diagnostics without PII: booleans only, never the email/password.
                logger.warning(
                    "[tenancy] admin_login rejected (no stored admin): "
                    "env_configured=%s email_matches_env=%s password_matches_env=%s",
                    bool(env_email and env_password),
                    email_ok,
                    password_ok,
                )
                return {"success": False, "error": "Invalid credentials"}

        locked_until = admin.get("locked_until")
        if locked_until:
            if locked_until.tzinfo is None:
                locked_until = locked_until.replace(tzinfo=timezone.utc)
            if locked_until > _now():
                return {"success": False, "error": "Account temporarily locked"}

        stored_hash = str(admin.get("password_hash") or "")
        password_ok = bool(stored_hash) and bcrypt.checkpw(
            str(body.password or "").encode("utf-8"), stored_hash.encode("utf-8")
        )
        if not password_ok:
            attempts = int(admin.get("failed_login_attempts") or 0) + 1
            lock = None
            if attempts >= ADMIN_LOCK_THRESHOLD:
                lock = _now() + timedelta(minutes=ADMIN_LOCK_MINUTES)
            failed_lookup = {"_id": admin["_id"]} if admin.get("_id") else {"email": email}
            await db.platform_admins.update_one(
                failed_lookup,
                {"$set": {"failed_login_attempts": attempts, "locked_until": lock}},
            )
            return {"success": False, "error": "Invalid credentials"}

        jwt_secret = os.getenv("JWT_SECRET_KEY") or ""
        if not jwt_secret:
            return {
                "success": False,
                "error": "Gateway is not configured for admin sign-in (JWT_SECRET_KEY missing)",
            }

        admin_id = str(admin.get("_id") or admin.get("email"))
        token_payload = {
            "sub": admin_id,
            "user_id": admin_id,
            "admin_id": admin_id,
            "email": email,
            "role": "admin",
            "tenant_kind": "platform",
            "exp": int(_now().timestamp()) + DEFAULT_TOKEN_TTL_SECONDS,
        }
        access_token = jwt.encode(token_payload, jwt_secret, algorithm="HS256")

        admin_lookup = {"_id": admin["_id"]} if admin.get("_id") else {"email": email}
        await db.platform_admins.update_one(
            admin_lookup,
            {"$set": {"failed_login_attempts": 0, "locked_until": None, "last_login": _now()}},
        )
        await _audit(
            db,
            action="admin_login",
            outcome="success",
            auth={"user_id": admin_id, "role": "admin", "type": "jwt_token"},
            context={"email": email},
        )
        return {
            "success": True,
            "message": "Authentication successful",
            "role": "admin",
            "email": email,
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": DEFAULT_TOKEN_TTL_SECONDS,
        }
    except HTTPException:
        raise
    except Exception as exc:
        import logging

        logging.getLogger(__name__).exception("admin_login failed: %s", exc)
        return {"success": False, "error": "Authentication error"}


# ── platform admin: hierarchy management ─────────────────────────────────────

def _serialize_node(node: tenant_registry.TenantNode) -> Dict[str, Any]:
    return node.to_dict()


@router.get("/v1/admin/tenants")
async def admin_list_tenants(auth=Depends(get_admin_auth)):
    """Full hierarchy (platform -> key accounts -> accounts) with linked
    Client-Portal accounts. Platform scope: every tenant is visible."""
    db = await get_mongo_db()
    snapshot = await tenant_registry.refresh(db)
    linked_ids = sorted(snapshot.by_client.keys())
    accounts = await _client_accounts_for(db, linked_ids)
    accounts_by_client = {a["client_id"]: a for a in accounts}

    tenants: List[Dict[str, Any]] = []
    for node in snapshot.nodes.values():
        payload = _serialize_node(node)
        payload["client_accounts"] = [
            accounts_by_client.get(cid, {"client_id": cid})
            for cid in node.client_ids
        ]
        tenants.append(payload)
    tenants.sort(key=lambda n: (n["level"] != "key_account", n["tenant_id"]))

    return {
        "success": True,
        "tree": snapshot.to_tree(accounts_by_client),
        "tenants": tenants,
        "counts": {
            "key_accounts": sum(1 for n in tenants if n["level"] == "key_account"),
            "accounts": sum(1 for n in tenants if n["level"] == "account"),
            "linked_clients": len(linked_ids),
        },
    }


@router.get("/v1/admin/clients")
async def admin_list_clients(search: str = "", auth=Depends(get_admin_auth)):
    """Every Client-Portal account with its ``client_id`` and which tenant
    (if any) it is linked to. Platform admins discover IDs here instead of
    querying MongoDB. ``search`` filters case-insensitively over company
    name, email and client_id."""
    db = await get_mongo_db()
    snapshot = await tenant_registry.refresh(db)

    # client_id -> tenant_id from the in-memory hierarchy (no extra query)
    linked_tenant: Dict[str, str] = {}
    for node in snapshot.nodes.values():
        for cid in node.client_ids:
            linked_tenant.setdefault(str(cid), node.tenant_id)

    query: Dict[str, Any] = {}
    term = str(search or "").strip()
    if term:
        pattern = {"$regex": re.escape(term), "$options": "i"}
        query = {
            "$or": [
                {"company_name": pattern},
                {"email": pattern},
                {"client_id": pattern},
            ]
        }

    cursor = db.clients.find(
        query,
        {
            "client_id": 1,
            "company_name": 1,
            "email": 1,
            "status": 1,
            "client_code": 1,
            "created_at": 1,
        },
    ).sort("company_name", 1)
    docs = await cursor.to_list(length=1000)

    clients = [
        {
            "client_id": str(d.get("client_id") or ""),
            "company_name": str(d.get("company_name") or ""),
            "email": str(d.get("email") or ""),
            "status": str(d.get("status") or "active"),
            "client_code": str(d.get("client_code") or ""),
            "created_at": str(d.get("created_at") or "") if d.get("created_at") else None,
            "linked_tenant_id": linked_tenant.get(str(d.get("client_id") or "")),
        }
        for d in docs
        if str(d.get("client_id") or "").strip()
    ]
    return {"success": True, "count": len(clients), "clients": clients}


@router.post("/v1/admin/tenants", status_code=201)
async def admin_create_tenant(body: AdminTenantCreate, auth=Depends(get_admin_auth)):
    """Create a key account (parent = platform) or an account under a key
    account. Optionally link an existing Client-Portal account or provision a
    brand-new one in the same call."""
    level = str(body.level or "").strip()
    if level not in tenant_registry.TENANT_LEVELS:
        raise HTTPException(
            status_code=400,
            detail=f"level must be one of: {', '.join(sorted(tenant_registry.TENANT_LEVELS))}",
        )
    if body.client_id and body.provision_client:
        raise HTTPException(
            status_code=400,
            detail="Provide either client_id (existing account) or provision_client (new account), not both",
        )

    db = await get_mongo_db()
    await _ensure_indexes(db)
    snapshot = await tenant_registry.refresh(db)
    existing_ids = set(snapshot.nodes.keys())

    if level == "key_account":
        parent = tenant_registry.PLATFORM_ROOT
    else:
        parent = str(body.parent_tenant_id or "").strip()
        if not parent or parent == tenant_registry.PLATFORM_ROOT:
            raise HTTPException(
                status_code=400,
                detail="Accounts can only be created directly under a key account",
            )
        parent_node = snapshot.node(parent)
        if parent_node is None:
            raise HTTPException(status_code=404, detail="Parent tenant not found")
        if not parent_node.is_key_account:
            raise HTTPException(
                status_code=400,
                detail="Accounts can only be created directly under a key account",
            )
        parent = parent_node.tenant_id

    tenant_id = str(body.tenant_id or "").strip().lower()
    if tenant_id:
        if not _valid_tenant_id(tenant_id):
            raise HTTPException(
                status_code=400,
                detail="tenant_id must be 2-64 chars: lowercase letters, digits, '-' or '_'",
            )
        if tenant_id in existing_ids or tenant_id == tenant_registry.PLATFORM_ROOT:
            raise HTTPException(status_code=409, detail="tenant_id already exists")
    else:
        tenant_id = _unique_tenant_id(body.name, existing_ids)

    # Optional Client-Portal linkage / provisioning
    client_ids: List[str] = []
    provisioned: Optional[Dict[str, Any]] = None
    if body.client_id:
        cid = str(body.client_id).strip()
        client = await db.clients.find_one({"client_id": cid})
        if not client and cid.isdigit():
            client = await db.clients.find_one({"client_id": int(cid)})
        if not client:
            raise HTTPException(
                status_code=404,
                detail="Client-Portal account not found (share its client_id or provision a new one)",
            )
        holder = await db.tenants.find_one({"client_ids": cid, "tenant_id": {"$ne": tenant_id}})
        if holder:
            raise HTTPException(
                status_code=409,
                detail=f"Client-Portal account is already linked to tenant '{holder.get('tenant_id')}'",
            )
        client_ids = [cid]
    elif body.provision_client:
        provisioned = await _provision_client_portal(db, body.provision_client, body.name)
        client_ids = [provisioned["client_id"]]

    path = (
        f"/{tenant_registry.PLATFORM_ROOT}/{tenant_id}/"
        if level == "key_account"
        else f"{snapshot.node(parent).path}{tenant_id}/"
    )
    doc = {
        "tenant_id": tenant_id,
        "name": str(body.name).strip(),
        "level": level,
        "parent_tenant_id": parent,
        "path": path,
        "client_ids": client_ids,
        "status": "active",
        "created_at": _now(),
        "updated_at": _now(),
    }
    try:
        await db.tenants.insert_one(doc)
    except Exception as exc:
        if "duplicate" in str(exc).lower():
            raise HTTPException(status_code=409, detail="tenant_id already exists")
        raise
    await _refresh_registry(db)
    await _audit(
        db,
        action="tenant_create",
        outcome="success",
        auth=auth,
        context={"tenant_id": tenant_id, "level": level, "parent_tenant_id": parent},
    )
    return {
        "success": True,
        "tenant": {k: v for k, v in doc.items() if k != "_id"},
        "client_account": provisioned,
    }


async def _provision_client_portal(db, spec: ClientProvision, fallback_name: str) -> Dict[str, Any]:
    """Create a Client-Portal login (clients collection) for a tenant node."""
    email = str(spec.email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email is required to provision a client")
    existing = await db.clients.find_one({"email": email})
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    password = str(spec.password or "")
    generated = False
    if not password:
        password = secrets.token_urlsafe(12)
        generated = True
    elif len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    company_name = str(spec.company_name or fallback_name or "").strip()
    if not company_name:
        raise HTTPException(status_code=400, detail="Company name is required")

    base = _slugify(email.split("@")[0]) or "client"
    client_id = f"{base}_{secrets.token_hex(3)}"
    client_code = str(uuid.uuid4())
    connection_id = str(uuid.uuid4().hex[:24])

    await db.clients.insert_one(
        {
            "client_id": client_id,
            "company_name": company_name,
            "email": email,
            "client_code": client_code,
            "connection_id": connection_id,
            "password_hash": bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8"),
            "status": "active",
            "failed_login_attempts": 0,
            "locked_until": None,
            "created_at": _now(),
        }
    )
    return {
        "client_id": client_id,
        "client_code": client_code,
        "connection_id": connection_id,
        "email": email,
        "company_name": company_name,
        "password": password,
        "generated_password": generated,
    }


@router.patch("/v1/admin/tenants/{tenant_id}")
async def admin_update_tenant(tenant_id: str, body: AdminTenantUpdate, auth=Depends(get_admin_auth)):
    db = await get_mongo_db()
    updates: Dict[str, Any] = {}
    if body.name is not None:
        updates["name"] = body.name.strip()
    if body.status is not None:
        if body.status not in tenant_registry.TENANT_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"status must be one of: {', '.join(sorted(tenant_registry.TENANT_STATUSES))}",
            )
        updates["status"] = body.status
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update")
    updates["updated_at"] = _now()

    result = await db.tenants.update_one({"tenant_id": tenant_id}, {"$set": updates})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Tenant not found")
    await _refresh_registry(db)
    await _audit(
        db,
        action="tenant_update",
        outcome="success",
        auth=auth,
        context={"tenant_id": tenant_id, "updates": {k: str(v) for k, v in updates.items()}},
    )
    return {"success": True, "tenant_id": tenant_id, "updates": {k: str(v) for k, v in updates.items()}}


@router.post("/v1/admin/tenants/{tenant_id}/link-client")
async def admin_link_client(tenant_id: str, body: LinkClient, auth=Depends(get_admin_auth)):
    """Attach an existing Client-Portal account (e.g. SVA's shared CP details)
    to a tenant node so it joins that company's subset."""
    db = await get_mongo_db()
    await _ensure_indexes(db)
    cid = str(body.client_id).strip()
    client = await db.clients.find_one({"client_id": cid})
    if not client and cid.isdigit():
        client = await db.clients.find_one({"client_id": int(cid)})
    if not client:
        raise HTTPException(status_code=404, detail="Client-Portal account not found")

    holder = await db.tenants.find_one({"client_ids": cid})
    if holder and str(holder.get("tenant_id")) != tenant_id:
        raise HTTPException(
            status_code=409,
            detail=f"Already linked to tenant '{holder.get('tenant_id')}'",
        )

    result = await db.tenants.update_one(
        {"tenant_id": tenant_id},
        {"$addToSet": {"client_ids": cid}, "$set": {"updated_at": _now()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Tenant not found")
    await _refresh_registry(db)
    await _audit(
        db,
        action="tenant_link_client",
        outcome="success",
        auth=auth,
        context={"tenant_id": tenant_id, "client_id": cid},
    )
    return {"success": True, "tenant_id": tenant_id, "client_id": cid}


@router.post("/v1/admin/tenants/{tenant_id}/unlink-client")
async def admin_unlink_client(tenant_id: str, body: LinkClient, auth=Depends(get_admin_auth)):
    db = await get_mongo_db()
    cid = str(body.client_id).strip()
    result = await db.tenants.update_one(
        {"tenant_id": tenant_id},
        {"$pull": {"client_ids": cid}, "$set": {"updated_at": _now()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Tenant not found")
    await _refresh_registry(db)
    await _audit(
        db,
        action="tenant_unlink_client",
        outcome="success",
        auth=auth,
        context={"tenant_id": tenant_id, "client_id": cid},
    )
    return {"success": True, "tenant_id": tenant_id, "client_id": cid}


# ── key account (e.g. SVA): its own tenanted subset ──────────────────────────

@router.get("/v1/tenant/info")
async def tenant_info(auth=Depends(get_auth)):
    """Where the authenticated caller sits in the hierarchy. Clients without a
    linked node get ``linked: false`` and unchanged single-tenant behaviour."""
    if auth.get("type") == "api_key" or auth.get("role") == "admin":
        return {
            "success": True,
            "scope": "platform",
            "linked": False,
            "level": "platform",
            "is_key_account": False,
        }
    client_id = _require_client_role(auth)
    node = tenant_registry.node_for_client(client_id)
    if node is None:
        return {
            "success": True,
            "scope": "client",
            "linked": False,
            "client_id": client_id,
            "level": None,
            "is_key_account": False,
            "visible_client_ids": [client_id],
        }
    snapshot = tenant_registry.get_snapshot()
    return {
        "success": True,
        "scope": "client",
        "linked": True,
        "client_id": client_id,
        "tenant_id": node.tenant_id,
        "name": node.name,
        "level": node.level,
        "parent_tenant_id": node.parent_tenant_id,
        "path": node.path,
        "status": node.status,
        "is_key_account": node.is_key_account,
        "client_ids": list(node.client_ids),
        "visible_client_ids": snapshot.visible_client_ids(client_id),
        "sub_client_count": len(snapshot.children.get(node.tenant_id, ())),
    }


@router.get("/v1/tenant/sub-clients")
async def list_sub_clients(auth=Depends(get_auth)):
    """Direct children (the key account's tenanted clients). Key accounts only."""
    caller = _key_account_of(auth)
    snapshot = tenant_registry.get_snapshot()
    db = await get_mongo_db()
    child_ids = list(snapshot.children.get(caller["node"].tenant_id, ()))
    children = [snapshot.node(cid) for cid in child_ids]
    children = [c for c in children if c is not None]
    linked = [cid for c in children for cid in c.client_ids]
    accounts = await _client_accounts_for(db, linked)
    accounts_by_client = {a["client_id"]: a for a in accounts}

    return {
        "success": True,
        "tenant": {
            "tenant_id": caller["node"].tenant_id,
            "name": caller["node"].name,
            "path": caller["node"].path,
        },
        "sub_clients": [
            {
                **c.to_dict(),
                "client_accounts": [
                    accounts_by_client.get(cid, {"client_id": cid}) for cid in c.client_ids
                ],
            }
            for c in children
        ],
        "count": len(children),
        "visible_client_ids": snapshot.visible_client_ids(caller["client_id"]),
    }


@router.post("/v1/tenant/sub-clients", status_code=201)
async def create_sub_client(body: SubClientCreate, auth=Depends(get_auth)):
    """Create a client inside the caller's own subset: tenant node + Client
    Portal login. Parent is always the caller's key-account node, so a tenant
    can never be created outside its own boundary."""
    caller = _key_account_of(auth)
    parent_node = caller["node"]
    db = await get_mongo_db()
    await _ensure_indexes(db)

    snapshot = await tenant_registry.refresh(db)
    existing_ids = set(snapshot.nodes.keys())

    email = str(body.email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email is required")
    if await db.clients.find_one({"email": email}):
        raise HTTPException(status_code=409, detail="Email already registered")

    provisioned = await _provision_client_portal(
        db,
        ClientProvision(email=email, password=body.password, company_name=body.company_name),
        body.name,
    )
    client_id = provisioned["client_id"]
    tenant_id = _unique_tenant_id(body.name, existing_ids)
    if tenant_id in existing_ids or await db.tenants.find_one({"tenant_id": tenant_id}):
        tenant_id = _unique_tenant_id(f"{tenant_id}-{secrets.token_hex(2)}", existing_ids)

    doc = {
        "tenant_id": tenant_id,
        "name": str(body.name).strip(),
        "level": "account",
        "parent_tenant_id": parent_node.tenant_id,
        "path": f"{parent_node.path}{tenant_id}/",
        "client_ids": [client_id],
        "status": "active",
        "created_at": _now(),
        "updated_at": _now(),
    }
    await db.tenants.insert_one(doc)
    await _refresh_registry(db)
    await _audit(
        db,
        action="tenant_create_sub_client",
        outcome="success",
        auth=auth,
        context={
            "tenant_id": tenant_id,
            "parent_tenant_id": parent_node.tenant_id,
            "client_id": client_id,
        },
    )
    return {
        "success": True,
        "tenant": {k: v for k, v in doc.items() if k != "_id"},
        "client_account": provisioned,
        "message": "Sub-client created. Share the Client-Portal credentials with them.",
    }
