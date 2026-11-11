"""
Audit tests for backend/scripts/link_identity_to_candidates.py (P1-7).

Run from the repository root:
    python -m pytest backend/tests/scripts/test_link_identity_script.py -v

Covers the independent-verification findings:
  * conflict detection considers legacy candidate_id / id fields
  * QUARANTINED identities are never linked automatically
  * re-runs classify as "already" (idempotent, reportable, no write)
  * streaming cursor (no silent .limit(5000) truncation)
"""

import importlib.util
import os
import sys

import pytest

BACKEND_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
REPO_ROOT = os.path.dirname(BACKEND_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

SCRIPT_PATH = os.path.join(BACKEND_DIR, "scripts", "link_identity_to_candidates.py")


def _load_script():
    spec = importlib.util.spec_from_file_location("link_identity_to_candidates", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Minimal pymongo test doubles
# ---------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, docs):
        self._docs = [dict(d) for d in docs]

    def sort(self, *args, **kwargs):
        self._docs.sort(key=lambda d: str(d.get("_id") or ""))
        return self

    def batch_size(self, n):
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __iter__(self):
        return iter([dict(d) for d in self._docs])


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]
        self.updates = []

    @staticmethod
    def _match(doc, flt):
        for key, want in (flt or {}).items():
            if key == "$or":
                if not any(FakeCollection._match(doc, w) for w in want):
                    return False
            elif str(doc.get(key)) != str(want):
                return False
        return True

    def find(self, flt=None, projection=None):
        return FakeCursor([d for d in self.docs if self._match(d, flt)])

    def update_one(self, flt, update, upsert=False):
        self.updates.append((flt, update))
        for doc in self.docs:
            if self._match(doc, flt):
                doc.update(update.get("$set", {}))
                return


class FakeDB:
    def __init__(self, identities=None, candidates=None):
        self.identity = FakeCollection(identities)
        self.candidates = FakeCollection(candidates)


class FakeAdmin:
    def command(self, name):
        return {"ok": 1}


class FakeClient:
    def __init__(self, db):
        self._db = db
        self.admin = FakeAdmin()

    def __getitem__(self, name):
        return self._db


@pytest.fixture()
def script(monkeypatch):
    mod = _load_script()
    monkeypatch.setenv("DATABASE_URL", "mongodb://fake")
    monkeypatch.setenv("MONGODB_DB_NAME", "test_db")
    return mod


def _run(script, monkeypatch, db, *argv):
    monkeypatch.setattr(script, "MongoClient", lambda *a, **k: FakeClient(db))
    monkeypatch.setattr(sys, "argv", ["link_identity_to_candidates.py", *argv])
    return script.main()


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def test_conflict_detected_via_legacy_candidate_id_field(script, monkeypatch, capsys):
    """Audit: a candidate whose legacy candidate_id disagrees with the
    identity's canonical id is a CONFLICT — never a second stamped id."""
    db = FakeDB(
        identities=[
            {
                "_id": "i1",
                "sampada_local_id": "sd_1",
                "canonical_candidate_id": "cc_NEW",
                "canonical_id_status": "VERIFIED",
                "email": "a@example.com",
            }
        ],
        candidates=[
            {
                "_id": "c1",
                "email": "a@example.com",
                "candidate_id": "cc_LEGACY",  # different id on the candidate
                "canonical_candidate_id": None,
                "id": None,
            }
        ],
    )
    rc = _run(script, monkeypatch, db, "--apply")
    assert rc == 0
    assert db.candidates.updates == []  # nothing written
    out = capsys.readouterr().out
    assert "conflict   : 1" in out
    assert "cc_LEGACY" in out


def test_quarantined_identity_is_never_linked(script, monkeypatch, capsys):
    db = FakeDB(
        identities=[
            {
                "_id": "i1",
                "sampada_local_id": "sd_q",
                "canonical_candidate_id": "cc_q",
                "canonical_id_status": "QUARANTINED",
                "email": "q@example.com",
            }
        ],
        candidates=[{"_id": "c1", "email": "q@example.com", "candidate_id": "cc_q"}],
    )
    rc = _run(script, monkeypatch, db, "--apply")
    assert rc == 0
    assert db.candidates.updates == []
    out = capsys.readouterr().out
    assert "quarantined: 1" in out
    assert "sd_q — skipped" in out


def test_second_run_classifies_already_idempotent(script, monkeypatch, capsys):
    identities = [
        {
            "_id": "i1",
            "sampada_local_id": "sd_a",
            "canonical_candidate_id": "cc_a",
            "canonical_id_status": "VERIFIED",
            "email": "a@example.com",
        }
    ]
    candidates = [
        {"_id": "c1", "email": "a@example.com", "candidate_id": None, "id": None}
    ]
    db = FakeDB(identities, candidates)

    # First run links (writes).
    _run(script, monkeypatch, db, "--apply")
    assert len(db.candidates.updates) == 1
    assert db.candidates.docs[0]["identity_local_id"] == "sd_a"
    assert db.candidates.docs[0]["canonical_candidate_id"] == "cc_a"

    # Second run: already linked + canonical consistent -> "already", no write.
    _run(script, monkeypatch, db, "--apply")
    assert len(db.candidates.updates) == 1  # unchanged
    out = capsys.readouterr().out
    assert "already    : 1" in out


def test_streaming_cursor_has_no_silent_5000_limit(script, monkeypatch, capsys):
    """Audit: >5000 identity records must all be processed (streamed)."""
    identities = [
        {
            "_id": f"i{n:05d}",
            "sampada_local_id": f"sd_{n:05d}",
            "canonical_candidate_id": None,
            "canonical_id_status": "UNKNOWN",
            "email": f"u{n}@example.com",
        }
        for n in range(5100)
    ]
    db = FakeDB(identities, candidates=[])
    rc = _run(script, monkeypatch, db)
    assert rc == 0
    out = capsys.readouterr().out
    assert "processed  : 5100" in out
    assert "unlinked   : 5100" in out
