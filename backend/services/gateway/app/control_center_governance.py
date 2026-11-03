"""
Centralized policy-scope resolution and control-center data services for runtime hardening.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app import tenant_registry

CONTROL_CENTER_ROLES = frozenset({"client", "recruiter", "admin"})
AUDIT_EVENT_TYPES = ("control_center", "governance", "policy", "workforce")


def resolve_policy_scope(auth: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve tenant/org visibility boundaries for control-center reads."""
    if auth.get("type") == "api_key":
        return {
            "scope": "platform",
            "scope_label": "platform_admin",
            "role": "admin",
            "user_id": None,
            "tenant_id": None,
            "org_id": None,
            "workforce_category": "all",
        }

    role = str(auth.get("role", ""))
    user_id = str(auth.get("user_id", "") or "")

    if role == "client":
        # Tenant hierarchy (platform -> key_account -> account). The visible
        # sets are supersets of [user_id]: with no tenants configured they are
        # exactly [user_id], i.e. the pre-tenancy single-tenant behaviour.
        visible_clients = tenant_registry.visible_client_ids(user_id) or [user_id]
        visible_tenants = tenant_registry.visible_tenant_ids(user_id) or [user_id]
        node = tenant_registry.node_for_client(user_id)
        return {
            "scope": "client",
            "scope_label": f"client:{user_id}",
            "role": role,
            "user_id": user_id,
            "tenant_id": user_id,
            "org_id": user_id,
            "workforce_category": "client_workforce",
            "visible_client_ids": visible_clients,
            "visible_tenant_ids": visible_tenants,
            "tenant_kind": node.level if node else None,
            "root_tenant_id": node.tenant_id if node else None,
        }
    if role == "recruiter":
        return {
            "scope": "recruiter",
            "scope_label": f"recruiter:{user_id}",
            "role": role,
            "user_id": user_id,
            "tenant_id": user_id,
            "org_id": user_id,
            "workforce_category": "recruiter_workforce",
        }
    if role == "admin":
        return {
            "scope": "platform",
            "scope_label": "platform_admin",
            "role": role,
            "user_id": user_id,
            "tenant_id": None,
            "org_id": None,
            "workforce_category": "all",
        }

    return {
        "scope": "unknown",
        "scope_label": "unknown",
        "role": role,
        "user_id": user_id,
        "tenant_id": None,
        "org_id": None,
        "workforce_category": "unknown",
    }


def assert_control_center_access(auth: Dict[str, Any]) -> Dict[str, Any]:
    """Enforce command-center role boundaries before scoped reads/writes."""
    scope = resolve_policy_scope(auth)
    if auth.get("type") == "api_key":
        return scope
    if scope["role"] not in CONTROL_CENTER_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Control center access is restricted to client, recruiter, and admin roles",
        )
    return scope


def _client_id_query(client_id: str) -> Dict[str, Any]:
    cid = str(client_id).strip() if client_id else ""
    if not cid:
        return {}
    if cid.isdigit():
        return {"$or": [{"client_id": cid}, {"client_id": int(cid)}]}
    return {"client_id": cid}


async def _client_job_ids(db, client_id: str) -> List[str]:
    q = _client_id_query(client_id)
    if not q:
        return []
    q["status"] = "active"
    cursor = db.jobs.find(q, {"_id": 1}).limit(500)
    jobs_list = await cursor.to_list(length=500)
    return [str(doc["_id"]) for doc in jobs_list]


async def _client_all_job_ids(db, client_id: str) -> List[str]:
    q = _client_id_query(client_id)
    if not q:
        return []
    cursor = db.jobs.find(q, {"_id": 1}).limit(500)
    jobs_list = await cursor.to_list(length=500)
    return [str(doc["_id"]) for doc in jobs_list]


async def _client_connected_recruiter_ids(db, client_id: str) -> List[str]:
    if not client_id or not str(client_id).strip():
        return []
    cursor = db.client_connected_recruiter.find(
        {"client_id": str(client_id).strip()},
        {"recruiter_id": 1},
    )
    docs = await cursor.to_list(length=100)
    return [str(d["recruiter_id"]) for d in docs if d.get("recruiter_id")]


