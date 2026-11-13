"""
KYC / identity-verification tests (Workstream C, honest rules).

Run from the repository root:
    python -m pytest backend/tests/verification -v

Acceptance coverage:
1. Upload pre-checks: PDF/JPEG/PNG only, ≤ 5 MB, non-empty (400 otherwise).
2. Honest verdicts: text PDF + profile-name match → VERIFIED; name mismatch →
   REJECTED; scanned/image-only → UNVERIFIABLE (never silently passed);
   encrypted/unparsable → REJECTED; image uploads → UNVERIFIABLE (no OCR).
3. Verdict is persisted on the candidate document and served by
   GET /v1/verification/kyc/status; missing profile → 404.
4. Candidate auth only (401 anonymous, 403 other roles); missing PyPDF2 → 503.
"""

import os
import sys

import pytest

BACKEND_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
REPO_ROOT = os.path.dirname(BACKEND_DIR)
GATEWAY_DIR = os.path.join(BACKEND_DIR, "services", "gateway")
NDA_TESTS_DIR = os.path.join(BACKEND_DIR, "tests", "nda")
for path in (REPO_ROOT, GATEWAY_DIR, NDA_TESTS_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

import jwt as pyjwt  # noqa: E402

import jwt_auth  # noqa: E402
from bson import ObjectId  # noqa: E402
from app.verification import (  # noqa: E402
    KYC_MAX_UPLOAD_BYTES,
    KYC_STATUS_NOT_SUBMITTED,
    KYC_STATUS_REJECTED,
    KYC_STATUS_UNVERIFIABLE,
    KYC_STATUS_VERIFIED,
    KycVerificationUnavailable,
    verify_kyc_document,
)

from test_nda_verification import (  # noqa: E402
    make_encrypted_pdf,
    make_scan_pdf,
    make_text_pdf,
)

CANDIDATE_SECRET = "kyc-test-candidate-secret"
CLIENT_JWT_SECRET = "kyc-test-client-secret"
CAND_ID = str(ObjectId())
CAND_NAME = "Asha Patil"
GHOST_ID = str(ObjectId())  # no candidates document for this one


def bearer(user_id: str, role: str = "candidate", name: str = CAND_NAME) -> dict:
    token = pyjwt.encode(
        {"user_id": user_id, "role": role, "email": f"{user_id}@example.com", "name": name},
        CANDIDATE_SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def client_bearer() -> dict:
    token = pyjwt.encode(
        {"user_id": "company_1", "role": "client", "email": "co@example.com", "name": "Co"},
        CLIENT_JWT_SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


GOOD_KYC_TEXT = (
    "IDENTITY DOCUMENT - Name: ASHA PATIL, DOB 1999-01-01, "
    "Address: Pune, Maharashtra. Issued by authority."
)


# ---------------------------------------------------------------------------
# Fake Mongo (candidates only)
# ---------------------------------------------------------------------------


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]

    @staticmethod
    def _match(doc, flt):
        for key, want in (flt or {}).items():
            if isinstance(want, dict) and "$in" in want:
                if doc.get(key) not in want["$in"]:
                    return False
            elif str(doc.get(key)) != str(want):
                return False
        return True

    async def find_one(self, flt, proj=None):
        for doc in self.docs:
            if self._match(doc, flt):
                return dict(doc)
        return None

    async def update_one(self, flt, update, **kwargs):
        for doc in self.docs:
            if self._match(doc, flt):
                doc.update((update or {}).get("$set", {}))
                return type("R", (), {"matched_count": 1})()
        return type("R", (), {"matched_count": 0})()


class FakeDB:
    def __init__(self):
        self.candidates = FakeCollection(
            [{"_id": ObjectId(CAND_ID), "name": CAND_NAME, "email": "asha@example.com"}]
        )


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(jwt_auth, "CANDIDATE_JWT_SECRET_KEY", CANDIDATE_SECRET)
    monkeypatch.setattr(jwt_auth, "JWT_SECRET_KEY", CLIENT_JWT_SECRET)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MONGODB_URI", raising=False)


@pytest.fixture()
def fake_db(monkeypatch):
    db = FakeDB()
    import app.database as database_module

    async def _get():
        return db

    monkeypatch.setattr(database_module, "get_mongo_db", _get)
    return db


@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.verification as verification_module

    app = FastAPI()
    app.include_router(verification_module.router, prefix="/v1")
    return TestClient(app)


def upload(client, content: bytes, content_type: str, headers=None):
    return client.post(
        "/v1/verification/kyc/document",
        headers=headers if headers is not None else bearer(CAND_ID),
        files={"file": ("kyc.pdf", content, content_type)},
    )


# ---------------------------------------------------------------------------
# 1. Pure verification function
# ---------------------------------------------------------------------------


def test_pure_empty_file_rejected():
    res = verify_kyc_document(b"", content_type="application/pdf", candidate_name=CAND_NAME)
    assert res["result"] == KYC_STATUS_REJECTED
    assert "empty_file" in res["reasons"]


def test_pure_unsupported_type_rejected():
    res = verify_kyc_document(b"hello", content_type="text/plain", candidate_name=CAND_NAME)
    assert res["result"] == KYC_STATUS_REJECTED
    assert any(r.startswith("unsupported_content_type") for r in res["reasons"])


def test_pure_name_match_verifies():
    res = verify_kyc_document(
        make_text_pdf(GOOD_KYC_TEXT),
        content_type="application/pdf",
        candidate_name=CAND_NAME,
    )
    assert res["result"] == KYC_STATUS_VERIFIED
    assert "name_match:True" in res["reasons"]
    assert res["method"] == "text-name-match"
    assert res["checked_at"]


def test_pure_name_mismatch_rejected():
    res = verify_kyc_document(
        make_text_pdf("Name: Somebody Else entirely, address unknown"),
        content_type="application/pdf",
        candidate_name=CAND_NAME,
    )
    assert res["result"] == KYC_STATUS_REJECTED
    assert "name_mismatch" in res["reasons"]


def test_pure_no_profile_name_unverifiable():
    res = verify_kyc_document(
        make_text_pdf("Some text with no name attached"),
        content_type="application/pdf",
        candidate_name=None,
    )
    assert res["result"] == KYC_STATUS_UNVERIFIABLE
    assert "candidate_name_unavailable" in res["reasons"]


def test_pure_short_name_unverifiable():
    res = verify_kyc_document(
        make_text_pdf("Name: Al, other details here"),
        content_type="application/pdf",
        candidate_name="Al",
    )
    assert res["result"] == KYC_STATUS_UNVERIFIABLE
    assert "candidate_name_too_short_for_matching" in res["reasons"]


def test_pure_scanned_pdf_unverifiable_never_passed():
    res = verify_kyc_document(
        make_scan_pdf(), content_type="application/pdf", candidate_name=CAND_NAME
    )
    assert res["result"] == KYC_STATUS_UNVERIFIABLE
    assert "no_extractable_text_scanned_or_image_pdf" in res["reasons"]


def test_pure_encrypted_pdf_rejected():
    res = verify_kyc_document(
        make_encrypted_pdf(), content_type="application/pdf", candidate_name=CAND_NAME
    )
    assert res["result"] == KYC_STATUS_REJECTED
    assert "pdf_encrypted" in res["reasons"]


def test_pure_image_upload_unverifiable_no_ocr():
    res = verify_kyc_document(
        b"\x89PNG\r\n\x1a\nfake", content_type="image/png", candidate_name=CAND_NAME
    )
    assert res["result"] == KYC_STATUS_UNVERIFIABLE
    assert "image_not_text_verifiable_no_ocr" in res["reasons"]


# ---------------------------------------------------------------------------
# 2. Upload endpoint: pre-checks + persistence + status
# ---------------------------------------------------------------------------


def test_status_defaults_to_not_submitted(client, fake_db):
    resp = client.get("/v1/verification/kyc/status", headers=bearer(CAND_ID))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == KYC_STATUS_NOT_SUBMITTED
    assert body["verification"] is None


def test_upload_verifies_and_persists_on_candidate_doc(client, fake_db):
    resp = upload(client, make_text_pdf(GOOD_KYC_TEXT), "application/pdf")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == KYC_STATUS_VERIFIED
    assert "name matched" in body["message"].lower()

    stored = fake_db.candidates.docs[0].get("kyc")
    assert stored and stored["result"] == KYC_STATUS_VERIFIED
    assert stored["document"]["filename"] == "kyc.pdf"

    status = client.get("/v1/verification/kyc/status", headers=bearer(CAND_ID)).json()
    assert status["status"] == KYC_STATUS_VERIFIED
    assert status["verification"]["method"] == "text-name-match"
    assert status["document"]["content_type"] == "application/pdf"


def test_upload_name_mismatch_is_rejected_and_persisted(client, fake_db):
    resp = upload(
        client,
        make_text_pdf("Name: Somebody Else entirely, address unknown"),
        "application/pdf",
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == KYC_STATUS_REJECTED

    status = client.get("/v1/verification/kyc/status", headers=bearer(CAND_ID)).json()
    assert status["status"] == KYC_STATUS_REJECTED
    assert "name_mismatch" in status["verification"]["reasons"]


def test_upload_scanned_pdf_is_unverifiable_not_passed(client, fake_db):
    resp = upload(client, make_scan_pdf(), "application/pdf")
    assert resp.status_code == 200
    assert resp.json()["status"] == KYC_STATUS_UNVERIFIABLE


def test_upload_png_is_unverifiable_no_ocr(client, fake_db):
    resp = upload(client, b"\x89PNG\r\n\x1a\nfake", "image/png")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == KYC_STATUS_UNVERIFIABLE
    assert "image_not_text_verifiable_no_ocr" in body["verification"]["reasons"]


def test_upload_rejects_bad_content_type(client, fake_db):
    resp = upload(client, b"some text", "text/plain")
    assert resp.status_code == 400
    assert "PDF, JPEG or PNG" in resp.json()["detail"]


def test_upload_rejects_empty_file(client, fake_db):
    resp = upload(client, b"", "application/pdf")
    assert resp.status_code == 400


def test_upload_rejects_oversized_file(client, fake_db):
    resp = upload(client, b"%PDF-" + b"x" * (KYC_MAX_UPLOAD_BYTES + 1), "application/pdf")
    assert resp.status_code == 400
    assert "5 MB" in resp.json()["detail"]


def test_missing_profile_is_404(client, fake_db):
    resp = upload(client, make_text_pdf(GOOD_KYC_TEXT), "application/pdf", bearer(GHOST_ID))
    assert resp.status_code == 404
    assert "profile" in resp.json()["detail"].lower()
    status = client.get("/v1/verification/kyc/status", headers=bearer(GHOST_ID))
    assert status.status_code == 404


# ---------------------------------------------------------------------------
# 3. Auth requirements
# ---------------------------------------------------------------------------


def test_anonymous_is_401(client, fake_db):
    assert client.post("/v1/verification/kyc/document").status_code == 401
    assert client.get("/v1/verification/kyc/status").status_code == 401


def test_non_candidate_roles_are_403(client, fake_db):
    assert (
        client.post(
            "/v1/verification/kyc/document",
            headers=client_bearer(),
            files={"file": ("kyc.pdf", b"%PDF-x", "application/pdf")},
        ).status_code
        == 403
    )
    assert (
        client.get("/v1/verification/kyc/status", headers=client_bearer()).status_code
        == 403
    )


def test_missing_pypdf2_is_503_not_a_verdict(client, fake_db, monkeypatch):
    """Server misconfiguration must surface 503 — never a fake REJECTED."""
    monkeypatch.setitem(sys.modules, "PyPDF2", None)
    resp = upload(client, make_text_pdf(GOOD_KYC_TEXT), "application/pdf")
    assert resp.status_code == 503
    assert "PyPDF2" in resp.json()["detail"]
    # Nothing was written as a verdict.
    assert "kyc" not in fake_db.candidates.docs[0]
