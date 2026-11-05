"""SETU cross-system signal ingestion and trace continuity."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.lineage_envelope import LineageEnvelope, attach_lineage, is_uuid_v4
from app.workforce_common import GOVERNANCE_AUDIT_EVENT, new_correlation_id, serialize_doc, tenant_id_for_scope, write_workforce_audit

SIGNAL_TYPES = frozenset({"niyantran_telemetry", "artha_payroll_visibility", "crm_participation", "setu_aggregation"})
OWNERSHIP_BY_TYPE = {
    "niyantran_telemetry": "niyantran",
    "artha_payroll_visibility": "artha",
    "crm_participation": "crm",
    "setu_aggregation": "setu",
}


class SetuSignalIngest(BaseModel):
    signal_type: str = ""
    payload: Dict[str, Any] = Field(default_factory=dict)
    workforce_ref_id: Optional[str] = None
    source_declaration: str = ""
    origin_system: Optional[str] = None
    owning_system: Optional[str] = None
    trace_id: Optional[str] = None
    correlation_id: Optional[str] = None
    trust_classification: str = "observed"
    visibility_scope: str = "tenant"


async def ingest_setu_signal(db, body: SetuSignalIngest, scope, signal_type: Optional[str] = None):
    st = signal_type or body.signal_type
    if st not in SIGNAL_TYPES:
        raise HTTPException(status_code=422, detail=f"signal_type must be one of: {sorted(SIGNAL_TYPES)}")
    # Phase 5D (P0-3): a supplied trace_id must be a valid UUIDv4.
    if body.trace_id and not is_uuid_v4(body.trace_id):
        raise HTTPException(status_code=422, detail="trace_id must be a valid UUIDv4")
    cid = body.correlation_id or new_correlation_id()
    owner = body.owning_system or OWNERSHIP_BY_TYPE.get(st, "unknown")
    origin = body.origin_system or owner
    envelope = LineageEnvelope(
        origin_system=origin,
        owning_system=owner,
        trace_id=body.trace_id or cid,
        correlation_id=cid,
        trust_classification=body.trust_classification,
        visibility_scope=body.visibility_scope,
    )
    signal_id = f"sig-{uuid.uuid4().hex[:12]}"
    doc = attach_lineage(
        {
            "signal_id": signal_id,
            "signal_type": st,
            "payload": body.payload,
            "workforce_ref_id": body.workforce_ref_id,
            "source_declaration": body.source_declaration or f"{origin} participation",
            "tenant_id": tenant_id_for_scope(scope),
            "created_at": datetime.now(timezone.utc),
        },
        envelope,
    )
    await db.setu_signals.insert_one(doc)
    await write_workforce_audit(db, action=f"setu_signal_{st}", outcome="ingested", scope=scope, correlation_id=cid, context={"signal_id": signal_id}, event_type=GOVERNANCE_AUDIT_EVENT)
    return serialize_doc(doc)


async def list_setu_signals(db, scope, signal_type=None, correlation_id=None, limit=50):
    q: Dict[str, Any] = {}
    if scope.get("scope") != "platform":
        q["tenant_id"] = tenant_id_for_scope(scope)
    if signal_type:
        q["signal_type"] = signal_type
    if correlation_id:
        q["lineage.correlation_id"] = correlation_id
    docs = await db.setu_signals.find(q).sort("created_at", -1).limit(limit).to_list(limit)
    return [serialize_doc(d) for d in docs]


async def setu_trace_continuity(db, trace_id: str, limit=50):
    signals = await db.setu_signals.find({"lineage.trace_id": trace_id}).sort("created_at", 1).limit(limit).to_list(limit)
    audits = await db.audit_logs.find({"trace_id": trace_id}).sort("created_at", 1).limit(limit).to_list(limit)
    return {
        "trace_id": trace_id,
        "signal_count": len(signals),
        "audit_count": len(audits),
        "signals": [serialize_doc(s) for s in signals],
        "audit_events": [{"action": a.get("action"), "outcome": a.get("outcome"), "correlation_id": a.get("correlation_id")} for a in audits],
    }
