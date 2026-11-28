"""
Regression tests: PUT /v1/candidate/profile/{candidate_id} validation errors.

Audit finding: `update_candidate_profile` wrapped its whole body in
`except Exception -> {"success": False, ...}` which swallowed the
`HTTPException(400)` raised for invalid input and returned HTTP 200.
The handler now re-raises HTTPException so validation failures surface
as real 400s.

Run from the repository root:
    python -m pytest backend/tests/gateway/test_candidate_profile_regression.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

GATEWAY_ROOT = Path(__file__).resolve().parents[2] / "services" / "gateway"
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

# Secrets must exist before jwt_auth is imported by any test module.
os.environ.setdefault("JWT_SECRET_KEY", "profile-regression-test-jwt-secret-01234567")
os.environ.setdefault(
    "CANDIDATE_JWT_SECRET_KEY", "profile-regression-cand-secret-0123456789"
)

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import app.main as main_module  # noqa: E402
import app.nda as nda_module  # noqa: E402
from app.main import CandidateProfileUpdate, update_candidate_profile  # noqa: E402

AUTH = {"type": "jwt_token", "role": "candidate", "user_id": "cand_1"}


class FakeCollection:
    def __init__(self):
        self.updates = []
        self.docs = [{"id": "cand_1", "name": "Old Name"}]

    async def update_one(self, flt, update, upsert=False):
        self.updates.append((flt, update))
        for doc in self.docs:
            if all(str(doc.get(k)) == str(v) for k, v in flt.items()):
                doc.update(update.get("$set", {}))
                break
        return type("R", (), {"matched_count": 1, "modified_count": 1})()


class FakeDB:
    def __init__(self):
        self.candidates = FakeCollection()


@pytest.fixture()
def db(monkeypatch):
    fake = FakeDB()

    async def _get():
        return fake

    monkeypatch.setattr(main_module, "get_mongo_db", _get)
    return fake


@pytest.fixture(autouse=True)
def nda_bypassed(monkeypatch):
    async def _gate(auth):
        return None

    monkeypatch.setattr(nda_module, "enforce_nda_gate", _gate)


def _run(coro):
    return asyncio.run(coro)


def test_invalid_phone_raises_http_400_not_200_body(db):
    with pytest.raises(HTTPException) as exc:
        _run(
            update_candidate_profile(
                "cand_1", CandidateProfileUpdate(phone="12345"), auth=AUTH
            )
        )
    assert exc.value.status_code == 400
    assert "phone" in str(exc.value.detail).lower()
    assert db.candidates.updates == []  # validation failed before any write


def test_negative_experience_raises_http_400(db):
    with pytest.raises(HTTPException) as exc:
        _run(
            update_candidate_profile(
                "cand_1", CandidateProfileUpdate(experience_years=-1), auth=AUTH
            )
        )
    assert exc.value.status_code == 400
    assert db.candidates.updates == []


def test_valid_update_still_succeeds(db):
    result = _run(
        update_candidate_profile(
            "cand_1",
            CandidateProfileUpdate(name="New Name", phone="+919876543210"),
            auth=AUTH,
        )
    )
    assert result["success"] is True
    assert len(db.candidates.updates) == 1
    assert db.candidates.docs[0]["name"] == "New Name"


def test_no_fields_returns_success_false_without_error_status(db):
    # Existing contract preserved: empty payload -> 200 {"success": False, ...}
    result = _run(
        update_candidate_profile("cand_1", CandidateProfileUpdate(), auth=AUTH)
    )
    assert result["success"] is False
    assert "No fields" in result.get("error", "")
    assert db.candidates.updates == []


@pytest.mark.parametrize(
    "auth",
    [
        {"type": "pravesh_sso", "role": "candidate", "user_id": "pravesh:u1"},
        {"type": "jwt_token", "auth_type": "pravesh_sso", "role": "recruiter", "user_id": "pravesh:u1"},
        {"type": "jwt_token", "role": "candidate", "user_id": "cand_1"},
    ],
)
def test_profile_access_is_scoped_to_the_authenticated_candidate(auth):
    """Cross-candidate and external-SSO access must fail before any DB work."""
    with pytest.raises(HTTPException) as exc:
        main_module._require_candidate_profile_access("cand_2", auth)
    assert exc.value.status_code == 403


def test_platform_admin_and_api_key_may_read_any_profile():
    for auth in (
        {"type": "jwt_token", "role": "admin", "user_id": "admin_1"},
        {"type": "api_key", "role": "admin", "user_id": "service"},
    ):
        main_module._require_candidate_profile_access("cand_2", auth)


def test_cross_candidate_profile_update_is_blocked(db):
    with pytest.raises(HTTPException) as exc:
        _run(
            update_candidate_profile(
                "cand_2", CandidateProfileUpdate(name="Hacked"), auth=AUTH
            )
        )
    assert exc.value.status_code == 403
    assert db.candidates.updates == []
