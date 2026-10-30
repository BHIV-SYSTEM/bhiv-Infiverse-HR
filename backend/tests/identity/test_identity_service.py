"""
Tests for the Sampada identity module (BHIV vNEXT Team 2 §3/§5).

Run from the repository root:
    python -m pytest backend/tests/identity -v

Covers: idempotent Abhiruchi intake (no duplicate candidates on retry),
canonical-id rules (never invented, never overwritten), institute-code
validation/aliases, duplicate flagging WITHOUT merging, and the versioned
HackaVerse enrolment contract (idempotency key, honest SIMULATED delivery).
"""

import os
import sys

import pytest

BACKEND_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
REPO_ROOT = os.path.dirname(BACKEND_DIR)
GATEWAY_DIR = os.path.join(BACKEND_DIR, "services", "gateway")
for path in (REPO_ROOT, GATEWAY_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.identity import service  # noqa: E402
from app.identity.models import (  # noqa: E402
    INSTITUTE_CODES,
    AbhiruchiIntakeRequest,
    EnrolmentPublishRequest,
    IdentityRecord,
    normalise_institute_code,
)
from app.identity.service import InMemoryIdentityStore  # noqa: E402


@pytest.fixture()
def store():
    return InMemoryIdentityStore()


def intake(**overrides) -> AbhiruchiIntakeRequest:
    base = {
        "source_system": "abhiruchi",
        "source_reference": "ABH-0001",
        "email": "student@example.com",
        "institute_code": "MSSU",
        "cohort_id": "MSSU-2026-B1",
    }
    base.update(overrides)
    return AbhiruchiIntakeRequest(**base)


# ---------------------------------------------------------------------------
# Idempotency — retries must not create duplicate candidates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intake_creates_record_with_unknown_canonical_status(store):
    result = await service.process_intake(intake(), store)
    assert result["status"] == "created"
    rec = result["record"]
    assert rec.sampada_local_id.startswith("sd_")
    assert rec.canonical_candidate_id is None
    assert rec.canonical_id_status == "UNKNOWN"
    assert rec.institute_code == "MSSU"
    assert any("does not invent" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_repeated_intake_reuses_record(store):
    await service.process_intake(intake(), store)
    again = await service.process_intake(intake(), store)
    assert again["status"] == "reused"
    records = await store.all_records()
    assert len(records) == 1  # no duplicate candidate on retry


@pytest.mark.asyncio
async def test_replay_with_new_source_ref_but_same_canonical_id_reuses(store):
    first = await service.process_intake(
        intake(canonical_candidate_id="cc_abc123"), store
    )
    second = await service.process_intake(
        intake(
            source_reference="ABH-0002",
            canonical_candidate_id="cc_abc123",
            email="other@example.com",
        ),
        store,
    )
    assert second["status"] == "reused"
    assert second["record"].sampada_local_id == first["record"].sampada_local_id
    assert len(await store.all_records()) == 1


@pytest.mark.asyncio
async def test_canonical_id_mismatch_is_not_applied(store):
    await service.process_intake(
        intake(source_reference="ABH-0009", canonical_candidate_id="cc_one"), store
    )
    result = await service.process_intake(
        intake(source_reference="ABH-0009", canonical_candidate_id="cc_two"), store
    )
    rec = result["record"]
    assert rec.canonical_candidate_id == "cc_one"
    assert any("mismatch" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_canonical_id_marked_verified_when_supplied(store):
    result = await service.process_intake(
        intake(canonical_candidate_id="cc_ok"), store
    )
    rec = result["record"]
    assert rec.canonical_id_status == "VERIFIED"
    assert rec.canonical_candidate_id == "cc_ok"


# ---------------------------------------------------------------------------
# Institute codes
# ---------------------------------------------------------------------------


def test_institute_codes_are_the_agreed_five():
    assert set(INSTITUTE_CODES) == {"MSSU", "DYP-A", "SIT-S", "SIPS-S", "VJTI"}


def test_rtmssu_alias_normalises_to_mssu():
    assert normalise_institute_code("rtmssu") == "MSSU"
    assert normalise_institute_code("MSSU") == "MSSU"


def test_invalid_institute_code_rejected():
    with pytest.raises(ValueError):
        normalise_institute_code("MIT")


@pytest.mark.asyncio
async def test_intake_rejects_invalid_institute(store):
    with pytest.raises(ValueError):
        await service.process_intake(intake(institute_code="HARVARD"), store)


# ---------------------------------------------------------------------------
# Duplicate flagging — never merge on email
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_email_different_source_refs_flagged_not_merged(store):
    a = await service.process_intake(intake(source_reference="ABH-100"), store)
    b = await service.process_intake(intake(source_reference="ABH-101"), store)

    records = await store.all_records()
    assert len(records) == 2  # distinct records, NOT merged
    assert a["record"].sampada_local_id != b["record"].sampada_local_id

    rec_a = await store.get_by_local_id(a["record"].sampada_local_id)
    rec_b = await store.get_by_local_id(b["record"].sampada_local_id)
    assert rec_b.sampada_local_id in rec_a.possible_duplicate_of
    assert rec_a.sampada_local_id in rec_b.possible_duplicate_of
    assert any("NOT merged" in w for w in b["warnings"])


@pytest.mark.asyncio
async def test_different_institute_same_email_not_flagged(store):
    await service.process_intake(intake(source_reference="ABH-200"), store)
    other = await service.process_intake(
        intake(source_reference="ABH-201", institute_code="VJTI"), store
    )
    assert other["record"].possible_duplicate_of == []


# ---------------------------------------------------------------------------
# Conflicting replays — nothing is silently discarded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_with_conflicting_email_warns_and_keeps_original(store):
    await service.process_intake(intake(source_reference="ABH-300"), store)
    result = await service.process_intake(
        intake(source_reference="ABH-300", email="changed@example.com"), store
    )
    assert result["status"] == "reused"
    assert result["record"].email == "student@example.com"  # first value wins
    assert any("email differs" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_replay_with_conflicting_institute_warns_and_keeps_original(store):
    await service.process_intake(intake(source_reference="ABH-301"), store)
    result = await service.process_intake(
        intake(source_reference="ABH-301", institute_code="VJTI"), store
    )
    assert result["status"] == "reused"
    assert result["record"].institute_code == "MSSU"  # first value wins
    assert any("institute_code" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_replay_with_conflicting_cohort_warns_and_keeps_original(store):
    await service.process_intake(intake(source_reference="ABH-302"), store)
    result = await service.process_intake(
        intake(source_reference="ABH-302", cohort_id="MSSU-2026-B9"), store
    )
    assert result["status"] == "reused"
    assert result["record"].cohort_id == "MSSU-2026-B1"  # first value wins
    assert any("cohort_id" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# Concurrency — racing intakes converge on one record (unique-index behaviour)
# ---------------------------------------------------------------------------


class RacingStore(InMemoryIdentityStore):
    """Simulates the Mongo unique-index race: the concurrent writer's record
    is invisible to our first lookup but present at insert time, so our save
    hits StoreConflictError."""

    def __init__(self):
        super().__init__()
        self._miss_once = True

    async def get_by_source_ref(self, source_system, source_reference):
        if self._miss_once:
            self._miss_once = False
            return None  # racing writer has not committed yet
        return await super().get_by_source_ref(source_system, source_reference)

    async def save_record(self, record):
        for other in self._records.values():
            if (
                other.source_system == record.source_system
                and record.source_reference in other.source_references
                and other.sampada_local_id != record.sampada_local_id
            ):
                raise service.StoreConflictError(
                    "unique index (source_system, source_references) violated"
                )
        return await super().save_record(record)


@pytest.mark.asyncio
async def test_racing_duplicate_intake_converges_on_winner():
    racing = RacingStore()

    # The concurrent writer's record lands first (the "winner").
    winner = IdentityRecord(
        sampada_local_id="sd_winner000000001",
        institute_code="MSSU",
        source_system="abhiruchi",
        source_reference="ABH-401",
        email="student@example.com",
        source_references=["ABH-401"],
    )
    await racing.save_record(winner)
    assert winner.sampada_local_id in {
        r.sampada_local_id for r in await racing.all_records()
    }

    # Our intake misses the winner on lookup, tries to create a second record,
    # is rejected by the unique index — it must converge, not duplicate.
    result = await service.process_intake(intake(source_reference="ABH-401"), racing)
    assert result["status"] == "reused"
    assert result["record"].sampada_local_id == "sd_winner000000001"
    assert any("concurrent duplicate intake" in w for w in result["warnings"])
    assert len(await racing.all_records()) == 1  # still exactly one candidate


# ---------------------------------------------------------------------------
# Enrolment contract (Sampada -> HackaVerse)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_envelope_is_versioned_and_idempotent(store):
    result = await service.process_intake(intake(), store)
    env1 = service.build_enrolment_envelope(result["record"])
    env2 = service.build_enrolment_envelope(result["record"])

    payload = env1.model_dump()
    assert payload["schema_version"] == "1.0"
    assert payload["event"] == "candidate.enrolment.v1"
    assert payload["source_system"] == "sampada"
    assert payload["idempotency_key"] == env2.idempotency_key  # stable on retry
    assert payload["identity_policy"] == "email_is_linking_attribute_only"
    # Unknown canonical id stays unknown in the contract — never invented
    assert payload["candidate"]["canonical_candidate_id"] is None
    assert payload["candidate"]["canonical_id_status"] == "UNKNOWN"
    assert payload["candidate"]["sampada_local_id"].startswith("sd_")


@pytest.mark.asyncio
async def test_envelope_key_prefers_canonical_id(store):
    result = await service.process_intake(
        intake(canonical_candidate_id="cc_zz"), store
    )
    env = service.build_enrolment_envelope(result["record"])
    assert env.idempotency_key.startswith("cc_zz:")


@pytest.mark.asyncio
async def test_envelope_cohort_override(store):
    result = await service.process_intake(intake(), store)
    env = service.build_enrolment_envelope(result["record"], cohort_id="MSSU-2026-B2")
    assert env.candidate.cohort_id == "MSSU-2026-B2"
    assert env.idempotency_key.endswith(":MSSU-2026-B2")


# ---------------------------------------------------------------------------
# University / branch (added to the shared contract — optional passthrough)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_university_and_branch_stored_and_forwarded(store):
    result = await service.process_intake(
        intake(university="RTM Nagpur University (RTMSSU)", branch="Computer Engineering"),
        store,
    )
    rec = result["record"]
    assert rec.university == "RTM Nagpur University (RTMSSU)"
    assert rec.branch == "Computer Engineering"

    env = service.build_enrolment_envelope(rec)
    payload = env.model_dump()
    assert payload["candidate"]["university"] == "RTM Nagpur University (RTMSSU)"
    assert payload["candidate"]["branch"] == "Computer Engineering"


@pytest.mark.asyncio
async def test_university_and_branch_default_to_unknown(store):
    result = await service.process_intake(intake(), store)
    rec = result["record"]
    assert rec.university is None  # never invented when the source omits them
    assert rec.branch is None
    payload = service.build_enrolment_envelope(rec).model_dump()
    assert payload["candidate"]["university"] is None
    assert payload["candidate"]["branch"] is None


@pytest.mark.asyncio
async def test_replay_fills_missing_university_but_never_overwrites(store):
    await service.process_intake(
        intake(source_reference="ABH-601", branch="Computer Engineering"), store
    )
    # Replay adds a previously-missing university and repeats the branch.
    filled = await service.process_intake(
        intake(
            source_reference="ABH-601",
            university="RTM Nagpur University",
            branch="Computer Engineering",
        ),
        store,
    )
    assert filled["status"] == "reused"
    assert filled["record"].university == "RTM Nagpur University"
    assert filled["record"].branch == "Computer Engineering"
    assert len(await store.all_records()) == 1  # still one candidate

    # A conflicting branch on a later replay is reported, never applied.
    conflict = await service.process_intake(
        intake(source_reference="ABH-601", branch="Mechanical Engineering"), store
    )
    assert conflict["record"].branch == "Computer Engineering"
    assert any("branch" in w for w in conflict["warnings"])


@pytest.mark.asyncio
async def test_free_text_is_trimmed_and_length_capped(store):
    result = await service.process_intake(
        intake(university="  RTM Nagpur University  ", branch="   "), store
    )
    assert result["record"].university == "RTM Nagpur University"
    assert result["record"].branch is None  # whitespace-only = unknown

    with pytest.raises(ValueError):
        await service.process_intake(intake(branch="x" * 201), store)


@pytest.mark.asyncio
async def test_campus_stored_forwarded_and_never_overwritten(store):
    first = await service.process_intake(
        intake(source_reference="ABH-701", campus="Kharghar"), store
    )
    assert first["record"].campus == "Kharghar"
    payload = service.build_enrolment_envelope(first["record"]).model_dump()
    assert payload["candidate"]["campus"] == "Kharghar"

    # Replay with a different campus: flagged, original kept, still one record
    conflict = await service.process_intake(
        intake(source_reference="ABH-701", campus="Pune"), store
    )
    assert conflict["record"].campus == "Kharghar"
    assert any("campus" in w for w in conflict["warnings"])
    assert len(await store.all_records()) == 1

    # No campus supplied -> stays unknown (never invented)
    fresh = await service.process_intake(intake(source_reference="ABH-702"), store)
    assert fresh["record"].campus is None


@pytest.mark.asyncio
async def test_institute_may_be_unknown_but_never_invalid(store):
    result = await service.process_intake(
        intake(institute_code=None), store
    )
    assert result["record"].institute_code is None  # unknown stays unknown
    with pytest.raises(ValueError):
        await service.process_intake(
            intake(source_reference="ABH-602", institute_code="NOT-A-CODE"), store
        )


# ---------------------------------------------------------------------------
# API endpoints (FastAPI TestClient, memory store, API-key auth)
# ---------------------------------------------------------------------------

API_SECRET = "identity-test-secret"


@pytest.fixture()
def api_client(monkeypatch, store):
    import jwt_auth
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.identity.routes as identity_routes

    monkeypatch.setattr(jwt_auth, "API_KEY_SECRET", API_SECRET)
    monkeypatch.setattr(service, "get_identity_store", _async_return(store))

    app = FastAPI()
    app.include_router(identity_routes.router, prefix="/api/v1")
    return TestClient(app)


def _async_return(value):
    async def _inner():
        return value

    return _inner


AUTH = {"Authorization": f"Bearer {API_SECRET}"}


def test_intake_requires_api_key(api_client):
    resp = api_client.post(
        "/api/v1/identity/abhiruchi/intake", json=institute_payload()
    )
    assert resp.status_code == 401


def institute_payload(**overrides):
    payload = {
        "source_system": "abhiruchi",
        "source_reference": "ABH-777",
        "email": "api@example.com",
        "institute_code": "VJTI",
        "cohort_id": "VJTI-2026-A",
    }
    payload.update(overrides)
    return payload


def test_intake_endpoint_created_then_reused(api_client):
    first = api_client.post(
        "/api/v1/identity/abhiruchi/intake", json=institute_payload(), headers=AUTH
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "created"
    assert first.json()["storage_mode"] == "memory"

    second = api_client.post(
        "/api/v1/identity/abhiruchi/intake", json=institute_payload(), headers=AUTH
    )
    assert second.json()["status"] == "reused"


def test_intake_endpoint_rejects_bad_institute(api_client):
    resp = api_client.post(
        "/api/v1/identity/abhiruchi/intake",
        json=institute_payload(institute_code="NOT-A-CODE"),
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert "institute" in resp.json()["detail"].lower()


def test_candidate_lookup_and_404(api_client):
    created = api_client.post(
        "/api/v1/identity/abhiruchi/intake", json=institute_payload(), headers=AUTH
    ).json()
    local_id = created["record"]["sampada_local_id"]

    ok = api_client.get(f"/api/v1/identity/candidate/{local_id}", headers=AUTH)
    assert ok.status_code == 200
    assert ok.json()["record"]["sampada_local_id"] == local_id
    assert ok.json()["read_only"] is True

    missing = api_client.get("/api/v1/identity/candidate/sd_nope", headers=AUTH)
    assert missing.status_code == 404


def test_publish_dry_run_is_simulated_never_live(api_client):
    created = api_client.post(
        "/api/v1/identity/abhiruchi/intake", json=institute_payload(), headers=AUTH
    ).json()
    local_id = created["record"]["sampada_local_id"]

    resp = api_client.post(
        "/api/v1/identity/enrolment/publish",
        json={"sampada_local_id": local_id, "dry_run": True, "trace_id": "trace-t2-1"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delivery"] == "SIMULATED"  # honest status — not LIVE
    assert body["envelope"]["event"] == "candidate.enrolment.v1"
    assert body["envelope"]["trace_id"] == "trace-t2-1"

    # Stored envelope can be retrieved by its idempotency key
    key = body["envelope"]["idempotency_key"]
    got = api_client.get(f"/api/v1/identity/enrolment/{key}", headers=AUTH)
    assert got.status_code == 200
    assert got.json()["envelope"]["idempotency_key"] == key


def test_publish_unknown_candidate_404(api_client):
    resp = api_client.post(
        "/api/v1/identity/enrolment/publish",
        json={"sampada_local_id": "sd_missing"},
        headers=AUTH,
    )
    assert resp.status_code == 404


def test_institutes_endpoint_lists_agreed_codes(api_client):
    resp = api_client.get("/api/v1/identity/institutes", headers=AUTH)
    assert resp.status_code == 200
    assert set(resp.json()["institute_codes"]) == set(INSTITUTE_CODES)
    assert resp.json()["aliases"]["RTMSSU"] == "MSSU"
