"""
CV builder service (BHIV vNEXT P0 — Editable CV Builder).

Design rules from the task (mandatory):

* **AI-assisted, not AI-authored.** Every field is either (a) prefilled from
  an authorised existing record with provenance, or (b) supplied/confirmed by
  the user. Nothing is ever invented: an honest blank beats a polished lie.
* **Provenance is visible.** Each prefilled field carries
  ``{value, source, verified}``; missing fields carry a question instead of a
  value.
* **Suggestions never create facts.** ``suggest()`` only proposes
  meaning-preserving grammar/format fixes on text the user already wrote, and
  always returns proposals for review BEFORE anything is applied.
* **Approval gate.** Export requires an explicit, recorded user approval.
* **One content model, many templates.** Switching templates re-renders the
  same content — it can never erase it.
* **Ownership.** Drafts belong to one authenticated user; recruiters can only
  ever see their own drafts, never a candidate's private CV.
"""

from __future__ import annotations

import html
import re
import uuid
from typing import Any, Dict, List, Optional

from .models import (
    CV_SCHEMA_VERSION,
    TEMPLATES,
    MISSING_QUESTIONS,
    CVContent,
    PrefillField,
)

# ---------------------------------------------------------------------------
# Storage (Mongo when available, in-process otherwise — mode reported honestly)
# ---------------------------------------------------------------------------


