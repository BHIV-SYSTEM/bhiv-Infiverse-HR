"""
CV content model (single source of truth shared by all three templates).

Changing a template only changes rendering — it can never erase content,
because templates never store data; they only style this model.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

CV_SCHEMA_VERSION = "cv-1.0"

TEMPLATES = ("professional", "modern", "minimal")
TEMPLATE_LABELS = {
    "professional": "Professional — clean, conventional, suitable for general employment",
    "modern": "Modern — contemporary layout with flexible sections and typography",
    "minimal": "Minimal — simple, compact, readable and easy to print",
}

SECTION_TYPES = (
    "header",
    "summary",
    "education",
    "experience",
    "skills",
    "projects",
    "certifications",
    "languages",
    "awards",
    "references",
    "custom",
)

# Sections users may add/remove/reorder/rename freely (header stays first).
OPTIONAL_SECTION_TYPES = (
    "summary",
    "experience",
    "education",
    "skills",
    "projects",
    "certifications",
    "languages",
    "awards",
    "references",
    "custom",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CVEntry(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:10])
    title: Optional[str] = None  # e.g. role, degree, skill group
    subtext: Optional[str] = None  # e.g. organisation, dates
    text: Optional[str] = None  # main body
    url: Optional[str] = None


class CVSection(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:10])
    type: str = "custom"
    heading: str = "Section"
    visible: bool = True
    entries: List[CVEntry] = []


class CVContent(BaseModel):
    """The one content model behind every template."""

    schema_version: str = CV_SCHEMA_VERSION
    sections: List[CVSection] = []

    def section_by_id(self, section_id: str) -> Optional[CVSection]:
        for s in self.sections:
            if s.id == section_id:
                return s
        return None


def default_content() -> CVContent:
    """Blank, honest skeleton — no fabricated text anywhere."""
    return CVContent(
        sections=[
            CVSection(id="header", type="header", heading="Header", entries=[CVEntry()]),
            CVSection(id="summary", type="summary", heading="Professional Summary"),
            CVSection(id="experience", type="experience", heading="Work Experience"),
            CVSection(id="education", type="education", heading="Education"),
            CVSection(id="skills", type="skills", heading="Skills"),
            CVSection(id="projects", type="projects", heading="Projects"),
            CVSection(id="certifications", type="certifications", heading="Certifications"),
            CVSection(id="languages", type="languages", heading="Languages"),
            CVSection(id="awards", type="awards", heading="Awards & Honours"),
            CVSection(id="references", type="references", heading="References"),
        ]
    )


class PrefillField(BaseModel):
    """Provenance wrapper: where this value came from, and whether it is verified."""

    value: Optional[Any] = None
    source: str = "none"  # "profile" | "none"
    verified: bool = False


# Short, relevant questions shown only for fields with no authorised source.
# The user supplies or confirms every one of these — nothing is guessed.
MISSING_QUESTIONS: List[Dict[str, Any]] = [
    {
        "field": "professional_summary",
        "question": "Write 2–4 sentences summarising your professional profile. "
        "This is your wording — we will not write it for you.",
        "required": True,
        "kind": "long_text",
    },
    {
        "field": "education",
        "question": "Add your education: institution, qualification, and years.",
        "required": False,
        "kind": "entries",
    },
    {
        "field": "experience",
        "question": "Add work experience: role, organisation, dates, and what you did.",
        "required": False,
        "kind": "entries",
    },
    {
        "field": "projects",
        "question": "Add projects or portfolio links you want to showcase.",
        "required": False,
        "kind": "entries",
    },
    {
        "field": "certifications",
        "question": "Add certifications, if any.",
        "required": False,
        "kind": "entries",
    },
    {
        "field": "languages",
        "question": "Add languages you speak and your proficiency.",
        "required": False,
        "kind": "entries",
    },
    {
        "field": "awards",
        "question": "Add awards or honours, if any.",
        "required": False,
        "kind": "entries",
    },
    {
        "field": "references",
        "question": "Add references only if you choose to provide them.",
        "required": False,
        "kind": "entries",
    },
]


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class DraftCreate(BaseModel):
    candidate_id: Optional[str] = None
    template: str = "professional"
    content: Optional[CVContent] = None


class DraftUpdate(BaseModel):
    template: Optional[str] = None
    content: Optional[CVContent] = None
    # Setting true records an explicit approval of the current content.
    approve: bool = False


class SuggestRequest(BaseModel):
    text: str = ""
    field_ref: str = "text"


class AssembleRequest(BaseModel):
    content: CVContent
    template: str = "professional"


class ExportRequest(BaseModel):
    draft_id: str


class ApproveRequest(BaseModel):
    # Explicit human confirmation phrase — approval must be deliberate.
    confirmation: str
