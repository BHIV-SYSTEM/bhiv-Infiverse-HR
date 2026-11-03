"""In-process tenant hierarchy registry (account isolation layer).

Hierarchy (Sampada multi-tenancy):

    platform                      <- BHIV / Sampada platform admins (JWT role=admin)
      └── key_account             <- e.g. SVA (the HR company, our client)
            └── account           <- e.g. SVA's own clients (their tenanted subset)

Each tenant node may link zero or more Client-Portal accounts (``clients``
documents) through ``client_ids``.  Every existing data document is already
keyed by ``client_id`` / ``tenant_id`` = client id, so visibility is expressed
as the *set of client ids inside a caller's subtree*:

* platform admin  -> no filter (everything)
* key account     -> own client ids + every linked client id in its subtree
* account         -> only its own client ids (never siblings, never other
                     key accounts)

The registry is a cached in-memory snapshot of the ``tenants`` collection.
It is refreshed by an HTTP middleware (TTL) and eagerly after any tenant
mutation, so scope resolution stays synchronous everywhere it is used.

If the registry is empty/unavailable (no database, no tenants configured)
every helper degrades to the pre-tenancy behaviour: a caller sees exactly
its own client id.  Existing deployments are therefore unaffected.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

PLATFORM_ROOT = "platform"
DEFAULT_TTL_SECONDS = 30.0
REFRESH_TIMEOUT_SECONDS = 3.0

TENANT_LEVELS = frozenset({"key_account", "account"})
TENANT_STATUSES = frozenset({"active", "suspended"})
TENANT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")


@dataclass(frozen=True)
class TenantNode:
    """One node of the tenant hierarchy."""

    tenant_id: str
    name: str
    level: str  # "key_account" | "account"
    parent_tenant_id: str
    path: str
    client_ids: Tuple[str, ...] = ()
    status: str = "active"
    created_at: Any = None

    @property
    def is_key_account(self) -> bool:
        return self.level == "key_account"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "name": self.name,
            "level": self.level,
            "parent_tenant_id": self.parent_tenant_id,
            "path": self.path,
            "client_ids": list(self.client_ids),
            "status": self.status,
            "created_at": self.created_at.isoformat() if hasattr(self.created_at, "isoformat") else self.created_at,
        }


@dataclass(frozen=True)
class TenantSnapshot:
    """Immutable view of the whole hierarchy, safe to read without locks."""

    nodes: Dict[str, TenantNode] = field(default_factory=dict)
    children: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    by_client: Dict[str, str] = field(default_factory=dict)
    loaded_at: float = 0.0

    @property
    def is_empty(self) -> bool:
        return not self.nodes

    def node(self, tenant_id: Optional[str]) -> Optional[TenantNode]:
        if not tenant_id:
            return None
        return self.nodes.get(str(tenant_id))

    def node_for_client(self, client_id: Optional[str]) -> Optional[TenantNode]:
        """Return the tenant node a Client-Portal account belongs to (if any)."""
        if not client_id:
            return None
        tenant_id = self.by_client.get(str(client_id).strip())
        return self.nodes.get(tenant_id) if tenant_id else None

    def subtree(self, tenant_id: Optional[str]) -> List[TenantNode]:
        """All nodes at or below ``tenant_id`` (depth-first, self first)."""
        root = self.node(tenant_id)
        if root is None:
            return []
        out: List[TenantNode] = []
        stack: List[str] = [root.tenant_id]
        seen = set()
        while stack:
            current = stack.pop(0)
            if current in seen:
                continue
            seen.add(current)
            node = self.nodes.get(current)
            if node is None:
                continue
            out.append(node)
            stack.extend(self.children.get(current, ()))
        return out

    def visible_client_ids(self, client_id: Optional[str]) -> List[str]:
        """Client-Portal ids a caller may read: self first, then its subtree.

        Always contains at least ``client_id`` itself, which makes this a
        drop-in superset of the pre-tenancy single-tenant behaviour.
        """
        cid = str(client_id).strip() if client_id else ""
        if not cid:
            return []
        node = self.node_for_client(cid)
        if node is None:
            return [cid]
        ids: List[str] = [cid]
        for member in self.subtree(node.tenant_id):
            for linked in member.client_ids:
                if linked and linked not in ids:
                    ids.append(linked)
        return ids

    def visible_tenant_ids(self, client_id: Optional[str]) -> List[str]:
        """Ids accepted in a document's ``tenant_id`` field for this caller.

        Workforce documents are written with ``tenant_id`` = the caller's
        client id (see ``tenant_id_for_scope``), so the readable set is the
        subtree's linked client ids — identical content to
        :meth:`visible_client_ids`, named for the query domain it serves.
        """
        return self.visible_client_ids(client_id)

    def to_tree(
        self, client_accounts: Optional[Dict[str, Dict[str, Any]]] = None
    ) -> List[Dict[str, Any]]:
        """Nested tree starting at the platform root (for admin UIs).

        ``client_accounts`` optionally maps client_id -> account summary so the
        tree can show company names/emails instead of bare ids.
        """

        def build(tenant_id: str) -> Optional[Dict[str, Any]]:
            node = self.nodes.get(tenant_id)
            if node is None:
                return None
            payload = node.to_dict()
            if client_accounts is not None:
                payload["client_accounts"] = [
                    client_accounts.get(cid, {"client_id": cid}) for cid in node.client_ids
                ]
            payload["children"] = [
                child for child in (build(cid) for cid in self.children.get(tenant_id, ())) if child
            ]
            return payload

        roots = [
            child
            for child in (build(cid) for cid in self.children.get(PLATFORM_ROOT, ()))
            if child
        ]
        return roots


# ── module-level snapshot store ───────────────────────────────────────────────

_EMPTY = TenantSnapshot()
_snapshot: TenantSnapshot = _EMPTY


def get_snapshot() -> TenantSnapshot:
    """Current snapshot (never None; empty when tenancy is not configured)."""
    return _snapshot


def set_snapshot(snapshot: TenantSnapshot) -> None:
    """Install a snapshot (used by refresh and by tests)."""
    global _snapshot
    _snapshot = snapshot


def invalidate() -> None:
    """Mark the snapshot stale so the next request refreshes it."""
    global _snapshot
    _snapshot = TenantSnapshot(
        nodes=_snapshot.nodes,
        children=_snapshot.children,
        by_client=_snapshot.by_client,
        loaded_at=0.0,
    )


def build_snapshot(docs: Iterable[Dict[str, Any]]) -> TenantSnapshot:
    """Pure builder: MongoDB tenant documents -> validated snapshot.

    Invalid documents are skipped (logged) so one bad record can never break
    auth scope resolution for the whole gateway.
    """
    nodes: Dict[str, TenantNode] = {}
    children: Dict[str, List[str]] = {}
    by_client: Dict[str, str] = {}

    for doc in docs:
        try:
            tenant_id = str(doc.get("tenant_id") or "").strip()
            level = str(doc.get("level") or "").strip()
            if not tenant_id or level not in TENANT_LEVELS:
                logger.warning("[tenancy] skipping invalid tenant document: %r", doc.get("tenant_id"))
                continue
            parent = str(doc.get("parent_tenant_id") or PLATFORM_ROOT).strip() or PLATFORM_ROOT
            raw_ids = doc.get("client_ids") or []
            if isinstance(raw_ids, str):
                raw_ids = [raw_ids]
            client_ids = tuple(str(c).strip() for c in raw_ids if str(c).strip())
            node = TenantNode(
                tenant_id=tenant_id,
                name=str(doc.get("name") or tenant_id),
                level=level,
                parent_tenant_id=parent,
                path=str(doc.get("path") or f"/{PLATFORM_ROOT}/{tenant_id}/"),
                client_ids=client_ids,
                status=str(doc.get("status") or "active"),
                created_at=doc.get("created_at"),
            )
            nodes[tenant_id] = node
            children.setdefault(parent, []).append(tenant_id)
            for cid in client_ids:
                # First link wins; tenancy_routes prevents double-linking.
                by_client.setdefault(cid, tenant_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[tenancy] failed to load tenant document: %s", exc)

    return TenantSnapshot(
        nodes=nodes,
        children={k: tuple(v) for k, v in children.items()},
        by_client=by_client,
        loaded_at=time.time(),
    )


async def refresh(db: Any = None) -> TenantSnapshot:
    """Rebuild the snapshot from MongoDB and swap it in atomically."""
    if db is None:
        from app.database import get_mongo_db  # lazy: avoids import cycles

        db = await get_mongo_db()
    cursor = db.tenants.find({})
    docs = await cursor.to_list(length=1000)
    snapshot = build_snapshot(docs)
    set_snapshot(snapshot)
    logger.info(
        "[tenancy] registry refreshed: %d tenant(s), %d linked client(s)",
        len(snapshot.nodes),
        len(snapshot.by_client),
    )
    return snapshot


async def ensure_loaded(ttl_seconds: float = DEFAULT_TTL_SECONDS) -> TenantSnapshot:
    """Refresh the snapshot when empty or stale; failures never break requests.

    A failed refresh keeps the previous snapshot and defers the next attempt
    for one TTL so a down database cannot add latency to every request.
    """
    global _snapshot
    current = _snapshot
    # loaded_at is bumped even for an empty hierarchy, so a deployment with no
    # tenants configured never re-queries Mongo on every request.
    if (time.time() - current.loaded_at) < ttl_seconds:
        return current
    try:
        return await asyncio.wait_for(refresh(), timeout=REFRESH_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.debug("[tenancy] registry refresh deferred/failed: %s", exc)
        _snapshot = TenantSnapshot(
            nodes=current.nodes,
            children=current.children,
            by_client=current.by_client,
            loaded_at=time.time(),
        )
        return _snapshot


# ── synchronous helpers used by scope resolution ─────────────────────────────


def visible_client_ids(client_id: Optional[str]) -> List[str]:
    """Sync visibility set for a Client-Portal account (self + subtree)."""
    return _snapshot.visible_client_ids(client_id)


def visible_tenant_ids(client_id: Optional[str]) -> List[str]:
    """Sync tenant-id visibility set for a Client-Portal account."""
    return _snapshot.visible_tenant_ids(client_id)


def node_for_client(client_id: Optional[str]) -> Optional[TenantNode]:
    """Sync lookup of the tenant node linked to a Client-Portal account."""
    return _snapshot.node_for_client(client_id)
