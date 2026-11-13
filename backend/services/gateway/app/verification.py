"""
Automated identity/KYC document verification — honest, machine-readable rules.

Flow:
  1. Candidate uploads a KYC document (PDF / JPEG / PNG, ≤ 5 MB)
     ``POST /v1/verification/kyc/document``.
  2. The server extracts text (PyPDF2 — PDFs only; no OCR, no government
     database calls) and compares it against the candidate's profile name:
       * VERIFIED       — text extracted AND the profile name appears in it
       * REJECTED       — hard rule violation: unparsable/encrypted/wrong
                          magic PDF, or text present but the name does not
                          match (someone else's document)
       * UNVERIFIABLE   — honestly cannot be confirmed automatically:
                          scanned/image-only PDFs, image uploads (no OCR
                          engine), or no profile name to compare against.
                          Never silently passed, never faked.
  3. The verdict is stored on the candidate document as ``kyc`` and served
     back by ``GET /v1/verification/kyc/status``.

Honesty rules (mirrors the NDA gate):
* ``VERIFIED`` is only ever claimed with text evidence + a name match.
* Every verdict carries machine-readable ``reasons``.
* Missing PyPDF2 is a server misconfiguration → 503, never a fake verdict.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from jwt_auth import get_auth

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KYC_STATUS_NOT_SUBMITTED = "NOT_SUBMITTED"
KYC_STATUS_VERIFIED = "VERIFIED"
KYC_STATUS_REJECTED = "REJECTED"
KYC_STATUS_UNVERIFIABLE = "UNVERIFIABLE"

KYC_MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
KYC_ALLOWED_CONTENT_TYPES = {"application/pdf", "image/jpeg", "image/png"}

router = APIRouter(prefix="/verification", tags=["Identity Verification"])


class KycVerificationUnavailable(Exception):
    """Raised when PyPDF2 is missing — callers must surface 503, not a verdict."""


# ---------------------------------------------------------------------------
# Pure verification function (unit-testable, no I/O)
# ---------------------------------------------------------------------------


def _normalize_name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def verify_kyc_document(
    content: bytes,
    *,
    content_type: str,
    candidate_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Return ``{result, reasons, method, checked_at}`` — never raises for
    document problems (only ``KycVerificationUnavailable`` for server gaps)."""
    reasons = []
    checked_at = datetime.now(timezone.utc).isoformat()

    def verdict(result: str, method: str = "text-name-match") -> Dict[str, Any]:
        return {
            "result": result,
            "reasons": reasons,
            "method": method,
            "checked_at": checked_at,
        }

    ctype = (content_type or "").strip().lower()

    if not content:
        reasons.append("empty_file")
        return verdict(KYC_STATUS_REJECTED)
    if len(content) > KYC_MAX_UPLOAD_BYTES:
        reasons.append("file_too_large")
        return verdict(KYC_STATUS_REJECTED)
    if ctype not in KYC_ALLOWED_CONTENT_TYPES:
        reasons.append(f"unsupported_content_type:{ctype or 'unknown'}")
        return verdict(KYC_STATUS_REJECTED)

    # Image uploads: accepted and stored, but there is NO OCR engine here —
    # say so honestly instead of guessing.
    if ctype in ("image/jpeg", "image/png"):
        reasons.append("image_not_text_verifiable_no_ocr")
        return verdict(KYC_STATUS_UNVERIFIABLE, method="no-ocr-image")

    # PDF path
    if not content.lstrip()[:5].upper().startswith(b"%PDF"):
        reasons.append("not_a_pdf_magic_header")
        return verdict(KYC_STATUS_REJECTED)

    try:
        from PyPDF2 import PdfReader
    except ImportError as exc:
        raise KycVerificationUnavailable(
            "PyPDF2 is not installed — KYC auto-verification unavailable"
        ) from exc

    try:
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted:
            reasons.append("pdf_encrypted")
            return verdict(KYC_STATUS_REJECTED)
        pages = len(reader.pages)
        if pages < 1:
            reasons.append("no_pages")
            return verdict(KYC_STATUS_REJECTED)
        text = ""
        for page in reader.pages:
            try:
                text += page.extract_text() or ""
            except Exception:
                continue
        text = " ".join(text.split())
    except Exception as exc:
        reasons.append(f"pdf_parse_error:{type(exc).__name__}")
        return verdict(KYC_STATUS_REJECTED)

    reasons.append(f"pages:{pages}")

    if not text.strip():
        # Scanned/image-only PDF: cannot be confirmed automatically.
        reasons.append("no_extractable_text_scanned_or_image_pdf")
        return verdict(KYC_STATUS_UNVERIFIABLE, method="no-ocr-image")
    reasons.append(f"text_chars:{len(text)}")

    norm_name = _normalize_name(candidate_name)
    if not norm_name:
        # Text exists but there is nobody to compare it against.
        reasons.append("candidate_name_unavailable")
        return verdict(KYC_STATUS_UNVERIFIABLE)
    if len(norm_name) < 3:
        # A 1–2 char name would "match" almost any document — do not pretend.
        reasons.append("candidate_name_too_short_for_matching")
        return verdict(KYC_STATUS_UNVERIFIABLE)

    if norm_name in _normalize_name(text):
        reasons.append("name_match:True")
        return verdict(KYC_STATUS_VERIFIED)

    reasons.append("name_match:False")
    reasons.append("name_mismatch")
    return verdict(KYC_STATUS_REJECTED)


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


