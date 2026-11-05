"""
Proxy Complete-Infiverse workflow task APIs for Sampada candidates.

Configure via environment (never commit secrets):
  WORKFLOW_API_BASE_URL   e.g. http://127.0.0.1:5001/api or https://<host>/api

Per-candidate login (recommended):
  Each candidate's JWT email is used as the Complete-Infiverse employee email unless overridden in MongoDB.
  WORKFLOW_USER_PASSWORD  Shared workflow password used when logging in as that email (typical dev / one org password).
  Optional on candidate document: workflow_employee_email, workflow_password (per-user workflow credentials).

Legacy single inbox (API key or matching bridge email only):
  WORKFLOW_BRIDGE_EMAIL / WORKFLOW_BRIDGE_PASSWORD — one workflow account; only used for API-key calls,
  or when the candidate's workflow email equals WORKFLOW_BRIDGE_EMAIL and no WORKFLOW_USER_PASSWORD is set.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from cryptography.fernet import Fernet

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from jwt_auth import get_candidate_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/candidate", tags=["Candidate Portal — Workflow Tasks"])


def _load_dotenv_for_workflow() -> None:
    """Load backend/.env when the gateway runs locally (uvicorn) without Docker-injected env."""
    try:
        from pathlib import Path

        from dotenv import load_dotenv

        p = Path(__file__).resolve().parent
        for _ in range(8):
            candidate = p / ".env"
            if candidate.is_file():
                load_dotenv(candidate, override=False)
                logger.info("Workflow bridge: loaded env file at %s", candidate)
                return
            if p.parent == p:
                break
            p = p.parent
    except Exception as exc:
        logger.debug("Workflow bridge: dotenv not loaded (%s)", exc)


_load_dotenv_for_workflow()


def _resolve_workflow_api_base_url() -> Tuple[str, str]:
    """Resolve workflow API URL across local, Docker, and deployed envs."""
    candidates = [
        ("WORKFLOW_API_BASE_URL", os.getenv("WORKFLOW_API_BASE_URL", "").strip()),
        ("WORKFLOW_API_BASE_URL_DOCKER", os.getenv("WORKFLOW_API_BASE_URL_DOCKER", "").strip()),
        ("WORKFLOW_API_URL", os.getenv("WORKFLOW_API_URL", "").strip()),
    ]
    for source, value in candidates:
        if value:
            return value.rstrip("/"), source
    return "http://127.0.0.1:5001/api", "default_localhost"


WORKFLOW_API_BASE_URL, WORKFLOW_API_BASE_URL_SOURCE = _resolve_workflow_api_base_url()
WORKFLOW_BRIDGE_EMAIL = os.getenv("WORKFLOW_BRIDGE_EMAIL", "").strip()
WORKFLOW_BRIDGE_PASSWORD = os.getenv("WORKFLOW_BRIDGE_PASSWORD", "").strip()
WORKFLOW_USER_PASSWORD = os.getenv("WORKFLOW_USER_PASSWORD", "").strip()


def _workflow_unreachable_hint() -> str:
    b = WORKFLOW_API_BASE_URL
    extra = ""
    if "127.0.0.1" in b or "localhost" in b:
        extra = (
            " In deployed environments, do NOT use localhost unless workflow runs in the same container/VM. "
            "Use the actual workflow service URL/domain, or host.docker.internal only for Docker-to-host local setups."
        )
    return (
        " Ensure Complete-Infiverse/server is running (npm start). "
        f"The URL must match its port — check the log line 'Server running on port …' (often 5000 or 5001). "
        f"Test: GET {b}/ping. "
        "If the BHIV gateway runs in Docker, use host.docker.internal instead of 127.0.0.1. "
        f"Current source: {WORKFLOW_API_BASE_URL_SOURCE}. "
        "Restart the gateway after changing WORKFLOW_API_BASE_URL (or WORKFLOW_API_BASE_URL_DOCKER / WORKFLOW_API_URL)."
        + extra
    )

_TOKEN_REFRESH_SECONDS = float(os.getenv("WORKFLOW_TOKEN_REFRESH_SECONDS", str(12 * 3600)))
# Per identity: (token, workflow_user_id, monotonic_time)
_SESSIONS: Dict[str, Tuple[str, str, float]] = {}
_SESSION_LOCK = asyncio.Lock()


def _session_cache_key(workflow_email: str, password: str) -> str:
    h = hashlib.sha256(f"{workflow_email}:{password}".encode()).hexdigest()[:24]
    return f"{workflow_email.strip().lower()}|{h}"


def _bridge_configured() -> bool:
    return bool(WORKFLOW_BRIDGE_EMAIL and WORKFLOW_BRIDGE_PASSWORD and WORKFLOW_API_BASE_URL)


def _workflow_api_configured() -> bool:
    return bool(WORKFLOW_API_BASE_URL)


def _wf_fernet() -> Fernet:
    base = os.getenv("CANDIDATE_JWT_SECRET_KEY") or os.getenv("GATEWAY_SECRET_KEY") or os.getenv("JWT_SECRET_KEY") or ""
    if not base:
        raise RuntimeError(
            "Set CANDIDATE_JWT_SECRET_KEY or GATEWAY_SECRET_KEY to encrypt stored workflow passwords."
        )
    key = base64.urlsafe_b64encode(hashlib.sha256(base.encode()).digest())
    return Fernet(key)


def _wf_encrypt(plain: str) -> str:
    return "enc:v1:" + _wf_fernet().encrypt(plain.encode()).decode()


def _parse_stored_workflow_password(stored: Any) -> Optional[str]:
    if stored is None:
        return None
    raw = str(stored).strip()
    if not raw:
        return None
    try:
        if raw.startswith("enc:v1:"):
            return _wf_fernet().decrypt(raw[7:].encode()).decode()
        return raw
    except Exception as exc:
        logger.warning("Could not decrypt workflow_password (%s)", exc)
        return None


async def _resolve_candidate_document(auth: dict) -> Optional[Dict[str, Any]]:
    """Find the local Sampada candidate document for this auth context.

    Phase 5D (P0-2): resolves three identity shapes instead of assuming the
    JWT user_id is always a MongoDB ObjectId:
      1. ObjectId-shaped user_id (classic local accounts) -> ``_id`` lookup
      2. ``pravesh:<uid>`` namespaced SSO identities -> matched by JWT email
      3. canonical / other non-hex candidate ids -> matched by email first,
         then by the candidate's own ``id`` / ``candidate_id`` string fields

    Returns ``None`` when no candidate document exists — callers fall back
    to JWT claims and never crash on identity shape.
    """
    from bson import ObjectId

    from app.database import get_mongo_db

    user_id = str(auth.get("user_id") or "").strip()
    email = (auth.get("email") or "").strip()

    db = await get_mongo_db()

    # 1. Classic local account: user_id is a real ObjectId
    if user_id and ObjectId.is_valid(user_id):
        cand = await db.candidates.find_one({"_id": ObjectId(user_id)})
        if cand:
            return cand

    # 2. Email match (covers pravesh:<uid> and canonical-id JWTs)
    if email:
        cand = await db.candidates.find_one({"email": email})
        if cand:
            return cand
        escaped = "".join(f"\\{c}" if c in ".^$*+?{}[]()|\\" else c for c in email)
        cand = await db.candidates.find_one(
            {"email": {"$regex": f"^{escaped}$", "$options": "i"}}
        )
        if cand:
            return cand

    # 3. Canonical / string candidate id stored on the candidate document
    if user_id:
        cand = await db.candidates.find_one(
            {"$or": [{"id": user_id}, {"candidate_id": user_id}]}
        )
        if cand:
            return cand

    return None


async def _credentials_for_candidate(auth: dict) -> Tuple[str, str]:
    """
    Resolve Complete-Infiverse login email and password for this request.
    Candidate JWT email is the default workflow employee email (must match a user in Complete-Infiverse).
    """
    if not _workflow_api_configured():
        raise HTTPException(
            status_code=503,
            detail="WORKFLOW_API_BASE_URL is not set on the gateway.",
        )

    if auth.get("type") == "api_key":
        if not _bridge_configured():
            raise HTTPException(
                status_code=503,
                detail="API key access requires WORKFLOW_BRIDGE_EMAIL and WORKFLOW_BRIDGE_PASSWORD.",
            )
        return WORKFLOW_BRIDGE_EMAIL, WORKFLOW_BRIDGE_PASSWORD

    raw_email = (auth.get("email") or "").strip()
    if not raw_email:
        raise HTTPException(status_code=400, detail="Candidate token is missing an email claim.")

    workflow_email = raw_email
    password: Optional[str] = None

    try:
        cand = await _resolve_candidate_document(auth)
    except Exception as exc:
        logger.warning("Workflow credentials: candidate lookup failed (%s)", exc)
        cand = None
    if cand:
        alt = (cand.get("workflow_employee_email") or "").strip()
        if alt:
            workflow_email = alt
        password = _parse_stored_workflow_password(cand.get("workflow_password"))

    if not password:
        password = WORKFLOW_USER_PASSWORD or None
    if not password and WORKFLOW_BRIDGE_EMAIL and WORKFLOW_BRIDGE_PASSWORD:
        if workflow_email.strip().lower() == WORKFLOW_BRIDGE_EMAIL.strip().lower():
            password = WORKFLOW_BRIDGE_PASSWORD
    if not password:
        raise HTTPException(
            status_code=503,
            detail=(
                "Workflow account not linked. Open the Tasks tab and sign in with the same email and password "
                "you use on Complete-Infiverse, or ask your admin to set WORKFLOW_USER_PASSWORD on the gateway "
                "only if all employees share one workflow password (dev)."
            ),
        )

    return workflow_email, password


async def _workflow_login(workflow_email: str, password: str) -> Tuple[str, str]:
    url = f"{WORKFLOW_API_BASE_URL}/auth/login"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=15.0)) as client:
            r = await client.post(
                url,
                json={"email": workflow_email, "password": password},
            )
    except httpx.RequestError as e:
        logger.exception("Workflow login failed (network)")
        raise HTTPException(
            status_code=502,
            detail=f"Cannot reach workflow API at {WORKFLOW_API_BASE_URL}: {e!s}.{_workflow_unreachable_hint()}",
        ) from e

    if r.status_code >= 400:
        logger.warning("Workflow login HTTP %s for %s: %s", r.status_code, workflow_email, r.text[:400])
        raise HTTPException(status_code=502, detail="Workflow login failed (check workflow email/password for this candidate).")

    data = r.json()
    token = data.get("token")
    user = data.get("user") or {}
    uid = user.get("id")
    if not token or not uid:
        raise HTTPException(status_code=502, detail="Workflow login response missing token or user id.")
    return str(token), str(uid)


async def _get_workflow_session(auth: dict, force_refresh: bool = False) -> Tuple[str, str]:
    wf_email, wf_password = await _credentials_for_candidate(auth)
    key = _session_cache_key(wf_email, wf_password)
    now = time.time()
    async with _SESSION_LOCK:
        if not force_refresh and key in _SESSIONS:
            tok, uid, at = _SESSIONS[key]
            if (now - at) < _TOKEN_REFRESH_SECONDS:
                return tok, uid
        tok, uid = await _workflow_login(wf_email, wf_password)
        _SESSIONS[key] = (tok, uid, now)
        return tok, uid


async def _wf_request(
    method: str,
    path: str,
    auth: dict,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, Any]] = None,
    files: Optional[Dict[str, Any]] = None,
) -> httpx.Response:
    token, _ = await _get_workflow_session(auth)
    url = f"{WORKFLOW_API_BASE_URL}{path}"
    headers = {"x-auth-token": token}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=20.0)) as client:
            r = await client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                data=data,
                files=files,
            )
            if r.status_code == 401:
                token, _ = await _get_workflow_session(auth, force_refresh=True)
                headers = {"x-auth-token": token}
                r = await client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    data=data,
                    files=files,
                )
            return r
    except httpx.RequestError as e:
        logger.exception("Workflow request failed: %s %s", method, path)
        raise HTTPException(
            status_code=502,
            detail=f"Cannot reach workflow API at {WORKFLOW_API_BASE_URL}: {e!s}.{_workflow_unreachable_hint()}",
        ) from e


def _str_id(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, dict):
        if "$oid" in val:
            return str(val["$oid"])
        return str(val.get("_id") or val.get("id") or "")
    return str(val)


def _department_name(task: Dict[str, Any]) -> Optional[str]:
    dep = task.get("department")
    if isinstance(dep, dict):
        return dep.get("name")
    return None


def _assignee_id(task: Dict[str, Any]) -> str:
    a = task.get("assignee")
    if isinstance(a, dict):
        return _str_id(a.get("_id") or a.get("id"))
    return _str_id(a)


def _map_submission(sub: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not sub:
        return None
    st = sub.get("status")
    mapped = None
    if st == "Pending":
        mapped = "Pending Review"
    elif st in ("Approved", "Rejected"):
        mapped = st
    return {
        "id": _str_id(sub.get("_id")),
        "status": mapped,
        "githubLink": sub.get("githubLink") or "",
        "documentLink": sub.get("documentLink") or "",
        "feedback": sub.get("feedback") or "",
    }


def _map_task(task: Dict[str, Any], submission: Optional[Dict[str, Any]], portal_candidate_id: str) -> Dict[str, Any]:
    tid = _str_id(task.get("_id"))
    due = task.get("dueDate")
    due_iso: Any = due
    if hasattr(due, "isoformat"):
        due_iso = due.isoformat()

    return {
        "id": tid,
        "title": task.get("title") or "",
        "description": task.get("description") or "",
        "workflowStatus": task.get("status") or "Pending",
        "priority": task.get("priority") or "Medium",
        "progress": int(task.get("progress") or 0),
        "dueDate": due_iso,
        "department": _department_name(task),
        "jobTitle": "Workflow",
        "candidate_id": portal_candidate_id,
        "submission": _map_submission(submission),
    }


async def _fetch_submission_for_task(task_id: str, auth: dict) -> Optional[Dict[str, Any]]:
    r = await _wf_request("GET", f"/submissions/task/{task_id}", auth)
    if r.status_code == 404:
        try:
            err = r.json()
            if err and err.get("error"):
                return None
        except Exception:
            return None
        return None
    if r.status_code >= 400:
        return None
    try:
        return r.json()
    except Exception:
        return None


async def _load_task_for_user(task_id: str, auth: dict, workflow_user_id: str) -> Dict[str, Any]:
    r = await _wf_request("GET", f"/tasks/{task_id}", auth)
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="Task not found")
    r.raise_for_status()
    task = r.json()
    if _assignee_id(task) != workflow_user_id:
        raise HTTPException(status_code=403, detail="Task is not assigned to this employee in Complete-Infiverse.")
    return task


def _resolve_portal_candidate_id(auth: dict, candidate_id: Optional[str]) -> str:
    if auth.get("type") == "api_key":
        return candidate_id or "api"
    uid = str(auth.get("user_id") or "")
    if candidate_id and str(candidate_id) != uid:
        raise HTTPException(status_code=403, detail="candidate_id does not match authenticated candidate.")
    return uid


@router.get("/workflow-bridge-health")
async def workflow_bridge_health():
    """
    No auth. Pings Complete-Infiverse `/api/ping` using WORKFLOW_API_BASE_URL.
    Use this to see whether the gateway can reach the workflow server (before candidate login).
    """
    ping_url = f"{WORKFLOW_API_BASE_URL.rstrip('/')}/ping"
    out: Dict[str, Any] = {
        "workflow_api_base_url": WORKFLOW_API_BASE_URL,
        "workflow_api_base_url_source": WORKFLOW_API_BASE_URL_SOURCE,
        "ping_url": ping_url,
        "bridge_credentials_configured": _bridge_configured(),
        "per_candidate_password_configured": bool(WORKFLOW_USER_PASSWORD),
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=5.0)) as client:
            r = await client.get(ping_url)
        out["reachable"] = r.status_code < 500
        out["http_status"] = r.status_code
        try:
            out["ping_json"] = r.json()
        except Exception:
            out["ping_text"] = r.text[:300]
    except httpx.RequestError as e:
        out["reachable"] = False
        out["error"] = str(e)
        out["hint"] = _workflow_unreachable_hint().strip()
    return out


async def _clear_workflow_sessions() -> None:
    async with _SESSION_LOCK:
        _SESSIONS.clear()


class WorkflowLinkBody(BaseModel):
    password: str = Field(..., min_length=1, description="Password for Complete-Infiverse (workflow) login")
    workflow_employee_email: Optional[str] = Field(
        None,
        description="Employee email in Complete-Infiverse if it differs from your Sampada login email",
    )

    model_config = {"populate_by_name": True}


@router.get("/workflow-link-status")
async def workflow_link_status(auth: dict = Depends(get_candidate_auth)):
    """
    Whether this candidate has saved workflow credentials (MongoDB) and whether a shared gateway password is set.
    """
    if auth.get("type") == "api_key":
        return {
            "linked": True,
            "shared_password_configured": bool(WORKFLOW_USER_PASSWORD),
            "workflow_employee_email": None,
        }

    user_id = auth.get("user_id")
    linked = False
    workflow_employee_email: Optional[str] = None
    if user_id:
        try:
            cand = await _resolve_candidate_document(auth)
            if cand and cand.get("workflow_password"):
                linked = True
            if cand:
                we = cand.get("workflow_employee_email")
                if we and str(we).strip():
                    workflow_employee_email = str(we).strip()
        except Exception as exc:
            logger.warning("workflow-link-status: %s", exc)

    return {
        "linked": linked,
        "shared_password_configured": bool(WORKFLOW_USER_PASSWORD),
        "workflow_employee_email": workflow_employee_email,
    }


@router.post("/workflow-link")
async def workflow_link_save(body: WorkflowLinkBody, auth: dict = Depends(get_candidate_auth)):
    """
    Verify Complete-Infiverse credentials, then store an encrypted workflow password for this candidate.
    When the workflow password changes on Complete-Infiverse, submit this again with the new password.
    """
    if auth.get("type") == "api_key":
        raise HTTPException(status_code=403, detail="Not available for API key auth.")

    if not _workflow_api_configured():
        raise HTTPException(status_code=503, detail="WORKFLOW_API_BASE_URL is not set on the gateway.")

    jwt_email = (auth.get("email") or "").strip()
    if not jwt_email:
        raise HTTPException(status_code=400, detail="Candidate token is missing an email claim.")

    resolved = (body.workflow_employee_email or jwt_email).strip()
    if not resolved:
        raise HTTPException(status_code=400, detail="workflow_employee_email cannot be empty.")

    await _workflow_login(resolved, body.password)

    try:
        enc = _wf_encrypt(body.password)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing user id.")

    from bson import ObjectId

    from app.database import get_mongo_db

    try:
        cand = await _resolve_candidate_document(auth)
    except Exception as exc:
        logger.warning("workflow-link: candidate lookup failed (%s)", exc)
        cand = None

    if cand is not None:
        target_filter: Dict[str, Any] = {"_id": cand["_id"]}
    elif ObjectId.is_valid(str(user_id)):
        # Classic local account with no candidate doc yet: address by _id
        # (update is a no-op if the document does not exist — same as before).
        target_filter = {"_id": ObjectId(str(user_id))}
    else:
        # pravesh:<uid> / canonical id with no local candidate account.
        raise HTTPException(
            status_code=404,
            detail=(
                "No local candidate account found to link workflow credentials. "
                "Sign in with a Sampada account first."
            ),
        )

    db = await get_mongo_db()

    set_doc: Dict[str, Any] = {"workflow_password": enc}
    update: Dict[str, Any] = {"$set": set_doc}

    if body.workflow_employee_email and body.workflow_employee_email.strip().lower() != jwt_email.lower():
        update["$set"]["workflow_employee_email"] = body.workflow_employee_email.strip()
    else:
        update["$unset"] = {"workflow_employee_email": ""}

    await db.candidates.update_one(target_filter, update)
    await _clear_workflow_sessions()

    return {"ok": True, "workflow_employee_email": resolved}


@router.delete("/workflow-link")
async def workflow_link_delete(auth: dict = Depends(get_candidate_auth)):
    """Remove saved workflow credentials (encrypted password and optional workflow email override)."""
    if auth.get("type") == "api_key":
        raise HTTPException(status_code=403, detail="Not available for API key auth.")

    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing user id.")

    from bson import ObjectId

    from app.database import get_mongo_db

    try:
        cand = await _resolve_candidate_document(auth)
    except Exception as exc:
        logger.warning("workflow-link-delete: candidate lookup failed (%s)", exc)
        cand = None

    if cand is not None:
        target_filter = {"_id": cand["_id"]}
    elif ObjectId.is_valid(str(user_id)):
        target_filter = {"_id": ObjectId(str(user_id))}
    else:
        # No local candidate account => nothing stored to delete (idempotent).
        return {"ok": True}

    db = await get_mongo_db()
    await db.candidates.update_one(
        target_filter,
        {"$unset": {"workflow_password": "", "workflow_employee_email": ""}},
    )
    await _clear_workflow_sessions()
    return {"ok": True}


@router.get("/workflow-tasks")
async def list_workflow_tasks(
    candidate_id: Optional[str] = None,
    auth: dict = Depends(get_candidate_auth),
):
    """
    Returns tasks for the Complete-Infiverse employee matching this candidate's email (JWT),
    optionally overridden by workflow_employee_email on the candidate document.
    """
    portal_cand = _resolve_portal_candidate_id(auth, candidate_id)
    _, wf_uid = await _get_workflow_session(auth)
    r = await _wf_request("GET", f"/users/{wf_uid}/tasks", auth)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Workflow user tasks failed: {r.text[:300]}")
    raw_tasks = r.json()
    if not isinstance(raw_tasks, list):
        raise HTTPException(status_code=502, detail="Unexpected workflow response for user tasks.")

    sem = asyncio.Semaphore(10)

    async def build_one(t: Dict[str, Any]) -> Dict[str, Any]:
        tid = _str_id(t.get("_id"))
        async with sem:
            sub = await _fetch_submission_for_task(tid, auth)
        return _map_task(t, sub, portal_cand)

    mapped = await asyncio.gather(*[build_one(t) for t in raw_tasks])
    return {"tasks": mapped, "workflow_user_id": wf_uid}


@router.get("/workflow-tasks/{task_id}")
async def get_workflow_task_detail(
    task_id: str,
    candidate_id: Optional[str] = None,
    auth: dict = Depends(get_candidate_auth),
):
    portal_cand = _resolve_portal_candidate_id(auth, candidate_id)
    _, wf_uid = await _get_workflow_session(auth)
    task = await _load_task_for_user(task_id, auth, wf_uid)
    sub = await _fetch_submission_for_task(task_id, auth)
    return _map_task(task, sub, portal_cand)


class WorkflowSubmitBody(BaseModel):
    submission_url: str = Field(..., min_length=1, description="Repository or deliverable URL (githubLink in workflow)")

    model_config = {"populate_by_name": True}


@router.post("/workflow-tasks/{task_id}/submit")
async def submit_workflow_task(
    task_id: str,
    body: WorkflowSubmitBody,
    auth: dict = Depends(get_candidate_auth),
):
    _, wf_uid = await _get_workflow_session(auth)
    await _load_task_for_user(task_id, auth, wf_uid)

    r_sub = await _wf_request("GET", f"/submissions/task/{task_id}", auth)
    token, workflow_user_id = await _get_workflow_session(auth)

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=20.0)) as client:
        headers = {"x-auth-token": token}
        if r_sub.status_code == 200:
            sub = r_sub.json()
            sid = _str_id(sub.get("_id"))
            url = f"{WORKFLOW_API_BASE_URL}/submissions/{sid}"
            r2 = await client.put(
                url,
                headers=headers,
                data={"githubLink": body.submission_url.strip(), "notes": ""},
            )
        else:
            url = f"{WORKFLOW_API_BASE_URL}/submissions"
            r2 = await client.post(
                url,
                headers=headers,
                data={
                    "task": task_id,
                    "userId": workflow_user_id,
                    "githubLink": body.submission_url.strip(),
                    "notes": "",
                },
            )
        if r2.status_code == 401:
            token, workflow_user_id = await _get_workflow_session(auth, force_refresh=True)
            headers = {"x-auth-token": token}
            if r_sub.status_code == 200:
                sub = r_sub.json()
                sid = _str_id(sub.get("_id"))
                r2 = await client.put(
                    f"{WORKFLOW_API_BASE_URL}/submissions/{sid}",
                    headers=headers,
                    data={"githubLink": body.submission_url.strip(), "notes": ""},
                )
            else:
                r2 = await client.post(
                    f"{WORKFLOW_API_BASE_URL}/submissions",
                    headers=headers,
                    data={
                        "task": task_id,
                        "userId": workflow_user_id,
                        "githubLink": body.submission_url.strip(),
                        "notes": "",
                    },
                )
        if r2.status_code >= 400:
            try:
                detail = r2.json()
            except Exception:
                detail = r2.text
            raise HTTPException(status_code=502, detail=f"Workflow submission failed: {detail}")

    sub_final = await _fetch_submission_for_task(task_id, auth)
    task = await _load_task_for_user(task_id, auth, wf_uid)
    portal_cand = _resolve_portal_candidate_id(auth, None)
    return {"ok": True, "task": _map_task(task, sub_final, portal_cand)}
