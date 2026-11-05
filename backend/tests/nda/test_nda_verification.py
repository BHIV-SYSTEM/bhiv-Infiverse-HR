"""
NDA gate tests (BHIV Phase 5D).

Run from the repository root:
    python -m pytest backend/tests/nda -v

Acceptance coverage:
1. Auto-verification honesty: VERIFIED only with parseable PDF + signature/stamp
   marker + identity match; scans and unconfirmed docs honestly NEEDS_REVIEW;
   junk/encrypted/empty files are REJECTED.
2. Upload/status/template endpoints work for candidates; admins get a review
   queue and an explicit decision endpoint.
3. enforce_nda_gate blocks candidates (403 NDA_NOT_VERIFIED) and bypasses
   everyone/thing it must: other roles, disabled switch, no database.
4. The gate is actually wired: CV export refuses until the NDA is VERIFIED.
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
from app.nda import (  # noqa: E402
    NDA_STATUS_NEEDS_REVIEW,
    NDA_STATUS_NOT_SUBMITTED,
    NDA_STATUS_REJECTED,
    NDA_STATUS_VERIFIED,
    enforce_nda_gate,
    verify_nda_pdf,
)

CANDIDATE_SECRET = "nda-test-candidate-secret"
SERVICE_API_KEY = "nda-test-service-secret"
SERVICE_HEADERS = {"Authorization": f"Bearer {SERVICE_API_KEY}"}
CANDIDATE_ID = "cand_1"
CANDIDATE_NAME = "Asha Patil"
CANDIDATE_EMAIL = "asha@example.com"


def bearer(user_id: str, role: str = "candidate", name: str = "Test User") -> dict:
    token = pyjwt.encode(
        {"user_id": user_id, "role": role, "email": f"{user_id}@example.com", "name": name},
        CANDIDATE_SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# PDF builders (no external assets)
# ---------------------------------------------------------------------------


def make_text_pdf(text: str) -> bytes:
    t = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    stream = ("BT /F1 12 Tf 50 750 Td (" + t + ") Tj ET").encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += ("%d 0 obj\n" % i).encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += ("xref\n0 %d\n" % (len(objects) + 1)).encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode()
    out += (
        "trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF"
        % (len(objects) + 1, xref_pos)
    ).encode()
    return bytes(out)


def make_scan_pdf() -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (80, 80), (255, 255, 255)).save(buf, "PDF")
    return buf.getvalue()


def make_encrypted_pdf() -> bytes:
    from io import BytesIO

    from PyPDF2 import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    writer.encrypt("pw")
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


GOOD_SIGNED_TEXT = (
    f"NDA between BHIV Infiverse and {CANDIDATE_NAME} ({CANDIDATE_EMAIL}). "
    "Signed and stamped by the Recipient."
)


# ---------------------------------------------------------------------------
# Fake Mongo (minimal subset used by app.nda)
# ---------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction=1):
        self._docs.sort(key=lambda d: str(d.get(key) or ""), reverse=direction < 0)
        return self

    def __aiter__(self):
        docs = list(self._docs)

        async def gen():
            for d in docs:
                yield d

        return gen()


class FakeCollection:
    def __init__(self):
        self.docs = []

    @staticmethod
    def _match(doc, flt):
        for key, want in (flt or {}).items():
            if isinstance(want, dict) and "$in" in want:
                if doc.get(key) not in want["$in"]:
                    return False
            elif str(doc.get(key)) != str(want):
                return False
        return True

    async def find_one(self, flt):
        for doc in self.docs:
            if self._match(doc, flt):
                return dict(doc)
        return None

    async def replace_one(self, flt, doc, upsert=False):
        for i, existing in enumerate(self.docs):
            if self._match(existing, flt):
                self.docs[i] = dict(doc)
                return
        if upsert:
            self.docs.append(dict(doc))

    async def update_one(self, flt, update):
        for doc in self.docs:
            if self._match(doc, flt):
                for key, val in (update or {}).get("$set", {}).items():
                    doc[key] = val
                return

    def find(self, flt=None, projection=None):
        return FakeCursor([dict(d) for d in self.docs if self._match(d, flt)])


class FakeDB:
    def __init__(self):
        self.nda_submissions = FakeCollection()
        self.candidates = FakeCollection()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(jwt_auth, "CANDIDATE_JWT_SECRET_KEY", CANDIDATE_SECRET)
    # Service auth for admin endpoints (role admin from the candidate secret
    # is coerced to candidate by get_auth, so tests use the API-key path).
    monkeypatch.setattr(jwt_auth, "API_KEY_SECRET", SERVICE_API_KEY)
    monkeypatch.setattr(jwt_auth, "JWT_SECRET_KEY", "")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("NDA_GATE_ENABLED", raising=False)


@pytest.fixture()
def fake_db(monkeypatch):
    db = FakeDB()
    db.candidates.docs.append(
        {"id": CANDIDATE_ID, "name": CANDIDATE_NAME, "email": CANDIDATE_EMAIL}
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


# ---------------------------------------------------------------------------
# 1. Auto-verification honesty (pure function)
# ---------------------------------------------------------------------------


def test_verify_empty_file_rejected():
    res = verify_nda_pdf(b"")
    assert res["result"] == NDA_STATUS_REJECTED
    assert "empty_file" in res["reasons"]


def test_verify_non_pdf_bytes_rejected():
    res = verify_nda_pdf(b"<html>this is not a pdf</html>")
    assert res["result"] == NDA_STATUS_REJECTED
    assert "not_a_pdf_magic_header" in res["reasons"]


def test_verify_oversized_rejected():
    res = verify_nda_pdf(b"%PDF-" + b"x" * (10 * 1024 * 1024))
    assert res["result"] == NDA_STATUS_REJECTED
    assert "file_too_large" in res["reasons"]


def test_verify_encrypted_pdf_rejected():
    res = verify_nda_pdf(make_encrypted_pdf())
    assert res["result"] == NDA_STATUS_REJECTED
    assert "pdf_encrypted" in res["reasons"]


def test_verify_signed_text_pdf_with_identity_verified():
    res = verify_nda_pdf(
        make_text_pdf(GOOD_SIGNED_TEXT),
        candidate_name=CANDIDATE_NAME,
        candidate_email=CANDIDATE_EMAIL,
    )
    assert res["result"] == NDA_STATUS_VERIFIED
    assert any(r.startswith("marker:") for r in res["reasons"])
    assert "name_match:True" in res["reasons"]
    assert "email_match:True" in res["reasons"]
    assert res["method"] == "pypdf2-auto"
    assert res["checked_at"]


def test_verify_text_without_signature_marker_needs_review():
    res = verify_nda_pdf(
        make_text_pdf(f"An ordinary document about {CANDIDATE_NAME}."),
        candidate_name=CANDIDATE_NAME,
    )
    assert res["result"] == NDA_STATUS_NEEDS_REVIEW
    assert "no_signature_or_stamp_marker_in_text" in res["reasons"]


def test_verify_scan_without_text_needs_review():
    """Scanned/stamped image PDFs are honestly handed to a human, never passed."""
    res = verify_nda_pdf(make_scan_pdf(), candidate_name=CANDIDATE_NAME)
    assert res["result"] == NDA_STATUS_NEEDS_REVIEW
    assert "no_extractable_text_scanned_or_image_pdf" in res["reasons"]


def test_verify_marker_but_wrong_identity_needs_review():
    res = verify_nda_pdf(
        make_text_pdf("Signed and stamped by Somebody Else other@example.com"),
        candidate_name=CANDIDATE_NAME,
        candidate_email=CANDIDATE_EMAIL,
    )
    assert res["result"] == NDA_STATUS_NEEDS_REVIEW
    assert "name_match:False" in res["reasons"]


# ---------------------------------------------------------------------------
# 2. NDA API: template, status, upload, download
# ---------------------------------------------------------------------------


def test_template_is_official_nda(client):
    resp = client.get("/v1/nda/template", headers=bearer(CANDIDATE_ID))
    assert resp.status_code == 200
    assert "NON-DISCLOSURE AGREEMENT" in resp.text
    assert "Signature" in resp.text and "Stamp" in resp.text


def test_status_defaults_to_not_submitted(client, fake_db):
    resp = client.get("/v1/nda/status", headers=bearer(CANDIDATE_ID))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == NDA_STATUS_NOT_SUBMITTED
    assert body["gate_passed"] is False


def test_upload_signed_pdf_verifies_and_unlocks(client, fake_db):
    resp = client.post(
        "/v1/nda/upload",
        headers=bearer(CANDIDATE_ID),
        files={
            "file": (
                "nda.pdf",
                make_text_pdf(GOOD_SIGNED_TEXT),
                "application/pdf",
            )
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == NDA_STATUS_VERIFIED
    assert body["gate_passed"] is True
    assert body["verification"]["method"] == "pypdf2-auto"
    assert body["document"]["filename"] == "nda.pdf"

    status = client.get("/v1/nda/status", headers=bearer(CANDIDATE_ID)).json()
    assert status["status"] == NDA_STATUS_VERIFIED
    assert status["gate_passed"] is True


def test_upload_scan_lands_in_needs_review(client, fake_db):
    resp = client.post(
        "/v1/nda/upload",
        headers=bearer(CANDIDATE_ID),
        files={"file": ("nda.pdf", make_scan_pdf(), "application/pdf")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == NDA_STATUS_NEEDS_REVIEW
    assert body["gate_passed"] is False
    assert "review" in body["message"].lower()


def test_upload_non_pdf_content_type_rejected(client, fake_db):
    resp = client.post(
        "/v1/nda/upload",
        headers=bearer(CANDIDATE_ID),
        files={"file": ("nda.txt", b"just text", "text/plain")},
    )
    assert resp.status_code == 400


def test_upload_junk_pdf_rejected_and_stays_gated(client, fake_db):
    resp = client.post(
        "/v1/nda/upload",
        headers=bearer(CANDIDATE_ID),
        files={"file": ("nda.pdf", b"not really a pdf", "application/pdf")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == NDA_STATUS_REJECTED
    assert body["gate_passed"] is False


def test_download_own_nda_roundtrip(client, fake_db):
    pdf = make_text_pdf(GOOD_SIGNED_TEXT)
    client.post(
        "/v1/nda/upload",
        headers=bearer(CANDIDATE_ID),
        files={"file": ("nda.pdf", pdf, "application/pdf")},
    )
    resp = client.get("/v1/nda/document", headers=bearer(CANDIDATE_ID))
    assert resp.status_code == 200
    assert resp.content[:5] == b"%PDF-"


def test_download_requires_own_or_admin(client, fake_db):
    client.post(
        "/v1/nda/upload",
        headers=bearer(CANDIDATE_ID),
        files={"file": ("nda.pdf", make_text_pdf(GOOD_SIGNED_TEXT), "application/pdf")},
    )
    resp = client.get(
        "/v1/nda/document",
        params={"candidate_id": "someone_else"},
        headers=bearer(CANDIDATE_ID),
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 3. Admin review queue + decisions
# ---------------------------------------------------------------------------


def test_admin_queue_and_review_flow(client, fake_db):
    client.post(
        "/v1/nda/upload",
        headers=bearer(CANDIDATE_ID),
        files={"file": ("nda.pdf", make_scan_pdf(), "application/pdf")},
    )

    admin = SERVICE_HEADERS
    queue = client.get("/v1/nda/admin/queue", headers=admin)
    assert queue.status_code == 200
    assert queue.json()["count"] == 1
    assert queue.json()["queue"][0]["candidate_id"] == CANDIDATE_ID

    reviewed = client.post(
        f"/v1/nda/admin/{CANDIDATE_ID}/review",
        headers=admin,
        json={"decision": "VERIFIED", "note": "stamped copy checked by hand"},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["gate_passed"] is True

    status = client.get("/v1/nda/status", headers=bearer(CANDIDATE_ID)).json()
    assert status["status"] == NDA_STATUS_VERIFIED
    assert status["review"]["decision"] == "VERIFIED"
    assert status["verification"]["admin_override"] == "VERIFIED"

    assert client.get("/v1/nda/admin/queue", headers=admin).json()["count"] == 0


def test_admin_review_can_reject(client, fake_db):
    client.post(
        "/v1/nda/upload",
        headers=bearer(CANDIDATE_ID),
        files={"file": ("nda.pdf", make_scan_pdf(), "application/pdf")},
    )
    admin = SERVICE_HEADERS
    reviewed = client.post(
        f"/v1/nda/admin/{CANDIDATE_ID}/review",
        headers=admin,
        json={"decision": "REJECTED", "note": "not signed"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["gate_passed"] is False

    status = client.get("/v1/nda/status", headers=bearer(CANDIDATE_ID)).json()
    assert status["status"] == NDA_STATUS_REJECTED


def test_admin_review_validates_decision_and_candidate(client, fake_db):
    admin = SERVICE_HEADERS
    bad = client.post(
        f"/v1/nda/admin/{CANDIDATE_ID}/review",
        headers=admin,
        json={"decision": "MAYBE"},
    )
    assert bad.status_code == 422

    missing = client.post(
        "/v1/nda/admin/ghost/review",
        headers=admin,
        json={"decision": "VERIFIED"},
    )
    assert missing.status_code == 404


def test_admin_endpoints_reject_candidates(client, fake_db):
    resp = client.get("/v1/nda/admin/queue", headers=bearer(CANDIDATE_ID))
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 4. enforce_nda_gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_blocks_unsubmitted_candidate(fake_db):
    with pytest.raises(Exception) as excinfo:
        await enforce_nda_gate({"type": "jwt_token", "role": "candidate", "user_id": CANDIDATE_ID})
    exc = excinfo.value
    assert getattr(exc, "status_code", None) == 403
    assert exc.detail["code"] == "NDA_NOT_VERIFIED"
    assert exc.detail["nda_status"] == NDA_STATUS_NOT_SUBMITTED


@pytest.mark.asyncio
async def test_gate_blocks_needs_review_and_rejected(fake_db):
    from datetime import datetime, timezone

    for status in (NDA_STATUS_NEEDS_REVIEW, NDA_STATUS_REJECTED):
        fake_db.nda_submissions.docs = [
            {
                "candidate_id": CANDIDATE_ID,
                "status": status,
                "updated_at": datetime.now(timezone.utc),
            }
        ]
        with pytest.raises(Exception) as excinfo:
            await enforce_nda_gate(
                {"type": "jwt_token", "role": "candidate", "user_id": CANDIDATE_ID}
            )
        assert excinfo.value.status_code == 403
        assert excinfo.value.detail["nda_status"] == status


@pytest.mark.asyncio
async def test_gate_passes_verified_candidate(fake_db):
    from datetime import datetime, timezone

    fake_db.nda_submissions.docs = [
        {
            "candidate_id": CANDIDATE_ID,
            "status": NDA_STATUS_VERIFIED,
            "updated_at": datetime.now(timezone.utc),
        }
    ]
    # Must NOT raise.
    await enforce_nda_gate(
        {"type": "jwt_token", "role": "candidate", "user_id": CANDIDATE_ID}
    )


@pytest.mark.asyncio
async def test_gate_bypasses_other_roles(fake_db):
    for auth in (
        {"type": "jwt_token", "role": "recruiter", "user_id": "r1"},
        {"type": "jwt_token", "role": "admin", "user_id": "a1"},
        {"type": "api_key", "role": "admin", "user_id": "service"},
    ):
        await enforce_nda_gate(auth)  # must not raise


@pytest.mark.asyncio
async def test_gate_applies_to_pravesh_candidate_sso(fake_db):
    # Pravesh SSO candidates are still candidates — the gate applies to them.
    with pytest.raises(Exception) as excinfo:
        await enforce_nda_gate(
            {"type": "pravesh", "role": "candidate", "user_id": "pravesh:u1"}
        )
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_gate_bypasses_guest_demo_sessions(fake_db):
    # Demo guest = preview mode: never gated (requirement: gate bypasses guests).
    await enforce_nda_gate(
        {
            "type": "jwt_token",
            "role": "candidate",
            "user_id": "guest:demo",
            "auth_type": "guest_demo",
        }
    )


@pytest.mark.asyncio
async def test_gate_disabled_by_env(fake_db, monkeypatch):
    monkeypatch.setenv("NDA_GATE_ENABLED", "false")
    await enforce_nda_gate(
        {"type": "jwt_token", "role": "candidate", "user_id": CANDIDATE_ID}
    )


@pytest.mark.asyncio
async def test_gate_fails_open_without_database(monkeypatch):
    import app.database as database_module

    async def _raise():
        raise RuntimeError("no database configured")

    monkeypatch.setattr(database_module, "get_mongo_db", _raise)
    await enforce_nda_gate(
        {"type": "jwt_token", "role": "candidate", "user_id": CANDIDATE_ID}
    )


# ---------------------------------------------------------------------------
# 5. Gate actually wired into an endpoint (CV export)
# ---------------------------------------------------------------------------


@pytest.fixture()
def cv_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.cv import service as cv_service
    import app.cv.routes as cv_routes

    cv_service._MEMORY = cv_service.InMemoryCVStore()
    app = FastAPI()
    app.include_router(cv_routes.router, prefix="/api/v1")
    return TestClient(app)


def test_cv_export_blocked_without_verified_nda(cv_client, fake_db):
    resp = cv_client.post(
        "/api/v1/cv/export",
        headers=bearer(CANDIDATE_ID),
        json={"draft_id": "any"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "NDA_NOT_VERIFIED"


def test_cv_export_gated_candidate_still_blocked_after_upload_only(
    cv_client, fake_db
):
    # Upload a scan -> NEEDS_REVIEW -> gate must STILL block export.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import app.nda as nda_module

    app = FastAPI()
    app.include_router(nda_module.router, prefix="/v1")
    nda_client = TestClient(app)
    nda_client.post(
        "/v1/nda/upload",
        headers=bearer(CANDIDATE_ID),
        files={"file": ("nda.pdf", make_scan_pdf(), "application/pdf")},
    )

    resp = cv_client.post(
        "/api/v1/cv/export",
        headers=bearer(CANDIDATE_ID),
        json={"draft_id": "any"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["nda_status"] == NDA_STATUS_NEEDS_REVIEW


def test_cv_export_bypasses_recruiter(cv_client):
    # No fake_db on purpose: a non-candidate is bypassed before any DB use,
    # so the CV store falls back to memory and the draft lookup 404s.
    resp = cv_client.post(
        "/api/v1/cv/export",
        headers=bearer("recruiter_1", role="recruiter"),
        json={"draft_id": "any"},
    )
    # Gate bypassed for non-candidates → normal flow (draft lookup 404).
    assert resp.status_code == 404