async def _find_candidate_doc(db, candidate_id: str) -> Optional[Dict[str, Any]]:
    from bson import ObjectId

    try:
        if ObjectId.is_valid(candidate_id):
            doc = await db.candidates.find_one({"_id": ObjectId(candidate_id)})
            if doc:
                return doc
    except Exception:
        pass
    return await db.candidates.find_one({"id": candidate_id})


# ---------------------------------------------------------------------------
# Routes (candidate JWT only)
# ---------------------------------------------------------------------------


def _candidate_identity(auth: Dict[str, Any]) -> str:
    role = str(auth.get("role") or "").strip().lower()
    auth_type = str(auth.get("type") or "").strip()
    if role != "candidate" and auth_type != "candidate_token":
        raise HTTPException(status_code=403, detail="Candidates only")
    user_id = str(
        auth.get("user_id") or auth.get("sub") or auth.get("candidate_id") or ""
    ).strip()
    if not user_id:
        raise HTTPException(status_code=403, detail="No candidate identity in token")
    return user_id


@router.post("/kyc/document", summary="Upload KYC document (automated text + name check)")
async def upload_kyc_document(
    file: UploadFile = File(...),
    auth: dict = Depends(get_auth),
):
    from app.database import get_mongo_db

    candidate_id = _candidate_identity(auth)

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(content) > KYC_MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File is too large. Max 5 MB.")
    content_type = (file.content_type or "").strip().lower()
    if content_type not in KYC_ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="KYC must be a PDF, JPEG or PNG file.")

    db = await get_mongo_db()

    # Identity for the name check: candidates collection is authoritative,
    # token name is the fallback (same pattern as the NDA gate).
    profile_name = None
    try:
        from bson import ObjectId

        cdoc = None
        if ObjectId.is_valid(candidate_id):
            cdoc = await db.candidates.find_one({"_id": ObjectId(candidate_id)})
        if not cdoc:
            cdoc = await db.candidates.find_one({"id": candidate_id})
        if cdoc:
            profile_name = cdoc.get("name") or profile_name
    except Exception:
        pass
    profile_name = profile_name or auth.get("name")

    try:
        verification = verify_kyc_document(
            content, content_type=content_type, candidate_name=profile_name
        )
    except KycVerificationUnavailable as exc:
        # Missing dependency = 503 misconfiguration, never a document verdict.
        raise HTTPException(status_code=503, detail=str(exc))

    now = datetime.now(timezone.utc)
    kyc = dict(verification)
    kyc["document"] = {
        "filename": file.filename or "kyc.pdf",
        "content_type": content_type,
        "size_bytes": len(content),
        "uploaded_at": now.isoformat(),
    }

    # Persist on the candidate document (authoritative store).
    from bson import ObjectId

    matched = 0
    if ObjectId.is_valid(candidate_id):
        result = await db.candidates.update_one(
            {"_id": ObjectId(candidate_id)}, {"$set": {"kyc": kyc}}
        )
        matched = getattr(result, "matched_count", 0) or 0
    if matched == 0:
        result = await db.candidates.update_one(
            {"id": candidate_id}, {"$set": {"kyc": kyc}}
        )
        matched = getattr(result, "matched_count", 0) or 0
    if matched == 0:
        raise HTTPException(
            status_code=404,
            detail="Candidate profile not found — complete your profile before KYC",
        )

    status = verification["result"]
    message = {
        KYC_STATUS_VERIFIED: "KYC document verified — text extracted and name matched.",
        KYC_STATUS_REJECTED: "KYC document rejected — see the machine-readable reasons.",
        KYC_STATUS_UNVERIFIABLE: (
            "Document stored — it cannot be confirmed automatically "
            "(no extractable text / no OCR)."
        ),
    }.get(status, "Document stored.")
    return {
        "ok": True,
        "candidate_id": candidate_id,
        "status": status,
        "verification": verification,
        "document": kyc["document"],
        "message": message,
    }


@router.get("/kyc/status", summary="My KYC verification status")
async def kyc_status(auth: dict = Depends(get_auth)):
    from app.database import get_mongo_db

    candidate_id = _candidate_identity(auth)
    db = await get_mongo_db()
    cdoc = await _find_candidate_doc(db, candidate_id)
    if not cdoc:
        raise HTTPException(
            status_code=404,
            detail="Candidate profile not found — complete your profile before KYC",
        )
    kyc = cdoc.get("kyc") or {}
    if not kyc:
        return {
            "candidate_id": candidate_id,
            "status": KYC_STATUS_NOT_SUBMITTED,
            "verification": None,
            "document": None,
            "updated_at": None,
        }
    return {
        "candidate_id": candidate_id,
        "status": str(kyc.get("result") or KYC_STATUS_NOT_SUBMITTED),
        "verification": {
            "result": kyc.get("result"),
            "reasons": kyc.get("reasons") or [],
            "method": kyc.get("method"),
            "checked_at": kyc.get("checked_at"),
        },
        "document": kyc.get("document"),
        "updated_at": kyc.get("checked_at"),
    }
