"""
Identity routes — Abhiruchi intake, canonical-id lookup, enrolment publishing.

Mounted by the gateway at ``/api/v1`` (see app/main.py).

Auth model:
* ``/identity/abhiruchi/intake`` — service-to-service (API key), because only
  the intake system performs handoffs.
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

from jwt_auth import get_api_key, get_auth  # noqa: E402

from app.identity.models import (  # noqa: E402
    INSTITUTE_CODES,
    AbhiruchiIntakeRequest,
    EnrolmentPublishRequest,
    IntakeResponse,
)
from app.identity import service  # noqa: E402

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
    auth: dict = Depends(get_auth),
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
    record = await store.get_by_local_id(body.sampada_local_id)
    if record is None:
        record = await store.get_by_canonical_id(body.sampada_local_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Identity record not found")

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
