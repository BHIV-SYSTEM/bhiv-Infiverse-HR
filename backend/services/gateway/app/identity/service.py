"""
Sampada identity service.

Responsibilities:
* Idempotent Abhiruchi intake: repeated/retried handoffs collapse onto one
  record — retries never create duplicate candidates.
* Canonical candidate id handling: used only when the authoritative source
  supplies it. Sampada NEVER generates or guesses a canonical id (unknowns
  stay UNKNOWN); it always issues its own ``sampada_local_id`` separately.
* Email is a linking attribute only: same email never merges two records —
  possible duplicates are *flagged* for review instead.
* Institute/cohort mapping restricted to the five agreed codes.
* Builds the versioned enrolment contract consumed by HackaVerse.

Storage is pluggable: MongoDB when available (gateway database), an
in-process store otherwise. The active mode is always reported honestly in
responses (``storage_mode``) so SIMULATED/ fallback behaviour is never
presented as LIVE.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from .models import (
    CANONICAL_ID_UNKNOWN,
    CANONICAL_ID_VERIFIED,
    AbhiruchiIntakeRequest,
    EnrolmentCandidate,
    EnrolmentEnvelope,
    IdentityRecord,
    normalise_institute_code,
    utc_now_iso,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------


class InMemoryIdentityStore:
    """Process-local store — used when Mongo is unavailable and in tests."""

    mode = "memory"

    def __init__(self) -> None:
        self._records: Dict[str, IdentityRecord] = {}
        self._enrolments: Dict[str, Dict[str, Any]] = {}

    async def get_by_local_id(self, local_id: str) -> Optional[IdentityRecord]:
        return self._records.get(local_id)

    async def get_by_canonical_id(self, canonical_id: str) -> Optional[IdentityRecord]:
        for rec in self._records.values():
            if rec.canonical_candidate_id == canonical_id:
                return rec
        return None

    async def get_by_source_ref(
        self, source_system: str, source_reference: str
    ) -> Optional[IdentityRecord]:
        for rec in self._records.values():
            if rec.source_system == source_system and source_reference in rec.source_references:
                return rec
        return None

    async def find_by_email_institute(
        self, email: str, institute_code: Optional[str]
    ) -> List[IdentityRecord]:
        out = []
        for rec in self._records.values():
            if (rec.email or "").lower() == email.lower() and (
                institute_code is None or rec.institute_code == institute_code
            ):
                out.append(rec)
        return out

    async def all_records(self) -> List[IdentityRecord]:
        return list(self._records.values())

    async def save_record(self, record: IdentityRecord) -> IdentityRecord:
        record.updated_at = utc_now_iso()
        self._records[record.sampada_local_id] = record
        return record

    async def save_enrolment(self, key: str, payload: Dict[str, Any]) -> None:
        self._enrolments[key] = payload

    async def get_enrolment(self, key: str) -> Optional[Dict[str, Any]]:
        return self._enrolments.get(key)


class MongoIdentityStore:
    """MongoDB-backed store (``identity`` / ``identity_enrolments``)."""

    mode = "mongo"

    RECORDS = "identity"
    ENROLMENTS = "identity_enrolments"

    def __init__(self, db) -> None:
        self._db = db

    @staticmethod
    def _to_record(doc: Optional[dict]) -> Optional[IdentityRecord]:
        if not doc:
            return None
        doc = dict(doc)
        doc.pop("_id", None)
        return IdentityRecord(**doc)

    async def get_by_local_id(self, local_id: str) -> Optional[IdentityRecord]:
        return self._to_record(
            await self._db[self.RECORDS].find_one({"sampada_local_id": local_id})
        )

    async def get_by_canonical_id(self, canonical_id: str) -> Optional[IdentityRecord]:
        return self._to_record(
            await self._db[self.RECORDS].find_one({"canonical_candidate_id": canonical_id})
        )

    async def get_by_source_ref(
        self, source_system: str, source_reference: str
    ) -> Optional[IdentityRecord]:
        return self._to_record(
            await self._db[self.RECORDS].find_one(
                {"source_system": source_system, "source_references": source_reference}
            )
        )

    async def find_by_email_institute(
        self, email: str, institute_code: Optional[str]
    ) -> List[IdentityRecord]:
        query: Dict[str, Any] = {"email": {"$regex": f"^{email}$", "$options": "i"}}
        if institute_code:
            query["institute_code"] = institute_code
        docs = await self._db[self.RECORDS].find(query).to_list(length=20)
        return [r for r in (self._to_record(d) for d in docs) if r]

    async def all_records(self) -> List[IdentityRecord]:
        docs = await self._db[self.RECORDS].find({}).to_list(length=1000)
        return [r for r in (self._to_record(d) for d in docs) if r]

    async def save_record(self, record: IdentityRecord) -> IdentityRecord:
        record.updated_at = utc_now_iso()
        await self._db[self.RECORDS].update_one(
            {"sampada_local_id": record.sampada_local_id},
            {"$set": record.model_dump()},
            upsert=True,
        )
        return record

    async def save_enrolment(self, key: str, payload: Dict[str, Any]) -> None:
        await self._db[self.ENROLMENTS].update_one(
            {"idempotency_key": key},
            {"$set": {"idempotency_key": key, "payload": payload, "saved_at": utc_now_iso()}},
            upsert=True,
        )

    async def get_enrolment(self, key: str) -> Optional[Dict[str, Any]]:
        doc = await self._db[self.ENROLMENTS].find_one({"idempotency_key": key})
        return doc.get("payload") if doc else None


_MEMORY_STORE = InMemoryIdentityStore()


async def get_identity_store():
    """Mongo when reachable, otherwise the in-process store (reported honestly)."""
    try:
        from app.database import get_mongo_db

        db = await get_mongo_db()
        if db is not None:
            return MongoIdentityStore(db)
    except Exception as exc:  # pragma: no cover - depends on runtime env
        logger.info("[IDENTITY] Mongo unavailable, using memory store: %s", exc)
    return _MEMORY_STORE


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------


def new_local_id() -> str:
    return f"sd_{uuid.uuid4().hex[:16]}"


async def process_intake(
    req: AbhiruchiIntakeRequest, store
) -> Dict[str, Any]:
    """Idempotent Abhiruchi handoff.

    Resolution order (never merges on email):
    1. authoritative ``canonical_candidate_id`` (reuse that record)
    2. same ``(source_system, source_reference)`` (retry / replay — reuse)
    3. otherwise create a NEW Sampada-local record (canonical id stays UNKNOWN)
    """
    institute = normalise_institute_code(req.institute_code)
    warnings: List[str] = []

    record: Optional[IdentityRecord] = None
    status = "created"

    if req.canonical_candidate_id:
        record = await store.get_by_canonical_id(req.canonical_candidate_id)

    if record is None:
        record = await store.get_by_source_ref(req.source_system, req.source_reference)

    if record is not None:
        status = "reused"
        if req.source_reference not in record.source_references:
            record.source_references.append(req.source_reference)
        # Fill previously-unknown attributes only; never overwrite a VERIFIED
        # canonical id with a different value.
        if req.canonical_candidate_id and not record.canonical_candidate_id:
            record.canonical_candidate_id = req.canonical_candidate_id
            record.canonical_id_status = CANONICAL_ID_VERIFIED
        elif (
            req.canonical_candidate_id
            and record.canonical_candidate_id
            and req.canonical_candidate_id != record.canonical_candidate_id
        ):
            warnings.append(
                "canonical_candidate_id mismatch with existing record — kept the "
                "existing one; conflicting value not applied"
            )
        if not record.email and req.email:
            record.email = req.email
        if not record.institute_code:
            record.institute_code = institute
        if not record.cohort_id and req.cohort_id:
            record.cohort_id = req.cohort_id
        if record.lifecycle_status is None and req.lifecycle_status is not None:
            record.lifecycle_status = req.lifecycle_status  # passthrough only
        if req.trace_id and not record.trace_id:
            record.trace_id = req.trace_id
    else:
        record = IdentityRecord(
            sampada_local_id=new_local_id(),
            canonical_candidate_id=req.canonical_candidate_id,
            canonical_id_status=(
                CANONICAL_ID_VERIFIED if req.canonical_candidate_id else CANONICAL_ID_UNKNOWN
            ),
            email=req.email,
            institute_code=institute,
            cohort_id=req.cohort_id,
            lifecycle_status=req.lifecycle_status,
            source_system=req.source_system,
            source_reference=req.source_reference,
            trace_id=req.trace_id,
            source_references=[req.source_reference],
        )
        if not req.canonical_candidate_id:
            warnings.append(
                "canonical_candidate_id not supplied by source — recorded as "
                "UNKNOWN; Sampada does not invent canonical ids"
            )

    # Duplicate *flagging* — never merging (email is a linking attribute).
    if record.email:
        siblings = await store.find_by_email_institute(record.email, record.institute_code)
        for other in siblings:
            if other.sampada_local_id == record.sampada_local_id:
                continue
            if other.sampada_local_id not in record.possible_duplicate_of:
                record.possible_duplicate_of.append(other.sampada_local_id)
            if record.sampada_local_id not in other.possible_duplicate_of:
                other.possible_duplicate_of.append(record.sampada_local_id)
                await store.save_record(other)
            warnings.append(
                f"possible duplicate of {other.sampada_local_id} (same email + "
                "institute) — flagged for review, NOT merged"
            )

    await store.save_record(record)

    return {
        "status": status,
        "record": record,
        "warnings": warnings,
        "storage_mode": store.mode,
        "trace_id": req.trace_id,
    }


# ---------------------------------------------------------------------------
# Enrolment contract (Sampada -> HackaVerse)
# ---------------------------------------------------------------------------


def build_enrolment_envelope(
    record: IdentityRecord, trace_id: Optional[str] = None, cohort_id: Optional[str] = None
) -> EnrolmentEnvelope:
    """Versioned, idempotent enrolment payload for HackaVerse."""
    cohort = cohort_id or record.cohort_id
    key_source = record.canonical_candidate_id or record.sampada_local_id
    idem = f"{key_source}:{cohort or 'no-cohort'}"
    return EnrolmentEnvelope(
        trace_id=trace_id or record.trace_id,
        idempotency_key=idem,
        candidate=EnrolmentCandidate(
            canonical_candidate_id=record.canonical_candidate_id,
            canonical_id_status=record.canonical_id_status,
            sampada_local_id=record.sampada_local_id,
            email=record.email,
            institute_code=record.institute_code,
            cohort_id=cohort,
            lifecycle_status=record.lifecycle_status,
        ),
    )
