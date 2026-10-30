"""
Models for the Sampada identity module.

Contract version: ``schema_version = "1.0"`` (shared with Rudra/Sanskar —
bump only with agreement; dependent changes must not merge before the
contract version is agreed).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"
ENROLMENT_EVENT = "candidate.enrolment.v1"

# Only these institute codes are valid (BHIV vNEXT §5). MSSU is canonical
# for RTMSSU — RTMSSU is accepted as an alias and normalised to MSSU.
INSTITUTE_CODES = ("MSSU", "DYP-A", "SIT-S", "SIPS-S", "VJTI")
INSTITUTE_ALIASES = {"RTMSSU": "MSSU", "RTM-MSSU": "MSSU"}

# Canonical-id status vocabulary — no new statuses invented.
# VERIFIED   : authoritative source supplied the canonical candidate_id
# UNKNOWN    : no canonical id yet (Sampada does NOT invent one)
CANONICAL_ID_VERIFIED = "VERIFIED"
CANONICAL_ID_UNKNOWN = "UNKNOWN"
CANONICAL_ID_STATUSES = (CANONICAL_ID_VERIFIED, CANONICAL_ID_UNKNOWN)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalise_institute_code(code: Optional[str]) -> Optional[str]:
    """Return a valid institute code, ``None`` when genuinely unknown, or raise.

    Empty/absent means the source did not state an institute — recorded as
    unknown rather than guessed (BHIV vNEXT: unknowns stay UNKNOWN). Any
    non-empty value outside the agreed five is rejected.
    """
    raw = (code or "").strip().upper()
    if not raw:
        return None
    raw = INSTITUTE_ALIASES.get(raw, raw)
    if raw not in INSTITUTE_CODES:
        raise ValueError(
            f"Invalid institute_code '{code}'. Allowed: {', '.join(INSTITUTE_CODES)} "
            f"(MSSU is canonical for RTMSSU)"
        )
    return raw


def _clean_optional(value: Optional[str], field: str) -> Optional[str]:
    """Trim a free-text contract field; empty becomes None (never invented)."""
    text = (value or "").strip()
    if not text:
        return None
    if len(text) > 200:
        raise ValueError(f"{field} exceeds 200 characters")
    return text


class IdentityRecord(BaseModel):
    """One candidate identity as Sampada holds it."""

    sampada_local_id: str
    canonical_candidate_id: Optional[str] = None
    canonical_id_status: str = CANONICAL_ID_UNKNOWN
    email: Optional[str] = None  # linking attribute ONLY, never an identity key
    institute_code: Optional[str] = None
    # University name and academic branch/stream as the source stated them
    # (e.g. "RTM Nagpur University (RTMSSU)", "Computer Engineering").
    # Optional passthrough — never derived or guessed by Sampada.
    university: Optional[str] = None
    branch: Optional[str] = None
    # Campus/location as the student selected it in Abhiruchi (e.g. "Kharghar",
    # "Pune"). Free text, optional, never derived — campuses are not part of
    # the institute-code vocabulary.
    campus: Optional[str] = None
    cohort_id: Optional[str] = None
    # Passthrough of the source system's own status; None when unknown.
    lifecycle_status: Optional[str] = None
    source_system: str = "abhiruchi"
    source_reference: str
    trace_id: Optional[str] = None
    schema_version: str = SCHEMA_VERSION
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    # Every intake that touched this record (idempotent retries collapse here)
    source_references: List[str] = []
    # Set when another record shares email+institute — review, never auto-merge
    possible_duplicate_of: List[str] = []


class AbhiruchiIntakeRequest(BaseModel):
    """Abhiruchi -> Sampada handoff payload."""

    source_system: str = "abhiruchi"
    source_reference: str = Field(..., min_length=1, max_length=200)
    trace_id: Optional[str] = None
    email: Optional[str] = None
    # Optional: an unknown institute stays unknown rather than being guessed.
    institute_code: Optional[str] = None
    # University and branch exactly as the candidate stated them in Abhiruchi.
    university: Optional[str] = None
    branch: Optional[str] = None
    # Campus the student picked in the Abhiruchi intake dropdown.
    campus: Optional[str] = None
    cohort_id: Optional[str] = None
    # Canonical id only when the authoritative source supplies it.
    canonical_candidate_id: Optional[str] = None
    # Source's own lifecycle status, passed through untouched (never invented).
    lifecycle_status: Optional[str] = None


class IntakeResponse(BaseModel):
    status: str  # "created" | "reused"
    record: IdentityRecord
    warnings: List[str] = []
    storage_mode: str = "memory"
    trace_id: Optional[str] = None


class EnrolmentCandidate(BaseModel):
    canonical_candidate_id: Optional[str] = None
    canonical_id_status: str = CANONICAL_ID_UNKNOWN
    sampada_local_id: str
    email: Optional[str] = None
    institute_code: Optional[str] = None
    university: Optional[str] = None
    branch: Optional[str] = None
    campus: Optional[str] = None
    cohort_id: Optional[str] = None
    lifecycle_status: Optional[str] = None


class EnrolmentEnvelope(BaseModel):
    """Versioned Sampada -> HackaVerse enrolment contract (v1.0)."""

    schema_version: str = SCHEMA_VERSION
    event: str = ENROLMENT_EVENT
    source_system: str = "sampada"
    trace_id: Optional[str] = None
    idempotency_key: str
    sent_at: str = Field(default_factory=utc_now_iso)
    candidate: EnrolmentCandidate
    # Identity guarantee note consumed by downstream systems
    identity_policy: str = "email_is_linking_attribute_only"


class EnrolmentPublishRequest(BaseModel):
    sampada_local_id: str
    cohort_id: Optional[str] = None  # overrides record cohort when provided
    trace_id: Optional[str] = None
    # When true, never call HackaVerse — returns the contract only.
    dry_run: bool = False


class EnrolmentPublishResponse(BaseModel):
    delivery: str  # "LIVE" | "SIMULATED" | "FAILED"
    envelope: EnrolmentEnvelope
    hackaverse_status: Optional[int] = None
    detail: Optional[str] = None
    storage_mode: str = "memory"
