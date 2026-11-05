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
import os
import uuid
from typing import Any, Dict, List, Optional

try:  # pragma: no cover - exercised only in Mongo mode
    from pymongo.errors import DuplicateKeyError
except ImportError:  # pragma: no cover - pymongo always present with motor
    class DuplicateKeyError(Exception):  # type: ignore[no-redef]
        """Stand-in so a Mongo-less environment still imports cleanly."""

from .models import (
    CANONICAL_ID_QUARANTINED,
    CANONICAL_ID_UNKNOWN,
    CANONICAL_ID_VERIFIED,
    IDENTITY_AMBIGUOUS_EVENT,
    AbhiruchiIntakeRequest,
    EnrolmentCandidate,
    EnrolmentEnvelope,
    IdentityRecord,
    _clean_optional,
    normalise_institute_code,
    utc_now_iso,
)

logger = logging.getLogger(__name__)


class StoreConflictError(Exception):
    """A concurrent writer created the same identity first.

    Raised when a unique index rejects an insert — i.e. two racing intakes
    for the same ``(source_system, source_reference)``. ``process_intake``
    catches this and converges on the winner's record, so a racing retry can
    never leave two candidates for one source reference.
    """


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------


class InMemoryIdentityStore:
    """Process-local store — used when Mongo is unavailable and in tests."""

    mode = "memory"

    def __init__(self) -> None:
        self._records: Dict[str, IdentityRecord] = {}
        self._enrolments: Dict[str, Dict[str, Any]] = {}
        self._quarantines: Dict[str, Dict[str, Any]] = {}

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

    async def save_quarantine(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        self._quarantines[str(doc["quarantine_id"])] = doc
        return doc

    async def get_quarantine(self, quarantine_id: str) -> Optional[Dict[str, Any]]:
        return self._quarantines.get(quarantine_id)

    async def list_quarantines(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        docs = list(self._quarantines.values())
        if status:
            docs = [d for d in docs if d.get("status") == status]
        return sorted(docs, key=lambda d: d.get("created_at") or "")


class MongoIdentityStore:
    """MongoDB-backed store (``identity`` / ``identity_enrolments``)."""

    mode = "mongo"

    RECORDS = "identity"
    ENROLMENTS = "identity_enrolments"
    QUARANTINES = "identity_quarantine"

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
        try:
            await self._db[self.RECORDS].update_one(
                {"sampada_local_id": record.sampada_local_id},
                {"$set": record.model_dump()},
                upsert=True,
            )
        except DuplicateKeyError as exc:
            # Unique index (sampada_local_id / source ref / canonical id)
            # rejected the insert — a concurrent writer won the race.
            raise StoreConflictError(str(exc)) from exc
        return record

    async def save_enrolment(self, key: str, payload: Dict[str, Any]) -> None:
        try:
            await self._db[self.ENROLMENTS].update_one(
                {"idempotency_key": key},
                {"$set": {"idempotency_key": key, "payload": payload, "saved_at": utc_now_iso()}},
                upsert=True,
            )
        except DuplicateKeyError:
            # Another writer stored the same key first — first envelope wins,
            # mirroring HackaVerse's ingest semantics. Never overwrite it.
            logger.info("[IDENTITY] concurrent enrolment write for %s — keeping the stored envelope", key)

    async def get_enrolment(self, key: str) -> Optional[Dict[str, Any]]:
        doc = await self._db[self.ENROLMENTS].find_one({"idempotency_key": key})
        return doc.get("payload") if doc else None

    async def save_quarantine(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        await self._db[self.QUARANTINES].update_one(
            {"quarantine_id": doc["quarantine_id"]},
            {"$set": doc},
            upsert=True,
        )
        return doc

    async def get_quarantine(self, quarantine_id: str) -> Optional[Dict[str, Any]]:
        doc = await self._db[self.QUARANTINES].find_one({"quarantine_id": quarantine_id})
        if doc:
            doc.pop("_id", None)
        return doc

    async def list_quarantines(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        query: Dict[str, Any] = {}
        if status:
            query["status"] = status
        docs = await (
            self._db[self.QUARANTINES].find(query).sort("created_at", 1).to_list(length=200)
        )
        for d in docs:
            d.pop("_id", None)
        return docs


_MEMORY_STORE = InMemoryIdentityStore()

_MONGO_INDEXES_DONE = False
_MONGO_INDEX_WARNED = False


async def _ensure_mongo_indexes(db) -> None:
    """Unique indexes that make idempotency real under concurrency.

    Without them, find-then-save races can mint two records for one source
    reference and upserts on ``idempotency_key`` can duplicate. Best-effort:
    if pre-existing duplicate documents block creation, the failure is logged
    once (they must be reconciled before the index can be built).
    """
    global _MONGO_INDEXES_DONE, _MONGO_INDEX_WARNED
    if _MONGO_INDEXES_DONE:
        return
    _MONGO_INDEXES_DONE = True  # attempt once per process
    plans = [
        (MongoIdentityStore.RECORDS,
         [("sampada_local_id", 1)],
         {"unique": True, "name": "sampada_local_id_unique"}),
        # Multikey: the same source reference may appear in only one record,
        # which is exactly the replay/idempotency guarantee.
        (MongoIdentityStore.RECORDS,
         [("source_system", 1), ("source_references", 1)],
         {"unique": True, "name": "source_ref_unique"}),
        # Canonical ids are globally unique when present (nulls exempted).
        (MongoIdentityStore.RECORDS,
         [("canonical_candidate_id", 1)],
         {"unique": True, "name": "canonical_id_unique",
          "partialFilterExpression": {"canonical_candidate_id": {"$type": "string"}}}),
        (MongoIdentityStore.ENROLMENTS,
         [("idempotency_key", 1)],
         {"unique": True, "name": "idempotency_key_unique"}),
    ]
    for collection, keys, opts in plans:
        try:
            await db[collection].create_index(keys, **opts)
        except Exception as exc:
            if not _MONGO_INDEX_WARNED:
                _MONGO_INDEX_WARNED = True
                logger.warning(
                    "[IDENTITY] could not ensure unique index on %s: %s "
                    "(pre-existing duplicates must be reconciled)",
                    collection, exc,
                )


async def get_identity_store():
    """Mongo when reachable, otherwise the in-process store (reported honestly)."""
    try:
        from app.database import get_mongo_db

        db = await get_mongo_db()
        if db is not None:
            await _ensure_mongo_indexes(db)
            return MongoIdentityStore(db)
    except Exception as exc:  # pragma: no cover - depends on runtime env
        logger.info("[IDENTITY] Mongo unavailable, using memory store: %s", exc)
    return _MEMORY_STORE


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------


def new_local_id() -> str:
    return f"sd_{uuid.uuid4().hex[:16]}"


def _merge_replay(
    req: AbhiruchiIntakeRequest,
    record: IdentityRecord,
    institute: Optional[str],
    warnings: List[str],
    conflicts: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Apply a replayed intake onto an existing record.

    Fills only what is missing and never overwrites a persisted value. Any
    conflicting supplied value is reported as a warning — nothing is
    silently discarded. Identity-critical conflicts (canonical id) are also
    appended to ``conflicts`` so the caller can quarantine the record
    (Phase 5D P1-6).
    """
    if conflicts is None:
        conflicts = []
    if req.source_reference not in record.source_references:
        record.source_references.append(req.source_reference)

    # Fill previously-unknown attributes only; never overwrite a VERIFIED
    # canonical id with a different value.
    if req.canonical_candidate_id and not record.canonical_candidate_id:
        if record.canonical_id_status != CANONICAL_ID_QUARANTINED:
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
        conflicts.append(
            {
                "type": "canonical_id_mismatch",
                "persisted_canonical_id": record.canonical_candidate_id,
                "supplied_canonical_id": req.canonical_candidate_id,
            }
        )
    if req.email:
        if not record.email:
            record.email = req.email
        elif req.email.strip().lower() != record.email.strip().lower():
            warnings.append(
                "email differs from the persisted record — kept the existing "
                "value; conflicting value not applied"
            )
    if institute:
        if not record.institute_code:
            record.institute_code = institute
        elif institute != record.institute_code:
            warnings.append(
                f"institute_code '{institute}' differs from persisted "
                f"'{record.institute_code}' — kept the existing value; "
                "conflicting value not applied"
            )
    # University / branch / campus follow the same fill-missing-never-overwrite rule.
    for label, supplied, persisted in (
        ("university", req.university, record.university),
        ("branch", req.branch, record.branch),
        ("campus", req.campus, record.campus),
    ):
        if not supplied:
            continue
        if not persisted:
            if label == "university":
                record.university = supplied
            elif label == "branch":
                record.branch = supplied
            else:
                record.campus = supplied
        elif supplied.strip().lower() != persisted.strip().lower():
            warnings.append(
                f"{label} '{supplied.strip()}' differs from persisted "
                f"'{persisted}' — kept the existing value; conflicting "
                "value not applied"
            )
    if req.cohort_id:
        if not record.cohort_id:
            record.cohort_id = req.cohort_id
        elif req.cohort_id != record.cohort_id:
            warnings.append(
                f"cohort_id '{req.cohort_id}' differs from persisted "
                f"'{record.cohort_id}' — kept the existing value; "
                "conflicting value not applied"
            )
    if record.lifecycle_status is None and req.lifecycle_status is not None:
        record.lifecycle_status = req.lifecycle_status  # passthrough only
    if req.trace_id and not record.trace_id:
        record.trace_id = req.trace_id


async def quarantine_record(
    store,
    record: IdentityRecord,
    *,
    reason: str,
    conflicts: List[Dict[str, Any]],
    trace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Freeze an identity with conflicting authoritative evidence (P1-6).

    Sets ``canonical_id_status = QUARANTINED`` (never invents/overwrites the
    canonical id itself), writes a doc to ``identity_quarantine``, and emits
    an ``identity.ambiguous.v1`` event carrying the same honest delivery
    semantics as enrolment (stored first; optional outbound webhook).
    """
    previous_status = record.canonical_id_status
    if record.canonical_id_status != CANONICAL_ID_QUARANTINED:
        record.canonical_id_status = CANONICAL_ID_QUARANTINED
        await store.save_record(record)

    quarantine_id = f"qtn-{uuid.uuid4().hex[:16]}"
    event = {
        "event": IDENTITY_AMBIGUOUS_EVENT,
        "schema_version": "1.0",
        "quarantine_id": quarantine_id,
        "sampada_local_id": record.sampada_local_id,
        "canonical_candidate_id": record.canonical_candidate_id,
        "email_linking_attribute": record.email,
        "institute_code": record.institute_code,
        "reason": reason,
        "conflicts": conflicts,
        "detected_at": utc_now_iso(),
        "trace_id": trace_id or record.trace_id,
        "merge_policy": "email_is_linking_attribute_only",
    }

    delivery = "SIMULATED"
    detail = "IDENTITY_AMBIGUOUS_URL not configured — event stored, not delivered"
    target = (os.getenv("IDENTITY_AMBIGUOUS_URL") or "").strip()
    if target:
        try:
            import httpx

            headers = {"Content-Type": "application/json"}
            api_key = (os.getenv("IDENTITY_AMBIGUOUS_API_KEY") or "").strip()
            if api_key:
                headers["X-API-Key"] = api_key
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(target, json=event, headers=headers)
            if 200 <= resp.status_code < 300:
                delivery = "LIVE"
                detail = f"accepted ({resp.status_code})"
            else:
                delivery = "FAILED"
                detail = f"webhook responded {resp.status_code}"
        except Exception as exc:  # honest failure — never fake LIVE
            delivery = "FAILED"
            detail = f"delivery error: {exc}"
    event["delivery"] = delivery
    event["delivery_detail"] = detail

    doc = {
        "quarantine_id": quarantine_id,
        "status": "open",
        "reason": reason,
        "conflicts": conflicts,
        "previous_canonical_id_status": previous_status,
        "record": {
            "sampada_local_id": record.sampada_local_id,
            "canonical_candidate_id": record.canonical_candidate_id,
            "email": record.email,
            "institute_code": record.institute_code,
        },
        "event": event,
        "created_at": utc_now_iso(),
    }
    await store.save_quarantine(doc)
    logger.warning(
        "[IDENTITY] quarantine %s for %s: %s (event delivery=%s)",
        quarantine_id,
        record.sampada_local_id,
        reason,
        delivery,
    )
    return doc


async def process_intake(
    req: AbhiruchiIntakeRequest, store
) -> Dict[str, Any]:
    """Idempotent Abhiruchi handoff.

    Resolution order (never merges on email):
    1. authoritative ``canonical_candidate_id`` (reuse that record)
    2. same ``(source_system, source_reference)`` (retry / replay — reuse)
    3. otherwise create a NEW Sampada-local record (canonical id stays UNKNOWN)

    Identity-critical replay conflicts quarantine the record (P1-6).
    """
    institute = normalise_institute_code(req.institute_code)
    # Trim/validate free-text passthrough; empty becomes None (never invented).
    university = _clean_optional(req.university, "university")
    branch = _clean_optional(req.branch, "branch")
    campus = _clean_optional(req.campus, "campus")
    warnings: List[str] = []
    conflicts: List[Dict[str, Any]] = []

    record: Optional[IdentityRecord] = None
    status = "created"

    if req.canonical_candidate_id:
        record = await store.get_by_canonical_id(req.canonical_candidate_id)

    if record is None:
        record = await store.get_by_source_ref(req.source_system, req.source_reference)

    if record is not None:
        status = "reused"
        # Carry cleaned values so the replay merge sees them.
        req.university, req.branch, req.campus = university, branch, campus
        _merge_replay(req, record, institute, warnings, conflicts)
    else:
        record = IdentityRecord(
            sampada_local_id=new_local_id(),
            canonical_candidate_id=req.canonical_candidate_id,
            canonical_id_status=(
                CANONICAL_ID_VERIFIED if req.canonical_candidate_id else CANONICAL_ID_UNKNOWN
            ),
            email=req.email,
            institute_code=institute,
            university=university,
            branch=branch,
            campus=campus,
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

    try:
        await store.save_record(record)
    except StoreConflictError:
        # A concurrent identical intake beat us to the insert (unique index
        # rejected ours). Converge on the winner instead of leaving two
        # records for one source reference.
        winner = await store.get_by_source_ref(
            req.source_system, req.source_reference
        )
        if winner is None and req.canonical_candidate_id:
            winner = await store.get_by_canonical_id(req.canonical_candidate_id)
        if winner is None:
            raise
        logger.info(
            "[IDENTITY] racing intake for %s/%s — converged on %s",
            req.source_system,
            req.source_reference,
            winner.sampada_local_id,
        )
        status = "reused"
        warnings.append(
            "concurrent duplicate intake detected — converged on the existing "
            "record; no duplicate candidate created"
        )
        req.university, req.branch, req.campus = university, branch, campus
        _merge_replay(req, winner, institute, warnings, conflicts)
        record = winner
        await store.save_record(record)

    # Phase 5D (P1-6): identity-critical replay conflicts freeze the record.
    quarantine_doc: Optional[Dict[str, Any]] = None
    if conflicts:
        quarantine_doc = await quarantine_record(
            store,
            record,
            reason=(
                "conflicting authoritative evidence on replay: "
                + ", ".join(sorted({c.get("type", "unknown") for c in conflicts}))
            ),
            conflicts=conflicts,
            trace_id=req.trace_id,
        )
        warnings.append(
            "identity quarantined for manual review (identity.ambiguous.v1) — "
            "enrolment publishing is blocked until resolved"
        )

    return {
        "status": status,
        "record": record,
        "warnings": warnings,
        "storage_mode": store.mode,
        "trace_id": req.trace_id,
        "quarantine": quarantine_doc,
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
            university=record.university,
            branch=record.branch,
            campus=record.campus,
            cohort_id=cohort,
            lifecycle_status=record.lifecycle_status,
        ),
    )
