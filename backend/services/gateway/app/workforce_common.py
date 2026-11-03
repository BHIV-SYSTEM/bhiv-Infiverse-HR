"""Shared workforce runtime utilities: scope, audit, and enums."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.control_center_governance import resolve_policy_scope
from app.lineage_envelope import LineageEnvelope

WORKFORCE_TYPES = frozenset(
    {"contractor", "employee", "consultant", "advisor", "intern", "vendor_workforce"}
)
LIFECYCLE_STATES = frozenset(
    {
        "draft",
        "onboarding",
        "active",
        "role_change_pending",
        "transfer_pending",
        "offboarding_prep",
        "inactive",
        "offboarded",
    }
)

WORKFORCE_AUDIT_EVENT = "workforce"
GOVERNANCE_AUDIT_EVENT = "governance"
POLICY_AUDIT_EVENT = "policy"
RUNTIME_AUDIT_EVENT_TYPES = ("control_center", "governance", "policy", "workforce")


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def doc_id_filter(entity_id: str) -> Dict[str, Any]:
    if ObjectId.is_valid(entity_id):
        return {"_id": ObjectId(entity_id)}
    return {"id": entity_id}


def workforce_scope_filter(scope: Dict[str, Any]) -> Dict[str, Any]:
    """Tenant isolation filter for workforce documents.

    * platform scope          -> no filter (everything)
    * key account (hierarchy) -> its own id plus every tenant/client id in
      its subtree (``visible_tenant_ids``)
    * single tenant           -> exactly ``{"tenant_id": <id>}``, identical to
      the pre-tenancy filter when no hierarchy is configured
    """
    if scope.get("scope") == "platform":
        return {}
    visible = [str(v) for v in (scope.get("visible_tenant_ids") or []) if str(v)]
    if visible:
        unique = list(dict.fromkeys(visible))
        if len(unique) == 1:
            return {"tenant_id": unique[0]}
        return {"tenant_id": {"$in": unique}}
    tenant_id = scope.get("tenant_id") or scope.get("user_id")
    return {"tenant_id": str(tenant_id) if tenant_id else "__none__"}


def assert_workforce_access(auth: Dict[str, Any]) -> Dict[str, Any]:
    scope = resolve_policy_scope(auth)
    if auth.get("type") == "api_key":
        return scope
    if scope["role"] not in ("client", "recruiter", "admin"):
        raise HTTPException(status_code=403, detail="Workforce APIs require client, recruiter, or admin role")
    return scope


async def write_workforce_audit(
    db: AsyncIOMotorDatabase,
    *,
    action: str,
    outcome: str,
    scope: Dict[str, Any],
    correlation_id: Optional[str] = None,
    detail: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    actor: Optional[Dict[str, Any]] = None,
    lineage: Optional[LineageEnvelope] = None,
    event_type: str = WORKFORCE_AUDIT_EVENT,
) -> None:
    envelope = lineage or LineageEnvelope.from_request(
        correlation_id=correlation_id,
        visibility_scope=scope.get("scope", "tenant"),
    )
    ctx = dict(context or {})
    ctx["lineage"] = envelope.to_dict()
    ctx["policy_scope"] = scope.get("scope")
    ctx["scope_label"] = scope.get("scope_label")
    await db.audit_logs.insert_one(
        {
            "event_type": event_type,
            "action": action,
            "outcome": outcome,
            "detail": detail,
            "correlation_id": envelope.correlation_id,
            "trace_id": envelope.trace_id,
            "context": ctx,
            "actor": actor or {"user_id": scope.get("user_id"), "role": scope.get("role")},
            "created_at": datetime.now(timezone.utc),
        }
    )


def serialize_doc(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not doc:
        return None
    out = dict(doc)
    if "_id" in out:
        out["id"] = str(out["_id"])
        del out["_id"]
    for key in ("created_at", "updated_at", "timestamp"):
        if key in out and hasattr(out[key], "isoformat"):
            out[key] = out[key].isoformat()
    return out


def tenant_id_for_scope(scope: Dict[str, Any]) -> str:
    if scope.get("scope") == "platform":
        return scope.get("tenant_id") or "platform"
    return str(scope.get("tenant_id") or scope.get("user_id") or "unknown")


def compute_inherited_roles(
    employee_role: str,
    department_roles: Optional[List[str]] = None,
    org_roles: Optional[List[str]] = None,
) -> List[str]:
    chain: List[str] = []
    if org_roles:
        chain.extend(org_roles)
    if department_roles:
        chain.extend(department_roles)
    if employee_role and employee_role not in chain:
        chain.append(employee_role)
    seen: List[str] = []
    for role in chain:
        if role and role not in seen:
            seen.append(role)
    return seen
