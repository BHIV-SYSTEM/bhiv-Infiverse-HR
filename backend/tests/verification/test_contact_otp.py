"""
Contact verification (email OTP) tests — independent candidates only.

Run from the repository root:
    python -m pytest backend/tests/verification/test_contact_otp.py -v

Coverage:
1. Eligibility: no offer + no owner → eligible; hired offer → not eligible.
2. Status: NOT_VERIFIED → CONTACT_VERIFIED after correct OTP; masked email only.
3. OTP never returned by send; wrong code increments attempts (max 5).
4. Expiry rejected; rate-limit (60s cooldown) → 429; missing Gmail → 503.
5. Candidates-only auth (403 client role); missing profile → 404.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

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
from app import contact_verification as cv  # noqa: E402

CANDIDATE_SECRET = "contact-test-candidate-secret"
CLIENT_JWT_SECRET = "contact-test-client-secret"
CAND_ID = str(ObjectId())
CAND_EMAIL = "asha@example.com"


def bearer(user_id: str, role: str = "candidate") -> dict:
    token = pyjwt.encode(
        {
            "user_id": user_id,
            "role": role,
            "email": f"{user_id}@example.com",
            "name": "Asha Patil",
        },
        CANDIDATE_SECRET if role == "candidate" else CLIENT_JWT_SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Fake Mongo
# ---------------------------------------------------------------------------


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]

    @staticmethod
    def _match(doc, flt):
        for key, want in (flt or {}).items():
            if key == "$or":
                # only used by ownership helper — not exercised here
                continue
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
                update = update or {}
                for k, v in (update.get("$set") or {}).items():
                    doc[k] = v
                for k in (update.get("$unset") or {}):
                    doc.pop(k, None)
                for k, v in (update.get("$inc") or {}).items():
                    # support nested contact_otp.attempts
                    parts = k.split(".")
                    target = doc
                    for p in parts[:-1]:
                        target = target.setdefault(p, {})
                    target[parts[-1]] = int(target.get(parts[-1]) or 0) + v
                return type("R", (), {"matched_count": 1})()
        return type("R", (), {"matched_count": 0})()

    async def count_documents(self, flt):
        return sum(1 for d in self.docs if self._match(d, flt))

    async def distinct(self, field, flt=None):
        return [
            d.get(field)
            for d in self.docs
            if self._match(d, flt or {})
        ]


class FakeDB:
    def __init__(self, *, hired: bool = False):
        self.candidates = FakeCollection(
            [
                {
                    "_id": ObjectId(CAND_ID),
                    "id": CAND_ID,
                    "name": "Asha Patil",
                    "email": CAND_EMAIL,
                }
            ]
        )
        self.offers = FakeCollection(
            [{"candidate_id": CAND_ID, "status": "hired"}] if hired else []
        )
        self.job_applications = FakeCollection([])
        self.jobs = FakeCollection([])


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(jwt_auth, "CANDIDATE_JWT_SECRET_KEY", CANDIDATE_SECRET)
    monkeypatch.setattr(jwt_auth, "JWT_SECRET_KEY", CLIENT_JWT_SECRET)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("GMAIL_EMAIL", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)


@pytest.fixture()
def fake_db(monkeypatch):
    db = FakeDB()
    import app.database as database_module

    async def _get():
        return db

    monkeypatch.setattr(database_module, "get_mongo_db", _get)
    return db


@pytest.fixture()
def hired_db(monkeypatch):
    db = FakeDB(hired=True)
    import app.database as database_module

    async def _get():
        return db

    monkeypatch.setattr(database_module, "get_mongo_db", _get)
    return db


@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(cv.router, prefix="/v1")
    return TestClient(app)


@pytest.fixture()
def mock_send(monkeypatch):
    sent = []

    def _send(to_email: str, code: str) -> None:
        sent.append((to_email, code))

    monkeypatch.setattr(cv, "send_otp_email", _send)
    return sent


@pytest.fixture()
def gmail_configured(monkeypatch):
    monkeypatch.setenv("GMAIL_EMAIL", "bot@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password-16ch")


def _set_otp(fake_db, code: str, *, expires_in_min: int = 10, attempts: int = 0):
    now = datetime.now(timezone.utc)
    fake_db.candidates.docs[0]["contact_otp"] = {
        "hash": cv._hash_otp(code),
        "expires_at": (now + timedelta(minutes=expires_in_min)).isoformat(),
        "attempts": attempts,
        "last_sent_at": now.isoformat(),
        "sends_last_hour_count": 1,
        "sends_hour_started_at": now.isoformat(),
    }


# ---------------------------------------------------------------------------
# Eligibility + status
# ---------------------------------------------------------------------------


def test_status_independent_candidate_eligible(client, fake_db):
    res = client.get("/v1/verification/contact/status", headers=bearer(CAND_ID))
    assert res.status_code == 200
    body = res.json()
    assert body["eligible"] is True
    assert body["status"] == "NOT_VERIFIED"
    assert body["email_masked"] == "a***a@example.com"
    # Full email must never leak
    assert CAND_EMAIL not in res.text


def test_status_hired_candidate_not_eligible(client, hired_db):
    res = client.get("/v1/verification/contact/status", headers=bearer(CAND_ID))
    assert res.status_code == 200
    body = res.json()
    assert body["eligible"] is False
    assert body["reason"] == "hired_or_placed"
    assert body["status"] is None


def test_status_missing_profile_404(client, fake_db):
    ghost = str(ObjectId())
    res = client.get("/v1/verification/contact/status", headers=bearer(ghost))
    assert res.status_code == 404


def test_status_client_role_403(client, fake_db):
    res = client.get(
        "/v1/verification/contact/status", headers=bearer(CAND_ID, role="client")
    )
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# Send OTP
# ---------------------------------------------------------------------------


def test_send_otp_happy_path_never_returns_code(client, fake_db, mock_send):
    res = client.post("/v1/verification/contact/otp/send", headers=bearer(CAND_ID))
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["already_verified"] is False
    assert body["expires_in"] == 600
    assert "a***a@example.com" in body["email_masked"]
    # Code only delivered via email side-channel, never in the API response
    assert CAND_EMAIL not in res.text
    assert body.get("code") is None
    assert len(mock_send) == 1
    to_email, code = mock_send[0]
    assert to_email == CAND_EMAIL
    assert len(code) == 6 and code.isdigit()
    # Stored hashed only
    stored = fake_db.candidates.docs[0]["contact_otp"]
    assert "hash" in stored and code not in stored.values()
    assert stored["hash"] == cv._hash_otp(code)


def test_send_otp_rate_limited_within_cooldown(client, fake_db, mock_send):
    first = client.post("/v1/verification/contact/otp/send", headers=bearer(CAND_ID))
    assert first.status_code == 200
    second = client.post("/v1/verification/contact/otp/send", headers=bearer(CAND_ID))
    assert second.status_code == 429


def test_send_otp_missing_gmail_is_503(client, fake_db, monkeypatch):
    # No send mock — real send_otp_email raises RuntimeError when unconfigured
    res = client.post("/v1/verification/contact/otp/send", headers=bearer(CAND_ID))
    assert res.status_code == 503
    assert "GMAIL" in res.json()["detail"] or "not configured" in res.json()["detail"]


def test_send_otp_hired_candidate_403(client, hired_db, mock_send):
    res = client.post("/v1/verification/contact/otp/send", headers=bearer(CAND_ID))
    assert res.status_code == 403
    assert mock_send == []


def test_send_otp_already_verified_idempotent(client, fake_db, mock_send):
    fake_db.candidates.docs[0]["contact_verification"] = {
        "status": "CONTACT_VERIFIED",
        "method": "email-otp",
        "email": CAND_EMAIL,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    res = client.post("/v1/verification/contact/otp/send", headers=bearer(CAND_ID))
    assert res.status_code == 200
    assert res.json()["already_verified"] is True
    assert mock_send == []


# ---------------------------------------------------------------------------
# Verify OTP
# ---------------------------------------------------------------------------


def test_verify_correct_code_marks_contact_verified(client, fake_db, mock_send):
    client.post("/v1/verification/contact/otp/send", headers=bearer(CAND_ID))
    code = mock_send[0][1]
    res = client.post(
        "/v1/verification/contact/otp/verify",
        headers=bearer(CAND_ID),
        json={"code": code},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "CONTACT_VERIFIED"
    cv_doc = fake_db.candidates.docs[0]["contact_verification"]
    assert cv_doc["method"] == "email-otp"
    assert cv_doc["email"] == CAND_EMAIL
    assert cv_doc["verified_at"]
    # OTP cleared after success
    assert "contact_otp" not in fake_db.candidates.docs[0]

    status = client.get("/v1/verification/contact/status", headers=bearer(CAND_ID))
    assert status.json()["status"] == "CONTACT_VERIFIED"


def test_verify_wrong_code_increments_attempts(client, fake_db, mock_send):
    client.post("/v1/verification/contact/otp/send", headers=bearer(CAND_ID))
    code = mock_send[0][1]
    wrong = "000000" if code != "000000" else "111111"
    res = client.post(
        "/v1/verification/contact/otp/verify",
        headers=bearer(CAND_ID),
        json={"code": wrong},
    )
    assert res.status_code == 400
    assert fake_db.candidates.docs[0]["contact_otp"]["attempts"] == 1
    assert "remaining" in res.json()["detail"]


def test_verify_too_many_attempts_429(client, fake_db):
    _set_otp(fake_db, "123456", attempts=5)
    res = client.post(
        "/v1/verification/contact/otp/verify",
        headers=bearer(CAND_ID),
        json={"code": "123456"},
    )
    assert res.status_code == 429


def test_verify_expired_code_400(client, fake_db):
    _set_otp(fake_db, "123456", expires_in_min=-1)
    res = client.post(
        "/v1/verification/contact/otp/verify",
        headers=bearer(CAND_ID),
        json={"code": "123456"},
    )
    assert res.status_code == 400
    assert "expired" in res.json()["detail"].lower()


def test_verify_invalid_format_400(client, fake_db):
    res = client.post(
        "/v1/verification/contact/otp/verify",
        headers=bearer(CAND_ID),
        json={"code": "abc"},
    )
    assert res.status_code == 400


def test_verify_hired_candidate_403(client, hired_db, mock_send):
    res = client.post(
        "/v1/verification/contact/otp/verify",
        headers=bearer(CAND_ID),
        json={"code": "123456"},
    )
    assert res.status_code == 403


def test_verify_client_role_403(client, fake_db):
    res = client.post(
        "/v1/verification/contact/otp/verify",
        headers=bearer(CAND_ID, role="client"),
        json={"code": "123456"},
    )
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# Gmail sender (unit)
# ---------------------------------------------------------------------------


def test_gmail_sender_unconfigured_raises(monkeypatch):
    monkeypatch.delenv("GMAIL_EMAIL", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="not configured"):
        cv.send_otp_email("to@example.com", "123456")


def test_gmail_sender_rejects_bad_recipient(monkeypatch):
    monkeypatch.setenv("GMAIL_EMAIL", "bot@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x" * 16)
    with pytest.raises(RuntimeError, match="Invalid recipient"):
        cv.send_otp_email("not-an-email", "123456")
