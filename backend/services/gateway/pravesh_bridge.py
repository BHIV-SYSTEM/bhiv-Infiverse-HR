"""
Pravesh (blackhole_auth) read-only SSO bridge — Sampada / Infiverse-HR gateway
=============================================================================

Pravesh is the BHIV shared authentication system (repo: blackhole_auth,
production origin https://pravesh.blackholeinfiverse.com). It issues an
httpOnly cookie named ``blackhole_token``: an HS256 JWT signed with the shared
secret whose payload carries::

    { user_id, email, tenant_id, roles[], permissions[], allowedApps[] }

The reference integration is the Express wrapper
``blackhole_auth/backend/src/middleware/blackholeAuth.js``: verify the cookie
locally with the shared secret, then read the identity. This module is the
equivalent for the Sampada gateway, and is **read-only by design**:

* It accepts and verifies the Pravesh cookie (or the ``X-Pravesh-Token``
  header for non-browser clients) and maps the identity into request auth.
* It never creates, merges or overwrites local accounts. The Pravesh identity
  is carried in its own namespace (``pravesh:<user_id>``).
* Email is a *linking attribute only* — we report whether a local account with
  the same email exists (``link_status``) but never merge on email equality
  (BHIV vNEXT identity rule).
* If ``PRAVESH_JWT_SECRET`` is unset the bridge is inert and the existing
  API-key / candidate-JWT / client-JWT flows behave exactly as before.

Environment
-----------
PRAVESH_JWT_SECRET        Shared HS256 secret with blackhole_auth (empty = disabled)
PRAVESH_APP_SLUG          This app's slug in allowedApps (default: "sampada")
PRAVESH_COOKIE_NAME       Cookie name (default: "blackhole_token")
PRAVESH_REQUIRE_APP_CLAIM "true" rejects tokens whose allowedApps omits the
                          app slug; "false" (default) lets any signed-in
                          Blackhole account open every product, so a newly
                          registered product works without reissuing tokens
PRAVESH_HEADER_NAME       Header carrying the token for API clients (default
                          "X-Pravesh-Token")
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import jwt as pyjwt

logger = logging.getLogger(__name__)

DEFAULT_COOKIE_NAME = "blackhole_token"
DEFAULT_APP_SLUG = "sampada"
DEFAULT_HEADER_NAME = "X-Pravesh-Token"
IDENTITY_PREFIX = "pravesh:"

# Pravesh role claim -> local platform role (never grants more than claimed)
_ROLE_MAP = {
    "admin": "admin",
    "recruiter": "recruiter",
    "candidate": "candidate",
    "client": "client",
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def pravesh_secret() -> str:
    return (os.getenv("PRAVESH_JWT_SECRET") or "").strip()


def pravesh_enabled() -> bool:
    return bool(pravesh_secret())


def app_slug() -> str:
    return (os.getenv("PRAVESH_APP_SLUG") or DEFAULT_APP_SLUG).strip().lower()


def cookie_name() -> str:
    return (os.getenv("PRAVESH_COOKIE_NAME") or DEFAULT_COOKIE_NAME).strip()


def header_name() -> str:
    return (os.getenv("PRAVESH_HEADER_NAME") or DEFAULT_HEADER_NAME).strip()


def require_app_claim() -> bool:
    """Whether a token must list this app's slug in ``allowedApps``.

    Defaults to off: one Blackhole sign-in grants every registered product,
    and registering a new product must not require reissuing existing tokens.
    Signature and expiry checks are unaffected. Set
    ``PRAVESH_REQUIRE_APP_CLAIM=true`` to enforce a per-app allow-list.
    """
    return (os.getenv("PRAVESH_REQUIRE_APP_CLAIM", "false") or "false").strip().lower() == "true"


def bridge_status() -> Dict[str, Any]:
    """Non-secret bridge configuration, for /pravesh/status evidence."""
    return {
        "enabled": pravesh_enabled(),
        "app_slug": app_slug(),
        "cookie_name": cookie_name(),
        "require_app_claim": require_app_claim(),
        "secret_configured": bool(pravesh_secret()),
        "mode": "read-only",
    }


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------


def verify_pravesh_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Verify a Pravesh JWT (HS256) and return its payload, or None.

    Rejects bad signatures, wrong algorithms, expiry and missing ``user_id``.
    Never raises.
    """
    secret = pravesh_secret()
    if not secret or not token:
        return None
    try:
        payload = pyjwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except pyjwt.ExpiredSignatureError:
        logger.debug("[PRAVESH] Token rejected: expired")
        return None
    except pyjwt.PyJWTError as exc:
        logger.warning("[PRAVESH] Token rejected: %s", exc)
        return None
    if not payload.get("user_id"):
        logger.debug("[PRAVESH] Token rejected: missing user_id")
        return None
    return payload


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


