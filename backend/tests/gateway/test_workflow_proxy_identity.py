"""
Audit hardening for workflow_proxy identity resolution (Phase 5D P0-2 +
independent verification findings).

Run from the repository root:
    python -m pytest backend/tests/gateway/test_workflow_proxy_identity.py -v

Covers:
  * canonical_candidate_id field lookup (was missing from the $or query)
  * multi-email / multi-document ambiguity is isolated (never first-hit)
  * QUARANTINED candidate docs and identity-store records never resolve
  * _resolve_portal_candidate_id emits the CANONICAL id in task payloads
  * jwt_auth preserves the candidate_id claim
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

import workflow_proxy  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal async Mongo test doubles
# ---------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, docs):
        self._docs = [dict(d) for d in docs]

    def sort(self, *args, **kwargs):
        return self

    def __aiter__(self):
        docs = list(self._docs)

        async def gen():
            for d in docs:
                yield d

        return gen()


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]

    @staticmethod
    def _match(doc, flt):
        for key, want in (flt or {}).items():
            if key == "$or":
                if not any(FakeCollection._match(doc, w) for w in want):
                    return False
            elif isinstance(want, dict) and "$regex" in want:
                import re

                flags = re.I if "i" in (want.get("$options") or "") else 0
                if not re.search(want["$regex"], str(doc.get(key) or ""), flags):
                    return False
            elif isinstance(doc, dict) and key in doc:
                if str(doc.get(key)) != str(want):
                    return False
            else:
                return False
        return True

    def find(self, flt=None, projection=None):
        return FakeCursor([d for d in self.docs if self._match(d, flt)])

    async def find_one(self, flt):
        for doc in self.docs:
            if self._match(doc, flt):
                return dict(doc)
        return None


class FakeDB:
    def __init__(self, candidates=None):
        self.candidates = FakeCollection(candidates)


@pytest.fixture()
def identity_store(monkeypatch):
    """Fresh in-memory identity store (isolated from the module global)."""
    import app.identity.service as identity_service

    store = identity_service.InMemoryIdentityStore()
    monkeypatch.setattr(identity_service, "get_identity_store", _async_return(store))
    return store


def _async_return(value):
    async def _inner():
        return value

    return _inner


@pytest.fixture()
def fake_db(monkeypatch, identity_store):
    import app.database as database_module

    state = {"docs": []}

    def _set(docs):
        state["docs"] = list(docs)
        db = FakeDB(state["docs"])
        monkeypatch.setattr(database_module, "get_mongo_db", _async_return(db))
        return db

    _set([])
    return _set


def auth_ctx(**overrides):
    base = {
        "type": "jwt_token",
        "user_id": "pravesh:bhiv_user_9",
        "email": "linked@example.com",
        "role": "candidate",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _resolve_candidate_document
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolves_canonical_candidate_id_field(fake_db):
    """Audit: canonical_candidate_id alone must resolve (step 3 $or)."""
    fake_db(
        [
            {
                "_id": "doc1",
                "email": "other@example.com",
                "canonical_candidate_id": "pravesh:bhiv_user_9",
            }
        ]
    )
    cand = await workflow_proxy._resolve_candidate_document(auth_ctx())
    assert cand is not None
    assert cand["_id"] == "doc1"


@pytest.mark.asyncio
async def test_multi_email_match_is_isolated_not_first_hit(fake_db):
    """Audit: >1 document on the same email must NOT silently pick the first."""
    fake_db(
        [
            {"_id": "a", "email": "linked@example.com"},
            {"_id": "b", "email": "linked@example.com"},
        ]
    )
    cand = await workflow_proxy._resolve_candidate_document(auth_ctx())
    assert cand is None


@pytest.mark.asyncio
async def test_multi_email_match_narrowed_by_canonical_id(fake_db):
    fake_db(
        [
            {"_id": "a", "email": "linked@example.com"},
            {
                "_id": "b",
                "email": "linked@example.com",
                "canonical_candidate_id": "pravesh:bhiv_user_9",
            },
        ]
    )
    cand = await workflow_proxy._resolve_candidate_document(auth_ctx())
    assert cand is not None
    assert cand["_id"] == "b"


@pytest.mark.asyncio
async def test_quarantined_candidate_doc_never_resolves(fake_db):
    fake_db(
        [
            {
                "_id": "q",
                "email": "linked@example.com",
                "canonical_id_status": "QUARANTINED",
            }
        ]
    )
    cand = await workflow_proxy._resolve_candidate_document(auth_ctx())
    assert cand is None


@pytest.mark.asyncio
async def test_quarantined_identity_store_blocks_resolution(
    fake_db, identity_store
):
    from app.identity.models import CANONICAL_ID_QUARANTINED, IdentityRecord

    fake_db([{"_id": "c", "email": "linked@example.com"}])
    await identity_store.save_record(
        IdentityRecord(
            sampada_local_id="sd_quarantine_test",
            source_system="abhiruchi",
            source_reference="ABH-Q1",
            email="linked@example.com",
            canonical_id_status=CANONICAL_ID_QUARANTINED,
        )
    )
    cand = await workflow_proxy._resolve_candidate_document(auth_ctx())
    assert cand is None


@pytest.mark.asyncio
async def test_no_match_returns_none_without_crash(fake_db):
    fake_db([])
    cand = await workflow_proxy._resolve_candidate_document(auth_ctx())
    assert cand is None


# ---------------------------------------------------------------------------
# _resolve_portal_candidate_id — canonical id in task/telemetry payloads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_portal_candidate_id_emits_canonical_id(fake_db):
    fake_db(
        [
            {
                "_id": "c",
                "email": "linked@example.com",
                "canonical_candidate_id": "cc_998877",
            }
        ]
    )
    out = await workflow_proxy._resolve_portal_candidate_id(auth_ctx(), None)
    assert out == "cc_998877"


@pytest.mark.asyncio
async def test_portal_candidate_id_falls_back_to_uid(fake_db):
    fake_db([])
    out = await workflow_proxy._resolve_portal_candidate_id(auth_ctx(), None)
    assert out == "pravesh:bhiv_user_9"


@pytest.mark.asyncio
async def test_portal_candidate_id_rejects_mismatched_query_param(fake_db):
    fake_db(
        [
            {
                "_id": "c",
                "email": "linked@example.com",
                "canonical_candidate_id": "cc_998877",
            }
        ]
    )
    with pytest.raises(Exception) as exc:
        await workflow_proxy._resolve_portal_candidate_id(auth_ctx(), "cc_000000")
    assert getattr(exc.value, "status_code", None) == 403


@pytest.mark.asyncio
async def test_portal_candidate_id_accepts_canonical_query_param(fake_db):
    fake_db(
        [
            {
                "_id": "c",
                "email": "linked@example.com",
                "canonical_candidate_id": "cc_998877",
            }
        ]
    )
    out = await workflow_proxy._resolve_portal_candidate_id(
        auth_ctx(), "cc_998877"
    )
    assert out == "cc_998877"


# ---------------------------------------------------------------------------
# jwt_auth preserves the candidate_id claim
# ---------------------------------------------------------------------------


def test_candidate_jwt_preserves_candidate_id_claim(monkeypatch):
    import time

    import jwt as pyjwt

    import jwt_auth

    secret = "workflow-proxy-claim-secret"
    monkeypatch.setattr(jwt_auth, "CANDIDATE_JWT_SECRET_KEY", secret)
    monkeypatch.setattr(jwt_auth, "API_KEY_SECRET", "not-this-key")
    monkeypatch.setattr(jwt_auth, "JWT_SECRET_KEY", "")
    token = pyjwt.encode(
        {
            "sub": "cand_77",
            "user_id": "cand_77",
            "candidate_id": "cc_7700",
            "email": "claim@example.com",
            "role": "candidate",
            "exp": int(time.time()) + 3600,
        },
        secret,
        algorithm="HS256",
    )
    creds = type("C", (), {"scheme": "Bearer", "credentials": token})()
    auth = jwt_auth.get_auth(creds, None)
    assert auth["candidate_id"] == "cc_7700"
    assert auth["user_id"] == "cand_77"
