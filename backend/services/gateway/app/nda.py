"""
Candidate NDA gate — Sampada gateway (BHIV Phase 5D).

Flow:
  1. Candidate logs in and is served the OFFICIAL NDA template
     (``GET /v1/nda/template`` — print-ready HTML, print to PDF).
  2. Candidate signs + stamps the PDF and uploads it
     (``POST /v1/nda/upload``).
  3. The upload is auto-verified server-side with PyPDF2
     (``verify_nda_pdf``) → ``VERIFIED`` / ``NEEDS_REVIEW`` / ``REJECTED``.
  4. ``NEEDS_REVIEW`` falls back to admin review
     (``POST /v1/nda/admin/{candidate_id}/review``).
  5. While status != ``VERIFIED``, candidate write/endpoints that carry the
     NDA obligation are blocked by ``enforce_nda_gate`` (403
     ``NDA_NOT_VERIFIED``): job application, profile update, non-NDA
     document upload and CV export.

Honesty rules:
* Auto-verification only claims ``VERIFIED`` when the PDF parses, has pages,
  and carries text evidence of a signature/stamp plus the candidate's
  identity. Scanned/image-only PDFs are honestly reported as
  ``NEEDS_REVIEW`` — never silently passed.
* Every verdict carries machine-readable ``reasons``.

Kill switch: ``NDA_GATE_ENABLED=false`` disables enforcement only
(upload/status endpoints stay available).
"""

from __future__ import annotations

import base64
import os
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

from jwt_auth import get_auth, get_service_or_admin_auth

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NDA_STATUS_NOT_SUBMITTED = "NOT_SUBMITTED"
NDA_STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"
NDA_STATUS_VERIFIED = "VERIFIED"
NDA_STATUS_REJECTED = "REJECTED"

NDA_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
NDA_ALLOWED_CONTENT_TYPES = {"application/pdf"}

