"""
Identity routes — Abhiruchi intake, canonical-id lookup, enrolment publishing.

Mounted by the gateway at ``/api/v1`` (see app/main.py).

Auth model:
* ``/identity/abhiruchi/intake`` — service-to-service (API key), because only
  the intake system performs handoffs.
* ``/identity/enrolment/publish`` — API key OR platform admin (Phase 5D P0-5);
  ordinary authenticated users can no longer trigger delivery.
* everything else — normal gateway auth (``get_auth``), i.e. an authenticated
  user or service. Reads are read-only.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

_gateway_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

from jwt_auth import get_api_key, get_auth, get_service_or_admin_auth  # noqa: E402

from app.identity.models import (  # noqa: E402
    CANONICAL_ID_QUARANTINED,
    INSTITUTE_CODES,
    AbhiruchiIntakeRequest,
    EnrolmentPublishRequest,
    IntakeResponse,
    QuarantineResolveRequest,
)
from app.identity import service  # noqa: E402
from app.lineage_envelope import is_uuid_v4  # noqa: E402

import httpx  # noqa: E402

router = APIRouter(prefix="/identity", tags=["Identity & Enrolment"])


async def _store():
    return await service.get_identity_store()


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------


@router.get("/institutes", summary="Agreed institute codes (MSSU is canonical for RTMSSU)")
async def list_institutes(auth: dict = Depends(get_auth)):
    return {
        "institute_codes": list(INSTITUTE_CODES),
        "aliases": {"RTMSSU": "MSSU"},
        "policy": "only these codes are valid; MSSU is canonical for RTMSSU",
    }


# ---------------------------------------------------------------------------
# Abhiruchi -> Sampada intake (idempotent)
# ---------------------------------------------------------------------------


@router.post(
    "/abhiruchi/intake",
    response_model=IntakeResponse,
    summary="Idempotent Abhiruchi candidate handoff",
)
async def abhiruchi_intake(
    body: AbhiruchiIntakeRequest,
    _api_key: str = Depends(get_api_key),
):
    """
    Accepts an Abhiruchi handoff. Replays/retries with the same
    ``(source_system, source_reference)`` reuse the existing record, so
    repeated provisioning never creates duplicate candidates.

    Canonical candidate ids are only recorded when Abhiruchi supplies them;
    Sampada never invents one (status stays UNKNOWN). Same-email records are
    flagged as possible duplicates and never merged.
    """
    # Some intake clients send the canonical id as `candidate_id`. Normalise
    # the alias here so the id is recorded either way (Sampada still never
    # invents one: an absent value stays UNKNOWN).
    if not body.canonical_candidate_id and body.candidate_id:
        body.canonical_candidate_id = body.candidate_id.strip() or None

    # Phase 5D (P0-3): a supplied trace_id must be a valid UUIDv4.
    if body.trace_id and not is_uuid_v4(body.trace_id):
        raise HTTPException(status_code=422, detail="trace_id must be a valid UUIDv4")

    try:
        result = await service.process_intake(body, await _store())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return IntakeResponse(**result)


# ---------------------------------------------------------------------------
# Canonical / local id lookup
# ---------------------------------------------------------------------------


@router.get("/candidate/{identifier}", summary="Identity record by local or canonical id")
async def get_candidate(
    identifier: str,
    auth: dict = Depends(get_auth),
    store=Depends(_store),
):
    record = await store.get_by_local_id(identifier)
    if record is None:
        record = await store.get_by_canonical_id(identifier)
    if record is None and "@" in identifier:
        matches = await store.find_by_email_institute(identifier, None)
        if matches:
            record = matches[0]
    if record is None:
        raise HTTPException(status_code=404, detail="Identity record not found")
    return {
        "record": record.model_dump(),
        "storage_mode": store.mode,
        "read_only": True,
    }


# ---------------------------------------------------------------------------
# Enrolment publishing (Sampada -> HackaVerse)
# ---------------------------------------------------------------------------


@router.post(
    "/enrolment/publish",
    summary="Build (and optionally deliver) the versioned HackaVerse enrolment contract",
)
async def publish_enrolment(
    body: EnrolmentPublishRequest,
    request: Request,
    # Phase 5D (P0-5): service-to-service (API key) OR platform admin only —
    # ordinary candidates/recruiters/clients can no longer trigger publish.
    auth: dict = Depends(get_service_or_admin_auth),
    store=Depends(_store),
):
    """
    Builds the agreed ``candidate.enrolment.v1`` envelope for one candidate.

    Delivery status is honest:
    * ``LIVE``     — actually POSTed to HackaVerse (``HACKAVERSE_ENROLMENT_URL``
      configured) and accepted (2xx).
    * ``SIMULATED``— envelope built but not delivered (no URL, or dry_run).
      SIMULATED is NEVER to be reported as LIVE.
    * ``FAILED``   — delivery attempted and rejected/errored.
    """
    # Phase 5D (P0-3): supplied trace_id must be UUIDv4; the gateway's own
    # X-Trace-Id (UUIDv4) is propagated outbound on delivery.
    if body.trace_id and not is_uuid_v4(body.trace_id):
        raise HTTPException(status_code=422, detail="trace_id must be a valid UUIDv4")
    gateway_trace_id = (getattr(request.state, "trace_id", "") or "").strip() or None

    record = await store.get_by_local_id(body.sampada_local_id)
    if record is None:
        record = await store.get_by_canonical_id(body.sampada_local_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Identity record not found")
    # Phase 5D (P1-6): quarantined identities never publish downstream.
    if record.canonical_id_status == CANONICAL_ID_QUARANTINED:
        raise HTTPException(
            status_code=409,
            detail=(
                "Identity is quarantined pending review (identity.ambiguous.v1) "
                "— enrolment publishing is blocked until an admin resolves it."
            ),
        )

    envelope = service.build_enrolment_envelope(
        record, trace_id=body.trace_id, cohort_id=body.cohort_id
    )
    payload = envelope.model_dump()

    delivery = "SIMULATED"
    hackaverse_status: Optional[int] = None
    detail: Optional[str] = None

    target = (os.getenv("HACKAVERSE_ENROLMENT_URL") or "").strip()
    if body.dry_run:
        detail = "dry_run=true — envelope built, nothing sent"
    elif not target:
        detail = "HACKAVERSE_ENROLMENT_URL not configured — envelope built, not delivered"
    else:
        api_key = (os.getenv("HACKAVERSE_ENROLMENT_API_KEY") or "").strip()
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        if gateway_trace_id:
            headers["X-Trace-Id"] = gateway_trace_id
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(target, json=payload, headers=headers)
            hackaverse_status = resp.status_code
            if 200 <= resp.status_code < 300:
                delivery = "LIVE"
                detail = f"accepted by HackaVerse ({resp.status_code})"
            else:
                delivery = "FAILED"
                detail = f"HackaVerse responded {resp.status_code}"
        except Exception as exc:
            delivery = "FAILED"
            detail = f"delivery error: {exc}"

    await store.save_enrolment(envelope.idempotency_key, payload)
    return {
        "delivery": delivery,
        "envelope": payload,
        "hackaverse_status": hackaverse_status,
        "detail": detail,
        "storage_mode": store.mode,
        "trace_id": envelope.trace_id,
    }


@router.get("/enrolment/{idempotency_key}", summary="Retrieve a stored enrolment envelope")
async def get_enrolment(
    idempotency_key: str,
    auth: dict = Depends(get_auth),
    store=Depends(_store),
):
    payload = await store.get_enrolment(idempotency_key)
    if payload is None:
        raise HTTPException(status_code=404, detail="Envelope not found")
    return {"envelope": payload, "storage_mode": store.mode}


# ---------------------------------------------------------------------------
# Identity quarantine (Phase 5D P1-6)
# ---------------------------------------------------------------------------


@router.get("/quarantine", summary="List identity quarantines (open by default)")
async def list_quarantines(
    status: Optional[str] = None,
    auth: dict = Depends(get_service_or_admin_auth),
    store=Depends(_store),
):
    """Service/admin view of ``identity_quarantine`` docs (default: open)."""
    docs = await store.list_quarantines(status or "open")
    return {"quarantines": docs, "storage_mode": store.mode, "read_only": True}


@router.post(
    "/quarantine/{quarantine_id}/resolve",
    summary="Resolve an identity quarantine (release or confirm)",
)
async def resolve_quarantine(
    quarantine_id: str,
    body: QuarantineResolveRequest,
    auth: dict = Depends(get_service_or_admin_auth),
    store=Depends(_store),
):
    """Admin resolution.

    * ``released``  — ambiguity resolved; restores the record's previous
      canonical-id status and closes the quarantine.
    * ``confirmed`` — ambiguity confirmed; the record STAYS quarantined
      for manual data remediation.
    """
    resolution = (body.resolution or "").strip().lower()
    if resolution not in ("released", "confirmed"):
        raise HTTPException(
            status_code=422,
            detail="resolution must be 'released' or 'confirmed'",
        )

    doc = await store.get_quarantine(quarantine_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Quarantine not found")
    if doc.get("status") != "open":
        raise HTTPException(status_code=409, detail="Quarantine is already resolved")

    local_id = (doc.get("record") or {}).get("sampada_local_id") or ""
    record = await store.get_by_local_id(local_id) if local_id else None

    if resolution == "released" and record is not None:
        previous = doc.get("previous_canonical_id_status") or "UNKNOWN"
        record.canonical_id_status = previous
        await store.save_record(record)

    doc["status"] = "closed"
    doc["resolution"] = resolution
    doc["resolved_by"] = body.resolved_by or str(auth.get("user_id") or "")
    doc["resolved_at"] = service.utc_now_iso()
    if body.note:
        doc["resolution_note"] = str(body.note)[:500]
    await store.save_quarantine(doc)

    return {
        "ok": True,
        "quarantine_id": quarantine_id,
        "resolution": resolution,
        "record_status": record.canonical_id_status if record else None,
        "storage_mode": store.mode,
    }