def extract_token(request) -> Optional[str]:
    """Read the Pravesh token from cookie (browser) or header (API/tests)."""
    if request is None:
        return None
    cookies = getattr(request, "cookies", None) or {}
    token = cookies.get(cookie_name())
    if token:
        return token
    headers = getattr(request, "headers", None)
    if headers is not None:
        try:
            return headers.get(header_name())
        except Exception:
            return None
    return None


def identity_from_request(request) -> Optional[Dict[str, Any]]:
    """Return the verified Pravesh identity dict, or None (no app-claim check)."""
    if not pravesh_enabled():
        return None
    token = extract_token(request)
    if not token:
        return None
    payload = verify_pravesh_token(token)
    if payload is None:
        return None

    allowed_apps = payload.get("allowedApps") or []
    slug = app_slug()
    allowed = any(str(a).lower() == slug for a in allowed_apps)
    roles = [str(r) for r in (payload.get("roles") or [])]
    return {
        "user_id": str(payload.get("user_id")),
        "email": payload.get("email"),
        "tenant_id": payload.get("tenant_id") or "tenant_default",
        "roles": roles,
        "permissions": list(payload.get("permissions") or []),
        "allowedApps": [str(a) for a in allowed_apps],
        "app_allowed": allowed,
    }


def app_allowed(identity: Dict[str, Any]) -> bool:
    if not require_app_claim():
        return True
    return bool(identity.get("app_allowed"))


def map_role(identity: Dict[str, Any]) -> str:
    """Map the Pravesh roles claim onto a local platform role.

    Unknown/absent roles map to the least-privileged useful value
    ("candidate") — never to admin.
    """
    for role in identity.get("roles") or []:
        mapped = _ROLE_MAP.get(str(role).lower())
        if mapped:
            return mapped
    return "candidate"


def auth_dict_from_identity(identity: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a Pravesh identity like a normal gateway auth result."""
    return {
        "type": "pravesh_sso",
        "user_id": IDENTITY_PREFIX + identity["user_id"],
        "email": identity.get("email"),
        "role": map_role(identity),
        "name": identity.get("email") or identity["user_id"],
        "tenant_id": identity.get("tenant_id"),
        "pravesh": {
            "roles": identity.get("roles") or [],
            "allowedApps": identity.get("allowedApps") or [],
        },
    }


def pravesh_auth_from_request(request) -> Optional[Dict[str, Any]]:
    """Cookie/header -> auth dict, or None.

    Returns None (so the caller falls through to 401) when the bridge is off,
    the token is invalid, or the token does not authorise this app.
    """
    identity = identity_from_request(request)
    if identity is None:
        return None
    if not app_allowed(identity):
        logger.warning(
            "[PRAVESH] Access denied for user %s: app '%s' not in allowedApps",
            identity.get("user_id"),
            app_slug(),
        )
        return None
    return auth_dict_from_identity(identity)


# ---------------------------------------------------------------------------
# Read-only identity mapping (never merges on email)
# ---------------------------------------------------------------------------


def map_identity_base(identity: Dict[str, Any]) -> Dict[str, Any]:
    """Static part of the mapping result (no database access)."""
    result: Dict[str, Any] = {
        "local_identity": IDENTITY_PREFIX + identity["user_id"],
        "link_status": "unmapped",
        "linked_local_user_id": None,
        "merge_policy": "email_is_linking_attribute_only",
    }
    if not (identity.get("email") or "").strip():
        result["link_status"] = "no_email"
    return result


async def map_identity_async(identity: Dict[str, Any]) -> Dict[str, Any]:
    """Look up a unique local account by email (read-only, never merges)."""
    result = map_identity_base(identity)
    if result["link_status"] == "no_email":
        return result

    email = (identity.get("email") or "").strip().lower()
    if not email:
        result["link_status"] = "no_email"
        return result

    try:
        from app.database import get_mongo_db

        db = await get_mongo_db()
        if db is None:
            result["link_status"] = "db_unavailable"
            return result
        matches = (
            await db["users"]
            .find({"email": email}, {"user_id": 1, "_id": 0})
            .to_list(length=3)
        )
    except ValueError as exc:
        # No Mongo configured in this environment — honest status, not an error
        logger.info("[PRAVESH] Local lookup skipped (no database configured): %s", exc)
        result["link_status"] = "db_unavailable"
        return result
    except Exception as exc:
        logger.warning("[PRAVESH] Local lookup failed: %s", exc)
        result["link_status"] = "lookup_error"
        return result

    if len(matches) == 1:
        result["link_status"] = "candidate_match"
        result["linked_local_user_id"] = matches[0].get("user_id")
    elif len(matches) > 1:
        result["link_status"] = "ambiguous_match"
    else:
        result["link_status"] = "no_local_account"
    return result