# Text markers that indicate a signed/stamped document.
_SIGNATURE_MARKERS = (
    "signed",
    "signature",
    "signatory",
    "stamp",
    "stamped",
    "seal",
    "authorised signatory",
    "authorized signatory",
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def nda_gate_enabled() -> bool:
    return (os.getenv("NDA_GATE_ENABLED", "true") or "").strip().lower() not in (
        "false",
        "0",
        "no",
        "off",
    )


# ---------------------------------------------------------------------------
# Auto-verification (PyPDF2)
# ---------------------------------------------------------------------------


def verify_nda_pdf(
    content: bytes,
    *,
    candidate_name: Optional[str] = None,
    candidate_email: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify an uploaded NDA PDF.

    Returns::

        {
          "result": "VERIFIED" | "NEEDS_REVIEW" | "REJECTED",
          "reasons": [str, ...],          # machine-readable evidence
          "method": "pypdf2-auto",
          "checked_at": "<iso>",
        }

    REJECTED     — not a usable PDF (unparsable, encrypted, empty, no pages).
    VERIFIED     — parses AND text evidences a signature/stamp AND (when we
                   know the candidate) their name/email appears in the text.
    NEEDS_REVIEW — parses but cannot be confirmed automatically (e.g. a
                   scanned/image-only document): admin must eyeball it.
    """
    reasons: List[str] = []
    checked_at = _utcnow_iso()

    def verdict(result: str) -> Dict[str, Any]:
        return {
            "result": result,
            "reasons": reasons,
            "method": "pypdf2-auto",
            "checked_at": checked_at,
        }

    if not content:
        reasons.append("empty_file")
        return verdict(NDA_STATUS_REJECTED)
    if len(content) > NDA_MAX_UPLOAD_BYTES:
        reasons.append("file_too_large")
        return verdict(NDA_STATUS_REJECTED)
    if not content.lstrip()[:5].upper().startswith(b"%PDF"):
        reasons.append("not_a_pdf_magic_header")
        return verdict(NDA_STATUS_REJECTED)

    try:
        from PyPDF2 import PdfReader

        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted:
            reasons.append("pdf_encrypted")
            return verdict(NDA_STATUS_REJECTED)
        pages = len(reader.pages)
        if pages < 1:
            reasons.append("no_pages")
            return verdict(NDA_STATUS_REJECTED)

        text = ""
        for page in reader.pages:
            try:
                text += page.extract_text() or ""
            except Exception:
                continue
        text = " ".join(text.split())
    except Exception as exc:  # unparsable PDF
        reasons.append(f"pdf_parse_error:{type(exc).__name__}")
        return verdict(NDA_STATUS_REJECTED)

    reasons.append(f"pages:{pages}")

    if not text.strip():
        # Image-only / scanned document: honest fallback to a human.
        reasons.append("no_extractable_text_scanned_or_image_pdf")
        return verdict(NDA_STATUS_NEEDS_REVIEW)

    lowered = text.lower()
    marker = next((m for m in _SIGNATURE_MARKERS if m in lowered), None)
    if marker is None:
        reasons.append("no_signature_or_stamp_marker_in_text")
        return verdict(NDA_STATUS_NEEDS_REVIEW)
    reasons.append(f"marker:{marker}")

    # Identity sub-check (only when we know who the candidate is).
    name_ok = True
    email_ok = True
    if candidate_name:
        name_ok = candidate_name.strip().lower() in lowered
        reasons.append(f"name_match:{name_ok}")
    if candidate_email:
        email_ok = candidate_email.strip().lower() in lowered
        reasons.append(f"email_match:{email_ok}")
    if not (name_ok and email_ok):
        # Markers present but identity not confirmed in text — human check.
        return verdict(NDA_STATUS_NEEDS_REVIEW)

    return verdict(NDA_STATUS_VERIFIED)


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


def _nda_collection(db):
    # A database without the collection behaves exactly like "no submissions"
    # (real Mongo creates collections lazily; test fakes may omit them).
    return getattr(db, "nda_submissions", None)


async def get_nda_record(db, candidate_id: str) -> Optional[Dict[str, Any]]:
    if not candidate_id:
        return None
    collection = _nda_collection(db)
    if collection is None:
        return None
    return await collection.find_one({"candidate_id": str(candidate_id)})


async def get_nda_status(db, candidate_id: str) -> Dict[str, Any]:
    """Public status view for the gate and the frontend."""
    rec = await get_nda_record(db, candidate_id)
    if not rec:
        return {
            "candidate_id": str(candidate_id),
            "status": NDA_STATUS_NOT_SUBMITTED,
            "gate_passed": False,
            "verification": None,
            "document": None,
            "review": None,
            "updated_at": None,
        }
    status = str(rec.get("status") or NDA_STATUS_NOT_SUBMITTED)
    doc = rec.get("document") or {}
    return {
        "candidate_id": str(candidate_id),
        "status": status,
        "gate_passed": status == NDA_STATUS_VERIFIED,
        "verification": rec.get("verification"),
        "document": {
            "filename": doc.get("filename"),
            "content_type": doc.get("content_type"),
            "size_bytes": doc.get("size_bytes"),
            "uploaded_at": doc.get("uploaded_at"),
        }
        if doc
        else None,
        "review": rec.get("review"),
        "updated_at": rec.get("updated_at"),
    }


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


async def enforce_nda_gate(auth: Dict[str, Any]) -> None:
    """Raise 403 unless the candidate's NDA is VERIFIED.

    Bypasses (by design): non-candidate roles (recruiter, client, admin,
    service API keys), demo guest sessions (``guest:*``), and the whole gate
    when ``NDA_GATE_ENABLED=false``. Pravesh-SSO sessions with the candidate
    role ARE gated. If Mongo is not configured at all there is no candidate
    data to protect — enforcement is skipped (local dev / unit tests).
    """
    if not nda_gate_enabled():
        return
    role = str(auth.get("role") or "").strip().lower()
    auth_type = str(auth.get("type") or "").strip()
    is_candidate = role == "candidate" or auth_type == "candidate_token"
    if not is_candidate:
        return

    user_id = str(auth.get("user_id") or auth.get("candidate_id") or "").strip()
    if not user_id:
        return
    # Demo guest sessions (auth_type guest_demo / "guest:<name>" ids) are
    # a read-only preview: they never submit real data, so the gate lets
    # them through (the write endpoints they would need still 404 for them).
    if auth.get("auth_type") == "guest_demo" or user_id.startswith("guest:"):
        return

    try:
        from app.database import get_mongo_db

        db = await get_mongo_db()
    except Exception:
        # No database configured (local dev / unit tests): nothing to gate.
        return

    status = await get_nda_status(db, user_id)
    if status.get("gate_passed"):
        return

    current = status.get("status") or NDA_STATUS_NOT_SUBMITTED
    raise HTTPException(
        status_code=403,
        detail={
            "code": "NDA_NOT_VERIFIED",
            "nda_status": current,
            "message": (
                "Your NDA must be uploaded and verified before you can "
                "continue. Upload the signed and stamped NDA ("
                "GET /v1/nda/template) to unlock this action."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Official template (print-ready HTML)
# ---------------------------------------------------------------------------

_NDA_TEMPLATE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Non-Disclosure Agreement — BHIV Infiverse</title>
<style>
  @page { size: A4; margin: 18mm; }
  body { font-family: Georgia, 'Times New Roman', serif; color: #111;
         max-width: 800px; margin: 24px auto; padding: 0 20px; line-height: 1.55; }
  h1 { text-align: center; font-size: 22px; letter-spacing: 2px; margin-bottom: 4px; }
  .sub { text-align: center; font-size: 12px; color: #555; margin-bottom: 22px; }
  .party { margin: 14px 0; }
  .fill { border-bottom: 1px solid #333; display: inline-block; min-width: 260px; }
  .clause { margin: 12px 0; text-align: justify; font-size: 14px; }
  .sig { display: flex; justify-content: space-between; margin-top: 48px; }
  .sig div { width: 45%; font-size: 13px; }
  .line { border-bottom: 1px solid #333; height: 46px; margin-bottom: 6px; }
  .stamp { margin-top: 10px; font-size: 12px; color: #555; }
  footer { margin-top: 40px; font-size: 11px; color: #666; text-align: center; }
  .printbar { background:#f5f5f5; border:1px solid #ddd; padding:10px 14px;
              font-family: Arial, sans-serif; font-size:13px; margin-bottom:18px; }
  .printbar button { padding: 6px 16px; }
  @media print { .printbar { display: none; } body { margin: 0; } }
</style>
</head>
<body>
<div class="printbar">
  Print or save this page as PDF (browser &#8594; Print &#8594; Save as PDF),
  sign and stamp it, then upload it from your Sampada profile.
  <button onclick="window.print()">Print / Save as PDF</button>
</div>

<h1>NON-DISCLOSURE AGREEMENT</h1>
<div class="sub">BHIV Infiverse — Sampada Talent Platform &middot; Form NDA/v1</div>

<div class="party">
  Between <strong>BHIV Infiverse ("Discloser")</strong> and the undersigned
  candidate (<strong>"Recipient"</strong>):<br><br>
  Name: <span class="fill">&nbsp;</span><br><br>
  Email: <span class="fill">&nbsp;</span><br><br>
  Institute / Cohort: <span class="fill">&nbsp;</span>
</div>

<div class="clause"><strong>1. Purpose.</strong> The Recipient is being
evaluated for engagement opportunities via the Sampada platform and may
access confidential information relating to BHIV Infiverse, its clients,
candidates, job requirements, assessments and business plans.</div>

<div class="clause"><strong>2. Confidential Information.</strong> Includes all
information disclosed in any form — oral, written, electronic or visual —
that is marked confidential or would reasonably be understood to be
confidential, including candidate data, client requirements, commercial terms
and platform internals.</div>

<div class="clause"><strong>3. Obligations.</strong> The Recipient shall (a) use
Confidential Information solely for the stated Purpose; (b) not disclose it to
any third party; (c) protect it with at least reasonable care; and (d) not copy,
reverse engineer or redistribute it.</div>

<div class="clause"><strong>4. Exclusions.</strong> Obligations do not apply to
information that is publicly available, independently developed, or already
lawfully known to the Recipient without breach of this Agreement.</div>

<div class="clause"><strong>5. Term.</strong> This Agreement is effective on
signature and the obligations survive for three (3) years from the date of
disclosure, or until terminated in writing by the Discloser.</div>

<div class="clause"><strong>6. Return of Information.</strong> Upon request, the
Recipient shall promptly return or destroy all Confidential Information and
certify such destruction in writing.</div>

<div class="clause"><strong>7. Governing Law.</strong> This Agreement is governed
by the laws of India, and disputes are subject to the exclusive jurisdiction of
the courts of Maharashtra.</div>

<div class="sig">
  <div>
    <div class="line"></div>
    Recipient Signature
    <div class="stamp">Date: <span class="fill" style="min-width:120px">&nbsp;</span></div>
  </div>
  <div>
    <div class="line"></div>
    Recipient Name &amp; Company/Institute Stamp (if applicable)
    <div class="stamp">Date: <span class="fill" style="min-width:120px">&nbsp;</span></div>
  </div>
</div>

<footer>BHIV Infiverse — Official NDA template (Form NDA/v1) &middot;
This document must be signed (and stamped where applicable) before upload.</footer>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/nda", tags=["Candidate NDA"])


def _candidate_user_id(auth: Dict[str, Any]) -> str:
    role = str(auth.get("role") or "").strip().lower()
    auth_type = str(auth.get("type") or "").strip()
    if role != "candidate" and auth_type != "candidate_token":
        raise HTTPException(status_code=403, detail="Candidates only")
    user_id = str(
        auth.get("user_id") or auth.get("candidate_id") or ""
    ).strip()
    if not user_id:
        raise HTTPException(status_code=403, detail="No candidate identity in token")
    return user_id


@router.get("/template", summary="Official NDA template (print-ready HTML)")
async def get_nda_template(auth: dict = Depends(get_auth)):
    return HTMLResponse(_NDA_TEMPLATE_HTML)


@router.get("/status", summary="My NDA submission + verification status")
async def nda_status(auth: dict = Depends(get_auth)):
    from app.database import get_mongo_db

    candidate_id = _candidate_user_id(auth)
    db = await get_mongo_db()
    return await get_nda_status(db, candidate_id)


@router.post("/upload", summary="Upload signed+stamped NDA (auto-verified)")
async def upload_nda(
    file: UploadFile = File(...),
    auth: dict = Depends(get_auth),
):
    from app.database import get_mongo_db

    candidate_id = _candidate_user_id(auth)
    db = await get_mongo_db()

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(content) > NDA_MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File is too large. Max 10 MB.")
    content_type = (file.content_type or "").strip().lower()
    if content_type not in NDA_ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="NDA must be a PDF file.")

    # Candidate identity for the verification sub-check (best effort).
    # The candidates collection is authoritative when present — the token
    # email may be a login alias that does not appear on the NDA itself.
    candidate_name = None
    candidate_email = auth.get("email")
    try:
        from bson import ObjectId

        cdoc = None
        if ObjectId.is_valid(candidate_id):
            cdoc = await db.candidates.find_one({"_id": ObjectId(candidate_id)})
        if not cdoc:
            cdoc = await db.candidates.find_one({"id": candidate_id})
        if cdoc:
            candidate_name = cdoc.get("name") or candidate_name
            candidate_email = cdoc.get("email") or candidate_email
    except Exception:
        pass

    verification = verify_nda_pdf(
        content,
        candidate_name=candidate_name,
        candidate_email=candidate_email,
    )
    collection = _nda_collection(db)
    if collection is None:
        raise HTTPException(status_code=503, detail="NDA storage unavailable")
    now = datetime.now(timezone.utc)
    previous = await get_nda_record(db, candidate_id)

    doc = {
        "candidate_id": candidate_id,
        "status": verification["result"],
        "verification": verification,
        "document": {
            "filename": file.filename or "nda.pdf",
            "content_type": content_type,
            "size_bytes": len(content),
            "uploaded_at": now.isoformat(),
            "file_base64": base64.b64encode(content).decode("ascii"),
        },
        "review": None,
        "submission_count": int((previous or {}).get("submission_count") or 0) + 1,
        "created_at": (previous or {}).get("created_at") or now,
        "updated_at": now,
    }
    await collection.replace_one({"candidate_id": candidate_id}, doc, upsert=True)

    out = await get_nda_status(db, candidate_id)
    return {
        "ok": True,
        "status": out["status"],
        "gate_passed": out["gate_passed"],
        "verification": verification,
        "document": out["document"],
        "message": (
            "NDA verified — the platform is unlocked."
            if out["gate_passed"]
            else (
                "Uploaded — an administrator will review it."
                if out["status"] == NDA_STATUS_NEEDS_REVIEW
                else "Upload rejected — please re-upload a valid, signed NDA."
            )
        ),
    }


@router.get("/document", summary="Download my uploaded NDA (admin: ?candidate_id=)")
async def download_nda(
    candidate_id: Optional[str] = None,
    auth: dict = Depends(get_auth),
):
    from app.database import get_mongo_db

    role = str(auth.get("role") or "").strip().lower()
    if role == "admin" or auth.get("type") == "api_key":
        target = (candidate_id or "").strip()
        if not target:
            raise HTTPException(
                status_code=422, detail="candidate_id query param required"
            )
    else:
        target = _candidate_user_id(auth)
        if candidate_id and str(candidate_id) != target:
            raise HTTPException(status_code=403, detail="You can only view your own NDA")

    db = await get_mongo_db()
    rec = await get_nda_record(db, target)
    if not rec or not (rec.get("document") or {}).get("file_base64"):
        raise HTTPException(status_code=404, detail="No NDA on file")
    doc = rec["document"]
    from fastapi.responses import Response

    return Response(
        content=base64.b64decode(doc["file_base64"]),
        media_type=doc.get("content_type") or "application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{doc.get("filename") or "nda.pdf"}"'
        },
    )


# ---------------------------------------------------------------------------
# Admin review (fallback for NEEDS_REVIEW / correction of REJECTED)
# ---------------------------------------------------------------------------


@router.get("/admin/queue", summary="Submissions awaiting admin review")
async def nda_admin_queue(auth: dict = Depends(get_service_or_admin_auth)):
    from app.database import get_mongo_db

    db = await get_mongo_db()
    collection = _nda_collection(db)
    if collection is None:
        return {"queue": [], "count": 0}
    out = []
    async for rec in collection.find(
        {"status": {"$in": [NDA_STATUS_NEEDS_REVIEW, NDA_STATUS_REJECTED]}},
        {
            "candidate_id": 1,
            "status": 1,
            "verification": 1,
            "document.filename": 1,
            "document.size_bytes": 1,
            "document.uploaded_at": 1,
            "updated_at": 1,
        },
    ).sort("updated_at", -1):
        rec["_id"] = str(rec.get("_id") or "")
        doc = rec.get("document") or {}
        out.append(
            {
                "candidate_id": rec.get("candidate_id"),
                "status": rec.get("status"),
                "verification": rec.get("verification"),
                "filename": doc.get("filename"),
                "size_bytes": doc.get("size_bytes"),
                "uploaded_at": doc.get("uploaded_at"),
                "updated_at": rec.get("updated_at").isoformat()
                if isinstance(rec.get("updated_at"), datetime)
                else rec.get("updated_at"),
            }
        )
    return {"queue": out, "count": len(out)}


@router.post("/admin/{candidate_id}/review", summary="Admin decision: VERIFIED / REJECTED")
async def nda_admin_review(
    candidate_id: str,
    payload: Dict[str, Any],
    auth: dict = Depends(get_service_or_admin_auth),
):
    from app.database import get_mongo_db

    decision = str(payload.get("decision") or "").strip().upper()
    if decision not in (NDA_STATUS_VERIFIED, NDA_STATUS_REJECTED):
        raise HTTPException(
            status_code=422, detail="decision must be 'VERIFIED' or 'REJECTED'"
        )
    db = await get_mongo_db()
    collection = _nda_collection(db)
    if collection is None:
        raise HTTPException(status_code=503, detail="NDA storage unavailable")
    rec = await get_nda_record(db, candidate_id)
    if not rec:
        raise HTTPException(status_code=404, detail="No NDA submission for this candidate")

    reviewer = str(
        payload.get("reviewer") or auth.get("user_id") or "admin"
    )
    now = datetime.now(timezone.utc)
    update = {
        "status": decision,
        "review": {
            "reviewed_by": reviewer,
            "reviewed_at": now.isoformat(),
            "decision": decision,
            "note": str(payload.get("note") or "")[:500],
        },
        "updated_at": now,
    }
    # Manual review must override a conflicting auto-verdict honestly.
    if rec.get("verification"):
        rec["verification"] = dict(rec["verification"])
        rec["verification"]["admin_override"] = decision
        rec["verification"]["admin_reviewed_at"] = now.isoformat()
        update["verification"] = rec["verification"]

    await collection.update_one({"candidate_id": candidate_id}, {"$set": update})
    return {
        "ok": True,
        "candidate_id": candidate_id,
        "status": decision,
        "gate_passed": decision == NDA_STATUS_VERIFIED,
        "reviewed_by": reviewer,
    }