class InMemoryCVStore:
    mode = "memory"

    def __init__(self) -> None:
        self._drafts: Dict[str, Dict[str, Any]] = {}

    async def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        return [d for d in self._drafts.values() if d["user_id"] == user_id]

    async def get(self, draft_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        doc = self._drafts.get(draft_id)
        if doc and doc["user_id"] == user_id:
            return doc
        return None  # never leak another user's draft — not even its existence

    async def save(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        self._drafts[doc["draft_id"]] = doc
        return doc


class MongoCVStore:
    mode = "mongo"
    COLLECTION = "cv_drafts"

    def __init__(self, db) -> None:
        self._db = db

    async def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        docs = await self._db[self.COLLECTION].find(
            {"user_id": user_id}, {"_id": 0}
        ).to_list(length=200)
        return docs

    async def get(self, draft_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        doc = await self._db[self.COLLECTION].find_one(
            {"draft_id": draft_id, "user_id": user_id}, {"_id": 0}
        )
        return doc

    async def save(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        await self._db[self.COLLECTION].update_one(
            {"draft_id": doc["draft_id"], "user_id": doc["user_id"]},
            {"$set": doc},
            upsert=True,
        )
        return doc


_MEMORY = InMemoryCVStore()


async def get_cv_store():
    try:
        from app.database import get_mongo_db

        db = await get_mongo_db()
        if db is not None:
            return MongoCVStore(db)
    except Exception:
        pass
    return _MEMORY


# ---------------------------------------------------------------------------
# Prefill — authorised existing records only, with provenance
# ---------------------------------------------------------------------------


def _field(value: Optional[Any], source: str, verified: bool = True) -> PrefillField:
    """Wrap a value with provenance. Empty values stay empty — never filled."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return PrefillField(value=None, source="none", verified=False)
    if isinstance(value, list):
        if not value:
            return PrefillField(value=None, source="none", verified=False)
        return PrefillField(value=value, source=source, verified=verified)
    return PrefillField(value=value, source=source, verified=verified)


def build_prefill(candidate_doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Map an authorised candidate/ recruiter record onto CV fields.

    ``candidate_doc`` is None when the record does not exist — every field
    then reports ``source: none`` and the questions list drives the flow.
    """
    doc = candidate_doc or {}
    skills_raw = doc.get("technical_skills")
    skills: List[str] = []
    if isinstance(skills_raw, str) and skills_raw.strip():
        skills = [s.strip() for s in re.split(r"[,;]", skills_raw) if s.strip()]
    elif isinstance(skills_raw, list):
        skills = [str(s).strip() for s in skills_raw if str(s).strip()]

    prefill = {
        "full_name": _field(doc.get("name"), "profile"),
        "email": _field(doc.get("email"), "profile"),
        "phone": _field(doc.get("phone"), "profile"),
        "location": _field(doc.get("location"), "profile"),
        "skills": _field(skills, "profile"),
        "education_level": _field(doc.get("education_level"), "profile"),
        "experience_years": _field(doc.get("experience_years"), "profile"),
        "seniority_level": _field(doc.get("seniority_level"), "profile"),
        # Fields with NO authorised source: honestly empty, never invented.
        "professional_summary": PrefillField(value=None, source="none", verified=False),
        "education": PrefillField(value=None, source="none", verified=False),
        "experience": PrefillField(value=None, source="none", verified=False),
        "projects": PrefillField(value=None, source="none", verified=False),
        "certifications": PrefillField(value=None, source="none", verified=False),
        "languages": PrefillField(value=None, source="none", verified=False),
        "awards": PrefillField(value=None, source="none", verified=False),
        "references": PrefillField(value=None, source="none", verified=False),
    }

    missing = [k for k, v in prefill.items() if v.source == "none"]
    questions = [q for q in MISSING_QUESTIONS if q["field"] in missing]

    return {
        "schema_version": CV_SCHEMA_VERSION,
        "prefill": {k: v.model_dump() for k, v in prefill.items()},
        "missing_fields": missing,
        "questions": questions,
        "policy": {
            "no_invention": True,
            "ask_dont_guess": True,
            "email_is_linking_attribute_only": True,
        },
        "source_record": {
            "available": bool(candidate_doc),
            # identifiers only — no private extras leak through this route
            "candidate_id": doc.get("id"),
        },
    }


# ---------------------------------------------------------------------------
# Validation — blanks stay blank, nothing is fabricated
# ---------------------------------------------------------------------------


def validate_content(content: CVContent) -> Dict[str, Any]:
    warnings: List[Dict[str, str]] = []
    for section in content.sections:
        if not section.visible:
            continue
        if section.type == "summary" and not any(
            (e.text or "").strip() for e in section.entries
        ):
            warnings.append(
                {
                    "section": section.id,
                    "code": "summary_blank",
                    "message": "Professional summary is empty — supply or confirm "
                    "your own wording; it will export blank rather than be invented.",
                }
            )
        for entry in section.entries:
            if entry.text is None:
                continue
            if entry.text != entry.text.strip():
                warnings.append(
                    {
                        "section": section.id,
                        "code": "whitespace",
                        "message": "Leading/trailing whitespace will be trimmed on export.",
                    }
                )
            if len(entry.text) > 4000:
                warnings.append(
                    {
                        "section": section.id,
                        "code": "too_long",
                        "message": "Entry exceeds 4000 characters and will be truncated.",
                    }
                )
    visible = [s for s in content.sections if s.visible]
    if not visible:
        warnings.append(
            {
                "section": "*",
                "code": "no_sections",
                "message": "No visible sections — the exported CV would be empty.",
            }
        )
    return {"valid": True, "warnings": warnings}


# ---------------------------------------------------------------------------
# AI-assisted suggestions — meaning-preserving only, shown before applying
# ---------------------------------------------------------------------------

_SENTENCE_START = re.compile(r"(^|[.!?]\s+)([a-z])")
_MULTI_SPACE = re.compile(r"  +")


def _proposals_for(text: str, field_ref: str) -> List[Dict[str, Any]]:
    """Deterministic, meaning-preserving clean-ups of user-written text.

    Strict boundary: rules only adjust whitespace and capitalisation of the
    first letter of a sentence. No word is added, removed or re-worded, so a
    suggestion can never introduce an unsupported claim.
    """
    if not text:
        return []
    out = text.strip()
    out = _MULTI_SPACE.sub(" ", out)
    out = _SENTENCE_START.sub(lambda m: m.group(1) + m.group(2).upper(), out)
    if out == text:
        return []
    return [
        {
            "proposal_id": uuid.uuid4().hex[:12],
            "field_ref": field_ref,
            "kind": "grammar_format_only",
            "original": text,
            "proposed": out,
            "reason": "whitespace / capitalisation only — wording untouched",
            "changes_meaning": False,
        }
    ]


def suggest(text: str, field_ref: str = "text") -> Dict[str, Any]:
    proposals = _proposals_for(text or "", field_ref)
    return {
        "proposals": proposals,
        "policy": {
            "no_invention": True,
            "review_required_before_apply": True,
            "wording_never_rewritten": True,
        },
    }


# ---------------------------------------------------------------------------
# Rendering — one content model, three templates
# ---------------------------------------------------------------------------

_TEMPLATE_CSS = {
    "professional": """
    body{font-family:Georgia,'Times New Roman',serif;color:#111;margin:40px;line-height:1.5}
    h1{font-size:26px;margin:0 0 4px;border-bottom:2px solid #1f4e79;padding-bottom:6px}
    h2{font-size:16px;margin:22px 0 8px;color:#1f4e79;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid #ccc;padding-bottom:3px}
    .meta{color:#444;font-size:13px}
    .entry{margin:0 0 10px}
    .entry .title{font-weight:bold}
    .entry .sub{color:#555;font-size:13px}
    """,
    "modern": """
    body{font-family:'Segoe UI',Helvetica,Arial,sans-serif;color:#1c1c1c;margin:40px;line-height:1.55}
    h1{font-size:30px;margin:0;letter-spacing:-.02em}
    .banner{background:#111;color:#fff;padding:18px 22px;margin:-40px -40px 18px}
    .banner .meta{color:#ddd;font-size:13px}
    h2{font-size:15px;margin:20px 0 8px;color:#0b6e6e;text-transform:uppercase;letter-spacing:.08em}
    .entry .title{font-weight:700}
    .entry .sub{color:#666;font-size:13px}
    """,
    "minimal": """
    body{font-family:Arial,Helvetica,sans-serif;color:#000;margin:36px;font-size:12.5pt;line-height:1.4}
    h1{font-size:18pt;font-weight:600;margin:0 0 2pt}
    h2{font-size:11pt;font-weight:700;margin:14pt 0 4pt;text-transform:uppercase}
    .meta{font-size:10.5pt;color:#333}
    .entry{margin:0 0 6pt}
    .entry .title{font-weight:600}
    .entry .sub{font-size:10.5pt;color:#444}
    """,
}


def render_html(content: CVContent, template: str) -> str:
    """Print-ready standalone HTML. Same content, template-specific style."""
    if template not in TEMPLATES:
        template = "professional"
    css = _TEMPLATE_CSS[template]
    parts: List[str] = []

    name = ""
    contact_bits: List[str] = []
    first = True
    for section in content.sections:
        if not section.visible:
            continue
        entries = [e for e in section.entries if (e.text or "").strip()]
        if section.type == "header" and entries:
            name = entries[0].text or ""
            sub = entries[0].subtext or ""
            if sub:
                contact_bits.append(sub)
            if entries[0].url:
                contact_bits.append(entries[0].url)
            first = False
            continue
        body = []
        for e in entries:
            sub_html = ""
            if e.subtext:
                sub_html = '<div class="sub">' + html.escape(e.subtext.strip()) + "</div>"
            if e.title and e.text:
                body.append(
                    '<div class="entry">'
                    '<div class="title">' + html.escape(e.title.strip()) + "</div>"
                    + sub_html
                    + "<div>" + html.escape(e.text.strip()) + "</div>"
                    "</div>"
                )
            else:
                title_text = (e.title or e.text or "").strip()
                body.append(
                    '<div class="entry">'
                    '<div class="title">' + html.escape(title_text) + "</div>"
                    + sub_html
                    + "</div>"
                )
        if not body:
            continue  # honest blank: an empty section simply does not render
        parts.append(f"<h2>{html.escape(section.heading)}</h2>" + "".join(body))

    contact = " · ".join(html.escape(c) for c in contact_bits if c)
    if template == "modern":
        head = (
            f'<div class="banner"><h1>{html.escape(name or "Your Name")}</h1>'
            f'<div class="meta">{contact}</div></div>'
        )
    else:
        head = (
            f"<h1>{html.escape(name or 'Your Name')}</h1>"
            f'<div class="meta">{contact}</div>'
        )

    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<title>CV — {html.escape(name or 'Untitled')}</title>"
        f"<style>{css}</style></head><body>{head}{''.join(parts)}"
        "</body></html>"
    )
