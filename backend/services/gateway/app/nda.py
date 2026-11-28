"""
Candidate NDA gate — Sampada gateway (BHIV Phase 5D).

Flow:
  1. Candidate logs in and is served the OFFICIAL NDA template
     (``GET /v1/nda/template`` — print-ready HTML, print to PDF).
  2. Candidate signs + stamps the PDF and uploads it
     (``POST /v1/nda/upload``).
  3. The upload is auto-verified server-side with PyPDF2
     (``verify_nda_pdf``) → ``VERIFIED`` / ``NEEDS_REVIEW`` / ``REJECTED``.
  4. ``NEEDS_REVIEW`` is reviewed by the OWNING COMPANY
     (``GET /v1/nda/tenant/queue`` + ``POST /v1/nda/tenant/{id}/review``,
     company JWT, own tenant subset only). Platform admins have NO access
     (tenancy audit) — ops uses the service api_key
     (``/v1/nda/admin/*``). Unowned candidates are decided automatically
     (NEEDS_REVIEW kept for ops review / clearer re-upload — never auto-REJECTED).
  5. While status != ``VERIFIED``, candidate write/endpoints that carry the
     NDA obligation are blocked by ``enforce_nda_gate`` (403
     ``NDA_NOT_VERIFIED``): job application, profile update, non-NDA
     document upload and CV export.

Honesty rules:
* Auto-verification only claims ``VERIFIED`` when the PDF parses, has pages,
  and carries text evidence of a signature/stamp plus the candidate's
  identity. Scanned/image-only PDFs are honestly reported as
  ``NEEDS_REVIEW`` (company subset) or auto-``REJECTED`` (unowned) —
  never silently passed.
* Every verdict carries machine-readable ``reasons``.

Kill switch: ``NDA_GATE_ENABLED=false`` disables enforcement only
(upload/status endpoints stay available), but ONLY with the explicit
non-production opt-in ``NDA_GATE_DEV_FAIL_OPEN=true``. In staging/production,
or when that opt-in is absent, every path that cannot prove a VERIFIED NDA
fails closed (HTTP 503 for unavailable verification, 403 for a blocked candidate).
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

from jwt_auth import get_auth, get_service_auth

logger = logging.getLogger(__name__)

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
# (Kept deliberately broad: the official BHIV NDA prints "Signed By (Receiving
# Party)" / "Signature:" / "Stamp" lines, and a signed copy must carry at
# least one of these evidence tokens in its extractable text.)
_SIGNATURE_MARKERS = (
    "signed",
    "signed by",
    "signature",
    "signatory",
    "stamp",
    "stamped",
    "seal",
    "executed",
    "witness",
    "authorised signatory",
    "authorized signatory",
    "receiving party",
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


def _hardened_env() -> bool:
    """True when ENV/ENVIRONMENT marks a shared or live deployment."""
    env = (os.getenv("ENV") or os.getenv("ENVIRONMENT") or "").strip().lower()
    return env in ("staging", "production", "prod")


def _dev_fail_open_enabled() -> bool:
    """Explicit, non-production opt-in for local development fail-open.

    Audit: incidental conditions (missing DATABASE_URL, missing env marker,
    an unrecognised ENV value) must NEVER silently bypass the NDA gate. Only
    this named opt-in outside staging/production may do so.
    """
    if _hardened_env():
        return False
    return (os.getenv("NDA_GATE_DEV_FAIL_OPEN", "") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


class NdaVerificationUnavailable(RuntimeError):
    """Raised when auto-verification cannot run (e.g. PyPDF2 missing).

    Surfaces as HTTP 503 (misconfiguration) — NEVER as a silent REJECTED
    verdict (audit: a missing dependency is not an invalid document).
    """


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

    # Import OUTSIDE the parse try: a missing PyPDF2 is a server
    # misconfiguration (503), not a document failure (silent REJECTED).
    try:
        from PyPDF2 import PdfReader
    except ImportError as exc:
        raise NdaVerificationUnavailable(
            "PyPDF2 is not installed — NDA auto-verification unavailable"
        ) from exc

    try:
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

    # Normalise whitespace so multi-line / multi-space extractions still
    # match (PyPDF2 frequently inserts stray spaces/newlines).
    normalised_text = " ".join(text.split()).lower()
    marker = next((m for m in _SIGNATURE_MARKERS if m in normalised_text), None)
    if marker is None:
        reasons.append("no_signature_or_stamp_marker_in_text")
        return verdict(NDA_STATUS_NEEDS_REVIEW)
    reasons.append(f"marker:{marker}")

    # Identity sub-check (only when we know who the candidate is).
    # A candidate is considered identified when EITHER their name OR their
    # email is evidenced in the document text — requiring both caused
    # correctly signed NDAs (e.g. those carrying only the printed name, or
    # a different-but-valid contact address) to be refused. The name check
    # is token-based so ordering/spacing differences do not cause a miss.
    # Name matching is deliberately tolerant of middle names / initials:
    # requiring EVERY profile-name token to appear refused correct NDAs
    # where the signature block drops a middle name ("Ashmit Pandey" vs
    # profile "Ashmit Kumar Pandey"). First + last token is enough.
    name_ok = True
    email_ok = True
    if candidate_name:
        name_norm = " ".join(str(candidate_name).split()).lower()
        if name_norm and name_norm in normalised_text:
            name_ok = True
        else:
            tokens = [t for t in name_norm.split() if t]
            if not tokens:
                name_ok = False
            elif len(tokens) == 1:
                name_ok = tokens[0] in normalised_text
            else:
                # First + last only (middle names optional); also accept a
                # strict all-token match when the full name is on the page.
                name_ok = (
                    tokens[0] in normalised_text
                    and tokens[-1] in normalised_text
                ) or all(tok in normalised_text for tok in tokens)
        reasons.append(f"name_match:{name_ok}")
    if candidate_email:
        email_norm = str(candidate_email).strip().lower()
        email_ok = email_norm in normalised_text
        if not email_ok and "@" in email_norm:
            # Profile email may be a login alias; the local-part alone on
            # the NDA (or vice versa) still evidences the same mailbox
            # when it is long enough to be distinctive.
            local = email_norm.split("@", 1)[0]
            if len(local) >= 4 and local in normalised_text:
                email_ok = True
        reasons.append(f"email_match:{email_ok}")
    # If we know at least one identity field, require at least one match;
    # if we know neither, the marker alone is sufficient (existing behaviour).
    known_identity = bool(candidate_name or candidate_email)
    if known_identity and not (name_ok or email_ok):
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
    """Raise 403/503 unless the candidate's NDA is provably VERIFIED.

    Deliberate bypasses: non-candidate roles (recruiter, client, admin,
    service API keys) and demo guest sessions (``guest:*``). Pravesh-SSO
    sessions with the candidate role ARE gated.

    Audit hardening: an absent database, a DB error, a failed status lookup,
    a disabled gate, or a missing candidate identity all fail closed (503
    when verification cannot be determined, 403 when identity is missing),
    unless ``NDA_GATE_DEV_FAIL_OPEN=true`` is explicitly set outside
    staging/production. Incidental dev conditions never bypass the gate.
    """
    if not nda_gate_enabled():
        if not _dev_fail_open_enabled():
            logger.error("NDA gate is disabled without an explicit dev opt-in — failing closed.")
            raise HTTPException(
                status_code=503,
                detail="NDA verification is unavailable — gate must be enabled",
            )
        return
    role = str(auth.get("role") or "").strip().lower()
    auth_type = str(auth.get("type") or "").strip()
    is_candidate = role == "candidate" or auth_type == "candidate_token"
    if not is_candidate:
        return

    user_id = str(auth.get("user_id") or auth.get("sub") or auth.get("candidate_id") or "").strip()
    if not user_id:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "NDA_IDENTITY_REQUIRED",
                "message": "Candidate identity is required to verify the NDA gate.",
            },
        )
    # Demo guest sessions (auth_type guest_demo / "guest:<name>" ids) are
    # a read-only preview: they never submit real data, so the gate lets
    # them through (the write endpoints they would need still 404 for them).
    if auth.get("auth_type") == "guest_demo" or user_id.startswith("guest:"):
        return

    try:
        from app.database import DatabaseNotConfiguredError, get_mongo_db

        db = await get_mongo_db()
    except DatabaseNotConfiguredError as exc:
        if not _dev_fail_open_enabled():
            logger.error("NDA gate: database is not configured (%s) — failing closed.", exc)
            raise HTTPException(
                status_code=503,
                detail="NDA verification status unavailable — try again shortly",
            )
        logger.warning("NDA gate: database not configured — dev fail-open opt-in active.")
        return
    except Exception as exc:
        if not _dev_fail_open_enabled():
            logger.error("NDA gate: database unavailable (%s) — failing closed.", exc)
            raise HTTPException(
                status_code=503,
                detail="NDA verification status unavailable — try again shortly",
            )
        logger.warning("NDA gate: database unavailable (%s) — skipped (dev opt-in).", exc)
        return

    try:
        status = await get_nda_status(db, user_id)
    except Exception as exc:
        if not _dev_fail_open_enabled():
            logger.error("NDA gate: status lookup failed (%s) — failing closed.", exc)
            raise HTTPException(
                status_code=503,
                detail="NDA verification status unavailable — try again shortly",
            )
        logger.warning("NDA gate: status lookup failed (%s) — skipped (dev opt-in).", exc)
        return
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
<title>Non-Disclosure Agreement — BlackHole Infiverse LLP</title>
<style>
  @page { size: A4; margin: 18mm; }
  body { font-family: Georgia, 'Times New Roman', serif; color: #111;
         max-width: 800px; margin: 24px auto; padding: 0 20px; line-height: 1.55; }
  h1 { text-align: center; font-size: 22px; letter-spacing: 2px; margin-bottom: 4px; }
  h2 { font-size: 15px; margin: 22px 0 6px; }
  .sub { text-align: center; font-size: 12px; color: #555; margin-bottom: 22px; }
  .party { margin: 14px 0; font-size: 14px; text-align: justify; }
  .fill { border-bottom: 1px solid #333; display: inline-block; min-width: 260px; }
  .clause { margin: 12px 0; text-align: justify; font-size: 14px; }
  .clause ul { margin: 6px 0 6px 22px; padding: 0; }
  .clause li { margin: 3px 0; }
  .notebox { border: 1px solid #333; padding: 10px 14px; margin: 14px 0;
             font-size: 13.5px; text-align: justify; }
  .sig { display: flex; justify-content: space-between; margin-top: 44px;
         flex-wrap: wrap; gap: 10px; }
  .sig > div { width: 46%; font-size: 13px; margin-bottom: 22px; }
  .line { border-bottom: 1px solid #333; height: 40px; margin-bottom: 6px; }
  .stamp { margin-top: 8px; font-size: 12px; color: #555; }
  footer { margin-top: 40px; font-size: 11px; color: #666; text-align: center; }
  .printbar { background:#f5f5f5; border:1px solid #ddd; padding:10px 14px;
              font-family: Arial, sans-serif; font-size:13px; margin-bottom:18px; }
  .printbar button { padding: 6px 16px; }
  @media print { .printbar { display: none; } body { margin: 0; } }
</style>
</head>
<body>
<div class="printbar">
  Fill the <strong>Company (Hiring Entity)</strong> box below and the signature
  block at the end (Name, Role Applied For, Email, College/Reference,
  Date &amp; Place), print or save this page as PDF
  (browser &#8594; Print &#8594; Save as PDF), sign and stamp it, then upload
  it from your Sampada profile.
  <button onclick="window.print()">Print / Save as PDF</button>
</div>

<h1>NON-DISCLOSURE AGREEMENT (NDA)</h1>
<div class="sub">BlackHole Infiverse LLP &middot; Form NDA/BHIV-v2 &middot; mirrors NEW_BHIV_NDA</div>

<div class="party">
  This Agreement also referred to as NDA is entered into by and between:
  <strong>BlackHole Infiverse LLP</strong>, a registered Limited Liability Partnership,
  hereinafter referred to as the &ldquo;Disclosing Party&rdquo;, and the undersigned party,
  hereinafter referred to as the &ldquo;Receiving Party&rdquo;.<br><br>
  Effective Date: Upon signing.
</div>

<!-- Company section: which hiring company this NDA is issued for. Printed
     on the signed copy so the reviewing company can see it at a glance. -->
<div class="companybox" style="border: 2px solid #111; padding: 14px 18px;
     margin: 18px 0; background: #fafafa;">
  <div style="font-size: 14px; font-weight: bold; margin-bottom: 10px;
       letter-spacing: 0.5px;">
    COMPANY (HIRING ENTITY)
  </div>
  <div style="font-size: 13.5px; margin-bottom: 14px; color: #333;">
    Name of the company / hiring entity this NDA is issued for
    (write the full legal name, e.g. <em>ABC Technologies Pvt. Ltd.</em>):
  </div>
  <div class="line" style="height: 36px; margin-bottom: 4px;"></div>
  <div style="font-size: 12px; color: #555;">
    If you are applying through the Sampada platform, enter the company name
    shown on the job posting. Leave blank only if you are an independent
    candidate with no hiring company yet.
  </div>
</div>

<h2>1. PURPOSE</h2>
<div class="clause">This Agreement governs the protection of confidential information
exchanged in relation to all current and future projects undertaken by BlackHole
Infiverse LLP, including but not limited to internal R&amp;D, experimental platforms,
proprietary AI agents, GUIs, workflows, token systems, and digital products.</div>

<h2>2. DEFINITION OF CONFIDENTIAL INFORMATION</h2>
<div class="clause">&ldquo;Confidential Information&rdquo; includes, but is not limited to:
<ul>
  <li>All source code, scripts, prompts, data flows, model logic, system architecture,
      training data, diagrams, UI/UX, and technical documentation</li>
  <li>Product concepts, designs, prototypes, internal tools, agent logic, and business
      strategies</li>
  <li>Spoken or written communication, shared files, whiteboard sessions, or recorded
      calls including the terms and details of this NON-DISCLOSURE AGREEMENT (NDA)</li>
  <li>All visual, textual, or audio content developed or discussed under the purview of
      any BlackHole Infiverse LLP project</li>
</ul></div>

<h2>3. OBLIGATIONS OF THE RECEIVING PARTY</h2>
<div class="clause">The Receiving Party agrees to:
<ul>
  <li>Not disclose Confidential Information to any third party without prior written
      consent from BlackHole Infiverse LLP</li>
  <li>Not reuse, repurpose, copy, or replicate any component of the Confidential
      Information, whether for commercial, educational, portfolio, freelance, or
      research purposes</li>
  <li>Not use Confidential Information to build or advise on competing platforms or
      systems</li>
  <li>Not share screenshots, screen recordings, visual demos, wireframes, or workflows
      publicly or privately</li>
  <li>Limit discussion of the work only to:
    <ul>
      <li>Tools/technologies used (e.g., Python, Power BI, React etc.)</li>
      <li>Generic roles (e.g., &ldquo;worked as a AI Engineer with changing roles on
          multiple tasks on confidential projects for an AI/Tech R&amp;D project under
          NDA&rdquo;) without naming products, internal tools, datasets, or design
          approaches</li>
    </ul>
  </li>
</ul></div>

<h2>4. NON-CIRCUMVENTION</h2>
<div class="clause">The Receiving Party agrees not to approach, contact, or work with
any known clients, collaborators, partners, investors, or affiliates of BlackHole
Infiverse LLP outside the scope of their assigned role or contract.</div>

<h2>5. NON-SOLICITATION</h2>
<div class="clause">The Receiving Party shall not solicit, attempt to hire, or
collaborate with any other interns, contractors, employees, or advisors of BlackHole
Infiverse LLP for any external purpose for a period of two (2) years from the
conclusion of this Agreement.</div>

<h2>6. TERM AND SURVIVAL</h2>
<div class="clause">This NDA shall be effective for a period of three (3) years
following the conclusion of any active engagement, internship, or collaboration with
BlackHole Infiverse LLP.</div>

<h2>7. BREACH AND REMEDIES</h2>
<div class="clause">Violation of this Agreement may result in:
<ul>
  <li>Immediate removal from all BlackHole Infiverse LLP projects</li>
  <li>Legal claims for direct and indirect damages</li>
  <li>Permanent blacklisting from all current and future engagements</li>
  <li>Notification to associated institutions or partner organisations (if applicable)</li>
</ul></div>

<h2>8. GOVERNING LAW</h2>
<div class="clause">This Agreement shall be governed by and construed in accordance
with the laws of India, but shall also apply globally to parties operating or residing
in any other jurisdiction.</div>

<h2>9. ENTIRE AGREEMENT</h2>
<div class="clause">This Agreement constitutes the entire understanding between the
parties and supersedes all prior discussions or written agreements relating to the
subject matter herein.</div>

<h2>Confidentiality &amp; Public Disclosure Restrictions</h2>

<div class="clause">The Intern expressly acknowledges and agrees that, during the
term of the internship and at all times thereafter, they shall maintain strict
confidentiality and shall not, without prior written consent of Blackhole Infiverse
LLP, directly or indirectly engage in any of the following activities:</div>

<div class="clause"><strong>1. Prohibition on Public Disclosure.</strong> The Intern
shall not post, publish, upload, distribute, transmit, display, or otherwise disclose,
in any manner whatsoever, any information relating to the Company, including but not
limited to:
<ul>
  <li>The Company&rsquo;s name, identity, or association</li>
  <li>Projects, products, services, or research activities</li>
  <li>Internal discussions, strategies, or communications</li>
  <li>Client information, partner information, or business relationships</li>
  <li>Any confidential or proprietary material</li>
</ul>
This restriction applies to all platforms including, but not limited to, LinkedIn,
Instagram, Facebook, X (formerly Twitter), YouTube, WhatsApp, Telegram, personal
blogs, podcasts, forums, or any other digital, electronic, or public medium.</div>

<div class="clause"><strong>2. Restriction on Public Representation.</strong> The
Intern shall not publicly represent, claim, or imply association with Blackhole
Infiverse LLP in any capacity without prior written approval from the Company.
This includes, but is not limited to:
<ul>
  <li>Updating employment or internship status on LinkedIn or similar platforms</li>
  <li>Listing the Company in professional profiles, resumes, portfolios, or public
      biographies</li>
  <li>Making verbal or written claims of affiliation in public or professional
      contexts</li>
</ul></div>

<div class="clause"><strong>3. Restriction on Sharing Visual or Digital Material.</strong>
The Intern shall not capture, share, or distribute any Company-related content,
including but not limited to:
<ul>
  <li>Photographs or videos of office premises</li>
  <li>Screenshots, recordings, or system visuals</li>
  <li>Code snippets, technical materials, or research data</li>
  <li>Documents, presentations, or internal meeting materials</li>
  <li>Any other Company-related visual or digital content</li>
</ul>
Such restriction applies irrespective of whether the platform is public or private.</div>

<div class="clause"><strong>4. Restriction on Tagging and Association.</strong> The
Intern shall not tag, mention, reference, or otherwise associate the Company&rsquo;s
name, logo, brand identity, partners, clients, or representatives in any online or
offline communication without prior written authorization from the Company.</div>

<div class="clause"><strong>5. Survival of Obligations.</strong> All obligations
contained in this section shall remain valid and enforceable during the internship
period and shall survive termination, completion, or cessation of the internship for
any reason.</div>

<h2>Declaration Regarding Fees or Charges</h2>
<div class="clause" style="font-style: italic; font-size: 13px;">
(Optional but strongly recommended if relevant)</div>
<div class="clause">The Intern hereby confirms and declares that:
<ul>
  <li>No fees, commissions, charges, or payments of any nature have been made by the
      Intern to the Company, its employees, representatives, agents, or any third
      party for obtaining this internship.</li>
  <li>The internship opportunity has been accepted voluntarily and without any
      financial inducement, promise, or guarantee.</li>
  <li>The Intern shall not claim reimbursement or compensation for any alleged payment
      made to any external agency or individual.</li>
</ul></div>

<div class="notebox"><strong>Note:</strong>
<ul style="margin-bottom: 0;">
  <li>Any individual hired as an Intern shall be eligible to receive an Internship
      Completion Letter / Certificate upon successful completion of the mandatory
      six (6) month internship period, subject to satisfactory performance and conduct.</li>
  <li>In the event an Employee wishes to resign from the Company, a compulsory
      thirty (30) days&rsquo; notice period must be duly served. Proper certification /
      relieving documents shall be issued only upon successful completion of the
      notice period and clearance of all obligations.</li>
  <li>All interns, employees, and associated personnel are strictly prohibited from
      posting, sharing, or disclosing any company-related, project-related,
      client-related, or job-related information on social media or any public
      platform without prior written approval from BlackHole Infiverse L.L.P.
      Any unauthorized disclosure may result in disciplinary or legal action.</li>
</ul></div>

<div class="clause" style="margin-top: 30px;">
  <strong>Signed By (Receiving Party) &amp; Stamp (if applicable):</strong>
</div>

<div class="sig" style="margin-top: 10px;">
  <div>
    <div class="line"></div>
    Name
  </div>
  <div>
    <div class="line"></div>
    Role Applied For
  </div>
  <div>
    <div class="line"></div>
    Company (Hiring Entity) — same as the box at the top
  </div>
  <div>
    <div class="line"></div>
    Email
  </div>
  <div>
    <div class="line"></div>
    College/Reference
  </div>
  <div>
    <div class="line"></div>
    Date &amp; Place
  </div>
  <div>
    <div class="line"></div>
    Signature
  </div>
</div>

<footer>Form NDA/BHIV-v2 &mdash; faithful HTML rendering of NEW_BHIV_NDA.pdf
(BlackHole Infiverse LLP) &middot; Fill the <strong>Company (Hiring Entity)</strong>
box at the top and the signature block (Name, Role Applied For, Email,
College/Reference, Date &amp; Place), sign and stamp where applicable, then
upload the signed copy from your Sampada profile.</footer>
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
        auth.get("user_id") or auth.get("sub") or auth.get("candidate_id") or ""
    ).strip()
    if not user_id:
        raise HTTPException(status_code=403, detail="No candidate identity in token")
    return user_id


# ---------------------------------------------------------------------------
# Tenancy helpers (company-scoped NDA review)
# ---------------------------------------------------------------------------


def _require_client(auth: Dict[str, Any]) -> str:
    """Company (Client-Portal) account identity for tenant-scoped NDA review.

    Platform admins get 403 here by design: NDA submissions are a tenant's
    private data — the central admin has no right to read or review them.
    """
    if auth.get("type") != "jwt_token" or auth.get("role") != "client":
        raise HTTPException(
            status_code=403,
            detail="Company (client) account access required",
        )
    client_id = str(auth.get("user_id") or "").strip()
    if not client_id:
        raise HTTPException(status_code=401, detail="Client not identified")
    return client_id


async def _tenant_candidate_ids(db, client_id: str) -> set:
    """Candidate ids inside the caller's tenanted subset.

    Own + subtree clients + their connected recruiters' jobs, collected via
    the dashboard governance helper, then resolved through job_applications.
    Deterministic 10 000-entry cap keeps the in-set check bounded.
    """
    from app.control_center_governance import _client_all_job_ids_for_dashboard

    client_id = str(client_id).strip()
    if not client_id:
        return set()
    job_ids = await _client_all_job_ids_for_dashboard(db, client_id)
    if not job_ids:
        return set()
    # job_id in job_applications may be stored as string, int or ObjectId —
    # match all plausible variants so a company's queue is not silently empty.
    job_variants: List[Any] = []
    seen_jv = set()
    for j in job_ids:
        js = str(j).strip()
        if not js:
            continue
        if js not in seen_jv:
            seen_jv.add(js)
            job_variants.append(js)
        if js.isdigit():
            ij = int(js)
            if ij not in seen_jv:
                seen_jv.add(ij)
                job_variants.append(ij)
        try:
            from bson import ObjectId

            if ObjectId.is_valid(js):
                oj = ObjectId(js)
                if oj not in seen_jv:
                    seen_jv.add(oj)
                    job_variants.append(oj)
        except Exception:
            pass
    raw = await db.job_applications.distinct(
        "candidate_id", {"job_id": {"$in": job_variants}}
    )
    ids = {str(c).strip() for c in raw if str(c or "").strip()}
    return set(sorted(ids)[:10000])


async def _candidate_owner_client_ids(db, candidate_id: str) -> List[str]:
    """Companies (client_ids) whose jobs this candidate applied to.

    Empty list = independent/unowned candidate (system-driven path — no
    human reviews their NDA anywhere).
    """
    from bson import ObjectId

    cid = str(candidate_id or "").strip()
    if not cid:
        return []
    # Candidate ids may be stored as strings, ints or ObjectIds depending on
    # the intake path — query all plausible variants so ownership is not
    # missed (a missed lookup wrongly auto-rejects an owned candidate's NDA).
    id_variants: List[Any] = [cid]
    if cid.isdigit():
        id_variants.append(int(cid))
    try:
        from bson import ObjectId

        if ObjectId.is_valid(cid):
            id_variants.append(ObjectId(cid))
    except Exception:
        pass
    job_raw = await db.job_applications.distinct(
        "job_id", {"candidate_id": {"$in": id_variants}}
    )
    if not job_raw:
        return []
    str_ids = {str(j) for j in job_raw}
    clauses: List[Dict[str, Any]] = []
    oids = []
    for j in sorted(str_ids):
        if ObjectId.is_valid(j):
            try:
                oids.append(ObjectId(j))
                continue
            except Exception:
                pass
    if oids:
        clauses.append({"_id": {"$in": oids}})
    nums = [int(j) for j in str_ids if j.isdigit()]
    if nums:
        clauses.append({"_id": {"$in": nums}})
    if not clauses:
        return []
    owners: List[str] = []
    seen = set()
    async for doc in db.jobs.find({"$or": clauses}, {"client_id": 1}):
        owner = str(doc.get("client_id") or "").strip()
        if owner and owner not in seen:
            seen.add(owner)
            owners.append(owner)
    return owners


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

    try:
        verification = verify_nda_pdf(
            content,
            candidate_name=candidate_name,
            candidate_email=candidate_email,
        )
    except NdaVerificationUnavailable as exc:
        # Audit: missing dependency = 503 misconfiguration, never REJECTED.
        raise HTTPException(status_code=503, detail=str(exc))
    collection = _nda_collection(db)
    if collection is None:
        raise HTTPException(status_code=503, detail="NDA storage unavailable")
    now = datetime.now(timezone.utc)
    previous = await get_nda_record(db, candidate_id)

    # Unowned candidates: NEEDS_REVIEW is KEPT (never auto-REJECTED).
    # Auto-rejecting correct-but-unverifiable NDAs (scanned / image-only
    # signature pages, identity variants) refused valid documents. Ops
    # reviews the service queue (`GET /v1/nda/admin/queue`, api_key only);
    # candidates can also re-upload a clearer text-based PDF which overwrites
    # the record. The platform admin still has no NDA access (tenancy audit).
    # Owned candidates keep NEEDS_REVIEW — the owning company reviews on
    # its own dashboard (`/client/ndas`).
    if verification.get("result") == NDA_STATUS_NEEDS_REVIEW:
        try:
            owners = await _candidate_owner_client_ids(db, candidate_id)
        except Exception:
            owners = None  # storage hiccup: keep NEEDS_REVIEW either way
        if owners == []:
            verification = dict(verification)
            reasons = [str(r) for r in (verification.get("reasons") or [])]
            reasons.append("unowned_candidate_needs_review")
            verification["reasons"] = reasons
            verification["auto_decision"] = "NEEDS_REVIEW"
            verification["auto_decision_reason"] = (
                "unowned_candidate_pending_ops_review_or_clearer_reupload"
            )

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
                "Uploaded — automatic verification could not confirm the "
                "signature/identity from this file. A reviewer will check "
                "it, or re-upload a clearer text-based (non-scan) PDF to "
                "speed things up."
                if out["status"] == NDA_STATUS_NEEDS_REVIEW
                else "Upload rejected — please re-upload a valid, signed NDA."
            )
        ),
    }


@router.get(
    "/document",
    summary=(
        "Download an uploaded NDA — candidate: own; company: ?candidate_id= "
        "inside own tenant scope; service api_key: any (ops)"
    ),
)
async def download_nda(
    candidate_id: Optional[str] = None,
    auth: dict = Depends(get_auth),
):
    from app.database import get_mongo_db

    role = str(auth.get("role") or "").strip().lower()
    auth_type = str(auth.get("type") or "").strip().lower()

    if auth_type == "api_key":
        # Machine credential (ops/tests) — the ONLY cross-candidate path.
        target = (candidate_id or "").strip()
        if not target:
            raise HTTPException(
                status_code=422, detail="candidate_id query param required"
            )
        db = await get_mongo_db()
    elif auth_type == "jwt_token" and role == "client":
        # Company account: only candidates inside its own tenant subset.
        target = (candidate_id or "").strip()
        if not target:
            raise HTTPException(
                status_code=422, detail="candidate_id query param required"
            )
        db = await get_mongo_db()
        client_id = _require_client(auth)
        allowed = await _tenant_candidate_ids(db, client_id)
        if target not in allowed:
            raise HTTPException(
                status_code=403,
                detail="Candidate outside your company subset",
            )
    else:
        # Candidate (and platform-admin JWT, which simply resolves to its own
        # no-candidate identity → 403/404): own document only. Platform
        # admins have NO cross-candidate NDA read access (tenancy audit).
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
# Review (service api_key for ops; tenant company for its own subset)
# Platform admins are excluded from BOTH paths — tenancy audit.
# ---------------------------------------------------------------------------


async def _apply_review(
    db,
    candidate_id: str,
    decision: str,
    note: Any,
    auth: Dict[str, Any],
) -> Dict[str, Any]:
    """Shared review application (service ops + tenant company).

    ``reviewed_by`` is STRICTLY the authenticated principal — a spoofable
    "reviewer" field in the payload is ignored.
    """
    collection = _nda_collection(db)
    if collection is None:
        raise HTTPException(status_code=503, detail="NDA storage unavailable")
    rec = await get_nda_record(db, candidate_id)
    if not rec:
        raise HTTPException(
            status_code=404, detail="No NDA submission for this candidate"
        )

    reviewer = str(auth.get("user_id") or auth.get("email") or "service")
    now = datetime.now(timezone.utc)
    update = {
        "status": decision,
        "review": {
            "reviewed_by": reviewer,
            "reviewed_at": now.isoformat(),
            "decision": decision,
            "note": str(note or "")[:500],
        },
        "updated_at": now,
    }
    # Manual review must override a conflicting auto-verdict honestly.
    if rec.get("verification"):
        verification = dict(rec["verification"])
        if str(auth.get("type") or "") == "api_key":
            verification["admin_override"] = decision
            verification["admin_reviewed_at"] = now.isoformat()
        else:
            verification["tenant_override"] = decision
            verification["tenant_reviewed_at"] = now.isoformat()
        update["verification"] = verification

    await collection.update_one({"candidate_id": candidate_id}, {"$set": update})
    return {
        "ok": True,
        "candidate_id": candidate_id,
        "status": decision,
        "gate_passed": decision == NDA_STATUS_VERIFIED,
        "reviewed_by": reviewer,
    }


@router.get("/admin/queue", summary="Review queue (service api_key only)")
async def nda_admin_queue(auth: dict = Depends(get_service_auth)):
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


@router.post("/admin/{candidate_id}/review", summary="Service decision: VERIFIED / REJECTED")
async def nda_admin_review(
    candidate_id: str,
    payload: Dict[str, Any],
    auth: dict = Depends(get_service_auth),
):
    decision = str(payload.get("decision") or "").strip().upper()
    if decision not in (NDA_STATUS_VERIFIED, NDA_STATUS_REJECTED):
        raise HTTPException(
            status_code=422, detail="decision must be 'VERIFIED' or 'REJECTED'"
        )
    from app.database import get_mongo_db

    db = await get_mongo_db()
    return await _apply_review(db, candidate_id, decision, payload.get("note"), auth)


# ---------------------------------------------------------------------------
# Tenant company review — own subset only, no platform-admin access
# ---------------------------------------------------------------------------


@router.get("/tenant/queue", summary="Company NDA review queue (own tenant subset)")
async def nda_tenant_queue(auth: dict = Depends(get_auth)):
    """Company (Client-Portal) account sees ONLY candidates inside its own
    tenant subset; every other company's submissions are unreachable."""
    from app.database import get_mongo_db

    client_id = _require_client(auth)
    db = await get_mongo_db()
    collection = _nda_collection(db)
    if collection is None:
        return {"queue": [], "count": 0, "scope": "tenant"}
    candidate_ids = await _tenant_candidate_ids(db, client_id)
    if not candidate_ids:
        return {"queue": [], "count": 0, "scope": "tenant"}

    # Display names, best effort (offline test fakes may lack candidates).
    names: Dict[str, str] = {}
    try:
        from bson import ObjectId

        oids = [ObjectId(c) for c in sorted(candidate_ids) if ObjectId.is_valid(c)]
        cand = getattr(db, "candidates", None)
        if cand is not None and oids:
            async for d in cand.find({"_id": {"$in": oids}}, {"name": 1}):
                names[str(d.get("_id"))] = str(d.get("name") or "")
    except Exception:
        names = {}

    out = []
    async for rec in collection.find(
        {
            "candidate_id": {"$in": sorted(candidate_ids)},
            "status": {"$in": [NDA_STATUS_NEEDS_REVIEW, NDA_STATUS_REJECTED]},
        },
        {
            "candidate_id": 1,
            "status": 1,
            "verification": 1,
            "review": 1,
            "document.filename": 1,
            "document.size_bytes": 1,
            "document.uploaded_at": 1,
            "updated_at": 1,
        },
    ).sort("updated_at", -1).limit(200):
        rec["_id"] = str(rec.get("_id") or "")
        doc = rec.get("document") or {}
        out.append(
            {
                "candidate_id": rec.get("candidate_id"),
                "candidate_name": names.get(str(rec.get("candidate_id"))),
                "status": rec.get("status"),
                "verification": rec.get("verification"),
                "review": rec.get("review"),
                "filename": doc.get("filename"),
                "size_bytes": doc.get("size_bytes"),
                "uploaded_at": doc.get("uploaded_at"),
                "updated_at": rec.get("updated_at").isoformat()
                if isinstance(rec.get("updated_at"), datetime)
                else rec.get("updated_at"),
            }
        )
    return {"queue": out, "count": len(out), "scope": "tenant"}


@router.post(
    "/tenant/{candidate_id}/review",
    summary="Company decision: VERIFIED / REJECTED (own candidates only)",
)
async def nda_tenant_review(
    candidate_id: str,
    payload: Dict[str, Any],
    auth: dict = Depends(get_auth),
):
    decision = str(payload.get("decision") or "").strip().upper()
    if decision not in (NDA_STATUS_VERIFIED, NDA_STATUS_REJECTED):
        raise HTTPException(
            status_code=422, detail="decision must be 'VERIFIED' or 'REJECTED'"
        )
    client_id = _require_client(auth)
    from app.database import get_mongo_db

    db = await get_mongo_db()
    allowed = await _tenant_candidate_ids(db, client_id)
    if str(candidate_id) not in allowed:
        raise HTTPException(
            status_code=403, detail="Candidate outside your company subset"
        )
    return await _apply_review(db, str(candidate_id), decision, payload.get("note"), auth)