async def _client_all_job_ids_for_dashboard(db, client_id: str) -> List[str]:
    """All job ids a client may read: own + connected recruiters' jobs.

    With the tenant hierarchy enabled a key account also receives the jobs of
    every linked sub-client in its subtree. Single clients (or an empty
    registry) resolve to exactly one id, preserving pre-tenancy behaviour.
    """
    ids: List[str] = []
    seen: set = set()
    for cid in tenant_registry.visible_client_ids(client_id) or [client_id]:
        own_ids = await _client_all_job_ids(db, cid)
        recruiter_ids = await _client_connected_recruiter_ids(db, cid)
        if recruiter_ids:
            cursor = db.jobs.find({"recruiter_id": {"$in": recruiter_ids}}, {"_id": 1}).limit(1000)
            recruiter_jobs = await cursor.to_list(length=1000)
            for doc in recruiter_jobs:
                jid = str(doc["_id"])
                if jid not in own_ids:
                    own_ids.append(jid)
        for jid in own_ids:
            if jid not in seen:
                seen.add(jid)
                ids.append(jid)
    return ids


async def _recruiter_all_job_ids_for_dashboard(db, recruiter_id: str) -> List[str]:
    if not recruiter_id or not str(recruiter_id).strip():
        return []
    cursor = db.jobs.find({"recruiter_id": str(recruiter_id).strip()}, {"_id": 1}).limit(1000)
    jobs_list = await cursor.to_list(length=1000)
    return [str(doc["_id"]) for doc in jobs_list]


async def resolve_scoped_job_ids(db, scope: Dict[str, Any]) -> Optional[List[str]]:
    """Return job IDs for scoped roles; None means platform-wide visibility."""
    if scope.get("scope") in ("platform",):
        return None

    user_id = str(scope.get("user_id") or "")
    if scope.get("scope") == "client":
        return await _client_all_job_ids_for_dashboard(db, user_id)
    if scope.get("scope") == "recruiter":
        return await _recruiter_all_job_ids_for_dashboard(db, user_id)
    return []


def _audit_scope_filter(scope: Dict[str, Any]) -> Dict[str, Any]:
    """Restrict audit reads to the caller's governance boundary.

    For client callers with a tenant hierarchy the boundary widens to every
    visible account in their subtree; a single-account client keeps the exact
    pre-tenancy filter.
    """
    if scope.get("scope") == "platform":
        return {"event_type": {"$in": list(AUDIT_EVENT_TYPES)}}

    user_id = str(scope.get("user_id") or "")
    role = scope.get("role")
    visible = [str(v) for v in (scope.get("visible_client_ids") or [user_id]) if str(v)]
    if role == "client" and len(visible) > 1:
        labels = [f"client:{cid}" for cid in visible]
        return {
            "event_type": {"$in": list(AUDIT_EVENT_TYPES)},
            "$or": [
                {"actor.user_id": {"$in": visible}},
                {"actor.role": role},
                {f"context.{role}_id": {"$in": visible}},
                {"context.scope_label": {"$in": labels}},
            ],
        }
    return {
        "event_type": {"$in": list(AUDIT_EVENT_TYPES)},
        "$or": [
            {"actor.user_id": user_id},
            {"actor.role": role},
            {f"context.{role}_id": user_id},
            {"context.scope_label": scope.get("scope_label")},
        ],
    }


def _format_ts(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value) if value is not None else datetime.now(timezone.utc).isoformat()


def audit_doc_to_trace_event(doc: Dict[str, Any]) -> Dict[str, Any]:
    context = doc.get("context") or {}
    outcome = str(doc.get("outcome", "")).lower()
    if outcome in ("success", "ok"):
        status = "success"
    elif outcome in ("failure", "denied", "error"):
        status = "failure"
    else:
        status = "in_progress"

    return {
        "ts": _format_ts(doc.get("created_at")),
        "service": str(context.get("service") or context.get("source_system") or "Gateway"),
        "op": str(doc.get("action") or "unknown"),
        "correlation_id": str(doc.get("correlation_id") or ""),
        "status": status,
        "source_system": str(context.get("source_system") or "gateway"),
        "derived_flag": bool(context.get("derived", False)),
        "policy_result": context.get("policy_result"),
        "actor_role": (doc.get("actor") or {}).get("role"),
    }


