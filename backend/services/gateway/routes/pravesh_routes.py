"""
Pravesh SSO bridge endpoints (read-only) — Sampada gateway.

GET  /api/v1/pravesh/status   — bridge configuration (no secrets), for evidence
GET  /api/v1/pravesh/session  — verified Pravesh identity + local link status
POST /api/v1/pravesh/exchange — SSO auto-login: valid Pravesh cookie in, local
                                 gateway JWT out, so the frontend can skip its
                                 own sign-in page. Read-only: the token carries
                                 a namespaced ``pravesh:<id>`` identity; no
                                 local account is created, merged or adopted.
"""

import os
import sys
import time

import jwt as pyjwt

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN

# Ensure the gateway directory is importable (mirrors main.py's pattern)
_gateway_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

from pravesh_bridge import (  # noqa: E402
    app_allowed,
    app_slug,
    bridge_status,
    cookie_name,
    identity_from_request,
    map_identity_async,
    pravesh_enabled,
)

router = APIRouter(prefix="/pravesh", tags=["Pravesh SSO"])


@router.get("/status", summary="Pravesh bridge configuration (no secrets)")
def get_status():
    return {"success": True, "bridge": bridge_status()}


# Pravesh role -> local Sampada platform role for SSO sessions.
# The gateway's candidate-JWT path only understands candidate/recruiter, so
# SSO sessions map onto those two; anything else lands as candidate.
def _sso_role(roles) -> str:
    lowered = {str(r).lower() for r in roles or []}
    if lowered & {"admin", "recruiter"}:
        return "recruiter"
    return "candidate"


@router.post("/exchange", summary="Exchange a valid Pravesh cookie for a local gateway token")
async def exchange_session(request: Request):
    """
    SSO auto-login used by the frontend on boot (credentials: 'include').

    Verifies the ``blackhole_token`` cookie (or ``X-Pravesh-Token`` header),
    enforces the app-claim, and mints a short-lived local gateway JWT for the
    namespaced Pravesh identity so every later request can use the normal
    Bearer flow. Returns 401/403 so the frontend falls back to its own login
    page when there is no valid Pravesh session.
    """
    if not pravesh_enabled():
        raise HTTPException(
            status_code=503,
            detail="Pravesh bridge not configured (set PRAVESH_JWT_SECRET)",
        )

    identity = identity_from_request(request)
    if identity is None:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Not authenticated with Pravesh",
        )

    if not app_allowed(identity):
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN,
            detail=f"Access denied: app '{app_slug()}' not in token allowedApps",
        )

    signing_secret = (os.getenv("CANDIDATE_JWT_SECRET_KEY") or "").strip()
    if not signing_secret:
        raise HTTPException(
            status_code=503,
            detail="CANDIDATE_JWT_SECRET_KEY not configured for SSO exchange",
        )

    role = _sso_role(identity.get("roles"))
    local_user_id = f"pravesh:{identity['user_id']}"
    email = identity.get("email") or ""

    # Canonical candidate ID resolution
    candidate_id = identity.get("candidate_id")
    if not candidate_id and email:
        try:
            from app.identity import service as id_service
            store = await id_service.get_identity_store()
            matches = await store.find_by_email_institute(email, None)
            if matches and matches[0].canonical_candidate_id:
                candidate_id = matches[0].canonical_candidate_id
        except Exception:
            pass

        # Fallback to live Abhiruchi students registry if not in store
        if not candidate_id:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=8.0) as client:
                    resp = await client.get("https://abhiruchi.blackholeinfiverse.com/api/students")
                    if resp.status_code == 200:
                        data = resp.json()
                        students = data if isinstance(data, list) else data.get("students", [])
                        for s in students:
                            if (s.get("email") or "").strip().lower() == email.lower():
                                candidate_id = s.get("candidate_id") or s.get("student_id")
                                if candidate_id:
                                    break
            except Exception:
                pass

    resolved_candidate_id = candidate_id or local_user_id
    now = int(time.time())
    payload = {
        "sub": local_user_id,
        "user_id": local_user_id,
        "candidate_id": resolved_candidate_id,
        "email": email,
        "name": email.split("@")[0] if email else identity["user_id"],
        "role": role,
        "auth_type": "pravesh_sso",
        "iat": now,
        "exp": now + 8 * 3600,
    }
    token = pyjwt.encode(payload, signing_secret, algorithm="HS256")

    return {
        "success": True,
        "sso": True,
        "provider": "pravesh",
        "token": token,
        "user": {
            "id": resolved_candidate_id,
            "candidate_id": resolved_candidate_id,
            "local_user_id": local_user_id,
            "email": email,
            "name": payload["name"],
            "role": role,
            "institute_code": identity.get("institute_code"),
            "cohort_id": identity.get("cohort_id"),
        },
    }


@router.post("/logout", summary="Clear the Pravesh SSO cookie for this browser")
async def logout(request: Request, response: Response):
    """
    Expire the Pravesh cookie so SSO auto-login cannot restore the session.

    The bridge is read-only: this only clears the browser cookie (host-only
    and eTLD+1 domain-scoped variants), it never touches Pravesh server state.
    """
    name = cookie_name()
    response.delete_cookie(name, path="/")
    labels = (request.headers.get("host") or "").split(":")[0].split(".")
    if len(labels) >= 2 and not labels[-1].isdigit() and not labels[-2].isdigit():
        response.delete_cookie(name, path="/", domain="." + ".".join(labels[-2:]))
    return {"success": True, "message": "Pravesh session cookie cleared"}


@router.get("/session", summary="Verified Pravesh identity for this request")
async def get_session(request: Request):
    """
    Read-only session introspection.

    Accepts the Pravesh ``blackhole_token`` cookie (browser SSO) or the
    ``X-Pravesh-Token`` header (API clients/tests). Returns the verified
    identity and how it *would* map to a local account — it never creates or
    merges accounts.
    """
    if not pravesh_enabled():
        raise HTTPException(
            status_code=503,
            detail="Pravesh bridge not configured (set PRAVESH_JWT_SECRET)",
        )

    identity = identity_from_request(request)
    if identity is None:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Not authenticated with Pravesh",
            headers={"WWW-Authenticate": 'Cookie realm="pravesh"'},
        )

    if not app_allowed(identity):
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN,
            detail=f"Access denied: app '{app_slug()}' not in token allowedApps",
        )

    mapping = await map_identity_async(identity)
    return {
        "success": True,
        "authenticated": True,
        "app": app_slug(),
        "mode": "read-only",
        "user": {
            "user_id": identity["user_id"],
            "email": identity["email"],
            "tenant_id": identity["tenant_id"],
            "roles": identity["roles"],
            "permissions": identity["permissions"],
            "allowedApps": identity["allowedApps"],
        },
        "identity_mapping": mapping,
    }
