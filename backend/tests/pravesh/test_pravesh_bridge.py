"""
Tests for the read-only Pravesh (blackhole_auth) SSO bridge — Sampada gateway.

Run from the repository root:
    python -m pytest backend/tests/pravesh/test_pravesh_bridge.py -v

Covers: HS256 signature/expiry validation, app-slug authorisation (allowedApps),
disabled bridge, namespaced identity with no account merging, role mapping,
the get_auth cookie fallback, and the session endpoint's 401/403/200/503
behaviour.
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

import pravesh_bridge as bridge  # noqa: E402

SECRET = "test_secret_for_sampada_pravesh"
APP_SLUG = "sampada"


def make_token(payload: dict, secret: str = SECRET, **kwargs) -> str:
    return pyjwt.encode(payload, secret, algorithm="HS256", **kwargs)


DEFAULT_PAYLOAD = {
    "user_id": "bhiv_user_1",
    "email": "candidate@example.com",
    "tenant_id": "tenant_default",
    "roles": ["candidate"],
    "permissions": ["apps:read"],
    "allowedApps": ["sampada", "hackaverse"],
}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv("PRAVESH_JWT_SECRET", SECRET)
    monkeypatch.setenv("PRAVESH_APP_SLUG", APP_SLUG)
    monkeypatch.delenv("PRAVESH_REQUIRE_APP_CLAIM", raising=False)
    monkeypatch.delenv("PRAVESH_COOKIE_NAME", raising=False)
    monkeypatch.delenv("PRAVESH_HEADER_NAME", raising=False)


class FakeRequest:
    def __init__(self, cookie=None, header=None):
        self.cookies = {}
        if cookie:
            self.cookies["blackhole_token"] = cookie
        self.headers = {}
        if header:
            self.headers["X-Pravesh-Token"] = header


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------


def test_valid_token_verifies():
    payload = bridge.verify_pravesh_token(make_token(DEFAULT_PAYLOAD))
    assert payload is not None
    assert payload["user_id"] == "bhiv_user_1"


def test_wrong_secret_rejected():
    token = make_token(DEFAULT_PAYLOAD, secret="other_secret")
    assert bridge.verify_pravesh_token(token) is None


def test_expired_token_rejected():
    token = make_token(dict(DEFAULT_PAYLOAD, exp=1))  # 1970
    assert bridge.verify_pravesh_token(token) is None


def test_none_alg_rejected():
    import base64
    import json

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    header = b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    body = b64(json.dumps(DEFAULT_PAYLOAD).encode())
    assert bridge.verify_pravesh_token(f"{header}.{body}.") is None


def test_missing_user_id_rejected():
    payload = {k: v for k, v in DEFAULT_PAYLOAD.items() if k != "user_id"}
    payload["sub"] = "only-sub"
    # pyjwt will happily encode it; the bridge must still reject it
    token = make_token(payload)
    assert bridge.verify_pravesh_token(token) is None


def test_malformed_token_rejected():
    assert bridge.verify_pravesh_token("not-a-token") is None
    assert bridge.verify_pravesh_token("") is None
    assert bridge.verify_pravesh_token(None) is None


def test_disabled_bridge_rejects_everything(monkeypatch):
    monkeypatch.delenv("PRAVESH_JWT_SECRET", raising=False)
    assert bridge.pravesh_enabled() is False
    assert bridge.verify_pravesh_token(make_token(DEFAULT_PAYLOAD)) is None
    assert bridge.identity_from_request(FakeRequest(cookie=make_token(DEFAULT_PAYLOAD))) is None


# ---------------------------------------------------------------------------
# App-slug authorisation + role mapping
# ---------------------------------------------------------------------------


def test_identity_allowed_when_slug_present():
    identity = bridge.identity_from_request(FakeRequest(cookie=make_token(DEFAULT_PAYLOAD)))
    assert identity["app_allowed"] is True
    assert bridge.app_allowed(identity) is True


def test_any_signed_in_account_may_open_any_product():
    """Default policy: one Blackhole sign-in reaches every registered product.

    The raw claim may omit this app's slug (token issued before the product
    was registered) — access is still granted, so new products work without
    reissuing tokens.
    """
    payload = dict(DEFAULT_PAYLOAD, allowedApps=["hackaverse"])
    identity = bridge.identity_from_request(FakeRequest(cookie=make_token(payload)))
    assert identity["app_allowed"] is False
    assert bridge.app_allowed(identity) is True
    assert bridge.pravesh_auth_from_request(FakeRequest(cookie=make_token(payload))) is not None


def test_app_claim_enforced_when_configured(monkeypatch):
    monkeypatch.setenv("PRAVESH_REQUIRE_APP_CLAIM", "true")
    payload = dict(DEFAULT_PAYLOAD, allowedApps=["hackaverse"])
    identity = bridge.identity_from_request(FakeRequest(cookie=make_token(payload)))
    assert bridge.app_allowed(identity) is False
    assert bridge.pravesh_auth_from_request(FakeRequest(cookie=make_token(payload))) is None


def test_app_claim_optional_when_disabled(monkeypatch):
    monkeypatch.setenv("PRAVESH_REQUIRE_APP_CLAIM", "false")
    payload = dict(DEFAULT_PAYLOAD, allowedApps=["other"])
    identity = bridge.identity_from_request(FakeRequest(cookie=make_token(payload)))
    assert bridge.app_allowed(identity) is True


def test_header_token_accepted_for_api_clients():
    identity = bridge.identity_from_request(FakeRequest(header=make_token(DEFAULT_PAYLOAD)))
    assert identity is not None
    assert identity["user_id"] == "bhiv_user_1"


def test_role_mapping_admin_recruiter_candidate_unknown():
    assert bridge.map_role({"roles": ["admin"]}) == "admin"
    assert bridge.map_role({"roles": ["recruiter"]}) == "recruiter"
    assert bridge.map_role({"roles": ["candidate"]}) == "candidate"
    # Unknown roles never escalate
    assert bridge.map_role({"roles": ["mystery"]}) == "candidate"
    assert bridge.map_role({"roles": []}) == "candidate"


def test_auth_dict_is_namespaced():
    identity = bridge.identity_from_request(FakeRequest(cookie=make_token(DEFAULT_PAYLOAD)))
    auth = bridge.auth_dict_from_identity(identity)
    assert auth["type"] == "pravesh_sso"
    assert auth["user_id"] == "pravesh:bhiv_user_1"
    assert auth["role"] == "candidate"


# ---------------------------------------------------------------------------
# get_auth cookie fallback (central auth path)
# ---------------------------------------------------------------------------


@pytest.fixture()
def auth_client(monkeypatch):
    import jwt_auth
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/protected")
    def protected(auth: dict = Depends(jwt_auth.get_auth)):
        return {"user_id": auth["user_id"], "type": auth["type"]}

    return TestClient(app)


async def _fake_mapping(identity=None):
    return {
        "local_identity": "pravesh:bhiv_user_1",
        "link_status": "candidate_match",
        "linked_local_user_id": "local_123",
        "merge_policy": "email_is_linking_attribute_only",
    }


def test_get_auth_accepts_pravesh_cookie(auth_client):
    resp = auth_client.get("/protected", cookies={"blackhole_token": make_token(DEFAULT_PAYLOAD)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "pravesh_sso"
    assert body["user_id"] == "pravesh:bhiv_user_1"


def test_get_auth_401_without_any_credentials(auth_client):
    resp = auth_client.get("/protected")
    assert resp.status_code == 401


def test_get_auth_401_with_invalid_cookie(auth_client):
    resp = auth_client.get("/protected", cookies={"blackhole_token": "garbage"})
    assert resp.status_code == 401


def test_get_auth_accepts_any_signed_in_account(auth_client):
    """Default policy: one Blackhole sign-in authenticates every product —
    including tokens issued before this product was registered."""
    payload = dict(DEFAULT_PAYLOAD, allowedApps=["hackaverse"])
    resp = auth_client.get("/protected", cookies={"blackhole_token": make_token(payload)})
    assert resp.status_code == 200
    assert resp.json()["user_id"].startswith("pravesh:")


def test_get_auth_401_when_app_not_authorised(auth_client, monkeypatch):
    """Per-app allow-list (opt-in) still denies a token without this slug."""
    monkeypatch.setenv("PRAVESH_REQUIRE_APP_CLAIM", "true")
    payload = dict(DEFAULT_PAYLOAD, allowedApps=["hackaverse"])
    resp = auth_client.get("/protected", cookies={"blackhole_token": make_token(payload)})
    assert resp.status_code == 401


def test_get_auth_never_returns_bare_local_identity(auth_client):
    resp = auth_client.get("/protected", cookies={"blackhole_token": make_token(DEFAULT_PAYLOAD)})
    assert resp.json()["user_id"].startswith("pravesh:")


# ---------------------------------------------------------------------------
# Session endpoint (minimal FastAPI app)
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import routes.pravesh_routes as pravesh_routes

    monkeypatch.setattr(pravesh_routes, "map_identity_async", _fake_mapping)
    app = FastAPI()
    app.include_router(pravesh_routes.router, prefix="/api/v1")
    return TestClient(app)


def test_session_401_without_cookie(session_client):
    assert session_client.get("/api/v1/pravesh/session").status_code == 401


def test_session_401_with_invalid_cookie(session_client):
    resp = session_client.get(
        "/api/v1/pravesh/session", cookies={"blackhole_token": "garbage"}
    )
    assert resp.status_code == 401


def test_session_403_without_app_slug_when_claim_required(session_client, monkeypatch):
    """Per-app allow-list (opt-in) still denies a token without this slug."""
    monkeypatch.setenv("PRAVESH_REQUIRE_APP_CLAIM", "true")
    payload = dict(DEFAULT_PAYLOAD, allowedApps=["hackaverse"])
    resp = session_client.get(
        "/api/v1/pravesh/session", cookies={"blackhole_token": make_token(payload)}
    )
    assert resp.status_code == 403
    assert "allowedApps" in resp.json()["detail"]


def test_session_200_when_app_slug_absent_from_token(session_client):
    """Default policy: a valid session opens this product regardless of the
    allow-list carried by an older token."""
    payload = dict(DEFAULT_PAYLOAD, allowedApps=["hackaverse"])
    resp = session_client.get(
        "/api/v1/pravesh/session", cookies={"blackhole_token": make_token(payload)}
    )
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is True


def test_session_200_returns_identity_and_mapping(session_client):
    resp = session_client.get(
        "/api/v1/pravesh/session", cookies={"blackhole_token": make_token(DEFAULT_PAYLOAD)}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is True
    assert body["app"] == "sampada"
    assert body["mode"] == "read-only"
    assert body["user"]["user_id"] == "bhiv_user_1"
    assert body["identity_mapping"]["merge_policy"] == "email_is_linking_attribute_only"
    assert "secret" not in str(body).lower().replace("secret_configured", "")


def test_session_503_when_not_configured(session_client, monkeypatch):
    monkeypatch.setenv("PRAVESH_JWT_SECRET", "")
    resp = session_client.get(
        "/api/v1/pravesh/session", cookies={"blackhole_token": make_token(DEFAULT_PAYLOAD)}
    )
    assert resp.status_code == 503


def test_status_endpoint_reports_bridge_config(session_client):
    resp = session_client.get("/api/v1/pravesh/status")
    assert resp.status_code == 200
    cfg = resp.json()["bridge"]
    assert cfg["enabled"] is True
    assert cfg["mode"] == "read-only"
    assert cfg["app_slug"] == "sampada"
    assert SECRET not in str(cfg)


# ---------------------------------------------------------------------------
# Read-only mapping: never merges on email
# ---------------------------------------------------------------------------


def test_map_identity_ambiguous_match(monkeypatch):
    class FakeCursor:
        def __init__(self, docs):
            self._docs = docs

        async def to_list(self, length=None):
            return self._docs

    class FakeCollection:
        def __init__(self, docs):
            self._docs = docs

        def find(self, *a, **k):
            return FakeCursor(self._docs)

    class FakeDB:
        def __init__(self, docs):
            self._docs = docs

        def __getitem__(self, name):
            return FakeCollection(self._docs)

    import types

    async def fake_get_db():
        return FakeDB([{"user_id": "a"}, {"user_id": "b"}])

    fake_app_database = types.ModuleType("app.database")
    fake_app_database.get_mongo_db = fake_get_db
    fake_app_pkg = types.ModuleType("app")
    fake_app_pkg.database = fake_app_database
    monkeypatch.setitem(sys.modules, "app", fake_app_pkg)
    monkeypatch.setitem(sys.modules, "app.database", fake_app_database)

    identity = {"user_id": "u1", "email": "dup@example.com"}
    import asyncio

    result = asyncio.run(bridge.map_identity_async(identity))
    assert result["link_status"] == "ambiguous_match"
    assert result["linked_local_user_id"] is None
    assert result["local_identity"] == "pravesh:u1"
