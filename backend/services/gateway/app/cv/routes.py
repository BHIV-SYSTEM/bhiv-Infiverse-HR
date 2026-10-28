"""
CV builder routes (Candidate + Recruiter entry points).

Mounted by the gateway at ``/api/v1`` (see app/main.py).

Security model:
* Every route requires gateway auth (``get_auth``) — Bearer JWT or the
  Pravesh read-only bridge.
* Drafts are strictly scoped to the authenticated ``user_id``: a recruiter
  can only ever reach their OWN drafts, never a candidate's private CV.
* Export is impossible without an explicit, recorded approval.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from uuid import uuid4

_gateway_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

from jwt_auth import get_auth  # noqa: E402

from app.cv import service  # noqa: E402
from app.cv.models import (  # noqa: E402
    CV_SCHEMA_VERSION,
    TEMPLATES,
    TEMPLATE_LABELS,
    ApproveRequest,
    AssembleRequest,
    CVContent,
    DraftCreate,
    DraftUpdate,
    ExportRequest,
    SuggestRequest,
    default_content,
    utc_now,
)

router = APIRouter(prefix="/cv", tags=["CV Builder"])

APPROVAL_PHRASE = "I have reviewed this CV"


def _owner(auth: dict) -> str:
    uid = auth.get("user_id")
    if not uid:
        raise HTTPException(status_code=401, detail="Authenticated user has no identity")
    return str(uid)


def _is_service_or_admin(auth: dict) -> bool:
    return auth.get("type") == "api_key" or str(auth.get("role", "")).lower() == "admin"


async def _load_candidate(candidate_id: str) -> Optional[dict]:
    """Read-only lookup of the authorised profile record (best effort)."""
    try:
        from app.database import get_mongo_db

        db = await get_mongo_db()
        if db is None:
            return None
        doc = None
        try:
            from bson import ObjectId

            doc = await db.candidates.find_one({"_id": ObjectId(candidate_id)})
        except Exception:
            doc = await db.candidates.find_one({"id": candidate_id})
        if not doc:
            doc = await db.candidates.find_one({"id": candidate_id})
        if not doc:
            return None
        doc = dict(doc)
        doc["id"] = str(doc.pop("_id", candidate_id))
        return doc
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Reference
# ---------------------------------------------------------------------------


@router.get("/templates", summary="Editable templates (one shared content model)")
async def list_templates(auth: dict = Depends(get_auth)):
    return {
        "templates": [
            {"id": t, "label": TEMPLATE_LABELS[t]} for t in TEMPLATES
        ],
        "policy": "templates only restyle content — switching never erases it",
        "schema_version": CV_SCHEMA_VERSION,
    }


# ---------------------------------------------------------------------------
# Prefill with provenance
# ---------------------------------------------------------------------------


@router.get("/prefill", summary="Authorised profile data mapped onto CV fields")
async def prefill(
    candidate_id: Optional[str] = None,
    auth: dict = Depends(get_auth),
):
    """
    Returns every CV field wrapped in ``{value, source, verified}``.

    * ``source: profile`` — read from the caller's own authorised record.
    * ``source: none``    — no authorised source exists; the field stays empty
      and a question is returned instead of a guess.

    A candidate may only read their OWN record; recruiters get their own
    record too — never a candidate's private data.
    """
    uid = _owner(auth)
    target = candidate_id or uid
    if not _is_service_or_admin(auth) and str(target) != str(uid):
        raise HTTPException(
            status_code=403,
            detail="You may only build a CV from your own authorised record",
        )

    doc = await _load_candidate(str(target))
    result = service.build_prefill(doc)
    result["prefill"]["full_name"] = dict(
        result["prefill"]["full_name"],
        value=result["prefill"]["full_name"]["value"] or auth.get("name"),
        source="profile"
        if result["prefill"]["full_name"]["value"]
        else ("auth" if auth.get("name") else "none"),
        verified=bool(result["prefill"]["full_name"]["value"]),
    )
    if result["prefill"]["full_name"]["source"] == "none":
        # keep honest: unknown stays unknown
        result["prefill"]["full_name"]["value"] = None
    return result


# ---------------------------------------------------------------------------
# Drafts (save / reopen — no data loss, ownership enforced)
# ---------------------------------------------------------------------------


@router.get("/drafts", summary="List my CV drafts")
async def list_drafts(auth: dict = Depends(get_auth), store=Depends(service.get_cv_store)):
    uid = _owner(auth)
    docs = await store.list_for_user(uid)
    return {
        "storage_mode": store.mode,
        "count": len(docs),
        "drafts": [
            {
                "draft_id": d.get("draft_id"),
                "template": d.get("template"),
                "approved": d.get("approved", False),
                "updated_at": d.get("updated_at"),
                "created_at": d.get("created_at"),
            }
            for d in docs
        ],
    }


@router.post("/drafts", summary="Create a CV draft")
async def create_draft(
    body: DraftCreate,
    auth: dict = Depends(get_auth),
    store=Depends(service.get_cv_store),
):
    uid = _owner(auth)
    template = body.template if body.template in TEMPLATES else "professional"
    content = body.content or default_content()
    doc = {
        "draft_id": f"cv_{uuid4().hex[:16]}",
        "user_id": uid,
        "candidate_id": body.candidate_id,
        "template": template,
        "content": content.model_dump(),
        "approved": False,
        "approved_at": None,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    await store.save(doc)
    return {"draft": doc, "storage_mode": store.mode}


@router.get("/drafts/{draft_id}", summary="Open one of my drafts")
async def get_draft(
    draft_id: str,
    auth: dict = Depends(get_auth),
    store=Depends(service.get_cv_store),
):
    doc = await store.get(draft_id, _owner(auth))
    if doc is None:
        # Ownership is invisible: other users' drafts simply do not exist here.
        raise HTTPException(status_code=404, detail="Draft not found")
    return {"draft": doc, "storage_mode": store.mode}


@router.put("/drafts/{draft_id}", summary="Update my draft (template/content)")
async def update_draft(
    draft_id: str,
    body: DraftUpdate,
    auth: dict = Depends(get_auth),
    store=Depends(service.get_cv_store),
):
    uid = _owner(auth)
    doc = await store.get(draft_id, uid)
    if doc is None:
        raise HTTPException(status_code=404, detail="Draft not found")

    if body.template is not None:
        if body.template not in TEMPLATES:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown template '{body.template}'. Allowed: {', '.join(TEMPLATES)}",
            )
        doc["template"] = body.template  # switching templates keeps content intact

    if body.content is not None:
        doc["content"] = body.content.model_dump()

    if body.approve:
        doc["approved"] = True
        doc["approved_at"] = utc_now()
        doc["approval_phrase"] = APPROVAL_PHRASE
    else:
        # Any edit invalidates a previous approval — export needs fresh review.
        doc["approved"] = False
        doc["approved_at"] = None

    doc["updated_at"] = utc_now()
    await store.save(doc)
    return {"draft": doc, "storage_mode": store.mode}


@router.post("/drafts/{draft_id}/approve", summary="Explicitly approve my CV for export")
async def approve_draft(
    draft_id: str,
    body: ApproveRequest,
    auth: dict = Depends(get_auth),
    store=Depends(service.get_cv_store),
):
    uid = _owner(auth)
    doc = await store.get(draft_id, uid)
    if doc is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    if body.confirmation.strip() != APPROVAL_PHRASE:
        raise HTTPException(
            status_code=422,
            detail=f"Approval requires the exact confirmation: '{APPROVAL_PHRASE}'",
        )
    doc["approved"] = True
    doc["approved_at"] = utc_now()
    doc["approval_phrase"] = APPROVAL_PHRASE
    doc["updated_at"] = utc_now()
    await store.save(doc)
    return {"draft_id": draft_id, "approved": True, "approved_at": doc["approved_at"]}


# ---------------------------------------------------------------------------
# AI-assisted (bounded) operations
# ---------------------------------------------------------------------------


@router.post("/suggest", summary="Proposed grammar/format fixes (review before applying)")
async def suggest(body: SuggestRequest, auth: dict = Depends(get_auth)):
    """
    Returns *proposals* only — nothing is applied server-side. Rules are
    strictly meaning-preserving (whitespace + sentence capitalisation), so a
    suggestion can never introduce an unsupported factual claim. The client
    must show them and save accepted changes through the normal draft update.
    """
    return service.suggest(body.text, body.field_ref)


@router.post("/assemble", summary="Render a preview from content + template")
async def assemble(body: AssembleRequest, auth: dict = Depends(get_auth)):
    if body.template not in TEMPLATES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown template '{body.template}'. Allowed: {', '.join(TEMPLATES)}",
        )
    report = service.validate_content(body.content)
    return {
        "template": body.template,
        "preview_html": service.render_html(body.content, body.template),
        "validation": report,
        "policy": {"blank_stays_blank": True, "no_invention": True},
    }


# ---------------------------------------------------------------------------
# Export (approval required)
# ---------------------------------------------------------------------------


@router.post("/export", summary="Export the approved CV as print-ready HTML")
async def export_cv(
    body: ExportRequest,
    auth: dict = Depends(get_auth),
    store=Depends(service.get_cv_store),
):
    uid = _owner(auth)
    doc = await store.get(body.draft_id, uid)
    if doc is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    if not doc.get("approved"):
        raise HTTPException(
            status_code=403,
            detail=(
                "Export blocked: the CV must be reviewed and approved first "
                f"(POST /cv/drafts/{body.draft_id}/approve with confirmation "
                f"'{APPROVAL_PHRASE}')"
            ),
        )

    content = CVContent(**doc.get("content") or {})
    template = doc.get("template", "professional")
    html_doc = service.render_html(content, template)
    return {
        "format": "html",
        "print_ready": True,
        "template": template,
        "filename": f"cv_{uid}_{doc.get('draft_id')}.html",
        "html": html_doc,
        "approved_at": doc.get("approved_at"),
        "note": "Open in a browser and print to PDF (Ctrl/Cmd+P).",
    }
