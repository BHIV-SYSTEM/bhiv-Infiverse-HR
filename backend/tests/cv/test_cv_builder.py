"""
CV builder tests — including the mandatory AI-boundary checks.

Run from the repository root:
    python -m pytest backend/tests/cv -v

Acceptance coverage:
1. Prefill uses only authorised data and labels provenance.
2. Missing information is *requested*, never invented.
3. Three templates share one content model; switching preserves content.
4. Sections can be edited/reordered/removed (content model round-trips).
5. Drafts save and reopen without loss; ownership is enforced (a recruiter
   cannot reach a candidate's private draft).
6. AI suggestions are bounded, meaning-preserving, and review-before-apply.
7. Export requires explicit recorded approval.
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
from app.cv import service  # noqa: E402
from app.cv.models import CVContent, CVEntry, default_content  # noqa: E402

CANDIDATE_SECRET = "cv-test-candidate-secret"
USER_A = "candidate_a"
USER_B = "recruiter_b"


def bearer(user_id: str, role: str = "candidate", name: str = "Test User") -> dict:
    token = pyjwt.encode(
        {"user_id": user_id, "role": role, "email": f"{user_id}@example.com", "name": name},
        CANDIDATE_SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(jwt_auth, "CANDIDATE_JWT_SECRET_KEY", CANDIDATE_SECRET)
    monkeypatch.setattr(jwt_auth, "API_KEY_SECRET", "")
    monkeypatch.setattr(jwt_auth, "JWT_SECRET_KEY", "")
    # Force the memory store — no Mongo in tests, no network, no hangs
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("PRAVESH_JWT_SECRET", raising=False)
    monkeypatch.setattr(service, "_MEMORY", service.InMemoryCVStore())


@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.cv.routes as cv_routes

    app = FastAPI()
    app.include_router(cv_routes.router, prefix="/api/v1")
    return TestClient(app)


CANDIDATE_PROFILE = {
    "id": "cand_1",
    "name": "Asha Patil",
    "email": "asha@example.com",
    "phone": "+919876543210",
    "location": "Pune",
    "experience_years": 3,
    "technical_skills": "Python, FastAPI, React",
    "education_level": "Bachelor's",
    "seniority_level": "mid",
}


@pytest.fixture()
def with_profile(monkeypatch):
    import app.cv.routes as cv_routes

    async def fake_load(candidate_id: str):
        return dict(CANDIDATE_PROFILE) if candidate_id == "cand_1" else None

    monkeypatch.setattr(cv_routes, "_load_candidate", fake_load)
    return fake_load


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_cv_routes_require_auth(client):
    for method, path in [
        ("get", "/api/v1/cv/templates"),
        ("get", "/api/v1/cv/prefill"),
        ("get", "/api/v1/cv/drafts"),
        ("post", "/api/v1/cv/suggest"),
    ]:
        resp = (
            client.post(path, json={})
            if method == "post"
            else client.get(path)
        )
        assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"


# ---------------------------------------------------------------------------
# Prefill: provenance + ask-don't-guess
# ---------------------------------------------------------------------------


def test_prefill_labels_provenance_and_asks_for_missing(client, with_profile):
    resp = client.get(
        "/api/v1/cv/prefill", params={"candidate_id": "cand_1"},
        headers=bearer("cand_1"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Profile-sourced fields are labelled and verified
    assert body["prefill"]["full_name"]["source"] == "profile"
    assert body["prefill"]["full_name"]["value"] == "Asha Patil"
    assert body["prefill"]["skills"]["value"] == ["Python", "FastAPI", "React"]

    # Fields with no authorised source stay empty — never invented
    for field in ("professional_summary", "experience", "education", "projects"):
        assert body["prefill"][field]["source"] == "none"
        assert body["prefill"][field]["value"] is None
        assert body["prefill"][field]["verified"] is False

    # ...and instead come back as questions
    assert body["missing_fields"]
    q_fields = {q["field"] for q in body["questions"]}
    assert "professional_summary" in q_fields
    assert body["policy"]["no_invention"] is True
    assert body["policy"]["ask_dont_guess"] is True


def test_prefill_without_record_reports_all_none(client):
    resp = client.get(
        "/api/v1/cv/prefill", params={"candidate_id": USER_A},
        headers=bearer(USER_A),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_record"]["available"] is False
    non_name = {k: v for k, v in body["prefill"].items() if k != "full_name"}
    # Only the authenticated identity's own name may appear (source "auth");
    # everything else has no authorised source and stays empty.
    assert all(f["source"] == "none" for f in non_name.values())
    assert body["prefill"]["full_name"]["source"] in ("auth", "none")
    assert body["questions"], "missing information must be requested, not invented"


def test_prefill_blocks_cross_user_access(client, with_profile):
    resp = client.get(
        "/api/v1/cv/prefill", params={"candidate_id": "cand_1"},
        headers=bearer(USER_B, role="recruiter"),
    )
    assert resp.status_code == 403  # recruiter cannot read a candidate's record


# ---------------------------------------------------------------------------
# Drafts: save, reopen, ownership
# ---------------------------------------------------------------------------


def _create_draft(client, headers, **overrides):
    payload = {"template": "professional"}
    payload.update(overrides)
    resp = client.post("/api/v1/cv/drafts", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["draft"]


def test_draft_roundtrip_no_data_loss(client):
    headers = bearer(USER_A)
    draft = _create_draft(client, headers)
    draft_id = draft["draft_id"]

    # Edit content, save, reopen
    content = default_content()
    content.sections[1].entries.append(
        CVEntry(text="Backend engineer with 3 years of experience.")
    )
    upd = client.put(
        f"/api/v1/cv/drafts/{draft_id}",
        json={"content": content.model_dump()},
        headers=headers,
    )
    assert upd.status_code == 200

    reopened = client.get(f"/api/v1/cv/drafts/{draft_id}", headers=headers)
    assert reopened.status_code == 200
    saved = reopened.json()["draft"]["content"]
    assert saved["sections"][1]["entries"][0]["text"].startswith("Backend engineer")

    listing = client.get("/api/v1/cv/drafts", headers=headers)
    assert listing.json()["count"] == 1


def test_drafts_are_private_between_users(client):
    headers_a = bearer(USER_A)
    headers_b = bearer(USER_B, role="recruiter")
    draft = _create_draft(client, headers_a)
    draft_id = draft["draft_id"]

    # Recruiter cannot read, update or approve the candidate's private draft
    assert client.get(f"/api/v1/cv/drafts/{draft_id}", headers=headers_b).status_code == 404
    assert (
        client.put(
            f"/api/v1/cv/drafts/{draft_id}",
            json={"template": "minimal"},
            headers=headers_b,
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/cv/drafts/{draft_id}/approve",
            json={"confirmation": "I have reviewed this CV"},
            headers=headers_b,
        ).status_code
        == 404
    )
    # And the recruiter's own list never contains the candidate's draft
    assert client.get("/api/v1/cv/drafts", headers=headers_b).json()["count"] == 0


def test_unknown_template_rejected(client):
    headers = bearer(USER_A)
    resp = client.post(
        "/api/v1/cv/drafts", json={"template": "fancy"}, headers=headers
    )
    # Unknown template silently falls back to a valid one on create...
    assert resp.json()["draft"]["template"] == "professional"
    # ...but is rejected on update
    draft = resp.json()["draft"]
    upd = client.put(
        f"/api/v1/cv/drafts/{draft['draft_id']}",
        json={"template": "fancy"},
        headers=headers,
    )
    assert upd.status_code == 422


# ---------------------------------------------------------------------------
# Templates: switching never erases content
# ---------------------------------------------------------------------------


def test_template_switch_preserves_content(client):
    headers = bearer(USER_A)
    draft = _create_draft(client, headers)
    draft_id = draft["draft_id"]

    content = default_content()
    content.sections[1].entries.append(CVEntry(text="Summary written by the user."))
    client.put(
        f"/api/v1/cv/drafts/{draft_id}",
        json={"content": content.model_dump()},
        headers=headers,
    )

    for template in ("modern", "minimal", "professional"):
        upd = client.put(
            f"/api/v1/cv/drafts/{draft_id}", json={"template": template},
            headers=headers,
        )
        assert upd.status_code == 200
        assert upd.json()["draft"]["template"] == template
        # Content intact after every switch
        entries = upd.json()["draft"]["content"]["sections"][1]["entries"]
        assert entries[0]["text"] == "Summary written by the user."


def test_templates_endpoint_lists_three(client):
    resp = client.get("/api/v1/cv/templates", headers=bearer(USER_A))
    ids = [t["id"] for t in resp.json()["templates"]]
    assert set(ids) == {"professional", "modern", "minimal"}


# ---------------------------------------------------------------------------
# AI boundaries — suggestions cannot invent or rewrite claims
# ---------------------------------------------------------------------------


def test_suggest_returns_proposals_for_review_only(client):
    resp = client.post(
        "/api/v1/cv/suggest",
        json={"text": "  experienced   developer. built dashboards. ", "field_ref": "summary"},
        headers=bearer(USER_A),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["proposals"], "messy text should produce a proposal"
    p = body["proposals"][0]
    assert p["original"] != p["proposed"]
    assert p["changes_meaning"] is False
    assert p["reason"].startswith("whitespace")
    assert body["policy"]["review_required_before_apply"] is True
    # Nothing was persisted — proposals are display-only
    assert client.get("/api/v1/cv/drafts", headers=bearer(USER_A)).json()["count"] == 0


def test_suggest_never_adds_or_rewords_tokens(client):
    samples = [
        "i led a team of 4 engineers",
        "worked at Acme Corp from 2021 to 2023",
        "skilled in python,  sql and react",
        "SHOUTING TITLE",
        "already clean text.",
        "",
    ]
    for text in samples:
        resp = client.post(
            "/api/v1/cv/suggest",
            json={"text": text, "field_ref": "x"},
            headers=bearer(USER_A),
        )
        assert resp.status_code == 200
        for p in resp.json()["proposals"]:
            # Word set must be identical — only whitespace/capitalisation may differ
            before = p["original"].split()
            after = p["proposed"].split()
            assert [w.lower() for w in before] == [w.lower() for w in after]
            assert p["changes_meaning"] is False


def test_suggest_never_creates_content_for_empty_text(client):
    resp = client.post(
        "/api/v1/cv/suggest", json={"text": "", "field_ref": "summary"},
        headers=bearer(USER_A),
    )
    assert resp.json()["proposals"] == []  # an honest blank stays blank


# ---------------------------------------------------------------------------
# Assembly: blanks stay blank, validation reports honestly
# ---------------------------------------------------------------------------


def test_assemble_renders_and_flags_blank_summary(client):
    content = default_content()
    resp = client.post(
        "/api/v1/cv/assemble",
        json={"content": content.model_dump(), "template": "professional"},
        headers=bearer(USER_A),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["preview_html"].startswith("<!DOCTYPE html>")
    codes = [w["code"] for w in body["validation"]["warnings"]]
    assert "summary_blank" in codes  # blank reported, NOT filled in
    assert body["policy"]["blank_stays_blank"] is True


def test_assemble_templates_render_same_content_differently(client):
    content = default_content()
    content.sections[0].entries = [CVEntry(text="Asha Patil", subtext="asha@example.com")]
    content.sections[1].entries = [CVEntry(text="User-written summary.")]

    html_by_template = {}
    for template in ("professional", "modern", "minimal"):
        resp = client.post(
            "/api/v1/cv/assemble",
            json={"content": content.model_dump(), "template": template},
            headers=bearer(USER_A),
        )
        html = resp.json()["preview_html"]
        assert "Asha Patil" in html
        assert "User-written summary." in html  # content identical everywhere
        html_by_template[template] = html

    assert len(set(html_by_template.values())) == 3  # styles differ
    assert "banner" in html_by_template["modern"]  # modern has its own layout


# ---------------------------------------------------------------------------
# Approval gate before export
# ---------------------------------------------------------------------------


def test_export_blocked_without_approval(client):
    headers = bearer(USER_A)
    draft = _create_draft(client, headers)

    resp = client.post(
        "/api/v1/cv/export", json={"draft_id": draft["draft_id"]}, headers=headers
    )
    assert resp.status_code == 403
    assert "approved" in resp.json()["detail"].lower()


def test_approval_requires_exact_confirmation(client):
    headers = bearer(USER_A)
    draft = _create_draft(client, headers)

    wrong = client.post(
        f"/api/v1/cv/drafts/{draft['draft_id']}/approve",
        json={"confirmation": "looks good to me"},
        headers=headers,
    )
    assert wrong.status_code == 422

    right = client.post(
        f"/api/v1/cv/drafts/{draft['draft_id']}/approve",
        json={"confirmation": "I have reviewed this CV"},
        headers=headers,
    )
    assert right.status_code == 200
    assert right.json()["approved"] is True


def test_edit_after_approval_revokes_export(client):
    headers = bearer(USER_A)
    draft = _create_draft(client, headers)
    draft_id = draft["draft_id"]
    client.post(
        f"/api/v1/cv/drafts/{draft_id}/approve",
        json={"confirmation": "I have reviewed this CV"},
        headers=headers,
    )

    content = default_content()
    client.put(
        f"/api/v1/cv/drafts/{draft_id}",
        json={"content": content.model_dump()},
        headers=headers,
    )
    resp = client.post(
        "/api/v1/cv/export", json={"draft_id": draft_id}, headers=headers
    )
    assert resp.status_code == 403  # changed content needs fresh review


def test_export_returns_print_ready_html_after_approval(client):
    headers = bearer(USER_A)
    draft = _create_draft(client, headers)
    draft_id = draft["draft_id"]

    content = default_content()
    content.sections[0].entries = [CVEntry(text="Asha Patil", subtext="Pune")]
    content.sections[1].entries = [CVEntry(text="Summary approved by me.")]
    client.put(
        f"/api/v1/cv/drafts/{draft_id}",
        json={"content": content.model_dump(), "approve": True},
        headers=headers,
    )

    resp = client.post(
        "/api/v1/cv/export", json={"draft_id": draft_id}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["print_ready"] is True
    assert body["filename"].endswith(".html")
    assert "Asha Patil" in body["html"]
    assert "Summary approved by me." in body["html"]
    assert body["approved_at"]


def test_export_of_foreign_draft_404(client):
    headers_a = bearer(USER_A)
    draft = _create_draft(client, headers_a)
    resp = client.post(
        "/api/v1/cv/export",
        json={"draft_id": draft["draft_id"]},
        headers=bearer(USER_B, role="recruiter"),
    )
    assert resp.status_code == 404
