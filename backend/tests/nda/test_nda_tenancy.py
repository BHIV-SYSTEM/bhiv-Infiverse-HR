"""
Tenancy tests for NDA review (admin-panel workstream, plan B).

Run from the repository root:
    python -m pytest backend/tests/nda/test_nda_tenancy.py -v

Coverage:
1. Company (client JWT) NDA queue/review/download is scoped to its OWN tenant
   subset — foreign candidates are unreachable (403), queues filtered.
2. Platform admin + service api_key + candidates have NO tenant endpoints.
3. Upload auto-verdict: unowned NEEDS_REVIEW is KEPT (never auto-rejected);
   owned candidates also keep NEEDS_REVIEW for the company queue.
4. Admin (platform JWT) has no cross-candidate document read (403).
5. Template carries the official NEW_BHIV_NDA content markers.
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

import jwt as pyjwt  # noqa: E402

import jwt_auth  # noqa: E402
from bson import ObjectId  # noqa: E402
from app.nda import (  # noqa: E402
    NDA_STATUS_NEEDS_REVIEW,
    NDA_STATUS_REJECTED,
    NDA_STATUS_VERIFIED,
)

from test_nda_verification import (  # noqa: E402
    CANDIDATE_SECRET,
    SERVICE_API_KEY,
    make_scan_pdf,
    make_text_pdf,
)

CAND_A = str(ObjectId())
CAND_B = str(ObjectId())
CAND_UNOWNED = str(ObjectId())
CLIENT_1 = "client_tenanted_one"
CLIENT_2 = "client_tenanted_two"

CLIENT_JWT_SECRET = "nda-tenancy-client-secret"


# ---------------------------------------------------------------------------
# Token helpers (candidate secret + client/admin JWT)
# ---------------------------------------------------------------------------


def candidate_bearer(user_id: str, name: str = "Test User") -> dict:
    token = pyjwt.encode(
        {
            "user_id": user_id,
            "role": "candidate",
            "email": f"{user_id}@example.com",
            "name": name,
        },
        CANDIDATE_SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def client_bearer(client_id: str) -> dict:
    """Client-Portal JWT — signed with JWT_SECRET_KEY (client branch of get_auth)."""
    token = pyjwt.encode(
        {
            "user_id": client_id,
            "role": "client",
            "email": f"{client_id}@example.com",
            "name": "Company User",
        },
        CLIENT_JWT_SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def admin_bearer() -> dict:
    """Platform-admin JWT — signed with JWT_SECRET_KEY like /v1/admin/login
    issues, role admin: auth succeeds, then tenant endpoints must 403."""
    token = pyjwt.encode(
        {"user_id": "admin_1", "role": "admin", "email": "admin@example.com"},
        CLIENT_JWT_SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


SERVICE_HEADERS = {"Authorization": f"Bearer {SERVICE_API_KEY}"}


# ---------------------------------------------------------------------------
# Fake Mongo (subset used by app.nda tenant paths + governance helpers)
# ---------------------------------------------------------------------------


def _doc_match(doc, flt) -> bool:
    for key, want in (flt or {}).items():
        if key == "$or":
            if not any(_doc_match(doc, sub) for sub in want):
                return False
            continue
        if key == "$and":
            if not all(_doc_match(doc, sub) for sub in want):
                return False
            continue
        if isinstance(want, dict):
            if "$in" in want:
                if doc.get(key) not in want["$in"]:
                    return False
            elif "$or" in want:
                if not any(_doc_match(doc, sub) for sub in want["$or"]):
                    return False
            elif "$ne" in want:
                if doc.get(key) == want["$ne"]:
                    return False
            elif "$gt" in want:
                got = doc.get(key)
                if got is None or not (got > want["$gt"]):
                    return False
            elif "$gte" in want:
                got = doc.get(key)
                if got is None or not (got >= want["$gte"]):
                    return False
            else:
                return False
        elif str(doc.get(key)) != str(want):
            return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)
        self._limit = None

    def sort(self, key, direction=1):
        self._docs.sort(key=lambda d: str(d.get(key) or ""), reverse=direction < 0)
        return self

    def limit(self, n):
        self._limit = n
        return self

    async def to_list(self, length=None):
        docs = list(self._docs)
        n = self._limit if self._limit is not None else length
        if n is not None:
            docs = docs[:n]
        return docs

    def __aiter__(self):
        docs = list(self._docs)
        if self._limit is not None:
            docs = docs[: self._limit]

        async def gen():
            for d in docs:
                yield d

        return gen()


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]

    def find(self, flt=None, projection=None):
        return FakeCursor([dict(d) for d in self.docs if _doc_match(d, flt)])

    async def find_one(self, flt):
        for doc in self.docs:
            if _doc_match(doc, flt):
                return dict(doc)
        return None

    async def replace_one(self, flt, doc, upsert=False):
        for i, existing in enumerate(self.docs):
            if _doc_match(existing, flt):
                self.docs[i] = dict(doc)
                return
        if upsert:
            self.docs.append(dict(doc))

    async def update_one(self, flt, update):
        matched = 0
        for doc in self.docs:
            if _doc_match(doc, flt):
                matched = 1
                for key, val in (update or {}).get("$set", {}).items():
                    doc[key] = val
                break
        return type("R", (), {"matched_count": matched})()

    async def distinct(self, field, flt=None):
        out = []
        for doc in self.docs:
            if _doc_match(doc, flt):
                val = doc.get(field)
                if val is not None and val not in out:
                    out.append(val)
        return out


class FakeDB:
    def __init__(self):
        self.nda_submissions = FakeCollection()
        self.candidates = FakeCollection()
        self.jobs = FakeCollection()
        self.job_applications = FakeCollection()
        self.client_connected_recruiter = FakeCollection()


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(jwt_auth, "CANDIDATE_JWT_SECRET_KEY", CANDIDATE_SECRET)
    monkeypatch.setattr(jwt_auth, "JWT_SECRET_KEY", CLIENT_JWT_SECRET)
    monkeypatch.setattr(jwt_auth, "API_KEY_SECRET", SERVICE_API_KEY)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("NDA_GATE_ENABLED", raising=False)


@pytest.fixture()
def fake_db(monkeypatch):
    db = FakeDB()
    job_a = ObjectId()
    job_b = ObjectId()
    db.candidates.docs = [
        {"_id": ObjectId(CAND_A), "name": "Asha Patil", "email": "asha@example.com"},
        {"_id": ObjectId(CAND_B), "name": "Bharat Rao", "email": "bharat@example.com"},
        {"_id": ObjectId(CAND_UNOWNED), "name": "Meera Sen", "email": "meera@example.com"},
    ]
    db.jobs.docs = [
        {"_id": job_a, "client_id": CLIENT_1, "status": "active"},
        {"_id": job_b, "client_id": CLIENT_2, "status": "active"},
    ]
    db.job_applications.docs = [
        {"job_id": str(job_a), "candidate_id": CAND_A},
        {"job_id": str(job_b), "candidate_id": CAND_B},
    ]
    # Both in review so tenant queues have something to filter.
    now = "2026-11-12T00:00:00+00:00"
    for cid in (CAND_A, CAND_B):
        db.nda_submissions.docs.append(
            {
                "candidate_id": cid,
                "status": NDA_STATUS_NEEDS_REVIEW,
                "verification": {
                    "result": NDA_STATUS_NEEDS_REVIEW,
                    "reasons": ["no_extractable_text_scanned_or_image_pdf"],
                    "method": "pypdf2-auto",
                    "checked_at": now,
                },
                "review": None,
                "document": {
                    "filename": "nda.pdf",
                    "size_bytes": 10,
                    "uploaded_at": now,
                    "file_base64": "JVBERi0xLjQK",
                },
                "submission_count": 1,
                "updated_at": now,
            }
        )

    import app.database as database_module

    async def _get():
        return db

    monkeypatch.setattr(database_module, "get_mongo_db", _get)
    return db


@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.nda as nda_module

    app = FastAPI()
    app.include_router(nda_module.router, prefix="/v1")
    return TestClient(app)


def _upload(client, candidate_id, pdf_bytes):
    return client.post(
        "/v1/nda/upload",
        headers=candidate_bearer(candidate_id),
        files={"file": ("nda.pdf", pdf_bytes, "application/pdf")},
    )


# ---------------------------------------------------------------------------
# 1. Tenant queue scoping
# ---------------------------------------------------------------------------


def test_tenant_queue_is_scoped_to_own_subset(client, fake_db):
    r1 = client.get("/v1/nda/tenant/queue", headers=client_bearer(CLIENT_1))
    assert r1.status_code == 200, r1.text
    body = r1.json()
    assert body["scope"] == "tenant"
    assert [q["candidate_id"] for q in body["queue"]] == [CAND_A]

    r2 = client.get("/v1/nda/tenant/queue", headers=client_bearer(CLIENT_2))
    assert r2.status_code == 200
    assert [q["candidate_id"] for q in r2.json()["queue"]] == [CAND_B]


def test_tenant_queue_enriches_candidate_names(client, fake_db):
    body = client.get("/v1/nda/tenant/queue", headers=client_bearer(CLIENT_1)).json()
    assert body["queue"][0]["candidate_name"] == "Asha Patil"


def test_tenant_queue_empty_for_client_without_jobs(client, fake_db):
    body = client.get(
        "/v1/nda/tenant/queue", headers=client_bearer("client_with_no_jobs")
    ).json()
    assert body == {"queue": [], "count": 0, "scope": "tenant"}


# ---------------------------------------------------------------------------
# 2. Tenant review: own 200 / foreign 403 / admin excluded
# ---------------------------------------------------------------------------


def test_tenant_review_own_candidate_verified(client, fake_db):
    resp = client.post(
        f"/v1/nda/tenant/{CAND_A}/review",
        headers=client_bearer(CLIENT_1),
        json={"decision": "VERIFIED", "note": "checked signed copy"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["gate_passed"] is True
    assert resp.json()["reviewed_by"] == CLIENT_1

    status = client.get("/v1/nda/status", headers=candidate_bearer(CAND_A)).json()
    assert status["status"] == NDA_STATUS_VERIFIED
    assert status["review"]["reviewed_by"] == CLIENT_1
    assert status["verification"]["tenant_override"] == NDA_STATUS_VERIFIED


def test_tenant_review_foreign_candidate_forbidden(client, fake_db):
    resp = client.post(
        f"/v1/nda/tenant/{CAND_B}/review",
        headers=client_bearer(CLIENT_1),
        json={"decision": "VERIFIED"},
    )
    assert resp.status_code == 403
    assert "outside your company subset" in resp.json()["detail"]
    # Unchanged.
    rec = fake_db.nda_submissions.docs
    foreign = next(d for d in rec if d["candidate_id"] == CAND_B)
    assert foreign["status"] == NDA_STATUS_NEEDS_REVIEW


def test_tenant_review_requires_client_jwt(client, fake_db):
    assert (
        client.post(
            f"/v1/nda/tenant/{CAND_A}/review",
            headers=admin_bearer(),
            json={"decision": "VERIFIED"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/nda/tenant/{CAND_A}/review",
            headers=SERVICE_HEADERS,
            json={"decision": "VERIFIED"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/nda/tenant/{CAND_A}/review",
            headers=candidate_bearer(CAND_A),
            json={"decision": "VERIFIED"},
        ).status_code
        == 403
    )


def test_tenant_queue_requires_client_jwt(client, fake_db):
    assert client.get("/v1/nda/tenant/queue", headers=admin_bearer()).status_code == 403
    assert (
        client.get("/v1/nda/tenant/queue", headers=SERVICE_HEADERS).status_code == 403
    )
    assert (
        client.get("/v1/nda/tenant/queue", headers=candidate_bearer(CAND_A)).status_code
        == 403
    )


def test_tenant_review_validates_decision(client, fake_db):
    resp = client.post(
        f"/v1/nda/tenant/{CAND_A}/review",
        headers=client_bearer(CLIENT_1),
        json={"decision": "MAYBE"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 3. Upload auto-verdict: unowned NEEDS_REVIEW is KEPT (no auto-reject);
#    owned candidates also keep NEEDS_REVIEW for the company queue.
# ---------------------------------------------------------------------------


def test_unowned_scan_upload_stays_needs_review(client, fake_db):
    # Correct-but-scanned NDAs must NOT be auto-REJECTED (false refuse).
    resp = _upload(client, CAND_UNOWNED, make_scan_pdf())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == NDA_STATUS_NEEDS_REVIEW
    assert body["gate_passed"] is False
    reasons = body["verification"]["reasons"]
    assert "unowned_candidate_needs_review" in reasons
    assert "no_extractable_text_scanned_or_image_pdf" in reasons
    assert body["verification"]["auto_decision"] == "NEEDS_REVIEW"


def test_owned_scan_upload_stays_needs_review_for_company(client, fake_db):
    # CAND_A applied to CLIENT_1's job -> the company reviews, not the platform.
    resp = _upload(client, CAND_A, make_scan_pdf())
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == NDA_STATUS_NEEDS_REVIEW
    assert "re-upload a clearer text-based" in resp.json()["message"] or "reviewer will check" in resp.json()["message"]


def test_unowned_signed_text_upload_still_verifies(client, fake_db):
    # Identity must match the OWNED lookup doc (candidates collection wins).
    good = (
        "NDA between BlackHole Infiverse LLP and Meera Sen (meera@example.com). "
        "Signed and stamped by the Recipient."
    )
    resp = _upload(client, CAND_UNOWNED, make_text_pdf(good))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == NDA_STATUS_VERIFIED


# ---------------------------------------------------------------------------
# 4. Document download scoping
# ---------------------------------------------------------------------------


def test_company_document_download_own_subset(client, fake_db):
    ok = client.get(
        f"/v1/nda/document?candidate_id={CAND_A}",
        headers=client_bearer(CLIENT_1),
    )
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "application/pdf"


def test_company_document_download_foreign_forbidden(client, fake_db):
    resp = client.get(
        f"/v1/nda/document?candidate_id={CAND_B}",
        headers=client_bearer(CLIENT_1),
    )
    assert resp.status_code == 403


def test_admin_cannot_read_cross_candidate_document(client, fake_db):
    resp = client.get(
        f"/v1/nda/document?candidate_id={CAND_A}", headers=admin_bearer()
    )
    assert resp.status_code == 403


def test_service_key_can_read_any_document(client, fake_db):
    resp = client.get(
        f"/v1/nda/document?candidate_id={CAND_A}", headers=SERVICE_HEADERS
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 5. Template = official NEW_BHIV_NDA content
# ---------------------------------------------------------------------------


def test_template_carries_official_ndda_pdf_content(client, fake_db):
    resp = client.get("/v1/nda/template", headers=candidate_bearer(CAND_A))
    assert resp.status_code == 200
    text = resp.text
    assert "BlackHole Infiverse LLP" in text
    assert "NON-CIRCUMVENTION" in text
    assert "NON-SOLICITATION" in text
    assert "Confidentiality &amp; Public Disclosure Restrictions" in text
    assert "Declaration Regarding Fees or Charges" in text
    assert "NON-DISCLOSURE AGREEMENT" in text
    assert "Form NDA/BHIV-v2" in text
    assert "Signature" in text and "Stamp" in text
    # Company section must be prominent (dedicated box, not only the sig line)
    assert "COMPANY (HIRING ENTITY)" in text
    assert "Company (Hiring Entity)" in text  # signature-block label too
    assert "companybox" in text
    # Signature block points back to the box so both stay consistent
    assert "Company (Hiring Entity) — same as the box at the top" in text
