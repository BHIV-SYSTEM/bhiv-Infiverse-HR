"""
Contact verification for independent candidates — email OTP (honest statuses).

Flow (Workstream C replacement for document KYC on the Profile card):
  1. Eligibility: candidate is independent when they have no owning client
     (``_candidate_owner_client_ids`` empty — same rule as the NDA unowned
     path) AND no offer in ``accepted``/``hired``. Hired/placed candidates
     never see the card (``eligible: false``).
  2. ``POST /verification/contact/otp/send`` — 6-digit code, SHA-256 hashed
     on the candidate document, 10-minute expiry, rate-limited (1/60s,
     5/hour). Delivered via free Gmail SMTP (``GMAIL_EMAIL`` /
     ``GMAIL_APP_PASSWORD``). The code is never returned by the API.
  3. ``POST /verification/contact/otp/verify`` — on match sets
     ``contact_verification.status = CONTACT_VERIFIED`` with method
     ``email-otp``. Wrong/expired codes increment attempts (max 5).
  4. ``GET /verification/contact/status`` — eligibility + status +
     masked email only.

Honesty rules (mirrors NDA/KYC):
* ``CONTACT_VERIFIED`` only after a real OTP match against the profile email.
* Missing Gmail config → 503, never a silent fake success.
* Independent = unowned AND not hired/placed — never invented.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from jwt_auth import get_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/verification/contact", tags=["Contact Verification"])

STATUS_NOT_VERIFIED = "NOT_VERIFIED"
STATUS_CONTACT_VERIFIED = "CONTACT_VERIFIED"

OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_COOLDOWN_SECONDS = 60
OTP_MAX_SENDS_PER_HOUR = 5

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


# ---------------------------------------------------------------------------
# Helpers
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


async def _candidate_filter(candidate_id: str) -> Dict[str, Any]:
    from bson import ObjectId

    if ObjectId.is_valid(candidate_id):
        return {"_id": ObjectId(candidate_id)}
    return {"id": candidate_id}


async def _is_independent(db, candidate_id: str) -> bool:
    """Independent = no owning client AND no accepted/hired offer."""
    try:
        from app.nda import _candidate_owner_client_ids

        owners = await _candidate_owner_client_ids(db, candidate_id)
    except Exception as exc:  # pragma: no cover — ownership helper failure
        logger.warning("ownership lookup failed for %s: %s", candidate_id, exc)
        owners = ["unknown"]  # fail closed: treat as owned → hide card
    if owners:
        return False
    hired = await db.offers.count_documents(
        {"candidate_id": candidate_id, "status": {"$in": ["accepted", "hired"]}}
    )
    return hired == 0


def _mask_email(email: str) -> str:
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked_local = local[:1] + "*"
    else:
        masked_local = local[0] + "***" + local[-1]
    return f"{masked_local}@{domain}"


def _hash_otp(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _gmail_config() -> tuple[str, str]:
    email = (os.environ.get("GMAIL_EMAIL") or "").strip()
    password = (os.environ.get("GMAIL_APP_PASSWORD") or "").strip()
    return email, password


def send_otp_email(to_email: str, code: str) -> None:
    """Send OTP via free Gmail SMTP. Raises RuntimeError when unconfigured
    or the SMTP call fails — callers map that to 503, never a fake success."""
    gmail_email, gmail_password = _gmail_config()
    if (
        not gmail_email
        or not gmail_password
        or gmail_email.startswith("<")
        or gmail_password.startswith("<")
    ):
        raise RuntimeError(
            "Email is not configured — set GMAIL_EMAIL and GMAIL_APP_PASSWORD "
            "(Gmail App Password) in backend/.env"
        )
    if not _EMAIL_RE.match(to_email):
        raise RuntimeError("Invalid recipient email address")

    msg = MIMEText(
        "Your BHIV contact verification code is:\n\n"
        f"    {code}\n\n"
        f"This code expires in {OTP_TTL_MINUTES} minutes.\n"
        "If you did not request it, you can ignore this email.\n",
        "plain",
    )
    msg["Subject"] = f"BHIV contact verification code: {code}"
    msg["From"] = gmail_email
    msg["To"] = to_email
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(gmail_email, gmail_password)
        server.send_message(msg)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class OtpVerifyRequest(BaseModel):
    code: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/status", summary="Contact verification eligibility + status")
async def contact_status(auth: dict = Depends(get_auth)):
    from app.database import get_mongo_db

    candidate_id = _candidate_identity(auth)
    db = await get_mongo_db()
    cdoc = await _find_candidate_doc(db, candidate_id)
    if not cdoc:
        raise HTTPException(
            status_code=404,
            detail="Candidate profile not found — complete your profile first",
        )

    eligible = await _is_independent(db, candidate_id)
    if not eligible:
        return {
            "candidate_id": candidate_id,
            "eligible": False,
            "reason": "hired_or_placed",
            "status": None,
            "email_masked": None,
            "verified_at": None,
        }

    email = str(cdoc.get("email") or auth.get("email") or "").strip()
    cv = cdoc.get("contact_verification") or {}
    status = (
        STATUS_CONTACT_VERIFIED
        if cv.get("status") == STATUS_CONTACT_VERIFIED
        else STATUS_NOT_VERIFIED
    )
    return {
        "candidate_id": candidate_id,
        "eligible": True,
        "reason": None,
        "status": status,
        "email_masked": _mask_email(email) if email else None,
        "verified_at": cv.get("verified_at"),
        "method": cv.get("method"),
    }


@router.post("/otp/send", summary="Send contact-verification OTP to profile email")
async def send_otp(auth: dict = Depends(get_auth)):
    from app.database import get_mongo_db

    candidate_id = _candidate_identity(auth)
    db = await get_mongo_db()
    cdoc = await _find_candidate_doc(db, candidate_id)
    if not cdoc:
        raise HTTPException(
            status_code=404,
            detail="Candidate profile not found — complete your profile first",
        )

    if not await _is_independent(db, candidate_id):
        raise HTTPException(
            status_code=403,
            detail="Contact verification is only for independent candidates",
        )

    email = str(cdoc.get("email") or "").strip()
    if not email or not _EMAIL_RE.match(email):
        raise HTTPException(
            status_code=400,
            detail="No valid email on your profile — add one before verifying contact",
        )

    # Already verified → idempotent success, no new code.
    cv = cdoc.get("contact_verification") or {}
    if cv.get("status") == STATUS_CONTACT_VERIFIED:
        return {
            "ok": True,
            "already_verified": True,
            "email_masked": _mask_email(email),
            "expires_in": 0,
            "message": "Contact already verified.",
        }

    now = _now()
    otp_doc = cdoc.get("contact_otp") or {}
    last_sent_raw = otp_doc.get("last_sent_at")
    if last_sent_raw:
        try:
            last_sent = datetime.fromisoformat(str(last_sent_raw))
            if last_sent.tzinfo is None:
                last_sent = last_sent.replace(tzinfo=timezone.utc)
            elapsed = (now - last_sent).total_seconds()
            if elapsed < OTP_RESEND_COOLDOWN_SECONDS:
                wait = int(OTP_RESEND_COOLDOWN_SECONDS - elapsed) + 1
                raise HTTPException(
                    status_code=429,
                    detail=f"Please wait {wait}s before requesting another code.",
                )
            # Hourly cap: count sends by walking a simple counter window.
            sends_this_hour = int(otp_doc.get("sends_last_hour_count") or 0)
            hour_start_raw = otp_doc.get("sends_hour_started_at")
            hour_started = (
                datetime.fromisoformat(str(hour_start_raw))
                if hour_start_raw
                else None
            )
            if hour_started and hour_started.tzinfo is None:
                hour_started = hour_started.replace(tzinfo=timezone.utc)
            if not hour_started or (now - hour_started) > timedelta(hours=1):
                sends_this_hour = 0
            if sends_this_hour >= OTP_MAX_SENDS_PER_HOUR:
                raise HTTPException(
                    status_code=429,
                    detail="Hourly limit reached — try again later.",
                )
        except HTTPException:
            raise
        except Exception:
            pass  # corrupt timestamp → allow resend

    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = now + timedelta(minutes=OTP_TTL_MINUTES)
    otp_doc = cdoc.get("contact_otp") or {}
    sends_count = int(otp_doc.get("sends_last_hour_count") or 0)
    hour_start_raw = otp_doc.get("sends_hour_started_at")
    try:
        hour_started = (
            datetime.fromisoformat(str(hour_start_raw)) if hour_start_raw else None
        )
        if hour_started and hour_started.tzinfo is None:
            hour_started = hour_started.replace(tzinfo=timezone.utc)
    except Exception:
        hour_started = None
    if not hour_started or (now - hour_started) > timedelta(hours=1):
        hour_started = now
        sends_count = 0

    try:
        send_otp_email(email, code)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.error("OTP email failed for %s: %s", candidate_id, exc)
        raise HTTPException(
            status_code=503,
            detail="Could not send verification email — try again shortly.",
        )

    await db.candidates.update_one(
        await _candidate_filter(candidate_id),
        {
            "$set": {
                "contact_otp": {
                    "hash": _hash_otp(code),
                    "expires_at": expires_at.isoformat(),
                    "attempts": 0,
                    "last_sent_at": now.isoformat(),
                    "sends_last_hour_count": sends_count + 1,
                    "sends_hour_started_at": hour_started.isoformat(),
                }
            }
        },
    )
    return {
        "ok": True,
        "already_verified": False,
        "email_masked": _mask_email(email),
        "expires_in": OTP_TTL_MINUTES * 60,
        "message": f"Code sent to {_mask_email(email)}.",
    }


@router.post("/otp/verify", summary="Verify contact-verification OTP")
async def verify_otp(body: OtpVerifyRequest, auth: dict = Depends(get_auth)):
    from app.database import get_mongo_db

    candidate_id = _candidate_identity(auth)
    code = (body.code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise HTTPException(status_code=400, detail="Enter the 6-digit code.")

    db = await get_mongo_db()
    cdoc = await _find_candidate_doc(db, candidate_id)
    if not cdoc:
        raise HTTPException(
            status_code=404,
            detail="Candidate profile not found — complete your profile first",
        )
    if not await _is_independent(db, candidate_id):
        raise HTTPException(
            status_code=403,
            detail="Contact verification is only for independent candidates",
        )

    cv = cdoc.get("contact_verification") or {}
    if cv.get("status") == STATUS_CONTACT_VERIFIED:
        return {
            "ok": True,
            "status": STATUS_CONTACT_VERIFIED,
            "message": "Contact already verified.",
            "verified_at": cv.get("verified_at"),
        }

    otp_doc = cdoc.get("contact_otp") or {}
    stored_hash = str(otp_doc.get("hash") or "")
    if not stored_hash:
        raise HTTPException(
            status_code=400, detail="No pending code — request a new one first."
        )

    attempts = int(otp_doc.get("attempts") or 0)
    if attempts >= OTP_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail="Too many wrong attempts — request a new code.",
        )

    try:
        expires_at = datetime.fromisoformat(str(otp_doc.get("expires_at")))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except Exception:
        raise HTTPException(
            status_code=400, detail="Code expired — request a new one."
        )
    if _now() > expires_at:
        raise HTTPException(status_code=400, detail="Code expired — request a new one.")

    if not secrets.compare_digest(stored_hash, _hash_otp(code)):
        await db.candidates.update_one(
            await _candidate_filter(candidate_id),
            {"$inc": {"contact_otp.attempts": 1}},
        )
        remaining = max(0, OTP_MAX_ATTEMPTS - (attempts + 1))
        raise HTTPException(
            status_code=400,
            detail=f"Invalid code. {remaining} attempt(s) remaining.",
        )

    now = _now()
    email = str(cdoc.get("email") or "").strip()
    await db.candidates.update_one(
        await _candidate_filter(candidate_id),
        {
            "$set": {
                "contact_verification": {
                    "status": STATUS_CONTACT_VERIFIED,
                    "method": "email-otp",
                    "email": email,
                    "verified_at": now.isoformat(),
                }
            },
            "$unset": {"contact_otp": ""},
        },
    )
    return {
        "ok": True,
        "status": STATUS_CONTACT_VERIFIED,
        "message": "Contact verified — your email was confirmed.",
        "verified_at": now.isoformat(),
    }
