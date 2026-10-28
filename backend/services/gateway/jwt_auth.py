"""
JWT Authentication Module for BHIV HR Platform Backend
Validates JWT tokens from frontend
"""

import os
import jwt
import httpx
from fastapi import HTTPException, Security, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional, Dict, Any
from functools import lru_cache
import logging

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)

# JWT configuration - Standardized variable names (see ENVIRONMENT_VARIABLES.md)
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
CANDIDATE_JWT_SECRET_KEY = os.getenv("CANDIDATE_JWT_SECRET_KEY", "")

# API Key for service-to-service communication
API_KEY_SECRET = os.getenv("API_KEY_SECRET", "")


def validate_api_key(api_key: str) -> bool:
    """Validate API key for service-to-service communication"""
    if not API_KEY_SECRET:
        return False
    return api_key == API_KEY_SECRET


def verify_jwt_token(token: str, secret: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Verify a JWT token and return the payload.
    Uses HS256 with the JWT secret from environment settings.
    Supports tokens with or without audience claim.
    """
    # Use explicit secret if provided, otherwise use JWT_SECRET_KEY
    jwt_secret = secret or JWT_SECRET_KEY
    
    if not jwt_secret:
        logger.error("JWT_SECRET_KEY not configured")
        return None
    
    if not token:
        logger.error("Empty token provided to verify_jwt_token")
        return None
    
    try:
        # First try without audience validation (for custom tokens without aud claim)
        # This is more efficient and handles tokens created by our login endpoints
        payload = jwt.decode(
            token,
            jwt_secret,
            algorithms=["HS256"],
            options={"verify_aud": False}
        )
        logger.debug("JWT token verified successfully without audience validation")
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("JWT token expired")
        return None
    except jwt.InvalidSignatureError as e:
        logger.error(f"[ERROR] JWT token signature invalid: {e}. Secret mismatch or wrong secret used.")
        logger.error(f"Secret provided: {bool(jwt_secret)}, Secret length: {len(jwt_secret) if jwt_secret else 0}")
        logger.error(f"Token (first 50 chars): {token[:50] if token else 'None'}...")
        # Try with audience validation as fallback
        try:
            logger.debug("Attempting verification with audience validation as fallback")
            payload = jwt.decode(
                token,
                jwt_secret,
                algorithms=["HS256"],
                audience="authenticated"
            )
            logger.debug("JWT token verified successfully with audience validation")
            return payload
        except Exception as e2:
            logger.debug(f"Fallback verification with audience also failed: {e2}")
            return None
    except jwt.InvalidAudienceError:
        # Token has aud claim but it doesn't match - try without audience validation
        logger.debug("JWT token audience mismatch, trying without audience validation")
        try:
            payload = jwt.decode(
                token,
                jwt_secret,
                algorithms=["HS256"],
                options={"verify_aud": False}
            )
            logger.debug("JWT token verified successfully without audience validation")
            return payload
        except Exception as e2:
            logger.debug(f"Verification without audience also failed: {e2}")
            return None
    except jwt.DecodeError as e:
        logger.warning(f"JWT token decode error: {e}")
        return None
    except jwt.InvalidTokenError as e:
        # Token might be missing aud claim - try without audience validation
        logger.debug(f"JWT token validation error (possibly missing aud claim): {e}, trying without audience")
        try:
            payload = jwt.decode(
                token,
                jwt_secret,
                algorithms=["HS256"],
                options={"verify_aud": False}
            )
            logger.debug("JWT token verified successfully without audience validation")
            return payload
        except Exception as e2:
            logger.warning(f"Invalid JWT token even without audience validation: {e2}")
            return None
    except Exception as e:
        logger.error(f"Unexpected error verifying JWT token: {e}")
        return None


def get_user_from_token(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract user information from JWT token payload"""
    # Support both standard JWT tokens (sub) and custom tokens (candidate_id, client_id, user_id)
    user_id = payload.get("sub") or payload.get("candidate_id") or payload.get("client_id") or payload.get("user_id")
    
    return {
        "user_id": user_id,
        "email": payload.get("email"),
        "role": payload.get("user_metadata", {}).get("role") or payload.get("role", "candidate"),
        "name": payload.get("user_metadata", {}).get("name") or payload.get("name", ""),
        "aud": payload.get("aud"),
        "exp": payload.get("exp"),
    }


def get_api_key(credentials: HTTPAuthorizationCredentials = Security(security)):
    """Dependency for API key only authentication"""
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")
    
    if not validate_api_key(credentials.credentials):
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    return credentials.credentials


def get_auth(
    credentials: HTTPAuthorizationCredentials = Security(security),
    request: Request = None,
):
    """
    Unified authentication: API key OR JWT token OR Pravesh SSO cookie
    - API keys: For service-to-service communication
    - JWT: For authenticated users from frontend
    Supports both client JWT tokens (JWT_SECRET_KEY) and candidate JWT tokens (CANDIDATE_JWT_SECRET_KEY)
    See ENVIRONMENT_VARIABLES.md for standardized variable names.

    Read-only Pravesh bridge: when no Authorization header is present, the
    Pravesh (blackhole_auth) cookie/header is accepted if it verifies and the
    app slug is authorised. The identity stays namespaced (``pravesh:<id>``)
    so no local account is ever adopted or merged. Falls back to the normal
    401 whenever the bridge is disabled or the token is absent/invalid.
    """
    if not credentials:
        # --- Pravesh read-only bridge (cookie/header, no Authorization header) ---
        try:
            from pravesh_bridge import pravesh_auth_from_request

            pravesh_auth = pravesh_auth_from_request(request)
        except Exception as _pravesh_exc:  # bridge must never break normal auth
            logger.warning(f"[PRAVESH] Bridge error (ignored): {_pravesh_exc}")
            pravesh_auth = None
        if pravesh_auth:
            logger.info(f"[AUTH] Pravesh SSO identity accepted: {pravesh_auth['user_id']}")
            return pravesh_auth

        logger.warning("No credentials provided in Authorization header")
        raise HTTPException(status_code=401, detail="Authentication required")
    
    token = credentials.credentials
    
    if not token:
        logger.warning("Empty token in credentials")
        raise HTTPException(status_code=401, detail="Authentication token is empty")
    
    logger.info(f"[AUTH] Attempting authentication with token (first 30 chars): {token[:30]}...")
    
    # Try API key first (for service-to-service)
    if validate_api_key(token):
        logger.debug("Authentication successful: API key")
        return {
            "type": "api_key",
            "credentials": token,
            "user_id": "service",
            "role": "admin"
        }
    
    # Try candidate JWT token first (CANDIDATE_JWT_SECRET_KEY)
    # This includes both candidates and recruiters (recruiters use candidate login endpoint)
    if CANDIDATE_JWT_SECRET_KEY:
        logger.info(f"Attempting candidate JWT validation with secret (exists: {bool(CANDIDATE_JWT_SECRET_KEY)}, length: {len(CANDIDATE_JWT_SECRET_KEY) if CANDIDATE_JWT_SECRET_KEY else 0})")
        payload = verify_jwt_token(token, secret=CANDIDATE_JWT_SECRET_KEY)
        if payload:
            user_info = get_user_from_token(payload)
            # Get role from token payload (supports both "candidate" and "recruiter")
            token_role = payload.get("role", "candidate")
            if token_role not in ["candidate", "recruiter"]:
                token_role = "candidate"  # Default to candidate if invalid role
            logger.info(f"[OK] Authentication successful: Candidate JWT token for user {user_info.get('user_id')} with role {token_role}")
            return {
                "type": "jwt_token",
                "user_id": user_info["user_id"],
                "email": user_info["email"],
                "role": token_role,  # Use role from token payload (supports recruiter)
                "name": user_info["name"],
            }
        else:
            logger.error(f"[ERROR] Candidate JWT token validation failed. Token (first 50 chars): {token[:50] if token else 'None'}...")
            logger.error(f"Secret configured: {bool(CANDIDATE_JWT_SECRET_KEY)}, Secret length: {len(CANDIDATE_JWT_SECRET_KEY) if CANDIDATE_JWT_SECRET_KEY else 0}")
    else:
        logger.error("[ERROR] CANDIDATE_JWT_SECRET_KEY not configured")
    
    # Try client JWT token (JWT_SECRET_KEY)
    jwt_secret = JWT_SECRET_KEY
    if jwt_secret:
        logger.debug(f"Attempting client JWT validation with secret (exists: {bool(jwt_secret)})")
        payload = verify_jwt_token(token, secret=jwt_secret)
        if payload:
            user_info = get_user_from_token(payload)
            logger.debug(f"Authentication successful: Client JWT token for user {user_info.get('user_id')}")
            return {
                "type": "jwt_token",
                "user_id": user_info["user_id"],
                "email": user_info["email"],
                "role": user_info["role"],
                "name": user_info["name"],
            }
        else:
            logger.warning("Client JWT token validation failed")
    else:
        logger.warning("JWT_SECRET_KEY not configured")
    
    logger.error(f"All authentication methods failed for token (first 20 chars): {token[:20]}...")
    raise HTTPException(status_code=401, detail="Invalid authentication token")


def auth_dependency(
    credentials: HTTPAuthorizationCredentials = Security(security),
    request: Request = None,
):
    """Standard auth dependency for all services"""
    return get_auth(credentials, request)


def require_role(*allowed_roles: str):
    """
    Dependency factory to require specific roles.
    Usage: Depends(require_role("admin", "recruiter"))
    """
    def role_checker(auth: dict = Security(get_auth)):
        user_role = auth.get("role", "")
        if user_role not in allowed_roles and auth.get("type") != "api_key":
            raise HTTPException(
                status_code=403,
                detail=f"Access denied. Required roles: {', '.join(allowed_roles)}"
            )
        return auth
    return role_checker


# Role-specific dependencies
def get_candidate_auth(
    credentials: HTTPAuthorizationCredentials = Security(security),
    request: Request = None,
):
    """Authentication for candidate-only endpoints"""
    auth = get_auth(credentials, request)
    if auth.get("type") == "api_key":
        return auth  # API keys have full access
    if auth.get("role") != "candidate":
        raise HTTPException(status_code=403, detail="Candidate access required")
    return auth


def get_recruiter_auth(
    credentials: HTTPAuthorizationCredentials = Security(security),
    request: Request = None,
):
    """Authentication for recruiter-only endpoints"""
    auth = get_auth(credentials, request)
    if auth.get("type") == "api_key":
        return auth
    if auth.get("role") not in ["recruiter", "admin"]:
        raise HTTPException(status_code=403, detail="Recruiter access required")
    return auth


def get_client_auth(
    credentials: HTTPAuthorizationCredentials = Security(security),
    request: Request = None,
):
    """Authentication for client-only endpoints"""
    auth = get_auth(credentials, request)
    if auth.get("type") == "api_key":
        return auth
    if auth.get("role") not in ["client", "admin"]:
        raise HTTPException(status_code=403, detail="Client access required")
    return auth


def get_admin_auth(
    credentials: HTTPAuthorizationCredentials = Security(security),
    request: Request = None,
):
    """Authentication for admin-only endpoints"""
    auth = get_auth(credentials, request)
    if auth.get("type") == "api_key":
        return auth
    if auth.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return auth


# Optional auth (for public endpoints that can optionally use auth)
def get_optional_auth(
    credentials: HTTPAuthorizationCredentials = Security(security),
    request: Request = None,
):
    """Optional authentication - returns None if not authenticated"""
    if not credentials:
        try:
            from pravesh_bridge import pravesh_auth_from_request

            pravesh_auth = pravesh_auth_from_request(request)
            if pravesh_auth:
                return pravesh_auth
        except Exception:
            pass
        return None

    try:
        return get_auth(credentials, request)
    except HTTPException:
        return None