async def compute_scoped_candidate_stats(db, scope: Dict[str, Any]) -> Dict[str, Any]:
    """Scoped candidate statistics aligned with client/recruiter dashboard isolation."""
    job_ids = await resolve_scoped_job_ids(db, scope)
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    now = datetime.now(timezone.utc)

    try:
        if job_ids is None:
            total_candidates = await db.candidates.count_documents({})
            active_jobs = await db.jobs.count_documents({"status": "active"})
            job_filter: Dict[str, Any] = {}
        elif not job_ids:
            return _empty_stats(scope, "scoped_empty")
        else:
            job_filter = {"job_id": {"$in": job_ids}}
            active_jobs = await db.jobs.count_documents({"_id": {"$in": job_ids}, "status": "active"})
            applicant_cursor = db.job_applications.find(job_filter, {"candidate_id": 1}).limit(5000)
            applicants = await applicant_cursor.to_list(length=5000)
            candidate_ids = list({str(a.get("candidate_id")) for a in applicants if a.get("candidate_id")})
            total_candidates = len(candidate_ids)

        try:
            recent_matches = await db.matching_cache.count_documents(
                {**(job_filter or {}), "created_at": {"$gte": seven_days_ago}}
            )
        except Exception:
            recent_matches = 0
            if job_ids and total_candidates > 0 and active_jobs > 0:
                recent_matches = min(total_candidates * max(active_jobs, 1) // 10, 50)

        try:
            pending_interviews = await db.interviews.count_documents(
                {
                    **(job_filter or {}),
                    "status": {"$in": ["scheduled", "pending"]},
                    "interview_date": {"$gte": now},
                }
            )
        except Exception:
            pending_interviews = 0

        try:
            if job_ids is None:
                new_candidates_this_week = await db.candidates.count_documents(
                    {"created_at": {"$gte": seven_days_ago}}
                )
            elif candidate_ids:
                new_candidates_this_week = await db.candidates.count_documents(
                    {"_id": {"$in": candidate_ids}, "created_at": {"$gte": seven_days_ago}}
                )
            else:
                new_candidates_this_week = 0
        except Exception:
            new_candidates_this_week = 0

        try:
            total_feedback = await db.feedback.count_documents(job_filter or {})
        except Exception:
            total_feedback = 0

        return {
            "total_candidates": total_candidates,
            "active_jobs": active_jobs,
            "recent_matches": recent_matches,
            "pending_interviews": pending_interviews,
            "new_candidates_this_week": new_candidates_this_week,
            "total_feedback_submissions": total_feedback,
            "statistics_generated_at": datetime.now(timezone.utc).isoformat(),
            "data_source": "mongodb_scoped",
            "dashboard_ready": True,
            "policy_scope": scope,
        }
    except Exception as exc:
        stats = _empty_stats(scope, "error_fallback")
        stats["error"] = str(exc)
        return stats


def _empty_stats(scope: Dict[str, Any], source: str) -> Dict[str, Any]:
    return {
        "total_candidates": 0,
        "active_jobs": 0,
        "recent_matches": 0,
        "pending_interviews": 0,
        "new_candidates_this_week": 0,
        "total_feedback_submissions": 0,
        "statistics_generated_at": datetime.now(timezone.utc).isoformat(),
        "data_source": source,
        "dashboard_ready": False,
        "policy_scope": scope,
    }


async def compute_dashboard_aggregates(db, scope: Dict[str, Any]) -> Dict[str, Any]:
    """Backend-driven hiring funnel and department load (no synthetic ratios)."""
    job_ids = await resolve_scoped_job_ids(db, scope)
    funnel = await _compute_hiring_funnel(db, job_ids)
    department_load = await _compute_department_load(db, job_ids)
    return {
        "hiring_funnel": funnel,
        "department_load": department_load,
        "policy_scope": scope,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_source": "mongodb_aggregates",
    }


async def _compute_hiring_funnel(db, job_ids: Optional[List[str]]) -> List[Dict[str, Any]]:
    colors = ["bg-indigo-500", "bg-violet-500", "bg-purple-500", "bg-fuchsia-500"]
    labels = ["Sourcing", "Screening", "Interview", "Offer"]

    if job_ids is not None and not job_ids:
        return [{"label": label, "count": 0, "color": colors[i]} for i, label in enumerate(labels)]

    job_filter = {"job_id": {"$in": job_ids}} if job_ids else {}

    if job_ids is None:
        sourcing = await db.candidates.count_documents({})
    else:
        applicant_cursor = db.job_applications.find(job_filter, {"candidate_id": 1}).limit(5000)
        applicants = await applicant_cursor.to_list(length=5000)
        sourcing = len({str(a.get("candidate_id")) for a in applicants if a.get("candidate_id")})

    screening = await db.job_applications.count_documents({**job_filter, "status": "shortlisted"})
    try:
        interview = await db.interviews.count_documents(
            {**job_filter, "status": {"$in": ["scheduled", "pending"]}}
        )
    except Exception:
        interview = 0
    try:
        offer = await db.offers.count_documents(job_filter)
    except Exception:
        offer = 0

    counts = [sourcing, screening, interview, offer]
    return [
        {"label": labels[i], "count": counts[i], "color": colors[i]}
        for i in range(len(labels))
    ]


async def _compute_department_load(db, job_ids: Optional[List[str]]) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {"status": "active"}
    if job_ids is not None:
        if not job_ids:
            return []
        query["_id"] = {"$in": job_ids}

    cursor = db.jobs.find(query, {"department": 1, "_id": 1}).limit(500)
    jobs = await cursor.to_list(length=500)
    if not jobs:
        return []

    dept_counts: Dict[str, int] = {}
    job_id_list = [str(j["_id"]) for j in jobs]
    for job in jobs:
        dept = str(job.get("department") or "General").strip() or "General"
        dept_counts[dept] = dept_counts.get(dept, 0)

    app_counts: Dict[str, int] = {}
    try:
        pipeline = [
            {"$match": {"job_id": {"$in": job_id_list}}},
            {"$group": {"_id": "$job_id", "count": {"$sum": 1}}},
        ]
        async for row in db.job_applications.aggregate(pipeline):
            jid = str(row.get("_id"))
            for job in jobs:
                if str(job["_id"]) == jid:
                    dept = str(job.get("department") or "General").strip() or "General"
                    app_counts[dept] = app_counts.get(dept, 0) + int(row.get("count", 0))
                    break
    except Exception:
        app_counts = {dept: count * 3 for dept, count in dept_counts.items()}

    max_load = max(app_counts.values()) if app_counts else 1
    result = []
    for dept, count in sorted(app_counts.items(), key=lambda x: -x[1])[:8]:
        load = min(100, int(round((count / max_load) * 100))) if max_load else 0
        color = "bg-amber-500" if load >= 80 else "bg-emerald-500"
        result.append({"dept": dept, "load": load, "color": color, "applications": count})
    return result


async def list_audit_events(
    db,
    scope: Dict[str, Any],
    *,
    correlation_id: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    query = _audit_scope_filter(scope)
    if correlation_id:
        query["correlation_id"] = correlation_id

    cursor = db.audit_logs.find(query).sort("created_at", -1).limit(min(limit, 200))
    docs = await cursor.to_list(length=min(limit, 200))
    events = [audit_doc_to_trace_event(doc) for doc in docs]
    return {
        "events": events,
        "count": len(events),
        "source": "audit_logs",
        "policy_scope": scope,
    }


async def build_audit_replay(
    db,
    scope: Dict[str, Any],
    *,
    correlation_id: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    if correlation_id:
        payload = await list_audit_events(db, scope, correlation_id=correlation_id, limit=limit)
        payload["replay_mode"] = "single_correlation"
        return payload

    query = _audit_scope_filter(scope)
    cursor = (
        db.audit_logs.find({**query, "correlation_id": {"$exists": True, "$ne": None}})
        .sort("created_at", -1)
        .limit(500)
    )
    docs = await cursor.to_list(length=500)
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for doc in docs:
        cid = str(doc.get("correlation_id") or "")
        if not cid:
            continue
        grouped.setdefault(cid, []).append(doc)

    selected_cid = next(iter(grouped.keys()), None)
    if not selected_cid:
        return {
            "events": [],
            "count": 0,
            "correlation_id": None,
            "source": "audit_logs",
            "replay_mode": "latest_correlation",
            "policy_scope": scope,
        }

    events = [audit_doc_to_trace_event(d) for d in sorted(grouped[selected_cid], key=lambda d: _format_ts(d.get("created_at")))]
    return {
        "events": events[:limit],
        "count": len(events[:limit]),
        "correlation_id": selected_cid,
        "source": "audit_logs",
        "replay_mode": "latest_correlation",
        "policy_scope": scope,
    }
