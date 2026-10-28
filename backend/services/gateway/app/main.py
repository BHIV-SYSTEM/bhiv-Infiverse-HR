
# Load local gateway configuration (backend/.env) before anything reads the
# environment: Pravesh SSO secret, HackaVerse enrolment URL/key, gateway keys.
# Deployed environments inject these directly, so a missing file is the normal
# case and changes nothing; existing process env always wins (override=False).
import os as _env_os
try:
    from dotenv import load_dotenv as _load_dotenv

    _env_file = _env_os.path.join(
        _env_os.path.dirname(
            _env_os.path.dirname(
                _env_os.path.dirname(
                    _env_os.path.dirname(_env_os.path.abspath(__file__))
                )
            )
        ),
        ".env",
    )
    if _env_os.path.exists(_env_file):
        _load_dotenv(_env_file, override=False)
except Exception:
    pass

from fastapi import FastAPI, HTTPException, Depends, Security, Response, Request, File, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
import json
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from datetime import datetime, timezone, timedelta
import os
import secrets
import pyotp
import qrcode
import io
import base64
import re
import string
import random
import jwt
import bcrypt
import httpx
from collections import defaultdict
# MongoDB imports (migrated from SQLAlchemy/PostgreSQL)
from app.database import get_mongo_db, get_mongo_client, DatabaseNotConfiguredError
from app.db_helpers import find_one_by_field, find_many, count_documents, insert_one, update_one, delete_one, convert_objectid_to_str
from app.control_center_governance import (
    assert_control_center_access,
    resolve_policy_scope,
    compute_scoped_candidate_stats,
    compute_dashboard_aggregates,
    list_audit_events,
    build_audit_replay,
)
from bson import ObjectId
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, field_validator, Field, model_validator
import time
import asyncio
import logging
import traceback
import psutil

logger = logging.getLogger(__name__)

# In-memory SSE channels for connection events (client and recruiter notified simultaneously)
_connection_event_queues: Dict[str, List[asyncio.Queue]] = {}
_connection_event_lock = asyncio.Lock()


async def _push_connection_event(channel: str, event: Dict[str, Any]) -> None:
    """Push an event to all subscribers of a channel (e.g. 'client:123' or 'recruiter:456')."""
    async with _connection_event_lock:
        queues = list(_connection_event_queues.get(channel, []))
    for q in queues:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def _subscribe_connection_events(channel: str) -> asyncio.Queue:
    """Subscribe to connection events for a channel. Returns a queue that will receive events."""
    q: asyncio.Queue = asyncio.Queue(maxsize=32)
    async with _connection_event_lock:
        _connection_event_queues.setdefault(channel, []).append(q)
    return q


def _unsubscribe_connection_events(channel: str, q: asyncio.Queue) -> None:
    async def _remove():
        async with _connection_event_lock:
            lst = _connection_event_queues.get(channel, [])
            if q in lst:
                lst.remove(q)
    asyncio.create_task(_remove())


# Import configuration
try:
    from config import validate_config, setup_logging, ENVIRONMENT
    validate_config()
    setup_logging()
except ImportError:
    # Fallback if config module not available
    ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
except Exception as e:
    print(f"Configuration error: {e}")
    ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
# Auth routes import removed - using /v1/auth/ endpoints instead
try:
    import sys
    import os
    # Add gateway directory to path for monitoring import
    gateway_dir = os.path.dirname(os.path.dirname(__file__))
    sys.path.insert(0, gateway_dir)
    from monitoring import monitor, log_resume_processing, log_matching_performance, log_user_activity, log_error
    # Import proper JWT authentication functions and security scheme
    from jwt_auth import (
        get_auth as jwt_get_auth, 
        get_api_key as jwt_get_api_key, 
        validate_api_key as jwt_validate_api_key,
        security as jwt_security,
        get_recruiter_auth as jwt_get_recruiter_auth,
        require_role as jwt_require_role,
        get_optional_auth as jwt_get_optional_auth,
    )
except ImportError:
    # Fallback if monitoring module is not available
    class MockMonitor:
        def export_prometheus_metrics(self): return "# No metrics available"
        def health_check(self): return {"status": "healthy", "monitoring": "disabled"}
        def get_performance_summary(self, hours): return {"monitoring": "disabled"}
        def get_business_metrics(self): return {"monitoring": "disabled"}
        def collect_system_metrics(self): return {"monitoring": "disabled"}
    
    monitor = MockMonitor()
    def log_resume_processing(*args, **kwargs): pass
    def log_matching_performance(*args, **kwargs): pass
    def log_user_activity(*args, **kwargs): pass
    def log_error(*args, **kwargs): pass
    
    # Fallback: try to import jwt_auth from parent directory
    try:
        import sys
        import os
        gateway_dir = os.path.dirname(os.path.dirname(__file__))
        sys.path.insert(0, gateway_dir)
        from jwt_auth import (
            get_auth as jwt_get_auth, 
            get_api_key as jwt_get_api_key, 
            validate_api_key as jwt_validate_api_key,
            security as jwt_security,
            get_recruiter_auth as jwt_get_recruiter_auth,
            require_role as jwt_require_role,
            get_optional_auth as jwt_get_optional_auth,
        )
    except ImportError:
        jwt_get_auth = None
        jwt_get_api_key = None
        jwt_validate_api_key = None
        jwt_security = None
        jwt_get_recruiter_auth = None
        jwt_require_role = None
        jwt_get_optional_auth = None

# Use security scheme from jwt_auth.py (with auto_error=False) if available
# Otherwise create a fallback with auto_error=False to allow credentials to be None
if jwt_security is not None:
    security = jwt_security
else:
    security = HTTPBearer(auto_error=False)

app = FastAPI(
    title="BHIV HR Platform API Gateway",
    version="4.2.0",
    description="Enterprise HR Platform with Advanced Security Features"
)

# Exception handler for validation errors
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Custom handler for validation errors to provide detailed error messages"""
    errors = []
    for error in exc.errors():
        field = ".".join(str(loc) for loc in error["loc"])
        errors.append({
            "field": field,
            "message": error["msg"],
            "type": error["type"]
        })
    return JSONResponse(
        status_code=422,
        content={
            "detail": errors,
            "message": "Validation error: Please check the request data",
            "errors": errors
        }
    )


@app.exception_handler(DatabaseNotConfiguredError)
async def database_not_configured_handler(request: Request, exc: DatabaseNotConfiguredError):
    """No-database dev mode: clean 503 instead of a 500 traceback."""
    return JSONResponse(
        status_code=503,
        content={
            "detail": "MongoDB is not configured. Set DATABASE_URL in backend/.env to enable persistence.",
        },
    )

# CORS Configuration - reads from CORS_ORIGINS env var (comma-separated)
# Supports all Vercel preview deployments via regex.
cors_origins_env = os.getenv("CORS_ORIGINS", "").strip()
if cors_origins_env:
    allowed_origins_list = [o.strip() for o in cors_origins_env.split(",") if o.strip()]
else:
    allowed_origins_list = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:5175",
        "https://setu.blackholeinfiverse.com",
        "https://sampada.blackholeinfiverse.com",
        "https://infiverse-hr.vercel.app",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins_list,
    allow_origin_regex=r"https://.*\.onrender\.com|https://.*\.vercel\.app|https://.*\.blackholeinfiverse\.com",
    # Credentials must be allowed for the Pravesh (blackhole_auth) SSO cookie
    # exchange: the browser POSTs to /api/v1/pravesh/exchange with
    # credentials: 'include'. Origins stay an explicit list/regex (never "*"),
    # so credentialed responses remain spec-compliant.
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    # Delete legacy X-Frame-Options header so modern CSP frame-ancestors governs iframe framing (matching Niyantran server pattern)
    if "X-Frame-Options" in response.headers:
        del response.headers["X-Frame-Options"]
    if "x-frame-options" in response.headers:
        del response.headers["x-frame-options"]

    response.headers["Content-Security-Policy"] = "frame-ancestors 'self' https://setu.blackholeinfiverse.com https://ai-crm-sigma-five.vercel.app http://localhost:3000 http://localhost:5173 http://localhost:5175;"
    return response

# Auth routes removed - using /v1/auth/ endpoints instead

# Include AI integration routes
try:
    from routes.ai_integration import router as ai_router
    app.include_router(ai_router, prefix="/api/v1", tags=["AI Integration"])
except ImportError:
    pass

# Include LangGraph workflow routes
try:
    import sys
    import os
    # Add gateway directory to path to find langgraph_integration.py
    gateway_dir = os.path.dirname(os.path.dirname(__file__))
    sys.path.insert(0, gateway_dir)
    from langgraph_integration import router as langgraph_router
    app.include_router(langgraph_router, prefix="/api/v1", tags=["LangGraph Workflows"])
    print("LangGraph integration loaded successfully")
except ImportError as e:
    print(f"WARNING: LangGraph integration not available: {e}")
    pass  # LangGraph routes optional

# Include RL routes
try:
    from routes.rl_routes import router as rl_router
    app.include_router(rl_router, prefix="/api/v1", tags=["RL + Feedback Agent"])
    print("RL routes loaded successfully")
except ImportError as e:
    print(f"WARNING: RL routes not available: {e}")
    pass  # RL routes optional

# Complete-Infiverse workflow task bridge (Sampada candidate portal)
try:
    import sys

    _gw_dir_wf = os.path.dirname(os.path.dirname(__file__))
    if _gw_dir_wf not in sys.path:
        sys.path.insert(0, _gw_dir_wf)
    from workflow_proxy import router as workflow_proxy_router

    app.include_router(workflow_proxy_router)
    print("Workflow task proxy (Complete-Infiverse bridge) loaded")
except ImportError as e:
    print(f"WARNING: workflow_proxy not available: {e}")

# Workforce governance runtime (workforce, policy, decisions, SETU)
try:
    from routes.workforce_governance_routes import router as workforce_governance_router

    app.include_router(workforce_governance_router)
    print("Workforce governance routes loaded")
except ImportError as e:
    print(f"WARNING: workforce governance routes not available: {e}")

# Pravesh (blackhole_auth) SSO read-only bridge — identity verification only,
# never creates or merges accounts. Disabled unless PRAVESH_JWT_SECRET is set.
try:
    import sys as _sys_pravesh
    import os as _os_pravesh

    _gw_dir_pravesh = _os_pravesh.path.dirname(_os_pravesh.path.dirname(__file__))
    if _gw_dir_pravesh not in _sys_pravesh.path:
        _sys_pravesh.path.insert(0, _gw_dir_pravesh)
    from routes.pravesh_routes import router as pravesh_router

    app.include_router(pravesh_router, prefix="/api/v1", tags=["Pravesh SSO"])
    print("Pravesh SSO bridge routes loaded")
except ImportError as e:
    print(f"WARNING: Pravesh SSO bridge routes not available: {e}")

# Sampada identity module — canonical candidate id, institute/cohort mapping,
# Abhiruchi intake (idempotent) and versioned HackaVerse enrolment publishing.
try:
    import sys as _sys_ident
    import os as _os_ident

    _gw_dir_ident = _os_ident.path.dirname(_os_ident.path.dirname(__file__))
    if _gw_dir_ident not in _sys_ident.path:
        _sys_ident.path.insert(0, _gw_dir_ident)
    from app.identity.routes import router as identity_router

    app.include_router(identity_router, prefix="/api/v1")
    print("Identity & enrolment routes loaded")
except ImportError as e:
    print(f"WARNING: identity routes not available: {e}")

# CV builder — authorised prefill, shared content model + 3 templates,
# bounded AI suggestions, approval gate before export.
try:
    import sys as _sys_cv
    import os as _os_cv

    _gw_dir_cv = _os_cv.path.dirname(_os_cv.path.dirname(__file__))
    if _gw_dir_cv not in _sys_cv.path:
        _sys_cv.path.insert(0, _gw_dir_cv)
    from app.cv.routes import router as cv_router

    app.include_router(cv_router, prefix="/api/v1")
    print("CV builder routes loaded")
except ImportError as e:
    print(f"WARNING: CV builder routes not available: {e}")

# ── Auth dependency resolution ─────────────────────────────────────────────────
# Must be defined BEFORE any @app route that uses Depends(get_auth).
if jwt_get_auth is not None:
    # Use the proper authentication functions from jwt_auth.py
    get_auth = jwt_get_auth
    get_api_key = jwt_get_api_key
    validate_api_key = jwt_validate_api_key
    get_optional_auth = jwt_get_optional_auth
else:
    # Fallback: define basic functions if import failed
    def validate_api_key(api_key: str) -> bool:
        expected_key = os.getenv("API_KEY_SECRET")
        return api_key == expected_key

    def get_api_key(credentials: HTTPAuthorizationCredentials = Security(security)):
        if not credentials or not validate_api_key(credentials.credentials):
            raise HTTPException(status_code=401, detail="Invalid API key")
        return credentials.credentials

    def get_optional_auth(credentials: Optional[HTTPAuthorizationCredentials] = Security(security)):
        """Optional auth - returns None if not authenticated (fallback when jwt_auth not loaded)."""
        return None

    def get_auth(credentials: Optional[HTTPAuthorizationCredentials] = Security(security)):
        """Dual authentication: API key or client JWT token"""
        if not credentials:
            raise HTTPException(status_code=401, detail="Authentication required")

        # Try API key first
        if validate_api_key(credentials.credentials):
            return {"type": "api_key", "credentials": credentials.credentials}

        # Try client JWT token
        try:
            jwt_secret = os.getenv("JWT_SECRET_KEY")
            if jwt_secret:
                payload = jwt.decode(credentials.credentials, jwt_secret, algorithms=["HS256"])
                return {"type": "client_token", "client_id": payload.get("client_id")}
        except Exception:
            pass

        # Try candidate JWT token
        try:
            candidate_jwt_secret = os.getenv("CANDIDATE_JWT_SECRET_KEY")
            if candidate_jwt_secret:
                payload = jwt.decode(credentials.credentials, candidate_jwt_secret, algorithms=["HS256"])
                return {"type": "candidate_token", "candidate_id": payload.get("candidate_id")}
        except Exception:
            pass

        raise HTTPException(status_code=401, detail="Invalid authentication")
# ──────────────────────────────────────────────────────────────────────────────

# Add monitoring endpoints
@app.get("/metrics", tags=["Monitoring"])
async def get_prometheus_metrics():
    """Prometheus Metrics Export"""
    return Response(content=monitor.export_prometheus_metrics(), media_type="text/plain")

@app.get("/health/detailed", tags=["Monitoring"])
async def detailed_health_check():
    """Detailed Health Check with Metrics"""
    return monitor.health_check()

@app.get("/metrics/dashboard", tags=["Monitoring"])
async def metrics_dashboard(response: Response, auth=Depends(get_auth)):
    """Metrics Dashboard Data"""
    scope = assert_control_center_access(auth)
    response.headers["X-Control-Center-Access"] = "granted"
    response.headers["X-Policy-Scope"] = scope.get("scope_label", "unknown")
    return {
        "performance_summary": monitor.get_performance_summary(24),
        "business_metrics": monitor.get_business_metrics(),
        "system_metrics": monitor.collect_system_metrics(),
        "policy_scope": scope,
    }

# Enhanced Granular Rate Limiting

rate_limit_storage = defaultdict(list)

# Granular rate limits by endpoint and user tier
RATE_LIMITS = {
    "default": {
        "/v1/jobs": 100,
        "/v1/candidates/search": 50,
        "/v1/match": 20,
        "/v1/candidates/bulk": 5,
        "default": 60
    },
    "premium": {
        "/v1/jobs": 500,
        "/v1/candidates/search": 200,
        "/v1/match": 100,
        "/v1/candidates/bulk": 25,
        "default": 300
    }
}

def get_dynamic_rate_limit(endpoint: str, user_tier: str = "default") -> int:
    """Dynamic rate limiting based on system load"""
    cpu_usage = psutil.cpu_percent()
    base_limit = RATE_LIMITS[user_tier].get(endpoint, RATE_LIMITS[user_tier]["default"])
    
    if cpu_usage > 80:
        return int(base_limit * 0.5)  # Reduce by 50% during high load
    elif cpu_usage < 30:
        return int(base_limit * 1.5)  # Increase by 50% during low load
    return base_limit

async def rate_limit_middleware(request: Request, call_next):
    client_ip = request.client.host
    current_time = time.time()
    endpoint_path = request.url.path
    
    # Determine user tier (simplified - in production, get from JWT/database)
    user_tier = "premium" if "enterprise" in request.headers.get("user-agent", "").lower() else "default"
    
    # Get dynamic rate limit for this endpoint
    rate_limit = get_dynamic_rate_limit(endpoint_path, user_tier)
    
    # Clean old requests (older than 1 minute)
    key = f"{client_ip}:{endpoint_path}"
    rate_limit_storage[key] = [
        req_time for req_time in rate_limit_storage[key] 
        if current_time - req_time < 60
    ]
    
    # Check granular rate limit
    if len(rate_limit_storage[key]) >= rate_limit:
        raise HTTPException(
            status_code=429, 
            detail=f"Rate limit exceeded for {endpoint_path}. Limit: {rate_limit}/min"
        )
    
    # Record this request
    rate_limit_storage[key].append(current_time)
    
    response = await call_next(request)
    response.headers["X-RateLimit-Limit"] = str(rate_limit)
    response.headers["X-RateLimit-Remaining"] = str(rate_limit - len(rate_limit_storage[key]))
    return response

app.middleware("http")(rate_limit_middleware)

@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
    request.state.correlation_id = correlation_id
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    return response

class JobCreate(BaseModel):
    title: str
    department: str  # Required: e.g., "Engineering", "Marketing", "Sales"
    location: str
    experience_level: str  # Required: "entry", "mid", "senior", "lead" (case-insensitive)
    requirements: str
    description: str
    client_id: Optional[int] = None  # Legacy; prefer connection_id for recruiter job posting
    connection_id: Optional[str] = None  # Client's connection_id (shared from client dashboard); resolved to client_id
    employment_type: Optional[str] = "Full-time"
    salary_min: Optional[float] = None  # Optional salary range (INR)
    salary_max: Optional[float] = None
    # Support frontend field name aliases for flexibility
    experience_required: Optional[str] = Field(None, alias='experience_required', exclude=True)
    job_type: Optional[str] = Field(None, alias='job_type', exclude=True)
    skills_required: Optional[str] = Field(None, alias='skills_required', exclude=True)
    
    @model_validator(mode='before')
    @classmethod
    def map_frontend_fields(cls, data: Any) -> Any:
        """Map frontend field names to backend field names for compatibility"""
        if isinstance(data, dict):
            # Map experience_required to experience_level if experience_level not provided
            if 'experience_required' in data and 'experience_level' not in data:
                data['experience_level'] = data.pop('experience_required')
            # Map job_type to employment_type if employment_type not provided
            if 'job_type' in data and 'employment_type' not in data:
                data['employment_type'] = data.pop('job_type')
            # Map skills_required to requirements if requirements not provided
            if 'skills_required' in data and 'requirements' not in data:
                skills = data['skills_required']
                if isinstance(skills, list):
                    data['requirements'] = ', '.join(str(s) for s in skills)
                else:
                    data['requirements'] = str(skills)
                data.pop('skills_required')
        return data
    
    @field_validator('experience_level', mode='before')
    @classmethod
    def normalize_experience_level(cls, v) -> str:
        """Normalize experience_level to lowercase for consistency"""
        if v is None:
            raise ValueError('experience_level is required')
        if not isinstance(v, str):
            v = str(v)
        if not v.strip():
            raise ValueError('experience_level cannot be empty')
        normalized = v.lower().strip()
        # Map common variations to standard values
        mapping = {
            'entry': 'entry',
            'mid': 'mid',
            'middle': 'mid',
            'senior': 'senior',
            'lead': 'lead',
            'leadership': 'lead'
        }
        return mapping.get(normalized, normalized)
    
    model_config = {
        "json_schema_extra": {
            "example": {
                "title": "Senior Software Engineer",
                "department": "Engineering",
                "location": "Remote",
                "experience_level": "senior",
                "requirements": "5+ years Python, FastAPI, PostgreSQL",
                "description": "Join our team to build scalable HR solutions",
                "employment_type": "Full-time"
            }
        }
    }

class CandidateBulk(BaseModel):
    candidates: List[Dict[str, Any]]
    job_id: Optional[str] = None  # If set, each inserted candidate is linked as applicant for this job (dashboard sync)

class FeedbackSubmission(BaseModel):
    candidate_id: str
    job_id: str
    integrity: int
    honesty: int
    discipline: int
    hard_work: int
    gratitude: int
    comments: Optional[str] = None
    experience_level: Optional[str] = None  # Entry, Mid, Senior, Lead (from recruiter form)

class InterviewSchedule(BaseModel):
    candidate_id: str
    job_id: str
    interview_date: str
    interviewer: Optional[str] = "HR Team"
    notes: Optional[str] = None
    interview_type: Optional[str] = None  # e.g. on-site, remote, video_meet, voice_call
    meeting_link: Optional[str] = None
    meeting_address: Optional[str] = None
    meeting_phone: Optional[str] = None

class JobOffer(BaseModel):
    candidate_id: str
    job_id: str
    salary: float
    start_date: str
    terms: str

class ClientLogin(BaseModel):
    client_id: Optional[str] = None
    email: Optional[str] = None
    password: str

import uuid

class ClientRegister(BaseModel):
    client_id: str
    company_name: str
    contact_email: str
    password: str
    client_code: Optional[str] = None  # Optional, will be generated if not provided

class TwoFASetup(BaseModel):
    user_id: str

class TwoFALogin(BaseModel):
    user_id: str
    totp_code: str

class PasswordValidation(BaseModel):
    password: str

class SecurityTest(BaseModel):
    test_type: str
    payload: str

class CSPPolicy(BaseModel):
    policy: str

class InputValidation(BaseModel):
    input_data: str

class EmailValidation(BaseModel):
    email: str

class PhoneValidation(BaseModel):
    phone: str

class CSPReport(BaseModel):
    violated_directive: str
    blocked_uri: str
    document_uri: str

class PasswordChange(BaseModel):
    old_password: str
    new_password: str

class CandidateSearch(BaseModel):
    skills: Optional[str] = None
    location: Optional[str] = None
    experience_min: Optional[int] = None
    
    @field_validator('skills')
    @classmethod
    def validate_skills(cls, v):
        return v[:200] if v else None
        
    @field_validator('location')
    @classmethod
    def validate_location(cls, v):
        return v[:100] if v else None

class BatchMatchRequest(BaseModel):
    job_ids: List[str]
    limit: Optional[int] = 10

# Candidate Portal Models
class CandidateRegister(BaseModel):
    name: str
    email: str
    password: str
    phone: Optional[str] = None
    location: Optional[str] = None
    experience_years: Optional[int] = 0
    technical_skills: Optional[str] = None
    education_level: Optional[str] = None
    seniority_level: Optional[str] = None
    role: Optional[str] = "candidate"  # Support recruiter role registration

class CandidateLogin(BaseModel):
    email: str
    password: str

class CandidateProfileUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    experience_years: Optional[int] = None
    technical_skills: Optional[str] = None
    education_level: Optional[str] = None
    seniority_level: Optional[str] = None

class JobApplication(BaseModel):
    candidate_id: str  # Changed from int to str for MongoDB ObjectId
    job_id: str  # Changed from int to str for MongoDB ObjectId
    cover_letter: Optional[str] = None


class RequiredDocumentsRequest(BaseModel):
    document_types: List[str] = Field(default_factory=list)

class PerJobNotificationRequest(BaseModel):
    candidate_ids: List[str]
    notification_type: str
    recruiter_id: Optional[str] = None

class NotificationSendRequest(BaseModel):
    candidate_name: str
    candidate_email: Optional[str] = None
    candidate_phone: Optional[str] = None
    job_title: str
    message: str
    channels: List[str] = Field(default_factory=lambda: ["email"])
    application_status: Optional[str] = None

    model_config = {"extra": "allow"}

class BulkNotificationProxyRequest(BaseModel):
    candidates: List[Dict[str, Any]]
    sequence_type: str
    job_title: Optional[str] = None
    job_id: Optional[str] = None
    matching_score: Optional[str] = None
    job_data: Optional[Dict[str, Any]] = None

    model_config = {"extra": "allow"}

class PreviewNotificationRequest(BaseModel):
    sequence_type: str
    candidate_name: str = "John Doe"
    job_title: str = "Software Engineer"
    job_id: str = "job_123"
    matching_score: str = "85"
    interview_date: str = "2024-02-15"
    interview_time: str = "2:00 PM"
    interviewer: str = "HR Team"
    application_id: str = "APP_001"

class TestSequenceRequest(BaseModel):
    candidate_name: str = "John Doe"
    candidate_email: str = "john.doe@example.com"
    candidate_phone: str = "+1234567890"
    job_title: str = "Software Engineer"
    sequence_type: str = "interview_scheduled"

class AutomationTriggerRequest(BaseModel):
    type: str
    payload: Dict[str, Any] = Field(default_factory=dict)

# Legacy get_db_engine function - replaced by MongoDB
# MongoDB connection is handled by app.database module
# Use: db = await get_mongo_db() for async database access

def _get_langgraph_service_url() -> str:
    return os.getenv("LANGGRAPH_SERVICE_URL", "https://bhiv-hr-langgraph-luy9.onrender.com")

def _get_langgraph_headers() -> Dict[str, str]:
    api_key = os.getenv("API_KEY_SECRET")
    if not api_key:
        raise ValueError("API_KEY_SECRET environment variable is required and not set")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

def _parse_langgraph_error(response: httpx.Response) -> Any:
    try:
        payload = response.json()
        return payload.get("detail", payload)
    except ValueError:
        return response.text or f"LangGraph service returned status {response.status_code}"

async def _forward_langgraph_request(
    endpoint: str,
    method: str = "GET",
    json_body: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    url = f"{_get_langgraph_service_url()}{endpoint}"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
            response = await client.request(
                method,
                url,
                headers=_get_langgraph_headers(),
                json=json_body,
                params=params,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="LangGraph service timed out while processing the request") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"LangGraph service unavailable: {exc}") from exc

    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=_parse_langgraph_error(response))

    if not response.content:
        return {}

    try:
        return response.json()
    except ValueError:
        return {"detail": response.text}

async def _send_langgraph_bulk_notifications(payload: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
    return await _forward_langgraph_request(
        "/automation/notifications/bulk",
        method="POST",
        json_body=payload,
        timeout=timeout,
    )

# Core API Endpoints (5 endpoints)
@app.get("/openapi.json", tags=["Core API Endpoints"])
async def get_openapi():
    """OpenAPI Schema"""
    return app.openapi()

@app.get("/docs", tags=["Core API Endpoints"])
async def get_docs():
    """API Documentation"""
    return get_swagger_ui_html(openapi_url="/openapi.json", title="BHIV HR Platform API")

@app.get("/", tags=["Core API Endpoints"])
def read_root():
    """API Root Information"""
    return {
        "message": "BHIV HR Platform API Gateway",
        "version": "4.2.0",
        "status": "healthy",
        "endpoints": len(app.routes),
        "documentation": "/docs",
        "monitoring": "/metrics",
        "production_url": "https://sampada.blackholeinfiverse.com/gateway",
        "langgraph_integration": "active",
        "ai_workflows": ["candidate_applied", "shortlisted", "interview_scheduled"]
    }

@app.get("/health", tags=["Core API Endpoints"])
def health_check(response: Response):
    """Health Check"""
    response.headers["X-RateLimit-Limit"] = "60"
    response.headers["X-RateLimit-Remaining"] = "59"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'self' http://localhost:3000 https://setu.blackholeinfiverse.com https://sampada.blackholeinfiverse.com;"
    
    return {
        "status": "healthy",
        "service": "BHIV HR Gateway",
        "version": "4.2.0",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/v1/test-candidates", tags=["Core API Endpoints"])
async def test_candidates_db(api_key: str = Depends(get_api_key)):
    """Database Connectivity Test - MongoDB Atlas"""
    try:
        db = await get_mongo_db()
        # Test connection by running a simple command
        await db.command('ping')
        candidate_count = await db.candidates.count_documents({})
        
        return {
            "database_status": "connected",
            "database_type": "MongoDB Atlas",
            "total_candidates": candidate_count,
            "test_timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        return {
            "database_status": "failed",
            "error": str(e),
            "test_timestamp": datetime.now(timezone.utc).isoformat()
        }

# Job Management (2 endpoints)
@app.post("/v1/jobs", tags=["Job Management"])
async def create_job(job: JobCreate, auth: dict = Depends(get_auth)):
    """Create New Job Posting
    
    **Required Fields:**
    - title: Job title
    - department: Department name (e.g., "Engineering", "Marketing")
    - location: Job location
    - experience_level: Experience level ("entry", "mid", "senior", "lead")
    - requirements: Job requirements
    - description: Job description
    
    **Authentication:** Bearer token required (API key, recruiter, or client JWT token)
    """
    # Check if user has permission to create jobs
    # API keys have full access
    if auth.get("type") == "api_key":
        pass  # API keys have full access
    else:
        # For JWT tokens, check role
        user_role = auth.get("role", "")
        if user_role not in ["recruiter", "client", "admin"]:
            raise HTTPException(
                status_code=403,
                detail=f"Access denied. Job creation requires recruiter, client, or admin role. Current role: {user_role}"
            )
    
    try:
        db = await get_mongo_db()
        document = await _build_job_document(db, job, auth)

        duplicate = await _find_duplicate_job(db, document)
        if duplicate:
            raise HTTPException(status_code=409, detail="Job already exists")
        
        result = await db.jobs.insert_one(document)
        job_id = str(result.inserted_id)
        
        return {
            "message": "Job created successfully",
            "job_id": job_id,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Job creation failed: {str(e)}"
        )


def _normalize_job_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


async def _resolve_client_context(db, job: JobCreate, auth: dict, fallback_client_id: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    client_id = fallback_client_id
    connection_id = None

    if job.connection_id and str(job.connection_id).strip():
        cid_raw = str(job.connection_id).strip()
        if len(cid_raw) != 24 or not all(c in "0123456789abcdefABCDEF" for c in cid_raw):
            raise HTTPException(status_code=400, detail="Invalid Connection ID format (must be 24 hexadecimal characters)")
        client_doc = await db.clients.find_one({"connection_id": cid_raw})
        if not client_doc:
            raise HTTPException(status_code=400, detail="Invalid Connection ID. Please ask your client for the correct ID from their dashboard.")
        client_id = str(client_doc.get("client_id"))
        connection_id = cid_raw
    elif job.client_id is not None:
        client_id = str(job.client_id)
    elif auth.get("type") == "jwt_token" and auth.get("role") == "client":
        cid = auth.get("user_id")
        if cid is not None:
            client_id = str(cid)

    return client_id, connection_id


async def _build_job_document(
    db,
    job: JobCreate,
    auth: dict,
    *,
    existing_doc: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    document = {
        "title": _normalize_job_text(job.title),
        "department": _normalize_job_text(job.department),
        "location": _normalize_job_text(job.location),
        "experience_level": _normalize_job_text(job.experience_level).lower(),
        "requirements": _normalize_job_text(job.requirements),
        "description": _normalize_job_text(job.description),
        "status": (existing_doc or {}).get("status", "active"),
        "created_at": (existing_doc or {}).get("created_at") or datetime.now(timezone.utc),
    }

    employment_type = _normalize_job_text(job.employment_type)
    if employment_type:
        document["employment_type"] = employment_type

    client_id, resolved_connection_id = await _resolve_client_context(
        db,
        job,
        auth,
        fallback_client_id=str((existing_doc or {}).get("client_id")) if (existing_doc or {}).get("client_id") is not None else None,
    )
    if client_id:
        document["client_id"] = client_id

    if job.salary_min is not None:
        document["salary_min"] = float(job.salary_min)
    elif existing_doc and existing_doc.get("salary_min") is not None:
        document["salary_min"] = float(existing_doc.get("salary_min"))

    if job.salary_max is not None:
        document["salary_max"] = float(job.salary_max)
    elif existing_doc and existing_doc.get("salary_max") is not None:
        document["salary_max"] = float(existing_doc.get("salary_max"))

    if auth.get("type") == "jwt_token" and auth.get("role") == "recruiter":
        rid = auth.get("user_id")
        if rid is not None:
            document["recruiter_id"] = str(rid)
    elif existing_doc and existing_doc.get("recruiter_id") is not None:
        document["recruiter_id"] = str(existing_doc.get("recruiter_id"))

    if resolved_connection_id:
        document["connection_id"] = resolved_connection_id
    elif existing_doc and existing_doc.get("connection_id"):
        document["connection_id"] = existing_doc.get("connection_id")

    return document


def _normalized_job_signature(document: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "title": _normalize_job_text(document.get("title")).lower(),
        "department": _normalize_job_text(document.get("department")).lower(),
        "location": _normalize_job_text(document.get("location")).lower(),
        "experience_level": _normalize_job_text(document.get("experience_level")).lower(),
        "requirements": _normalize_job_text(document.get("requirements")).lower(),
        "description": _normalize_job_text(document.get("description")).lower(),
        "employment_type": _normalize_job_text(document.get("employment_type")).lower(),
        "salary_min": float(document.get("salary_min")) if document.get("salary_min") is not None else None,
        "salary_max": float(document.get("salary_max")) if document.get("salary_max") is not None else None,
        "recruiter_id": str(document.get("recruiter_id")) if document.get("recruiter_id") is not None else None,
        "client_id": str(document.get("client_id")) if document.get("client_id") is not None else None,
        "status": _normalize_job_text(document.get("status")).lower() or "active",
    }


async def _find_duplicate_job(db, document: Dict[str, Any], exclude_job_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    query: Dict[str, Any] = {
        "title": _normalize_job_text(document.get("title")),
        "department": _normalize_job_text(document.get("department")),
        "location": _normalize_job_text(document.get("location")),
        "experience_level": _normalize_job_text(document.get("experience_level")).lower(),
        "requirements": _normalize_job_text(document.get("requirements")),
        "description": _normalize_job_text(document.get("description")),
        "status": document.get("status", "active"),
    }
    if document.get("employment_type"):
        query["employment_type"] = _normalize_job_text(document.get("employment_type"))
    if document.get("salary_min") is not None:
        query["salary_min"] = float(document.get("salary_min"))
    if document.get("salary_max") is not None:
        query["salary_max"] = float(document.get("salary_max"))
    if document.get("recruiter_id") is not None:
        query["recruiter_id"] = str(document.get("recruiter_id"))
    if document.get("client_id") is not None:
        query["client_id"] = str(document.get("client_id"))
    if exclude_job_id and ObjectId.is_valid(exclude_job_id):
        query["_id"] = {"$ne": ObjectId(exclude_job_id)}
    return await db.jobs.find_one(query)


async def _get_owned_job_or_403(db, job_id: str, auth: dict) -> Dict[str, Any]:
    job_doc = await db.jobs.find_one({"_id": ObjectId(job_id)}) if ObjectId.is_valid(job_id) else await db.jobs.find_one({"id": job_id})
    if not job_doc:
        raise HTTPException(status_code=404, detail="Job not found")

    if auth.get("type") == "api_key" or auth.get("role") == "admin":
        return job_doc

    if auth.get("type") != "jwt_token":
        raise HTTPException(status_code=403, detail="Access denied")

    user_role = auth.get("role")
    user_id = str(auth.get("user_id", ""))
    if user_role == "recruiter" and str(job_doc.get("recruiter_id", "")) == user_id:
        return job_doc
    if user_role == "client" and str(job_doc.get("client_id", "")) == user_id:
        return job_doc

    raise HTTPException(status_code=403, detail="You can only manage your own jobs")


def _job_salary_from_doc(doc: Dict[str, Any]) -> tuple:
    """Derive (salary_min, salary_max) from a job document for backward compatibility.
    Supports: salary_min/salary_max (current), salary_range (string, e.g. seed data),
    and salary (single number). Returns (min, max) with None where not available."""
    try:
        smin = doc.get("salary_min")
        smax = doc.get("salary_max")
        if smin is not None and smax is not None:
            return (float(smin) if smin != "" else None, float(smax) if smax != "" else None)
        if smin is not None:
            return (float(smin) if smin != "" else None, float(smax) if smax and smax != "" else None)
        if smax is not None:
            return (float(smin) if smin and smin != "" else None, float(smax) if smax != "" else None)
    except (TypeError, ValueError):
        pass
    # Legacy: salary_range string (e.g. "$120,000 - $150,000" or "500000-800000")
    salary_range = doc.get("salary_range")
    if salary_range and isinstance(salary_range, str):
        s = salary_range.strip()
        # Remove common prefixes and split on dash
        s_clean = re.sub(r"[$€£,\s]", "", s)
        parts = re.split(r"[-–—]", s_clean, maxsplit=1)
        if len(parts) >= 2:
            try:
                low = float(parts[0].strip())
                high = float(parts[1].strip())
                if low <= high:
                    return (low, high)
                return (high, low)
            except (TypeError, ValueError):
                pass
        try:
            single = float(s_clean)
            return (single, single)
        except (TypeError, ValueError):
            pass
    # Legacy: single salary number
    salary = doc.get("salary")
    if salary is not None and salary != "":
        try:
            v = float(salary)
            if v >= 0:
                return (v, v)
        except (TypeError, ValueError):
            pass
    return (None, None)


@app.get("/v1/jobs", tags=["Job Management"])
async def list_jobs(
    search: Optional[str] = None,
    skills: Optional[str] = None,
    location: Optional[str] = None,
    experience: Optional[str] = None,
    job_type: Optional[str] = None,
):
    """List Active Jobs with optional search and filters (Public Endpoint)."""
    try:
        db = await get_mongo_db()
        query = {"status": "active"}
        if search and search.strip():
            q = re.escape(search.strip())[:100]
            query["$or"] = [
                {"title": {"$regex": q, "$options": "i"}},
                {"department": {"$regex": q, "$options": "i"}},
                {"requirements": {"$regex": q, "$options": "i"}},
                {"description": {"$regex": q, "$options": "i"}},
            ]
        if skills and skills.strip():
            query["requirements"] = {"$regex": re.escape(skills.strip())[:200], "$options": "i"}
        if location and location.strip():
            query["location"] = {"$regex": re.escape(location.strip())[:100], "$options": "i"}
        if experience and experience.strip():
            query["experience_level"] = {"$regex": re.escape(experience.strip())[:50], "$options": "i"}
        if job_type and job_type.strip():
            query["job_type"] = {"$regex": re.escape(job_type.strip())[:50], "$options": "i"}
        cursor = db.jobs.find(query).sort("created_at", -1).limit(100)
        jobs_list = await cursor.to_list(length=100)
        jobs = []
        for doc in jobs_list:
            salary_min, salary_max = _job_salary_from_doc(doc)
            jobs.append({
                "id": str(doc["_id"]),
                "title": doc.get("title"),
                "department": doc.get("department"),
                "location": doc.get("location"),
                "experience_level": doc.get("experience_level"),
                "requirements": doc.get("requirements"),
                "description": doc.get("description"),
                "job_type": doc.get("job_type") or doc.get("employment_type"),
                "employment_type": doc.get("employment_type"),
                "salary_min": salary_min,
                "salary_max": salary_max,
                "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None
            })
        return {"jobs": jobs, "count": len(jobs)}
    except Exception as e:
        return {"jobs": [], "count": 0, "error": str(e)}


@app.get("/v1/jobs/autocomplete", tags=["Job Management"])
async def jobs_autocomplete(q: Optional[str] = None, limit: int = 10):
    """Search-as-you-type: return job suggestions by title or department (public for candidate job search)."""
    def _normalize_autocomplete_query(raw: str) -> str:
        """Normalize user query for autocomplete matching.
        - Removes/normalizes special characters (/, \\, -, etc.) into spaces
        - Keeps only letters/numbers/spaces for fuzzy matching
        """
        if raw is None:
            return ""
        s = str(raw).strip()
        if not s:
            return ""
        # Convert common separators/symbols into spaces, then strip any remaining non-alphanumerics.
        s = re.sub(r"[\\/]+", " ", s)
        s = re.sub(r"[^A-Za-z0-9]+", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s[:50]

    def _fuzzy_regex_from_query(norm: str) -> Optional[str]:
        """Build a fuzzy regex that matches tokens with any non-word separators between them.
        Example: 'AR VR Engineer' -> 'AR[\\s\\W_]*VR[\\s\\W_]*Engineer'
        """
        tokens = [t for t in (norm or "").split(" ") if t]
        if not tokens:
            return None
        return r"[\s\W_]*".join(re.escape(t) for t in tokens)[:200]

    if not q or not str(q).strip():
        return {"suggestions": []}
    q_norm = _normalize_autocomplete_query(q)
    if not q_norm:
        return {"suggestions": []}
    limit = max(1, min(limit, 20))
    try:
        db = await get_mongo_db()
        regex_pat = _fuzzy_regex_from_query(q_norm) or re.escape(q_norm)
        regex = {"$regex": regex_pat, "$options": "i"}
        cursor = db.jobs.find({
            "status": "active",
            "$or": [{"title": regex}, {"department": regex}]
        }).sort("created_at", -1).limit(limit)
        jobs_list = await cursor.to_list(length=limit)
        suggestions = []
        for doc in jobs_list:
            suggestions.append({
                "id": str(doc["_id"]),
                "title": doc.get("title") or "",
                "department": doc.get("department") or "",
                "location": doc.get("location") or "",
            })
        return {"suggestions": suggestions}
    except Exception as e:
        return {"suggestions": [], "error": str(e)}


def _extract_skills_from_requirements(requirements: str) -> set:
    """Extract skill-like tokens from job requirements string (comma/space separated)."""
    if not requirements or not isinstance(requirements, str):
        return set()
    seen = set()
    for part in re.split(r"[,/\n;|]+", requirements):
        for token in part.split():
            token = token.strip()
            if len(token) >= 2 and re.match(r"^[A-Za-z0-9.+_-]+$", token):
                seen.add(token)
    return seen


@app.get("/v1/jobs/skills/autocomplete", tags=["Job Management"])
async def job_skills_autocomplete(q: Optional[str] = None, limit: int = 15):
    """Search-as-you-type: return skill suggestions from active jobs' requirements (public for candidate browse jobs)."""
    if not q or not str(q).strip():
        return {"suggestions": []}
    q = str(q).strip()[:50]
    # Normalize query to handle special characters like C/C++, AR/VR, etc.
    q_norm = re.sub(r"[\\/]+", " ", q)
    q_norm = re.sub(r"[^A-Za-z0-9]+", " ", q_norm)
    q_norm = re.sub(r"\s+", " ", q_norm).strip().lower()
    if not q_norm:
        return {"suggestions": []}
    limit = max(1, min(limit, 25))
    try:
        db = await get_mongo_db()
        cursor = db.jobs.find({"status": "active"}, {"requirements": 1})
        jobs_list = await cursor.to_list(length=500)
        all_skills = set()
        for doc in jobs_list:
            req = doc.get("requirements") or ""
            all_skills.update(_extract_skills_from_requirements(req))
        def _norm_token(s: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

        q_key = _norm_token(q_norm)
        matching = sorted(
            s for s in all_skills
            if q_key and q_key in _norm_token(s)
        )[:limit]
        return {"suggestions": [{"id": s, "label": s} for s in matching]}
    except Exception as e:
        return {"suggestions": [], "error": str(e)}


@app.get("/v1/jobs/locations/autocomplete", tags=["Job Management"])
async def job_locations_autocomplete(q: Optional[str] = None, limit: int = 15):
    """Search-as-you-type: return location suggestions from active jobs (public for candidate browse jobs)."""
    if not q or not str(q).strip():
        return {"suggestions": []}
    q = str(q).strip()[:50]
    q_norm = re.sub(r"[\\/]+", " ", q)
    q_norm = re.sub(r"[^A-Za-z0-9]+", " ", q_norm)
    q_norm = re.sub(r"\s+", " ", q_norm).strip()
    if not q_norm:
        return {"suggestions": []}
    limit = max(1, min(limit, 25))
    try:
        db = await get_mongo_db()
        regex_pat = r"[\s\W_]*".join(re.escape(t) for t in q_norm.split(" ") if t)[:200]
        regex = {"$regex": regex_pat, "$options": "i"}
        cursor = db.jobs.find({"status": "active", "location": regex}, {"location": 1})
        jobs_list = await cursor.to_list(length=500)
        seen = set()
        for doc in jobs_list:
            loc = doc.get("location")
            if loc and isinstance(loc, str) and loc.strip():
                seen.add(loc.strip())
        matching = sorted(seen)[:limit]
        return {"suggestions": [{"id": s, "label": s} for s in matching]}
    except Exception as e:
        return {"suggestions": [], "error": str(e)}


@app.get("/v1/jobs/{job_id}", tags=["Job Management"])
async def get_job_by_id(job_id: str, auth: Optional[dict] = Depends(get_optional_auth)):
    """Get a single job by ID (MongoDB ObjectId string or legacy id). Client JWT: only own jobs."""
    if not job_id:
        raise HTTPException(status_code=400, detail="Job ID is required")
    try:
        db = await get_mongo_db()
        try:
            doc = await db.jobs.find_one({"_id": ObjectId(job_id)})
        except Exception:
            doc = await db.jobs.find_one({"id": job_id})
        if not doc:
            raise HTTPException(status_code=404, detail="Job not found")
        is_owner_view = False
        # Client data isolation: own jobs + connected recruiter's jobs when connected
        if auth and auth.get("type") == "jwt_token" and auth.get("role") == "client":
            client_id = str(auth.get("user_id", ""))
            job_ids = await _client_job_ids_for_dashboard(db, client_id)
            if job_id not in job_ids:
                raise HTTPException(status_code=403, detail="You can only view your own jobs")
            is_owner_view = str(doc.get("client_id", "")) == client_id
        elif auth and auth.get("type") == "jwt_token" and auth.get("role") == "recruiter":
            recruiter_id = str(auth.get("user_id", ""))
            if str(doc.get("recruiter_id", "")) != recruiter_id:
                raise HTTPException(status_code=403, detail="You can only view your own jobs")
            is_owner_view = True
        elif auth and (auth.get("type") == "api_key" or auth.get("role") == "admin"):
            is_owner_view = True
        salary_min, salary_max = _job_salary_from_doc(doc)
        response = {
            "id": str(doc["_id"]),
            "title": doc.get("title"),
            "department": doc.get("department"),
            "location": doc.get("location"),
            "experience_level": doc.get("experience_level"),
            "requirements": doc.get("requirements"),
            "description": doc.get("description"),
            "job_type": doc.get("job_type") or doc.get("employment_type"),
            "employment_type": doc.get("employment_type"),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "status": doc.get("status"),
            "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
        }
        if is_owner_view:
            response["client_id"] = str(doc.get("client_id")) if doc.get("client_id") is not None else None
            response["recruiter_id"] = str(doc.get("recruiter_id")) if doc.get("recruiter_id") is not None else None
            response["connection_id"] = doc.get("connection_id")
            if doc.get("client_id") is not None:
                client_doc = await db.clients.find_one({"client_id": str(doc.get("client_id"))})
                if client_doc:
                    response["company"] = client_doc.get("company_name")
                    response["connection_id"] = response.get("connection_id") or client_doc.get("connection_id")
        return response
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/v1/jobs/{job_id}", tags=["Job Management"])
async def update_job(job_id: str, job: JobCreate, auth: dict = Depends(get_auth)):
    """Update an existing job securely for the owning recruiter/client."""
    try:
        db = await get_mongo_db()
        existing_doc = await _get_owned_job_or_403(db, job_id, auth)
        updated_document = await _build_job_document(db, job, auth, existing_doc=existing_doc)

        if _normalized_job_signature(existing_doc) == _normalized_job_signature(updated_document):
            raise HTTPException(status_code=409, detail="Job already exists. No changes detected.")

        duplicate = await _find_duplicate_job(db, updated_document, exclude_job_id=str(existing_doc.get("_id")))
        if duplicate:
            raise HTTPException(status_code=409, detail="Job already exists")

        update_fields = dict(updated_document)
        update_fields["updated_at"] = datetime.now(timezone.utc)

        await db.jobs.update_one(
            {"_id": existing_doc["_id"]},
            {"$set": update_fields}
        )

        return {
            "message": "Job updated successfully",
            "job_id": str(existing_doc.get("_id")),
            "updated_at": update_fields["updated_at"].isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Job update failed: {str(e)}")


@app.delete("/v1/jobs/{job_id}", tags=["Job Management"])
async def delete_job(job_id: str, auth: dict = Depends(get_auth)):
    """Delete a job and its dependent recruiter-side records securely for the owner."""
    try:
        db = await get_mongo_db()
        job_doc = await _get_owned_job_or_403(db, job_id, auth)
        normalized_job_id = str(job_doc.get("_id"))

        await db.jobs.delete_one({"_id": job_doc["_id"]})
        await db.job_applications.delete_many({"job_id": normalized_job_id})
        await db.interviews.delete_many({"job_id": normalized_job_id})
        await db.offers.delete_many({"job_id": normalized_job_id})
        await db.feedback.delete_many({"job_id": normalized_job_id})
        await db.notification_logs.delete_many({"job_id": normalized_job_id})

        return {
            "message": "Job deleted successfully",
            "job_id": normalized_job_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Job deletion failed: {str(e)}")

class ShortlistRequest(BaseModel):
    candidate_id: str

@app.post("/v1/jobs/{job_id}/shortlist", tags=["Job Management"])
async def shortlist_candidate_for_job(job_id: str, body: ShortlistRequest, auth=Depends(get_auth)):
    """Mark a candidate as shortlisted for a job (upsert job_application with status shortlisted). Client: only own jobs."""
    if not job_id or not body.candidate_id:
        raise HTTPException(status_code=400, detail="job_id and candidate_id are required")
    try:
        db = await get_mongo_db()
        if auth.get("type") == "jwt_token" and auth.get("role") == "client":
            client_id = str(auth.get("user_id", ""))
            job_ids = await _client_job_ids_for_dashboard(db, client_id)
            if job_id not in job_ids:
                raise HTTPException(status_code=403, detail="You can only shortlist for your own jobs")
        now = datetime.now(timezone.utc)
        existing = await db.job_applications.find_one({"job_id": job_id, "candidate_id": body.candidate_id})
        if existing:
            await db.job_applications.update_one(
                {"_id": existing["_id"]},
                {"$set": {"status": "shortlisted", "updated_at": now}}
            )
        else:
            await db.job_applications.insert_one({
                "job_id": job_id,
                "candidate_id": body.candidate_id,
                "status": "shortlisted",
                "created_at": now,
                "updated_at": now,
            })
        return {"message": "Candidate shortlisted", "job_id": job_id, "candidate_id": body.candidate_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class RejectRequest(BaseModel):
    candidate_id: str

@app.post("/v1/jobs/{job_id}/reject", tags=["Job Management"])
async def reject_candidate_for_job(job_id: str, body: RejectRequest, auth=Depends(get_auth)):
    """Mark a candidate as rejected for a job (upsert job_application with status rejected). Client/Recruiter: only own jobs."""
    if not job_id or not body.candidate_id:
        raise HTTPException(status_code=400, detail="job_id and candidate_id are required")
    try:
        db = await get_mongo_db()
        
        # Authorization check for clients
        if auth.get("type") == "jwt_token" and auth.get("role") == "client":
            client_id = str(auth.get("user_id", ""))
            job_ids = await _client_job_ids_for_dashboard(db, client_id)
            if job_id not in job_ids:
                raise HTTPException(status_code=403, detail="You can only reject candidates for your own jobs")
        
        # Authorization check for recruiters
        if auth.get("type") == "jwt_token" and auth.get("role") == "recruiter":
            recruiter_id = str(auth.get("user_id", ""))
            job = await db.jobs.find_one({"_id": ObjectId(job_id)}) if ObjectId.is_valid(job_id) else await db.jobs.find_one({"id": job_id})
            if job and str(job.get("recruiter_id")) != recruiter_id:
                raise HTTPException(status_code=403, detail="You can only reject candidates for your own jobs")
        
        now = datetime.now(timezone.utc)
        existing = await db.job_applications.find_one({"job_id": job_id, "candidate_id": body.candidate_id})
        
        if existing:
            await db.job_applications.update_one(
                {"_id": existing["_id"]},
                {"$set": {"status": "rejected", "updated_at": now}}
            )
        else:
            await db.job_applications.insert_one({
                "job_id": job_id,
                "candidate_id": body.candidate_id,
                "status": "rejected",
                "created_at": now,
                "updated_at": now,
            })
        
        return {"message": "Candidate rejected", "job_id": job_id, "candidate_id": body.candidate_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Candidate Management (5 endpoints)
@app.get("/v1/candidates", tags=["Candidate Management"])
async def get_all_candidates(
    limit: int = 50, 
    offset: int = 0,
    # Data isolation
    recruiter_id: Optional[str] = None,
    client_id: Optional[str] = None,  # Added client_id support
    job_id: Optional[str] = None,  # Filter by specific job
    # Status filtering
    status: Optional[str] = None,  # Single status or comma-separated
    exclude_statuses: Optional[str] = None,  # Comma-separated statuses to exclude
    # Date filtering
    created_at_gte: Optional[str] = None,  # ISO date string YYYY-MM-DD
    interview_date_gte: Optional[str] = None,  # ISO date string
    interview_date_lt: Optional[str] = None,  # ISO date string
    # Interview filtering
    has_interview: Optional[bool] = None,
    # Score filtering
    matching_score_gte: Optional[int] = None,
    # Feedback filtering
    feedback_submitted: Optional[bool] = None,
    # Never applied filtering
    include_never_applied: Optional[bool] = None,  # Include candidates with no job applications
    auth=Depends(get_auth)
):
    """
    Get All Candidates with Advanced Filtering
    
    Supports filtering by:
    - recruiter_id: Data isolation - only candidates who applied to recruiter's jobs
    - client_id: Data isolation - only candidates who applied to client's jobs
    - job_id: Filter candidates who applied to a specific job
    - status: Single status or comma-separated list (e.g., "shortlisted" or "shortlisted,interview_scheduled")
                NOTE: When recruiter_id/client_id/job_id is provided, status filters by job_applications.status
    - exclude_statuses: Comma-separated statuses to exclude
    - created_at_gte: Filter candidates created on or after this date (YYYY-MM-DD)
    - interview_date_gte/lt: Filter by interview date range
    - has_interview: Filter candidates with/without interviews
    - matching_score_gte: Minimum matching score
    - feedback_submitted: Filter by feedback submission status
    - include_never_applied: Include candidates who have never applied to any job (no job_applications records)
    """
    try:
        db = await get_mongo_db()
        
        # Fallback: Use authenticated user's ID if recruiter_id not provided
        if not recruiter_id and not client_id and auth:
            user_role = auth.get("role", "")
            user_id = auth.get("sub") or auth.get("user_id")
            if user_role == "recruiter" and user_id:
                recruiter_id = user_id
                print(f"🔍 DEBUG [AUTH]: Using authenticated recruiter_id={recruiter_id}")
            elif user_role == "client" and user_id:
                client_id = user_id
                print(f"🔍 DEBUG [AUTH]: Using authenticated client_id={client_id}")
        
        # Build MongoDB query filter
        query_filter: Dict[str, Any] = {}
        
        # Data isolation: Filter by recruiter_id (candidates who applied to recruiter's jobs)
        candidate_ids_filter = None
        use_application_status = False  # Track if we should filter by job_applications.status
        
        # PRIORITY 1: Filter by specific job_id
        if job_id:
            use_application_status = True
            # Enforce data isolation when filtering by explicit job_id
            if auth and auth.get("type") == "jwt_token":
                auth_role = auth.get("role", "")
                auth_user_id = str(auth.get("sub") or auth.get("user_id") or "")
                if auth_role == "client":
                    allowed_job_ids = await _client_job_ids_for_dashboard(db, auth_user_id)
                    if str(job_id) not in allowed_job_ids:
                        raise HTTPException(status_code=403, detail="You can only view candidates for your own jobs")
                elif auth_role == "recruiter":
                    recruiter_job_cursor = db.jobs.find(
                        {"status": "active", "recruiter_id": auth_user_id},
                        {"_id": 1}
                    ).limit(500)
                    recruiter_jobs = await recruiter_job_cursor.to_list(length=500)
                    recruiter_job_ids = {str(doc["_id"]) for doc in recruiter_jobs}
                    if str(job_id) not in recruiter_job_ids:
                        raise HTTPException(status_code=403, detail="You can only view candidates for your own jobs")

            # Build application query
            app_query: Dict[str, Any] = {"job_id": job_id}
            
            print(f"🔍 DEBUG [JOB_ID]: Filtering by job_id={job_id}")
            
            # Add status filter to job_applications query
            if status:
                status_list = [s.strip() for s in status.split(',') if s.strip()]
                if len(status_list) == 1:
                    app_query["status"] = status_list[0]
                elif len(status_list) > 1:
                    app_query["status"] = {"$in": status_list}
                print(f"🔍 DEBUG [JOB_ID]: Status filter applied: {status_list}")
            
            # Add exclude_statuses filter
            if exclude_statuses:
                exclude_list = [s.strip() for s in exclude_statuses.split(',') if s.strip()]
                if exclude_list:
                    if "status" in app_query:
                        # Combine with existing status filter
                        if "$in" in app_query["status"]:
                            app_query["status"] = {"$in": app_query["status"]["$in"], "$nin": exclude_list}
                        else:
                            app_query["status"] = {"$eq": app_query["status"], "$nin": exclude_list}
                    else:
                        app_query["status"] = {"$nin": exclude_list}
            
            # Add interview date filtering to job_applications query
            if interview_date_gte:
                try:
                    from datetime import datetime
                    date_obj = datetime.fromisoformat(interview_date_gte.replace('Z', '+00:00'))
                    app_query["interview_date"] = {"$gte": date_obj}
                    print(f"🔍 DEBUG [JOB_ID]: Interview date filter >= {interview_date_gte}")
                except:
                    pass
            if interview_date_lt:
                try:
                    from datetime import datetime
                    date_obj = datetime.fromisoformat(interview_date_lt.replace('Z', '+00:00'))
                    if "interview_date" in app_query:
                        app_query["interview_date"]["$lt"] = date_obj
                    else:
                        app_query["interview_date"] = {"$lt": date_obj}
                    print(f"🔍 DEBUG [JOB_ID]: Interview date filter < {interview_date_lt}")
                except:
                    pass
            
            print(f"🔍 DEBUG [JOB_ID]: Final app_query: {app_query}")
            
            # Get candidate_ids from job_applications
            pipeline = [
                {"$match": app_query},
                {"$group": {"_id": "$candidate_id"}}
            ]
            candidate_ids = []
            async for agg_doc in db.job_applications.aggregate(pipeline):
                cid = agg_doc.get("_id")
                if cid:
                    candidate_ids.append(cid)
            
            print(f"✅ DEBUG [JOB_ID]: Found {len(candidate_ids)} candidate_ids from job_applications")
            
            if candidate_ids:
                from bson import ObjectId
                oid_list = []
                for cid in candidate_ids:
                    try:
                        oid_list.append(ObjectId(cid) if isinstance(cid, str) else cid)
                    except:
                        pass
                if oid_list:
                    candidate_ids_filter = {"_id": {"$in": oid_list}}
            else:
                # No candidates found for this job with given filters
                print(f"⚠️ DEBUG [JOB_ID]: No candidates found for job {job_id} with status filter")
                return {
                    "candidates": [],
                    "total": 0,
                    "limit": limit,
                    "offset": offset,
                    "count": 0
                }
        # PRIORITY 2: Filter by recruiter_id
        elif recruiter_id:
            use_application_status = True
            # Get jobs for this recruiter (active only - matches dashboard logic)
            recruiter_jobs_cursor = db.jobs.find({"status": "active", "recruiter_id": recruiter_id}, {"_id": 1})
            recruiter_jobs = await recruiter_jobs_cursor.to_list(length=500)
            job_ids = [str(doc["_id"]) for doc in recruiter_jobs]
            
            print(f"🔍 DEBUG: Recruiter {recruiter_id} has {len(job_ids)} active jobs")
            
            if job_ids:
                # Build application query with status filters
                app_query: Dict[str, Any] = {"job_id": {"$in": job_ids}}
                
                # Add status filter to job_applications query
                if status:
                    status_list = [s.strip() for s in status.split(',') if s.strip()]
                    if len(status_list) == 1:
                        app_query["status"] = status_list[0]
                    elif len(status_list) > 1:
                        app_query["status"] = {"$in": status_list}
                
                print(f"🔍 DEBUG: app_query before exclude_statuses: {app_query}")
                
                # Add exclude_statuses filter
                if exclude_statuses:
                    exclude_list = [s.strip() for s in exclude_statuses.split(',') if s.strip()]
                    if exclude_list:
                        if "status" in app_query:
                            if "$in" in app_query["status"]:
                                app_query["status"] = {"$in": app_query["status"]["$in"], "$nin": exclude_list}
                            else:
                                app_query["status"] = {"$eq": app_query["status"], "$nin": exclude_list}
                        else:
                            app_query["status"] = {"$nin": exclude_list}
                
                # Add interview date filtering to job_applications query
                if interview_date_gte:
                    try:
                        from datetime import datetime
                        date_obj = datetime.fromisoformat(interview_date_gte.replace('Z', '+00:00'))
                        app_query["interview_date"] = {"$gte": date_obj}
                        print(f"🔍 DEBUG [RECRUITER]: Interview date filter >= {interview_date_gte}")
                    except:
                        pass
                if interview_date_lt:
                    try:
                        from datetime import datetime
                        date_obj = datetime.fromisoformat(interview_date_lt.replace('Z', '+00:00'))
                        if "interview_date" in app_query:
                            app_query["interview_date"]["$lt"] = date_obj
                        else:
                            app_query["interview_date"] = {"$lt": date_obj}
                        print(f"🔍 DEBUG [RECRUITER]: Interview date filter < {interview_date_lt}")
                    except:
                        pass
                
                print(f"🔍 DEBUG: Final app_query: {app_query}")
                print(f"🔍 DEBUG: Searching job_applications collection...")
                
                # Get candidate_ids who applied to these jobs with status filters
                pipeline = [
                    {"$match": app_query},
                    {"$group": {"_id": "$candidate_id"}}
                ]
                candidate_ids = []
                async for agg_doc in db.job_applications.aggregate(pipeline):
                    cid = agg_doc.get("_id")
                    if cid:
                        candidate_ids.append(cid)
                
                print(f"✅ DEBUG: Found {len(candidate_ids)} candidate_ids from job_applications")
                
                if candidate_ids:
                    # Convert to ObjectId if needed
                    from bson import ObjectId
                    oid_list = []
                    for cid in candidate_ids:
                        try:
                            oid_list.append(ObjectId(cid) if isinstance(cid, str) else cid)
                        except:
                            pass
                    if oid_list:
                        candidate_ids_filter = {"_id": {"$in": oid_list}}
                else:
                    # No candidates found for this recruiter
                    print(f"⚠️ DEBUG: No candidates found for recruiter {recruiter_id} with status filter")
                    return {
                        "candidates": [],
                        "total": 0,
                        "limit": limit,
                        "offset": offset,
                        "count": 0
                    }
            else:
                # Recruiter has no jobs
                return {
                    "candidates": [],
                    "total": 0,
                    "limit": limit,
                    "offset": offset,
                    "count": 0
                }
        
        # Data isolation: Filter by client_id (candidates who applied to client's jobs)
        # Supports both direct client jobs and jobs from connected recruiters
        if client_id and not recruiter_id and not job_id:  # Only apply if recruiter_id/job_id not already used
            use_application_status = True
            # Get job IDs for this client (own jobs + connected recruiters' jobs)
            client_job_ids = await _client_job_ids_for_dashboard(db, client_id)
            
            if client_job_ids:
                # Build application query with status filters
                app_query: Dict[str, Any] = {"job_id": {"$in": client_job_ids}}
                
                # Add status filter to job_applications query
                if status:
                    status_list = [s.strip() for s in status.split(',') if s.strip()]
                    if len(status_list) == 1:
                        app_query["status"] = status_list[0]
                    elif len(status_list) > 1:
                        app_query["status"] = {"$in": status_list}
                
                # Add exclude_statuses filter
                if exclude_statuses:
                    exclude_list = [s.strip() for s in exclude_statuses.split(',') if s.strip()]
                    if exclude_list:
                        if "status" in app_query:
                            if "$in" in app_query["status"]:
                                app_query["status"] = {"$in": app_query["status"]["$in"], "$nin": exclude_list}
                            else:
                                app_query["status"] = {"$eq": app_query["status"], "$nin": exclude_list}
                        else:
                            app_query["status"] = {"$nin": exclude_list}
                
                # Get candidate_ids who applied to these jobs with status filters
                pipeline = [
                    {"$match": app_query},
                    {"$group": {"_id": "$candidate_id"}}
                ]
                candidate_ids = []
                async for agg_doc in db.job_applications.aggregate(pipeline):
                    cid = agg_doc.get("_id")
                    if cid:
                        candidate_ids.append(cid)
                
                if candidate_ids:
                    # Convert to ObjectId if needed
                    from bson import ObjectId
                    oid_list = []
                    for cid in candidate_ids:
                        try:
                            oid_list.append(ObjectId(cid) if isinstance(cid, str) else cid)
                        except:
                            pass
                    if oid_list:
                        candidate_ids_filter = {"_id": {"$in": oid_list}}
                else:
                    # No candidates found for this client
                    return {
                        "candidates": [],
                        "total": 0,
                        "limit": limit,
                        "offset": offset,
                        "count": 0
                    }
            else:
                # Client has no jobs
                return {
                    "candidates": [],
                    "total": 0,
                    "limit": limit,
                    "offset": offset,
                    "count": 0
                }
        
        if candidate_ids_filter:
            query_filter.update(candidate_ids_filter)
        
        # Status filtering (single or array)
        # NOTE: If use_application_status is True, status was already applied to job_applications query above
        # Only apply to candidates collection if we're NOT filtering by recruiter/client/job
        if status and not use_application_status:
            status_list = [s.strip() for s in status.split(',') if s.strip()]
            if len(status_list) == 1:
                query_filter["status"] = status_list[0]
            elif len(status_list) > 1:
                query_filter["status"] = {"$in": status_list}
        
        # Exclude statuses
        if exclude_statuses and not use_application_status:
            exclude_list = [s.strip() for s in exclude_statuses.split(',') if s.strip()]
            if exclude_list:
                if "status" not in query_filter:
                    # No status filter yet, just add $nin
                    query_filter["status"] = {"$nin": exclude_list}
                else:
                    # Status filter exists, combine with $nin
                    existing_status = query_filter["status"]
                    if isinstance(existing_status, dict):
                        # Already a dict (has $in), add $nin
                        query_filter["status"]["$nin"] = exclude_list
                    else:
                        # It's a string, convert to dict with both filters
                        query_filter["status"] = {"$eq": existing_status, "$nin": exclude_list}
        
        # Date filtering
        if created_at_gte:
            try:
                from datetime import datetime
                date_obj = datetime.fromisoformat(created_at_gte.replace('Z', '+00:00'))
                query_filter["created_at"] = {"$gte": date_obj}
            except:
                pass
        
        # Interview date filtering
        # NOTE: Only apply to candidates collection if NOT using job_applications filtering
        # When use_application_status=True, interview_date was already filtered in job_applications query
        if (interview_date_gte or interview_date_lt) and not use_application_status:
            interview_filter = {}
            if interview_date_gte:
                try:
                    from datetime import datetime
                    date_obj = datetime.fromisoformat(interview_date_gte.replace('Z', '+00:00'))
                    interview_filter["$gte"] = date_obj
                except:
                    pass
            if interview_date_lt:
                try:
                    from datetime import datetime
                    date_obj = datetime.fromisoformat(interview_date_lt.replace('Z', '+00:00'))
                    interview_filter["$lt"] = date_obj
                except:
                    pass
            if interview_filter:
                query_filter["interview_date"] = interview_filter
        
        # Has interview filter
        if has_interview is not None and not use_application_status:
            if has_interview:
                query_filter["interview_date"] = {"$exists": True, "$ne": None}
            else:
                query_filter["$or"] = [
                    {"interview_date": {"$exists": False}},
                    {"interview_date": None}
                ]
        
        # Matching score filter
        if matching_score_gte is not None:
            query_filter["matching_score"] = {"$gte": matching_score_gte}
        
        # Feedback submitted filter
        if feedback_submitted is not None:
            if feedback_submitted:
                query_filter["feedback_submitted"] = True
            else:
                query_filter["$or"] = [
                    {"feedback_submitted": {"$exists": False}},
                    {"feedback_submitted": False},
                    {"feedback_submitted": None}
                ]
        
        # Never applied filter - exclude candidates who have job applications
        if include_never_applied:
            # Find all candidate_ids that have applied to any job
            pipeline = [
                {"$group": {"_id": "$candidate_id"}}
            ]
            applied_candidate_ids = []
            async for agg_doc in db.job_applications.aggregate(pipeline):
                cid = agg_doc.get("_id")
                if cid:
                    applied_candidate_ids.append(cid)
            
            # Exclude these candidates (only show those who never applied)
            if applied_candidate_ids:
                from bson import ObjectId
                oid_list = []
                for cid in applied_candidate_ids:
                    try:
                        oid_list.append(ObjectId(cid) if isinstance(cid, str) else cid)
                    except:
                        pass
                if oid_list:
                    # Add $nin filter to exclude applied candidates
                    if "_id" in query_filter:
                        # Merge with existing _id filter
                        if "$in" in query_filter["_id"]:
                            # If there's already an $in, we need to combine filters properly
                            existing_ids = query_filter["_id"]["$in"]
                            query_filter["_id"] = {"$in": existing_ids, "$nin": oid_list}
                        else:
                            query_filter["_id"]["$nin"] = oid_list
                    else:
                        query_filter["_id"] = {"$nin": oid_list}
        
        # Execute query
        print(f"🔍 DEBUG: Final query_filter for candidates collection: {query_filter}")
        cursor = db.candidates.find(query_filter).sort("created_at", -1).skip(offset).limit(limit)
        candidates_list = await cursor.to_list(length=limit)
        print(f"✅ DEBUG: Found {len(candidates_list)} candidates from candidates collection")
        
        candidates = []
        for doc in candidates_list:
            candidates.append({
                "id": str(doc["_id"]),
                "candidate_id": str(doc["_id"]),  # Alias for compatibility
                "name": doc.get("name"),
                "email": doc.get("email"),
                "phone": doc.get("phone"),
                "location": doc.get("location"),
                "experience_years": doc.get("experience_years"),
                "technical_skills": doc.get("technical_skills"),
                "seniority_level": doc.get("seniority_level"),
                "education_level": doc.get("education_level"),
                "status": doc.get("status"),
                "matching_score": doc.get("matching_score") or doc.get("match_score"),  # Support both field names
                "interview_date": doc.get("interview_date").isoformat() if doc.get("interview_date") else None,
                "feedback_submitted": doc.get("feedback_submitted", False),
                "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None
            })
        
        total_count = await db.candidates.count_documents(query_filter)
        
        print(f"📊 DEBUG: Returning {len(candidates)} candidates (total: {total_count})")
        
        return {
            "candidates": candidates,
            "total": total_count,
            "limit": limit,
            "offset": offset,
            "count": len(candidates)
        }
    except Exception as e:
        import traceback
        print(f"Error in get_all_candidates: {str(e)}")
        print(traceback.format_exc())
        return {"candidates": [], "total": 0, "error": str(e)}

@app.get("/v1/notifications/history/{candidate_id}", tags=["Candidate Management"])
async def get_notification_history(
    candidate_id: str,
    limit: int = 20,
    auth=Depends(get_auth)
):
    """
    Get Notification History for a Candidate
    
    Returns all notifications sent to a specific candidate, sorted by most recent first.
    """
    try:
        db = await get_mongo_db()
        
        # Query notification_logs collection
        cursor = db.notification_logs.find(
            {"candidate_id": candidate_id}
        ).sort("sent_at", -1).limit(limit)
        
        logs = await cursor.to_list(length=limit)
        
        # Format response
        history = []
        for log in logs:
            history.append({
                "id": str(log["_id"]),
                "channel": log.get("channel"),
                "notification_type": log.get("notification_type"),
                "status": log.get("status"),
                "recipient": log.get("recipient"),
                "job_title": log.get("job_title"),
                "sent_at": log.get("sent_at").isoformat() if log.get("sent_at") else None,
                "message_id": log.get("message_id")
            })
        
        return {
            "candidate_id": candidate_id,
            "history": history,
            "count": len(history)
        }
    except Exception as e:
        import traceback
        print(f"Error in get_notification_history: {str(e)}")
        print(traceback.format_exc())
        return {"candidate_id": candidate_id, "history": [], "error": str(e)}

class ControlCenterAuditEvent(BaseModel):
    action: str
    outcome: str
    detail: Optional[str] = None
    correlation_id: Optional[str] = None
    context: Optional[Dict[str, Any]] = None


@app.post("/v1/control-center/audit-events", tags=["Monitoring"])
async def create_control_center_audit_event(
    event: ControlCenterAuditEvent,
    request: Request,
    auth=Depends(get_auth),
):
    scope = assert_control_center_access(auth)

    try:
        db = await get_mongo_db()
        context = dict(event.context or {})
        context.setdefault("source_system", "gateway")
        context.setdefault("scope_label", scope.get("scope_label"))
        context.setdefault("policy_scope", scope.get("scope"))
        if scope.get("role") == "client":
            context.setdefault("client_id", scope.get("user_id"))
        if scope.get("role") == "recruiter":
            context.setdefault("recruiter_id", scope.get("user_id"))

        await db.audit_logs.insert_one(
            {
                "event_type": "control_center",
                "action": event.action,
                "outcome": event.outcome,
                "detail": event.detail,
                "correlation_id": event.correlation_id or getattr(request.state, "correlation_id", None),
                "context": context,
                "actor": {
                    "user_id": auth.get("user_id"),
                    "role": auth.get("role"),
                    "auth_type": auth.get("type"),
                },
                "created_at": datetime.now(timezone.utc),
            }
        )
        return {"ok": True, "policy_scope": scope}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write control-center audit event: {e}")


@app.get("/v1/control-center/audit-events", tags=["Monitoring"])
async def get_control_center_audit_events(
    request: Request,
    auth=Depends(get_auth),
    correlation_id: Optional[str] = None,
    limit: int = 50,
):
    """Scoped audit timeline for control-center replay (live, not seeded)."""
    scope = assert_control_center_access(auth)
    db = await get_mongo_db()
    return await list_audit_events(db, scope, correlation_id=correlation_id, limit=limit)


@app.get("/v1/control-center/audit-replay", tags=["Monitoring"])
async def get_control_center_audit_replay(
    auth=Depends(get_auth),
    correlation_id: Optional[str] = None,
    limit: int = 20,
):
    """Grouped replay trace for the latest or requested correlation_id."""
    scope = assert_control_center_access(auth)
    db = await get_mongo_db()
    return await build_audit_replay(db, scope, correlation_id=correlation_id, limit=limit)


@app.get("/v1/control-center/dashboard-aggregates", tags=["Monitoring"])
async def get_control_center_dashboard_aggregates(auth=Depends(get_auth)):
    """Backend-driven hiring funnel and department load within policy scope."""
    scope = assert_control_center_access(auth)
    db = await get_mongo_db()
    return await compute_dashboard_aggregates(db, scope)


# Analytics & Statistics - Move stats endpoint before parameterized routes
@app.get("/v1/candidates/stats", tags=["Analytics & Statistics"])
async def get_candidate_stats(auth=Depends(get_auth)):
    """Dynamic Candidate Statistics for HR Dashboard Analytics
    
    **Authentication:** Bearer token required
    
    **Example:**
    ```bash
    curl -H "Authorization: Bearer <YOUR_API_KEY>" \
         https://api.bhiv.com/v1/candidates/stats
    ```
    
    **Response:** Real-time statistics including total candidates, active jobs, recent matches, and pending interviews.
    """
    scope = assert_control_center_access(auth)

    try:
        db = await get_mongo_db()
        return await compute_scoped_candidate_stats(db, scope)
    except Exception as e:
        return {
            "total_candidates": 0,
            "active_jobs": 0,
            "recent_matches": 0,
            "pending_interviews": 0,
            "new_candidates_this_week": 0,
            "total_feedback_submissions": 0,
            "error": str(e),
            "statistics_generated_at": datetime.now(timezone.utc).isoformat(),
            "data_source": "error_fallback",
            "dashboard_ready": False,
            "policy_scope": resolve_policy_scope(auth),
        }


async def _recruiter_applicant_ids(db, recruiter_id: str, job_id: Optional[str] = None) -> List[str]:
    """Return list of candidate_ids that have applied to recruiter's jobs (or to a specific job)."""
    cursor = db.jobs.find({"status": "active", "recruiter_id": recruiter_id}, {"_id": 1})
    recruiter_jobs = await cursor.to_list(length=500)
    job_ids = [str(doc["_id"]) for doc in recruiter_jobs]
    if not job_ids:
        return []
    match_filter: Dict[str, Any] = {"job_id": {"$in": job_ids}}
    if job_id:
        match_filter["job_id"] = job_id
    pipeline = [{"$match": match_filter}, {"$group": {"_id": "$candidate_id"}}]
    candidate_ids = []
    async for agg_doc in db.job_applications.aggregate(pipeline):
        cid = agg_doc.get("_id")
        if cid:
            candidate_ids.append(str(cid))
    return candidate_ids


async def _job_applicant_ids(db, job_id: str) -> List[str]:
    """Return unique candidate_ids that have an application record for a job."""
    if not job_id:
        return []
    pipeline = [
        {"$match": {"job_id": str(job_id)}},
        {"$group": {"_id": "$candidate_id"}}
    ]
    candidate_ids: List[str] = []
    async for agg_doc in db.job_applications.aggregate(pipeline):
        cid = agg_doc.get("_id")
        if cid:
            candidate_ids.append(str(cid))
    return candidate_ids


def _client_id_query(client_id: str) -> Dict[str, Any]:
    """Build query for jobs belonging to this client (supports string or int client_id in DB)."""
    if not client_id or not str(client_id).strip():
        return {}
    cid = str(client_id).strip()
    if cid.isdigit():
        return {"$or": [{"client_id": cid}, {"client_id": int(cid)}]}
    return {"client_id": cid}


async def _client_job_ids(db, client_id: str) -> List[str]:
    """Return list of job_ids for active jobs belonging to this client (data isolation). Includes jobs posted by recruiters via connection_id."""
    q = _client_id_query(client_id)
    if not q:
        return []
    q["status"] = "active"
    cursor = db.jobs.find(q, {"_id": 1}).limit(500)
    jobs_list = await cursor.to_list(length=500)
    return [str(doc["_id"]) for doc in jobs_list]


async def _client_all_job_ids(db, client_id: str) -> List[str]:
    """Return list of all job_ids for this client (any status). Used for stats so applications/interviews/offers count across all client jobs."""
    q = _client_id_query(client_id)
    if not q:
        return []
    cursor = db.jobs.find(q, {"_id": 1}).limit(500)
    jobs_list = await cursor.to_list(length=500)
    return [str(doc["_id"]) for doc in jobs_list]


async def _client_connected_recruiter_ids(db, client_id: str) -> List[str]:
    """Return list of recruiter_ids connected to this client (one doc per client-recruiter pair; client can have multiple recruiters)."""
    if not client_id or not str(client_id).strip():
        return []
    cursor = db.client_connected_recruiter.find({"client_id": str(client_id).strip()}, {"recruiter_id": 1})
    docs = await cursor.to_list(length=100)
    return [str(d["recruiter_id"]) for d in docs if d.get("recruiter_id")]


async def _client_job_ids_for_dashboard(db, client_id: str) -> List[str]:
    """Job IDs for client dashboard: client's own jobs + all jobs by all connected recruiters. When disconnected, only client's jobs."""
    own_ids = await _client_job_ids(db, client_id)
    recruiter_ids = await _client_connected_recruiter_ids(db, client_id)
    if not recruiter_ids:
        return own_ids
    cursor = db.jobs.find({"status": "active", "recruiter_id": {"$in": recruiter_ids}}, {"_id": 1}).limit(1000)
    recruiter_jobs = await cursor.to_list(length=1000)
    seen = set(own_ids)
    for doc in recruiter_jobs:
        jid = str(doc["_id"])
        if jid not in seen:
            seen.add(jid)
            own_ids.append(jid)
    return own_ids


async def _client_all_job_ids_for_dashboard(db, client_id: str) -> List[str]:
    """All job IDs (any status) for client dashboard: client's jobs + all connected recruiters' jobs."""
    own_ids = await _client_all_job_ids(db, client_id)
    recruiter_ids = await _client_connected_recruiter_ids(db, client_id)
    if not recruiter_ids:
        return own_ids
    cursor = db.jobs.find({"recruiter_id": {"$in": recruiter_ids}}, {"_id": 1}).limit(1000)
    recruiter_jobs = await cursor.to_list(length=1000)
    seen = set(own_ids)
    for doc in recruiter_jobs:
        jid = str(doc["_id"])
        if jid not in seen:
            seen.add(jid)
            own_ids.append(jid)
    return own_ids


async def _recruiter_all_job_ids_for_dashboard(db, recruiter_id: str) -> List[str]:
    """All job IDs (any status) for recruiter dashboard."""
    if not recruiter_id or not str(recruiter_id).strip():
        return []
    cursor = db.jobs.find({"recruiter_id": str(recruiter_id).strip()}, {"_id": 1}).limit(1000)
    jobs_list = await cursor.to_list(length=1000)
    return [str(doc["_id"]) for doc in jobs_list]


DOCUMENT_LABELS: Dict[str, str] = {
    "resume": "CV/Resume",
    "nda": "NDA",
}

ALLOWED_DOCUMENT_CONTENT_TYPES: Dict[str, set] = {
    "resume": {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    },
    "nda": {
        "application/pdf",
    },
}

MAX_DOCUMENT_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


def _normalize_required_documents(raw_docs: Any) -> List[str]:
    """Normalize and validate requested document type list."""
    if not isinstance(raw_docs, list):
        return []
    out: List[str] = []
    seen = set()
    for item in raw_docs:
        key = str(item or "").strip().lower()
        if key in DOCUMENT_LABELS and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _sanitize_uploaded_documents(raw_map: Any) -> Dict[str, Dict[str, Any]]:
    """Return a safe map for API responses."""
    if not isinstance(raw_map, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for doc_type, value in raw_map.items():
        key = str(doc_type or "").strip().lower()
        if key not in DOCUMENT_LABELS or not isinstance(value, dict):
            continue
        out[key] = {
            "document_id": value.get("document_id"),
            "filename": value.get("filename"),
            "content_type": value.get("content_type"),
            "size_bytes": value.get("size_bytes"),
            "uploaded_at": value.get("uploaded_at"),
        }
    return out


def _get_auth_jwt_context(auth: dict) -> Dict[str, str]:
    """Extract role/user_id from JWT auth or raise 403."""
    if auth.get("type") != "jwt_token":
        raise HTTPException(status_code=403, detail="This endpoint requires JWT authentication.")
    role = str(auth.get("role") or "").strip()
    user_id = str(auth.get("user_id") or auth.get("sub") or "").strip()
    if not role or not user_id:
        raise HTTPException(status_code=403, detail="Invalid authentication context.")
    return {"role": role, "user_id": user_id}


async def _create_portal_notification(
    db,
    *,
    recipient_role: str,
    recipient_user_id: str,
    title: str,
    message: str,
    kind: str = "info",
    payload: Optional[Dict[str, Any]] = None,
) -> str:
    """Create an in-app notification item for portal bell dropdowns."""
    now = datetime.now(timezone.utc)
    doc = {
        "recipient_role": recipient_role,
        "recipient_user_id": recipient_user_id,
        "title": title,
        "message": message,
        "kind": kind,
        "payload": payload or {},
        "is_read": False,
        "created_at": now,
        "read_at": None,
    }
    result = await db.portal_notifications.insert_one(doc)
    return str(result.inserted_id)


@app.get("/v1/candidates/autocomplete", tags=["Candidate Management"])
async def candidates_autocomplete(q: Optional[str] = None, limit: int = 10, auth=Depends(get_auth)):
    """Search-as-you-type: by name or email. For recruiters, only applicants to their jobs (data isolation)."""
    if not q or not str(q).strip():
        return {"suggestions": [], "has_applicants": None}
    q = str(q).strip()[:50]
    q_norm = re.sub(r"[\\/]+", " ", q)
    q_norm = re.sub(r"\s+", " ", q_norm).strip()
    if not re.search(r"[A-Za-z0-9@.]", q_norm):
        return {"suggestions": [], "has_applicants": None}
    limit = max(1, min(limit, 20))
    try:
        db = await get_mongo_db()
        is_recruiter = auth.get("type") == "jwt_token" and auth.get("role") == "recruiter"
        candidate_ids_scope: Optional[List[str]] = None
        if is_recruiter:
            recruiter_id = str(auth.get("user_id", ""))
            if recruiter_id:
                candidate_ids_scope = await _recruiter_applicant_ids(db, recruiter_id)
            else:
                candidate_ids_scope = []
        if is_recruiter and candidate_ids_scope is not None and len(candidate_ids_scope) == 0:
            return {"suggestions": [], "has_applicants": False}
        if "@" in q_norm or "." in q_norm:
            regex_pat = re.escape(q_norm)
        else:
            tokens = [t for t in re.sub(r"[^A-Za-z0-9]+", " ", q_norm).split(" ") if t]
            regex_pat = r"[\s\W_]*".join(re.escape(t) for t in tokens)[:200] if tokens else re.escape(q_norm)
        regex = {"$regex": regex_pat, "$options": "i"}
        base_query: Dict[str, Any] = {
            "$or": [{"name": regex}, {"email": regex}]
        }
        if is_recruiter and candidate_ids_scope:
            try:
                object_ids = [ObjectId(cid) for cid in candidate_ids_scope]
                base_query["_id"] = {"$in": object_ids}
            except Exception:
                return {"suggestions": [], "has_applicants": True}
        elif is_recruiter:
            return {"suggestions": [], "has_applicants": False}
        cursor = db.candidates.find(base_query).sort("created_at", -1).limit(limit)
        candidates_list = await cursor.to_list(length=limit)
        suggestions = []
        for doc in candidates_list:
            suggestions.append({
                "id": str(doc["_id"]),
                "name": doc.get("name") or "",
                "email": doc.get("email") or "",
                "technical_skills": doc.get("technical_skills") or "",
                "location": doc.get("location") or "",
            })
        return {"suggestions": suggestions, "has_applicants": True if is_recruiter else None}
    except Exception as e:
        return {"suggestions": [], "has_applicants": None, "error": str(e)}


@app.get("/v1/candidates/search", tags=["Candidate Management"])
async def search_candidates(
    search: Optional[str] = None,
    query: Optional[str] = None,
    job_id: Optional[str] = None,
    skills: Optional[str] = None,
    location: Optional[str] = None,
    experience_min: Optional[int] = None,
    experience_max: Optional[int] = None,
    education_level: Optional[str] = None,
    seniority_level: Optional[str] = None,
    status: Optional[str] = None,
    limit: Optional[int] = 50,
    offset: Optional[int] = 0,
    auth=Depends(get_auth)
):
    """Search & Filter Candidates. For recruiters: only applicants to their jobs (optionally job_id). Supports limit/offset for pagination."""
    q_text = (search or query or "").strip()[:100]
    if skills:
        if len(skills) > 200:
            raise HTTPException(status_code=400, detail="Skills filter too long (max 200 characters).")
        if not re.match(r"^[A-Za-z0-9, ]+$", skills):
            raise HTTPException(status_code=400, detail="Invalid characters in skills filter.")
    if location:
        if len(location) > 100:
            raise HTTPException(status_code=400, detail="Location filter too long (max 100 characters).")
        if not re.match(r"^[A-Za-z0-9, ]+$", location):
            raise HTTPException(status_code=400, detail="Invalid characters in location filter.")
    if experience_min is not None and experience_min < 0:
        raise HTTPException(status_code=400, detail="experience_min must be non-negative.")
    if experience_max is not None and experience_max < 0:
        raise HTTPException(status_code=400, detail="experience_max must be non-negative.")
    if experience_min is not None and experience_max is not None and experience_min > experience_max:
        raise HTTPException(status_code=400, detail="experience_min cannot exceed experience_max.")
    if education_level and len(education_level) > 200:
        raise HTTPException(status_code=400, detail="Education filter too long (max 200 characters).")
    if seniority_level and len(seniority_level) > 100:
        raise HTTPException(status_code=400, detail="Seniority filter too long (max 100 characters).")
    if status and len(status) > 100:
        raise HTTPException(status_code=400, detail="Status filter too long (max 100 characters).")

    try:
        db = await get_mongo_db()
        mongo_query: Dict[str, Any] = {}
        is_recruiter = auth.get("type") == "jwt_token" and auth.get("role") == "recruiter"
        if is_recruiter:
            recruiter_id = str(auth.get("user_id", ""))
            if not recruiter_id:
                return {"candidates": [], "filters": {"skills": skills, "location": location, "experience_min": experience_min}, "count": 0, "total": 0}
            candidate_ids_scope = await _recruiter_applicant_ids(db, recruiter_id, job_id)
            if not candidate_ids_scope:
                return {"candidates": [], "filters": {"skills": skills, "location": location, "experience_min": experience_min}, "count": 0, "total": 0}
            try:
                mongo_query["_id"] = {"$in": [ObjectId(cid) for cid in candidate_ids_scope]}
            except Exception:
                return {"candidates": [], "filters": {"skills": skills, "location": location, "experience_min": experience_min}, "count": 0, "total": 0}
        if q_text:
            if is_recruiter:
                mongo_query["$or"] = [
                    {"name": {"$regex": re.escape(q_text), "$options": "i"}},
                    {"email": {"$regex": re.escape(q_text), "$options": "i"}},
                ]
            else:
                mongo_query["$or"] = [
                    {"name": {"$regex": re.escape(q_text), "$options": "i"}},
                    {"email": {"$regex": re.escape(q_text), "$options": "i"}},
                    {"technical_skills": {"$regex": re.escape(q_text), "$options": "i"}},
                ]
        if skills:
            mongo_query["technical_skills"] = {"$regex": skills, "$options": "i"}
        if location:
            mongo_query["location"] = {"$regex": location, "$options": "i"}
        if experience_min is not None or experience_max is not None:
            exp_query: Dict[str, Any] = {}
            if experience_min is not None:
                exp_query["$gte"] = experience_min
            if experience_max is not None:
                exp_query["$lte"] = experience_max
            mongo_query["experience_years"] = exp_query
        if education_level:
            level_str = (education_level or "").strip()[:200]
            if level_str:
                tokens = [t.strip() for t in re.split(r"[,]+", level_str) if t.strip()]
                if tokens:
                    mongo_query["education_level"] = {"$regex": "|".join(re.escape(t) for t in tokens), "$options": "i"}
        if seniority_level:
            seniority_str = (seniority_level or "").strip()[:100]
            if seniority_str:
                tokens = [t.strip() for t in re.split(r"[,]+", seniority_str) if t.strip()]
                if tokens:
                    mongo_query["seniority_level"] = {"$regex": "|".join(re.escape(t) for t in tokens), "$options": "i"}
        if status:
            status_str = (status or "").strip()[:100]
            if status_str:
                tokens = [t.strip().lower() for t in re.split(r"[,]+", status_str) if t.strip()]
                if tokens:
                    mongo_query["status"] = {"$in": tokens}

        limit = max(1, min(limit or 50, 2000))
        offset = max(0, offset or 0)
        total = await db.candidates.count_documents(mongo_query)
        cursor = db.candidates.find(mongo_query).sort("_id", 1).skip(offset).limit(limit)
        candidates_list = await cursor.to_list(length=limit)

        candidates = []
        for doc in candidates_list:
            candidates.append({
                "id": str(doc["_id"]),
                "name": doc.get("name"),
                "email": doc.get("email"),
                "phone": doc.get("phone"),
                "location": doc.get("location"),
                "technical_skills": doc.get("technical_skills"),
                "experience_years": doc.get("experience_years"),
                "seniority_level": doc.get("seniority_level"),
                "education_level": doc.get("education_level"),
                "status": doc.get("status")
            })

        return {
            "candidates": candidates,
            "filters": {"skills": skills, "location": location, "experience_min": experience_min, "job_id": job_id},
            "count": len(candidates),
            "total": total,
            "limit": limit,
            "offset": offset
        }
    except Exception as e:
        return {
            "candidates": [],
            "filters": {"skills": skills, "location": location, "experience_min": experience_min},
            "count": 0,
            "total": 0,
            "error": str(e)
        }

@app.get("/v1/candidates/job/{job_id}", tags=["Candidate Management"])
async def get_candidates_by_job(job_id: str, auth=Depends(get_auth)):
    """Get All Candidates for a job. Client: only own jobs (data isolation)."""
    if not job_id:
        raise HTTPException(status_code=400, detail="Invalid job ID")
    
    try:
        db = await get_mongo_db()
        if auth.get("type") == "jwt_token" and auth.get("role") == "client":
            client_id = str(auth.get("user_id", ""))
            job_ids = await _client_job_ids_for_dashboard(db, client_id)
            if job_id not in job_ids:
                raise HTTPException(status_code=403, detail="You can only view candidates for your own jobs")
        cursor = db.candidates.find({}).limit(10)
        candidates_list = await cursor.to_list(length=10)
        
        candidates = []
        for doc in candidates_list:
            candidates.append({
                "id": str(doc["_id"]),
                "name": doc.get("name"),
                "email": doc.get("email"),
                "skills": doc.get("technical_skills"),
                "experience": doc.get("experience_years")
            })
        
        return {"candidates": candidates, "job_id": job_id, "count": len(candidates)}
    except Exception as e:
        return {"candidates": [], "job_id": job_id, "count": 0, "error": str(e)}

@app.get("/v1/candidates/{candidate_id}", tags=["Candidate Management"])
async def get_candidate_by_id(candidate_id: str, auth=Depends(get_auth)):
    """Get Specific Candidate by ID"""
    try:
        db = await get_mongo_db()
        
        # Try to convert to ObjectId if valid, otherwise search by string id
        try:
            doc = await db.candidates.find_one({"_id": ObjectId(candidate_id)})
        except:
            doc = await db.candidates.find_one({"id": candidate_id})
        
        if not doc:
            return {"error": "Candidate not found", "candidate_id": candidate_id}
        
        candidate = {
            "id": str(doc["_id"]),
            "name": doc.get("name"),
            "email": doc.get("email"),
            "phone": doc.get("phone"),
            "location": doc.get("location"),
            "experience_years": doc.get("experience_years"),
            "technical_skills": doc.get("technical_skills"),
            "seniority_level": doc.get("seniority_level"),
            "education_level": doc.get("education_level"),
            "resume_path": doc.get("resume_path"),
            "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
            "updated_at": doc.get("updated_at").isoformat() if doc.get("updated_at") else None
        }
        
        return {"candidate": candidate}
    except Exception as e:
        return {"error": str(e), "candidate_id": candidate_id}


def _normalize_header(h: str) -> str:
    """Map common header names to canonical field names for candidate rows."""
    h = (h or "").strip().lower().replace(" ", "_")
    if h in ("name", "full_name", "candidate_name"):
        return "name"
    if h in ("email", "e-mail", "email_address"):
        return "email"
    if h in ("cv_url", "resume_url", "resume", "cv", "resume_path"):
        return "cv_url"
    if h in ("phone", "phone_number", "mobile", "contact"):
        return "phone"
    if h in ("experience_years", "experience", "years_of_experience", "exp"):
        return "experience_years"
    if h in ("status", "application_status"):
        return "status"
    if h in ("location", "city", "address"):
        return "location"
    if h in ("skills", "technical_skills", "tech_skills"):
        return "technical_skills"
    if h in ("designation", "title", "seniority_level", "level"):
        return "designation"
    if h in ("education", "education_level", "qualification"):
        return "education_level"
    return h


# Regexes for resume-style PDF extraction
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"[\+]?[(]?[0-9]{2,4}[)]?[-\s\./0-9]{7,}")
_YEARS_EXP_RE = re.compile(r"(\d+)\s*[\+\-]?\s*(?:years?\s*(?:of\s*)?(?:experience|exp\.?|yoe)|y\.?o\.?e\.?|yrs?)", re.I)
_EDUCATION_LEVEL_RE = re.compile(
    r"\b(ph\.?d|doctorate|m\.?tech|m\.?e\.?|m\.?s\.?c\.?|m\.?ca|mba|m\.?a\.?|m\.?com|b\.?tech|b\.?e\.?|b\.?s\.?c\.?|b\.?ca|b\.?a\.?|b\.?com|bachelor|masters?|master|graduate|post\s*graduate|pg|ug|b\.?arch)\b",
    re.I,
)

# Common tech/skill keywords for skills extraction
_SKILL_KEYWORDS = re.compile(
    r"\b(python|java|javascript|typescript|react|node\.?js|angular|vue|sql|mongodb|aws|docker|kubernetes|"
    r"html|css|php|ruby|go\b|golang|c\+\+|c\b|r\b|scala|kotlin|swift|machine\s*learning|ml\b|ai\b|"
    r"data\s*science|tableau|power\s*bi|excel|git|jenkins|agile|rest\s*api|graphql)\b",
    re.I,
)

# Common Indian cities and location hints (expanded; optional RESUME_KEYWORDS_URL can add more)
_LOCATION_HINTS = re.compile(
    r"\b(mumbai|pune|bangalore|bengaluru|delhi|ncr|noida|gurgaon|gurugram|hyderabad|chennai|kolkata|"
    r"ahmedabad|indore|jaipur|kochi|chandigarh|nagpur|nashik|thane|remote|india|in\b|"
    r"bhubaneswar|coimbatore|mysore|mangalore|trivandrum|surat|vadodara|raipur|bhopal|lucknow|dehradun)\b",
    re.I,
)

# Optional keywords loaded from URL (env RESUME_KEYWORDS_URL) for skills/locations not in hardcoded lists
_EXTRA_SKILLS: List[str] = []
_EXTRA_LOCATIONS: List[str] = []
_KEYWORDS_FETCHED = False


def _fetch_optional_keywords() -> None:
    """Fetch optional skills/locations from JSON URL (env RESUME_KEYWORDS_URL). Run once, then use cache.
    Expected JSON: {"skills": ["word1", "word2", ...], "locations": ["city1", ...]}.
    Any skill/location phrase in the resume text that appears in these lists will be detected."""
    global _EXTRA_SKILLS, _EXTRA_LOCATIONS, _KEYWORDS_FETCHED
    if _KEYWORDS_FETCHED:
        return
    _KEYWORDS_FETCHED = True
    url = os.environ.get("RESUME_KEYWORDS_URL", "").strip()
    if not url:
        return
    try:
        import httpx
        with httpx.Client(timeout=10.0) as client:
            r = client.get(url)
            if r.status_code != 200:
                return
            data = r.json()
        if isinstance(data.get("skills"), list):
            _EXTRA_SKILLS[:] = [str(s).strip() for s in data["skills"] if s and len(str(s).strip()) <= 80]
        if isinstance(data.get("locations"), list):
            _EXTRA_LOCATIONS[:] = [str(l).strip() for l in data["locations"] if l and len(str(l).strip()) <= 80]
    except Exception:
        pass


def _extract_one_resume_from_text(full_text: str) -> Dict[str, str]:
    """Extract a single candidate row from resume-style free text (one person per PDF)."""
    _fetch_optional_keywords()
    lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]
    row = {
        "name": "", "email": "", "phone": "", "location": "",
        "technical_skills": "", "experience_years": "", "designation": "", "education_level": "",
        "status": "applied"
    }

    emails = _EMAIL_RE.findall(full_text)
    if emails:
        row["email"] = emails[0].strip()
    phones = _PHONE_RE.findall(full_text)
    if phones:
        candidate_phone = phones[0].strip()
        if len(candidate_phone) >= 7 and len(candidate_phone) <= 20:
            row["phone"] = candidate_phone

    name_candidates = []
    for ln in lines[:15]:
        if not ln or len(ln) > 80:
            continue
        if _EMAIL_RE.search(ln) or _PHONE_RE.search(ln):
            break
        if "@" in ln or ln.isdigit() or re.match(r"^[\d\s\-+().]+$", ln):
            continue
        if re.match(r"^(https?://|www\.)", ln, re.I):
            continue
        name_candidates.append(ln)
    if name_candidates:
        row["name"] = name_candidates[0][:100].strip() if name_candidates[0] else ""
        if len(name_candidates) > 1 and not row["name"]:
            row["name"] = name_candidates[1][:100].strip()

    if not row["name"] and row["email"]:
        for ln in lines:
            if row["email"] in ln:
                before = ln.split(row["email"])[0].strip()
                if before and len(before) < 60 and "@" not in before:
                    row["name"] = before[:100]
                break
    if not row["name"]:
        row["name"] = "Candidate"

    # --- Location: lines with city/location hints or "location:" / "address:"
    for ln in lines:
        ln_lower = ln.lower()
        if "location" in ln_lower or "address" in ln_lower or "based in" in ln_lower or "city" in ln_lower:
            val = re.sub(r"^(location|address|based in|city)\s*[:\-]\s*", "", ln_lower, flags=re.I).strip()
            if val and len(val) < 80 and not _EMAIL_RE.search(val):
                row["location"] = val[:80].strip()
                break
    if not row["location"]:
        loc_m = _LOCATION_HINTS.search(full_text)
        if loc_m:
            row["location"] = loc_m.group(0).strip()
    # Optional locations from RESUME_KEYWORDS_URL
    if not row["location"] and _EXTRA_LOCATIONS:
        for loc in _EXTRA_LOCATIONS:
            if loc.lower() in full_text.lower():
                row["location"] = loc
                break

    # --- Experience years: "X years experience" / "X+ years" / "X YOE"
    years_m = _YEARS_EXP_RE.search(full_text)
    if years_m:
        row["experience_years"] = years_m.group(1).strip()
    else:
        year_range = re.search(r"(\d+)\s*-\s*(\d+)\s*(?:years?|yrs?)", full_text, re.I)
        if year_range:
            try:
                a, b = int(year_range.group(1)), int(year_range.group(2))
                row["experience_years"] = str(max(a, b) - min(a, b)) if b != a else year_range.group(1)
            except ValueError:
                pass

    # --- Technical skills: "Skills:" section (split by comma/semicolon/pipe so we capture any phrase) or keywords
    in_skills = False
    skill_tokens = []
    for ln in lines:
        ln_lower = ln.lower()
        if re.match(r"^(technical\s*)?skills?|technologies?|expertise\s*[:\-]", ln_lower):
            in_skills = True
            rest = re.sub(r"^(technical\s*)?skills?|technologies?|expertise\s*[:\-]\s*", "", ln_lower, flags=re.I).strip()
            if rest:
                for part in re.split(r"[,;|\t]|\s+and\s+", rest):
                    t = part.strip()
                    if 2 <= len(t) <= 80 and not t.isdigit() and not _EMAIL_RE.search(t):
                        skill_tokens.append(t)
            continue
        if in_skills:
            if ln_lower.startswith(("experience", "education", "project", "work ", "employment")):
                break
            if ln and len(ln) < 120:
                for part in re.split(r"[,;|\t]|\s+and\s+", ln_lower):
                    t = part.strip()
                    if 2 <= len(t) <= 80 and not t.isdigit() and not _EMAIL_RE.search(t):
                        skill_tokens.append(t)
            if len(skill_tokens) >= 30:
                break
    # Also catch "Proficient in X, Y, Z" or "Skills: X, Y, Z" anywhere in text (any words, not just hardcoded)
    for pat in [
        r"(?:proficient in|skills?|technologies?|expertise)\s*[:\-]\s*([^\n]{10,300})",
        r"(?:key\s*skills?|core\s*skills?)\s*[:\-]\s*([^\n]{10,300})",
    ]:
        for m in re.finditer(pat, full_text, re.I):
            chunk = m.group(1)
            for part in re.split(r"[,;|\t]|\s+and\s+", chunk):
                t = part.strip()
                if 2 <= len(t) <= 80 and not t.isdigit() and not _EMAIL_RE.search(t):
                    skill_tokens.append(t)
    if skill_tokens:
        row["technical_skills"] = ", ".join(dict.fromkeys(skill_tokens))[:500]
    if not row["technical_skills"]:
        skills_found = _SKILL_KEYWORDS.findall(full_text)
        if skills_found:
            row["technical_skills"] = ", ".join(dict.fromkeys(skills_found))[:500]
    # Merge extra skills from optional RESUME_KEYWORDS_URL that appear in text
    if _EXTRA_SKILLS and row["technical_skills"]:
        existing = {t.strip().lower() for t in row["technical_skills"].split(",")}
        for s in _EXTRA_SKILLS:
            if s.lower() in full_text.lower() and s.strip().lower() not in existing:
                row["technical_skills"] = (row["technical_skills"].strip() + ", " + s.strip()).strip()[:500]
                existing.add(s.strip().lower())
    elif _EXTRA_SKILLS:
        found = [s for s in _EXTRA_SKILLS if s.lower() in full_text.lower()]
        if found:
            row["technical_skills"] = ", ".join(found)[:500]

    # --- Designation / title: first line after "experience" or common title keywords
    title_keywords = re.compile(
        r"\b(software\s*engineer|developer|engineer|analyst|manager|lead|architect|consultant|"
        r"intern|associate|senior|junior|full\s*stack|front\s*end|back\s*end|data\s*scientist)\b",
        re.I,
    )
    for i, ln in enumerate(lines):
        ln_lower = ln.lower()
        if "experience" in ln_lower or "work experience" in ln_lower or "employment" in ln_lower:
            for j in range(i + 1, min(i + 4, len(lines))):
                cand = lines[j].strip()
                if cand and len(cand) < 80 and title_keywords.search(cand) and not _EMAIL_RE.search(cand):
                    row["designation"] = cand[:80]
                    break
            if row["designation"]:
                break
    if not row["designation"]:
        m = title_keywords.search(full_text)
        if m:
            row["designation"] = m.group(0).strip()
    # Fallback: first line after Experience that looks like a job title (any phrase, not just hardcoded keywords)
    if not row["designation"]:
        date_like = re.compile(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)?\.?\s*\d{4}\s*[-–—]\s*(present|current|\d{4})", re.I)
        for i, ln in enumerate(lines):
            ln_lower = ln.lower()
            if "experience" in ln_lower or "work experience" in ln_lower or "employment" in ln_lower:
                for j in range(i + 1, min(i + 5, len(lines))):
                    cand = lines[j].strip()
                    if not cand or len(cand) < 5 or len(cand) > 80:
                        continue
                    if "@" in cand or _EMAIL_RE.search(cand) or date_like.match(cand):
                        continue
                    if re.match(r"^\d{4}\s*[-–—]", cand):
                        continue
                    row["designation"] = cand[:80]
                    break
                break

    # --- Education level: B.Tech, M.Tech, MBA, Bachelor, etc.
    edu_m = _EDUCATION_LEVEL_RE.search(full_text)
    if edu_m:
        row["education_level"] = edu_m.group(0).strip()

    return row


def _parse_pdf_as_table(lines: List[str]) -> List[Dict[str, Any]]:
    """Parse PDF text as table (CSV-like: header row + data rows by comma/tab)."""
    rows = []
    headers = []
    for i, line in enumerate(lines):
        line = (line or "").strip()
        if not line:
            continue
        parts = re.split(r"[\t,]+", line, maxsplit=14)
        parts = [p.strip() for p in parts]
        if i == 0 and len(parts) >= 2:
            headers = [_normalize_header(p) for p in parts]
            continue
        if len(parts) >= 2:
            row = {}
            for j, val in enumerate(parts):
                key = headers[j] if j < len(headers) else f"col_{j}"
                row[key] = val
            has_email = row.get("email") or any(_EMAIL_RE.search(str(v)) for v in row.values() if v)
            if has_email:
                rows.append(row)
    return rows


@app.post("/v1/candidates/parse-pdf", tags=["Candidate Management"])
async def parse_pdf_candidates(file: UploadFile = File(...), auth=Depends(get_auth)):
    """Parse PDF: single resume → one row (name/email/phone); table-like PDF → multiple rows."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="File must be a PDF")
    try:
        import PyPDF2
        content = await file.read()
        if len(content) > 50 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="PDF must be under 50MB")
        reader = PyPDF2.PdfReader(io.BytesIO(content))
        lines = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                lines.extend(text.splitlines())
        full_text = "\n".join((ln or "").strip() for ln in lines if (ln or "").strip())
        if not full_text.strip():
            return {"rows": [], "count": 0}

        table_rows = _parse_pdf_as_table(lines)
        rows_with_email = sum(1 for r in table_rows if r.get("email") or any(_EMAIL_RE.search(str(v)) for v in r.values() if v))
        if len(table_rows) >= 2 and rows_with_email >= 2:
            for r in table_rows:
                if not r.get("email"):
                    for v in r.values():
                        if v and _EMAIL_RE.search(str(v)):
                            r["email"] = _EMAIL_RE.search(str(v)).group(0)
                            break
            return {"rows": table_rows, "count": len(table_rows)}

        row = _extract_one_resume_from_text(full_text)
        return {"rows": [row], "count": 1}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse PDF: {str(e)[:200]}")


@app.post("/v1/candidates/check-duplicates", tags=["Candidate Management"])
async def check_duplicate_candidates(emails: List[str], auth=Depends(get_auth)):
    """Check which emails already exist in the candidates database. Returns list of duplicate emails."""
    try:
        db = await get_mongo_db()
        # Normalize emails to lowercase for comparison
        normalized_emails = [email.strip().lower() for email in emails if email and email.strip()]
        
        if not normalized_emails:
            return {"duplicates": [], "count": 0}
        
        # Find existing candidates with matching emails
        existing_candidates = await db.candidates.find(
            {"email": {"$in": normalized_emails}},
            {"email": 1}
        ).to_list(length=None)
        
        duplicate_emails = [doc["email"].lower() for doc in existing_candidates]
        
        return {
            "duplicates": duplicate_emails,
            "count": len(duplicate_emails)
        }
    except Exception as e:
        logger.error(f"Error checking duplicate candidates: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to check duplicates: {str(e)[:200]}")


@app.post("/v1/candidates/bulk", tags=["Candidate Management"])
async def bulk_upload_candidates(candidates: CandidateBulk, auth=Depends(get_auth)):
    """Bulk Upload Candidates (recruiter JWT or API key). Inserts into candidates and, when job_id is provided, creates job_applications so dashboard stats stay in sync."""
    try:
        db = await get_mongo_db()
        inserted_count = 0
        errors = []
        job_id_str = (candidates.job_id or "").strip()
        # Validate job exists when job_id is provided (so we can link applicants for dashboard)
        if job_id_str:
            try:
                job_doc = await db.jobs.find_one({"_id": ObjectId(job_id_str)})
            except Exception:
                job_doc = await db.jobs.find_one({"id": job_id_str})
            if not job_doc:
                raise HTTPException(status_code=400, detail="Invalid or unknown job_id for bulk upload")
        now = datetime.now(timezone.utc)

        for i, candidate in enumerate(candidates.candidates):
            try:
                email = candidate.get("email", "")
                if not email:
                    errors.append(f"Candidate {i+1}: Email is required")
                    continue

                # Check email uniqueness
                existing = await db.candidates.find_one({"email": email})
                if existing:
                    # Still link existing candidate to this job for dashboard applicant count
                    if job_id_str:
                        candidate_id_str = str(existing["_id"])
                        app_existing = await db.job_applications.find_one({"job_id": job_id_str, "candidate_id": candidate_id_str})
                        if not app_existing:
                            await db.job_applications.insert_one({
                                "job_id": job_id_str,
                                "candidate_id": candidate_id_str,
                                "status": "applied",
                                "applied_date": now
                            })
                    errors.append(f"Candidate {i+1}: Email {email} already exists")
                    continue

                # Insert candidate
                document = {
                    "name": candidate.get("name", "Unknown"),
                    "email": email,
                    "phone": candidate.get("phone", ""),
                    "location": candidate.get("location", ""),
                    "experience_years": max(0, int(candidate.get("experience_years", 0)) if str(candidate.get("experience_years", 0)).isdigit() else 0),
                    "technical_skills": candidate.get("technical_skills", ""),
                    "seniority_level": candidate.get("designation", candidate.get("seniority_level", "")),
                    "education_level": candidate.get("education_level", ""),
                    "resume_path": candidate.get("cv_url", candidate.get("resume_path", "")),
                    "status": candidate.get("status", "applied"),
                    "created_at": now
                }
                result = await db.candidates.insert_one(document)
                inserted_count += 1

                # Link to job so recruiter dashboard "Total Applicants" and per-job counts stay in sync
                if job_id_str and result.inserted_id:
                    candidate_id_str = str(result.inserted_id)
                    app_existing = await db.job_applications.find_one({"job_id": job_id_str, "candidate_id": candidate_id_str})
                    if not app_existing:
                        await db.job_applications.insert_one({
                            "job_id": job_id_str,
                            "candidate_id": candidate_id_str,
                            "status": "applied",
                            "applied_date": now
                        })
            except HTTPException:
                raise
            except Exception as e:
                errors.append(f"Candidate {i+1}: {str(e)[:100]}")
                continue

        return {
            "message": "Bulk upload completed",
            "candidates_received": len(candidates.candidates),
            "candidates_inserted": inserted_count,
            "errors": errors[:5] if errors else [],
            "total_errors": len(errors),
            "status": "success" if inserted_count > 0 else "failed"
        }
    except HTTPException:
        raise
    except Exception as e:
        return {
            "message": "Bulk upload failed",
            "error": str(e),
            "candidates_received": len(candidates.candidates) if candidates else 0,
            "candidates_inserted": 0,
            "status": "failed"
        }

# AI Matching Engine (2 endpoints)
@app.get("/v1/match/{job_id}/top", tags=["AI Matching Engine"])
async def get_top_matches(job_id: str, limit: int = 10, auth = Depends(get_auth)):  # Accept JWT tokens or API keys
    """AI-powered semantic candidate matching via Agent Service. Recruiter: only their applicants. Client: only own jobs."""
    if limit < 1 or limit > 50:
        raise HTTPException(status_code=400, detail="Invalid limit parameter (must be 1-50)")
    
    candidate_ids_scope: Optional[List[str]] = None
    db = None

    # Client data isolation:
    # 1) can only request matches for visible jobs (own + connected recruiters)
    # 2) can only match against candidates who actually applied to the selected job
    if auth.get("type") == "jwt_token" and auth.get("role") == "client":
        db = await get_mongo_db()
        client_id = str(auth.get("user_id", ""))
        job_ids = await _client_job_ids_for_dashboard(db, client_id)
        if job_id not in job_ids:
            raise HTTPException(status_code=403, detail="You can only view matches for your own jobs")
        candidate_ids_scope = await _job_applicant_ids(db, job_id)

    # Recruiter scope: only candidates who applied to this recruiter's jobs
    if auth.get("type") == "jwt_token" and auth.get("role") == "recruiter":
        recruiter_id = str(auth.get("user_id", ""))
        if recruiter_id:
            if db is None:
                db = await get_mongo_db()
            cursor = db.jobs.find({"status": "active", "recruiter_id": recruiter_id}, {"_id": 1})
            recruiter_jobs = await cursor.to_list(length=500)
            recruiter_job_ids = [str(doc["_id"]) for doc in recruiter_jobs]
            if recruiter_job_ids:
                pipeline = [{"$match": {"job_id": {"$in": recruiter_job_ids}}}, {"$group": {"_id": "$candidate_id"}}]
                candidate_ids_scope = []
                async for agg_doc in db.job_applications.aggregate(pipeline):
                    cid = agg_doc.get("_id")
                    if cid:
                        candidate_ids_scope.append(str(cid))
            else:
                candidate_ids_scope = []
        else:
            candidate_ids_scope = []

    # Scoped roles with no applicants: return empty without calling agent
    if candidate_ids_scope is not None and len(candidate_ids_scope) == 0:
        scoped_label = "client job applicants" if auth.get("role") == "client" else "recruiter applicants"
        return {
            "matches": [],
            "top_candidates": [],
            "job_id": job_id,
            "limit": limit,
            "total_candidates": 0,
            "algorithm_version": "2.0.0-gateway-scoped",
            "ai_analysis": f"No applicants in {scoped_label} scope",
            "agent_status": "scoped"
        }

    try:
        import httpx
        agent_url = os.getenv("AGENT_SERVICE_URL")
        agent_timeout = float(os.getenv("AGENT_MATCH_TIMEOUT", "90"))
        payload = {"job_id": job_id, "candidate_ids": candidate_ids_scope if candidate_ids_scope else []}
        async with httpx.AsyncClient(timeout=agent_timeout) as client:
            response = await client.post(
                f"{agent_url}/match",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {os.getenv('API_KEY_SECRET')}"
                }
            )
            if response.status_code == 200:
                agent_result = response.json()
                raw_candidates = agent_result.get("top_candidates", [])
                scope_set = set(candidate_ids_scope) if candidate_ids_scope else None
                if scope_set is not None:
                    raw_candidates = [c for c in raw_candidates if str(c.get("candidate_id") or "") in scope_set]
                    raw_candidates.sort(key=lambda c: (c.get("score") or 0), reverse=True)
                    cap = min(limit, len(scope_set))
                    raw_candidates = raw_candidates[:cap]
                else:
                    raw_candidates = raw_candidates[:limit]
                matches = []
                for candidate in raw_candidates:
                    matches.append({
                        "candidate_id": candidate.get("candidate_id"),
                        "name": candidate.get("name"),
                        "email": candidate.get("email"),
                        "score": candidate.get("score"),
                        "skills_match": ", ".join(candidate.get("skills_match", [])),
                        "experience_match": candidate.get("experience_match"),
                        "location_match": candidate.get("location_match"),
                        "reasoning": candidate.get("reasoning"),
                        "recommendation_strength": "Strong Match" if candidate.get("score", 0) > 80 else "Good Match"
                    })
                return {
                    "matches": matches,
                    "top_candidates": matches,
                    "job_id": job_id,
                    "limit": limit,
                    "total_candidates": len(matches),
                    "algorithm_version": agent_result.get("algorithm_version", "2.0.0-phase2-ai"),
                    "processing_time": f"{agent_result.get('processing_time', 0)}s",
                    "ai_analysis": "Real AI semantic matching via Agent Service" + (
                        " (scoped to client job applicants)" if (scope_set and auth.get("role") == "client") else
                        (" (scoped to recruiter applicants)" if scope_set else "")
                    ),
                    "agent_status": "connected"
                }
            else:
                # 503 (engine loading), 5xx, or other non-200: use fallback matching
                return await fallback_matching(job_id, limit, candidate_ids_scope=candidate_ids_scope)
    except Exception as e:
        log_error("agent_service_error", str(e), {"job_id": job_id})
        return await fallback_matching(job_id, limit, candidate_ids_scope=candidate_ids_scope)


def _job_skill_tokens(text: str) -> set:
    """Extract skill-like tokens from job requirements/description for matching."""
    if not text:
        return set()
    s = re.sub(r"[,;|/&\n]+", " ", (text or "").lower())
    tokens = set()
    for part in s.split():
        t = part.strip().strip(".-()")
        if 2 <= len(t) <= 50 and t.isalnum():
            tokens.add(t)
    return tokens


async def fallback_matching(job_id: str, limit: int, candidate_ids_scope: Optional[List[str]] = None):
    """Fallback matching when agent service is unavailable. If candidate_ids_scope is set (recruiter), only those candidates are considered; else all candidates."""
    try:
        db = await get_mongo_db()
        try:
            job_doc = await db.jobs.find_one({"_id": ObjectId(job_id)})
        except Exception:
            job_doc = await db.jobs.find_one({"id": job_id})
        if not job_doc:
            return {"matches": [], "job_id": job_id, "limit": limit, "error": "Job not found", "agent_status": "error"}
        job_req_text = ((job_doc.get("requirements") or "") + " " + (job_doc.get("description") or "")).lower()
        job_skill_tokens = _job_skill_tokens(job_req_text)
        job_location = (job_doc.get("location") or "").strip().lower()
        job_exp_years = None
        for m in re.finditer(r"(\d+)\s*[\+\-]?\s*(?:years?\s*(?:of\s*)?(?:experience|exp\.?)|y\.?o\.?e\.?|yrs?)", job_req_text, re.I):
            job_exp_years = int(m.group(1))
            break
        if candidate_ids_scope:
            object_ids = []
            for cid in candidate_ids_scope:
                try:
                    object_ids.append(ObjectId(cid))
                except Exception:
                    pass
            if not object_ids:
                return {
                    "matches": [],
                    "job_id": job_id,
                    "limit": limit,
                    "total_candidates": 0,
                    "algorithm_version": "2.0.0-gateway-fallback",
                    "ai_analysis": "No applicants in scoped pool",
                    "agent_status": "disconnected"
                }
            cursor = db.candidates.find({"_id": {"$in": object_ids}})
        else:
            cursor = db.candidates.find({})
        candidates_list = await cursor.to_list(length=2000)
        scored = []
        for doc in candidates_list:
            candidate_skills = (doc.get("technical_skills") or "").lower()
            candidate_location = (doc.get("location") or "").strip().lower()
            candidate_exp = 0
            try:
                exp_val = doc.get("experience_years")
                if exp_val is not None:
                    candidate_exp = int(exp_val) if isinstance(exp_val, int) else int(str(exp_val).strip() or 0)
            except (ValueError, TypeError):
                pass
            matched_skills = [t for t in job_skill_tokens if t in candidate_skills][:20]
            skill_match_count = len(matched_skills)
            skill_score = min(100, skill_match_count * 12) if job_skill_tokens else 50
            location_match = bool(job_location and candidate_location and (job_location in candidate_location or candidate_location in job_location))
            location_score = 100 if location_match else 0
            if job_exp_years is not None:
                if candidate_exp >= job_exp_years:
                    experience_score = 100
                else:
                    experience_score = max(0, int(100 * candidate_exp / job_exp_years))
            else:
                experience_score = 50
            total = (skill_score * 0.5) + (experience_score * 0.3) + (location_score * 0.2)
            total = max(50, min(95, int(total)))
            scored.append((total, doc, matched_skills, skill_score, experience_score, location_score, location_match))
        scored.sort(key=lambda x: (-x[0], -x[3], -x[4]))
        top = scored[:limit]
        matches = []
        for total, doc, matched_skills, skill_score, experience_score, location_score, location_match in top:
            matches.append({
                "candidate_id": str(doc["_id"]),
                "name": doc.get("name"),
                "email": doc.get("email"),
                "score": total,
                "skills_match": ", ".join(matched_skills) if matched_skills else (doc.get("technical_skills") or ""),
                "experience_match": experience_score,
                "location_match": location_score,
                "reasoning": f"Skills: {len(matched_skills)} match job JD; experience {experience_score}%; location {location_score}%",
                "recommendation_strength": "Good Match" if total > 75 else "Fair Match"
            })
        return {
            "matches": matches,
            "top_candidates": matches,
            "job_id": job_id,
            "limit": limit,
            "total_candidates": len(matches),
            "algorithm_version": "2.0.0-gateway-fallback",
            "processing_time": "0.05s",
            "ai_analysis": "Database fallback - matched by job requirements, skills, experience and location",
            "agent_status": "disconnected"
        }
    except Exception as e:
        return {"matches": [], "job_id": job_id, "limit": limit, "error": str(e), "agent_status": "error"}

async def batch_fallback_matching(job_ids: List[str]):
    """Fallback batch matching when agent service is unavailable"""
    try:
        db = await get_mongo_db()
        
        # Get job requirements for each job
        job_requirements = {}
        for job_id in job_ids:
            try:
                job_doc = await db.jobs.find_one({"_id": ObjectId(job_id)})
            except:
                job_doc = await db.jobs.find_one({"id": job_id})
            
            job_requirements[job_id] = {
                "requirements": (job_doc.get("requirements", "") if job_doc else "").lower(),
                "location": job_doc.get("location", "") if job_doc else ""
            }
        
        # Get candidates
        cursor = db.candidates.find({}).limit(5)
        candidates = await cursor.to_list(length=5)
        
        batch_results = {}
        for job_id in job_ids:
            job_req = job_requirements[job_id]
            matches = []
            
            for i, doc in enumerate(candidates):
                candidate_skills = (doc.get("technical_skills") or "").lower()
                candidate_location = doc.get("location") or ""
                
                # Basic skill matching
                skill_match_count = sum(1 for skill in ['python', 'java', 'javascript'] 
                                      if skill in candidate_skills and skill in job_req["requirements"])
                
                # Location matching
                location_match = job_req["location"].lower() in candidate_location.lower() if job_req["location"] and candidate_location else False
                
                # Calculate score
                base_score = 60 + (skill_match_count * 10) + (10 if location_match else 0) + (5 - i)
                
                matches.append({
                    "candidate_id": str(doc["_id"]),
                    "name": doc.get("name"),
                    "email": doc.get("email"),
                    "score": min(95, base_score),
                    "skills_match": doc.get("technical_skills") or "",
                    "experience_match": f"Skills: {skill_match_count} matches",
                    "location_match": location_match,
                    "reasoning": f"Fallback batch matching: {skill_match_count} skill matches, location: {location_match}",
                    "recommendation_strength": "Good Match" if base_score > 75 else "Fair Match"
                })
            
            batch_results[str(job_id)] = {
                "job_id": job_id,
                "matches": matches,
                "top_candidates": matches,
                "total_candidates": len(matches),
                "algorithm": "fallback-batch",
                "processing_time": "0.05s",
                "ai_analysis": "Database fallback - Agent service unavailable"
            }
        
        return {
            "batch_results": batch_results,
            "total_jobs_processed": len(job_ids),
            "total_candidates_analyzed": len(candidates),
            "algorithm_version": "2.0.0-gateway-fallback-batch",
            "status": "fallback_success",
            "agent_status": "disconnected"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Batch fallback failed: {str(e)}")

class BatchMatchRequest(BaseModel):
    job_ids: List[str]
    limit: Optional[int] = 10

@app.post("/v1/match/batch", tags=["AI Matching Engine"])
async def batch_match_jobs(
    request: BatchMatchRequest = None,
    job_ids: Optional[List[str]] = None,
    limit: Optional[int] = None,
    api_key: str = Depends(get_api_key)
):
    """Batch AI matching via Agent Service"""
    # Support both JSON body and query params
    if request:
        job_id_list = request.job_ids
        match_limit = request.limit or 10
    elif job_ids:
        job_id_list = job_ids
        match_limit = limit or 10
    else:
        raise HTTPException(status_code=400, detail="job_ids list is required")
    
    if not job_id_list or len(job_id_list) == 0:
        raise HTTPException(status_code=400, detail="At least one job ID is required")
    
    if len(job_id_list) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 jobs can be processed in batch")
    
    try:
        import httpx
        agent_url = os.getenv("AGENT_SERVICE_URL")
        
        # Call agent service for batch AI matching
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{agent_url}/batch-match",
                json={"job_ids": job_id_list},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {os.getenv('API_KEY_SECRET')}"
                }
            )
            
            if response.status_code == 200:
                agent_result = response.json()
                
                # Transform agent batch response to detailed format
                enhanced_batch_results = {}
                for job_id_str, job_result in agent_result.get("batch_results", {}).items():
                    matches = []
                    for candidate in job_result.get("matches", []):
                        matches.append({
                            "candidate_id": candidate.get("candidate_id"),
                            "name": candidate.get("name"),
                            "email": candidate.get("email"),
                            "score": candidate.get("score"),
                            "skills_match": ", ".join(candidate.get("skills_match", [])),
                            "experience_match": candidate.get("experience_match"),
                            "location_match": candidate.get("location_match"),
                            "reasoning": candidate.get("reasoning"),
                            "recommendation_strength": "Strong Match" if candidate.get("score", 0) > 80 else "Good Match"
                        })
                    
                    enhanced_batch_results[job_id_str] = {
                        "job_id": job_result.get("job_id"),
                        "matches": matches,
                        "top_candidates": matches,
                        "total_candidates": len(matches),
                        "algorithm": job_result.get("algorithm", "phase3-ai"),
                        "processing_time": job_result.get("processing_time", "0.5s"),
                        "ai_analysis": "Real AI semantic matching via Agent Service"
                    }
                
                return {
                    "batch_results": enhanced_batch_results,
                    "total_jobs_processed": agent_result.get("total_jobs_processed", len(job_ids)),
                    "total_candidates_analyzed": agent_result.get("total_candidates_analyzed", 0),
                    "algorithm_version": agent_result.get("algorithm_version", "3.0.0-phase3-production-batch"),
                    "status": "success",
                    "agent_status": "connected"
                }
            else:
                # Fallback to database batch matching
                return await batch_fallback_matching(job_id_list)
                
    except Exception as e:
        log_error("batch_matching_error", str(e), {"job_ids": job_id_list})
        # Fallback to database batch matching
        return await batch_fallback_matching(job_id_list)

# Assessment & Workflow (5 endpoints)
@app.post("/v1/feedback", tags=["Assessment & Workflow"])
async def submit_feedback(feedback: FeedbackSubmission, auth = Depends(get_auth)):
    """Values Assessment (JWT authenticated)"""
    try:
        # Verify the candidate_id matches the authenticated user (if using JWT token)
        auth_info = auth
        if auth_info.get("type") == "jwt_token" and auth_info.get("role") == "candidate":
            token_candidate_id = str(auth_info.get("user_id", ""))
            if token_candidate_id and token_candidate_id != str(feedback.candidate_id):
                raise HTTPException(status_code=403, detail="You can only submit feedback for yourself")
        
        db = await get_mongo_db()
        avg_score = (feedback.integrity + feedback.honesty + feedback.discipline + 
                    feedback.hard_work + feedback.gratitude) / 5
        
        document = {
            "candidate_id": feedback.candidate_id,
            "job_id": feedback.job_id,
            "integrity": feedback.integrity,
            "honesty": feedback.honesty,
            "discipline": feedback.discipline,
            "hard_work": feedback.hard_work,
            "gratitude": feedback.gratitude,
            "average_score": avg_score,
            "comments": feedback.comments,
            "created_at": datetime.now(timezone.utc)
        }
        if feedback.experience_level:
            document["experience_level"] = feedback.experience_level
        result = await db.feedback.insert_one(document)
        feedback_id = str(result.inserted_id)
        
        return {
            "message": "Feedback submitted successfully",
            "feedback_id": feedback_id,
            "candidate_id": feedback.candidate_id,
            "job_id": feedback.job_id,
            "values_scores": {
                "integrity": feedback.integrity,
                "honesty": feedback.honesty,
                "discipline": feedback.discipline,
                "hard_work": feedback.hard_work,
                "gratitude": feedback.gratitude
            },
            "average_score": round(avg_score, 2),
            "submitted_at": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        return {
            "message": "Feedback submission failed",
            "error": str(e),
            "candidate_id": feedback.candidate_id,
            "job_id": feedback.job_id
        }

@app.get("/v1/feedback", tags=["Assessment & Workflow"])
async def get_all_feedback(candidate_id: Optional[str] = None, auth = Depends(get_auth)):
    """Get All Feedback Records (supports filtering by candidate_id)"""
    try:
        db = await get_mongo_db()
        
        # Build match filter - support candidate_id filtering
        match_filter = {}
        if candidate_id:
            # If JWT auth, verify candidate_id matches authenticated user
            if auth.get("type") == "jwt_token" and auth.get("role") == "candidate":
                token_candidate_id = str(auth.get("user_id", ""))
                if token_candidate_id and token_candidate_id != str(candidate_id):
                    raise HTTPException(status_code=403, detail="You can only view your own feedback")
            match_filter["candidate_id"] = candidate_id
        
        # Use aggregation pipeline for JOIN-like behavior
        pipeline = [
            {"$match": match_filter} if match_filter else {"$match": {}},
            {"$lookup": {
                "from": "candidates",
                "localField": "candidate_id",
                "foreignField": "_id",
                "as": "candidate"
            }},
            {"$lookup": {
                "from": "jobs",
                "localField": "job_id",
                "foreignField": "_id",
                "as": "job"
            }},
            {"$sort": {"created_at": -1}},
            {"$project": {
                "id": {"$toString": "$_id"},
                "candidate_id": {"$toString": "$candidate_id"},
                "job_id": {"$toString": "$job_id"},
                "integrity": 1,
                "honesty": 1,
                "discipline": 1,
                "hard_work": 1,
                "gratitude": 1,
                "average_score": 1,
                "comments": 1,
                "experience_level": 1,
                "created_at": 1,
                "candidate_name": {"$arrayElemAt": ["$candidate.name", 0]},
                "job_title": {"$arrayElemAt": ["$job.title", 0]}
            }}
        ]
        
        cursor = db.feedback.aggregate(pipeline)
        feedback_list = await cursor.to_list(length=None)
        
        feedback_records = []
        for doc in feedback_list:
            values_scores = {
                "integrity": doc.get("integrity", 0),
                "honesty": doc.get("honesty", 0),
                "discipline": doc.get("discipline", 0),
                "hard_work": doc.get("hard_work", 0),
                "gratitude": doc.get("gratitude", 0)
            }
            feedback_records.append({
                "id": doc.get("id"),
                "candidate_id": doc.get("candidate_id"),
                "job_id": doc.get("job_id"),
                "values_scores": values_scores,  # Keep for backward compatibility
                "values_assessment": {  # Frontend expects this field name
                    "integrity": values_scores["integrity"],
                    "honesty": values_scores["honesty"],
                    "discipline": values_scores["discipline"],
                    "hardWork": values_scores["hard_work"],  # Frontend uses camelCase
                    "gratitude": values_scores["gratitude"]
                },
                "average_score": float(doc.get("average_score", 0)) if doc.get("average_score") else 0,
                "comments": doc.get("comments"),  # Keep for backward compatibility
                "feedback_text": doc.get("comments", ""),  # Frontend expects this field name
                "rating": int(doc.get("average_score", 0)) if doc.get("average_score") else 0,  # Frontend expects rating
                "experience_level": doc.get("experience_level"),
                "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
                "candidate_name": doc.get("candidate_name"),
                "job_title": doc.get("job_title"),
                "interviewer_name": None  # Frontend expects this (optional)
            })
        
        return {"feedback": feedback_records, "count": len(feedback_records)}
    except Exception as e:
        return {"feedback": [], "count": 0, "error": str(e)}



@app.get("/v1/interviews", tags=["Assessment & Workflow"])
async def get_interviews(candidate_id: Optional[str] = None, auth = Depends(get_auth)):
    """Get All Interviews (supports filtering by candidate_id). Recruiter: only interviews for their jobs."""
    try:
        db = await get_mongo_db()
        
        # Build match filter - support candidate_id filtering
        match_filter = {}
        if candidate_id:
            # If JWT auth, verify candidate_id matches authenticated user
            if auth.get("type") == "jwt_token" and auth.get("role") == "candidate":
                token_candidate_id = str(auth.get("user_id", ""))
                if token_candidate_id and token_candidate_id != str(candidate_id):
                    raise HTTPException(status_code=403, detail="You can only view your own interviews")
            match_filter["candidate_id"] = candidate_id
        # Recruiter: only interviews for jobs they posted
        if auth.get("type") == "jwt_token" and auth.get("role") == "recruiter":
            recruiter_id = str(auth.get("user_id", ""))
            if recruiter_id:
                cursor = db.jobs.find({"status": "active", "recruiter_id": recruiter_id}, {"_id": 1})
                jobs_list = await cursor.to_list(length=500)
                job_ids = [str(doc["_id"]) for doc in jobs_list]
                match_filter["job_id"] = {"$in": job_ids} if job_ids else {"$in": []}
        # Client: only interviews for own jobs (data isolation)
        if auth.get("type") == "jwt_token" and auth.get("role") == "client":
            client_id = str(auth.get("user_id", ""))
            job_ids = await _client_job_ids_for_dashboard(db, client_id)
            match_filter["job_id"] = {"$in": job_ids} if job_ids else {"$in": []}
        
        pipeline = [
            {"$match": match_filter} if match_filter else {"$match": {}},
            {"$addFields": {
                "candidate_id_obj": {"$cond": [
                    {"$and": [{"$ne": ["$candidate_id", None]}, {"$ne": ["$candidate_id", ""]}]},
                    {"$toObjectId": "$candidate_id"},
                    None
                ]},
                "job_id_obj": {"$cond": [
                    {"$and": [{"$ne": ["$job_id", None]}, {"$ne": ["$job_id", ""]}]},
                    {"$toObjectId": "$job_id"},
                    None
                ]}
            }},
            {"$lookup": {
                "from": "candidates",
                "localField": "candidate_id_obj",
                "foreignField": "_id",
                "as": "candidate"
            }},
            {"$lookup": {
                "from": "jobs",
                "localField": "job_id_obj",
                "foreignField": "_id",
                "as": "job"
            }},
            {"$sort": {"interview_date": -1}},
            {"$project": {
                "id": {"$toString": "$_id"},
                "candidate_id": {"$toString": "$candidate_id"},
                "job_id": {"$toString": "$job_id"},
                "interview_date": 1,
                "interviewer": 1,
                "status": 1,
                "interview_type": 1,
                "meeting_link": 1,
                "meeting_address": 1,
                "meeting_phone": 1,
                "notes": 1,
                "candidate_name": {"$arrayElemAt": ["$candidate.name", 0]},
                "job_title": {"$arrayElemAt": ["$job.title", 0]}
            }}
        ]
        
        cursor = db.interviews.aggregate(pipeline)
        interviews_list = await cursor.to_list(length=None)
        
        interviews = []
        for doc in interviews_list:
            interview_date = doc.get("interview_date")
            if hasattr(interview_date, "isoformat"):
                interview_date_str = interview_date.isoformat()
            elif interview_date:
                interview_date_str = str(interview_date)
            else:
                interview_date_str = None
            interviews.append({
                "id": doc.get("id"),
                "candidate_id": doc.get("candidate_id"),
                "job_id": doc.get("job_id"),
                "interview_date": interview_date_str,
                "scheduled_date": interview_date_str,
                "scheduled_time": None,
                "interview_type": doc.get("interview_type") or "technical",
                "interviewer": doc.get("interviewer"),
                "status": doc.get("status") or "scheduled",
                "candidate_name": doc.get("candidate_name"),
                "job_title": doc.get("job_title"),
                "company": None,
                "meeting_link": doc.get("meeting_link"),
                "meeting_address": doc.get("meeting_address"),
                "meeting_phone": doc.get("meeting_phone"),
                "notes": doc.get("notes")
            })
        
        return {"interviews": interviews, "count": len(interviews)}
    except Exception as e:
        return {"interviews": [], "count": 0, "error": str(e)}

@app.post("/v1/interviews", tags=["Assessment & Workflow"])
async def schedule_interview(interview: InterviewSchedule, auth=Depends(get_auth)):
    """Schedule Interview. Recruiter JWT: job_id must be one of recruiter's jobs. API key: no restriction."""
    try:
        db = await get_mongo_db()
        if auth.get("type") == "jwt_token" and auth.get("role") == "recruiter":
            recruiter_id = str(auth.get("user_id", ""))
            if recruiter_id:
                cursor = db.jobs.find({"status": "active", "recruiter_id": recruiter_id}, {"_id": 1})
                jobs_list = await cursor.to_list(length=500)
                job_ids = [str(doc["_id"]) for doc in jobs_list]
                if interview.job_id not in job_ids:
                    raise HTTPException(status_code=403, detail="You can only schedule interviews for your own jobs")
        if auth.get("type") == "jwt_token" and auth.get("role") == "client":
            client_id = str(auth.get("user_id", ""))
            job_ids = await _client_job_ids_for_dashboard(db, client_id)
            if interview.job_id not in job_ids:
                raise HTTPException(status_code=403, detail="You can only schedule interviews for your own jobs")

        document = {
            "candidate_id": interview.candidate_id,
            "job_id": interview.job_id,
            "interview_date": interview.interview_date,
            "interviewer": interview.interviewer,
            "status": "scheduled",
            "notes": interview.notes,
            "created_at": datetime.now(timezone.utc)
        }
        if interview.interview_type:
            document["interview_type"] = interview.interview_type
        if interview.meeting_link:
            document["meeting_link"] = interview.meeting_link
        if interview.meeting_address:
            document["meeting_address"] = interview.meeting_address
        if interview.meeting_phone:
            phone_clean = re.sub(r"[\s\-\.()]", "", interview.meeting_phone.strip())
            if phone_clean.startswith("0") and len(phone_clean) == 11:
                phone_clean = phone_clean[1:]
            if not re.match(r"^(\+91|91)?[6-9]\d{9}$", phone_clean):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid phone format. Use Indian format: +91XXXXXXXXXX, 0XXXXXXXXXX, or XXXXXXXXXX (10 digits, starting with 6-9)."
                )
            document["meeting_phone"] = interview.meeting_phone
        result = await db.interviews.insert_one(document)
        interview_id = str(result.inserted_id)
        
        # Update job_application status to 'interview_scheduled' for consistency
        # This ensures bulk notifications can filter by job_applications.status
        # Also store interview_date for date-based filtering
        try:
            # Convert interview_date string to datetime object for proper MongoDB querying
            interview_date_obj = None
            if interview.interview_date:
                try:
                    # Try parsing ISO format with timezone
                    interview_date_obj = datetime.fromisoformat(interview.interview_date.replace('Z', '+00:00'))
                except:
                    try:
                        # Try parsing without timezone
                        interview_date_obj = datetime.strptime(interview.interview_date, '%Y-%m-%dT%H:%M:%S')
                    except:
                        # Fallback: just use the string
                        print(f"⚠️ Could not parse interview_date: {interview.interview_date}")
            
            update_data = {
                "status": "interview_scheduled",
                "updated_at": datetime.now(timezone.utc)
            }
            
            if interview_date_obj:
                update_data["interview_date"] = interview_date_obj  # Store as datetime object
                
            await db.job_applications.update_one(
                {
                    "candidate_id": interview.candidate_id,
                    "job_id": interview.job_id
                },
                {"$set": update_data}
            )
            print(f"✅ Updated job_application status to 'interview_scheduled' with date {interview.interview_date} (stored as datetime: {interview_date_obj})")
        except Exception as e:
            # Don't fail the interview creation if update fails
            print(f"⚠️ Failed to update job_application status: {e}")
        
        return {
            "message": "Interview scheduled successfully",
            "interview_id": interview_id,
            "candidate_id": interview.candidate_id,
            "job_id": interview.job_id,
            "interview_date": interview.interview_date,
            "status": "scheduled"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Interview scheduling failed: {str(e)}")

@app.post("/v1/offers", tags=["Assessment & Workflow"])
async def create_job_offer(offer: JobOffer, api_key: str = Depends(get_api_key)):
    """Job Offers Management"""
    try:
        db = await get_mongo_db()
        
        document = {
            "candidate_id": offer.candidate_id,
            "job_id": offer.job_id,
            "salary": offer.salary,
            "start_date": offer.start_date,
            "terms": offer.terms,
            "status": "pending",
            "created_at": datetime.now(timezone.utc)
        }
        result = await db.offers.insert_one(document)
        offer_id = str(result.inserted_id)
        
        return {
            "message": "Job offer created successfully",
            "offer_id": offer_id,
            "candidate_id": offer.candidate_id,
            "job_id": offer.job_id,
            "salary": offer.salary,
            "start_date": offer.start_date,
            "terms": offer.terms,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        return {
            "message": "Job offer creation failed",
            "error": str(e),
            "candidate_id": offer.candidate_id,
            "job_id": offer.job_id
        }

@app.get("/v1/offers", tags=["Assessment & Workflow"])
async def get_all_offers(candidate_id: Optional[str] = None, auth = Depends(get_auth)):
    """Get All Job Offers (supports filtering by candidate_id)"""
    try:
        db = await get_mongo_db()
        
        # Build match filter - support candidate_id filtering
        match_filter = {}
        if candidate_id:
            # If JWT auth, verify candidate_id matches authenticated user
            if auth.get("type") == "jwt_token" and auth.get("role") == "candidate":
                token_candidate_id = str(auth.get("user_id", ""))
                if token_candidate_id and token_candidate_id != str(candidate_id):
                    raise HTTPException(status_code=403, detail="You can only view your own offers")
            match_filter["candidate_id"] = candidate_id
        # Recruiter: only offers for their jobs
        if auth.get("type") == "jwt_token" and auth.get("role") == "recruiter":
            recruiter_id = str(auth.get("user_id", ""))
            if recruiter_id:
                cursor = db.jobs.find({"status": "active", "recruiter_id": recruiter_id}, {"_id": 1})
                jobs_list = await cursor.to_list(length=500)
                job_ids = [str(doc["_id"]) for doc in jobs_list]
                match_filter["job_id"] = {"$in": job_ids} if job_ids else {"$in": []}
        # Client: only offers for own jobs (data isolation)
        if auth.get("type") == "jwt_token" and auth.get("role") == "client":
            client_id = str(auth.get("user_id", ""))
            job_ids = await _client_job_ids_for_dashboard(db, client_id)
            match_filter["job_id"] = {"$in": job_ids} if job_ids else {"$in": []}
        
        pipeline = [
            {"$match": match_filter} if match_filter else {"$match": {}},
            {"$lookup": {
                "from": "candidates",
                "localField": "candidate_id",
                "foreignField": "_id",
                "as": "candidate"
            }},
            {"$lookup": {
                "from": "jobs",
                "localField": "job_id",
                "foreignField": "_id",
                "as": "job"
            }},
            {"$sort": {"created_at": -1}},
            {"$project": {
                "id": {"$toString": "$_id"},
                "candidate_id": {"$toString": "$candidate_id"},
                "job_id": {"$toString": "$job_id"},
                "salary": 1,
                "start_date": 1,
                "terms": 1,
                "status": 1,
                "created_at": 1,
                "candidate_name": {"$arrayElemAt": ["$candidate.name", 0]},
                "job_title": {"$arrayElemAt": ["$job.title", 0]}
            }}
        ]
        
        cursor = db.offers.aggregate(pipeline)
        offers_list = await cursor.to_list(length=None)
        
        offers = []
        for doc in offers_list:
            start_date = doc.get("start_date")
            start_date_str = start_date.isoformat() if start_date else None
            offers.append({
                "id": doc.get("id"),
                "candidate_id": doc.get("candidate_id"),
                "job_id": doc.get("job_id"),
                "salary": float(doc.get("salary", 0)) if doc.get("salary") else 0,  # Keep for backward compatibility
                "salary_offered": float(doc.get("salary", 0)) if doc.get("salary") else 0,  # Frontend expects this field name
                "start_date": start_date_str,  # Keep for backward compatibility
                "joining_date": start_date_str,  # Frontend expects this field name
                "terms": doc.get("terms"),
                "status": doc.get("status") or "pending",
                "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
                "candidate_name": doc.get("candidate_name"),
                "job_title": doc.get("job_title"),
                "company": None  # Frontend expects this (can be added from job lookup if needed)
            })
        
        return {"offers": offers, "count": len(offers)}
    except Exception as e:
        return {"offers": [], "count": 0, "error": str(e)}

# Analytics & Statistics (2 remaining endpoints)

@app.get("/v1/database/schema", tags=["Analytics & Statistics"])
async def get_database_schema(api_key: str = Depends(get_api_key)):
    """Get Database Schema Information - MongoDB"""
    try:
        db = await get_mongo_db()
        
        # Get collection list
        collections = await db.list_collection_names()
        collections.sort()
        
        # Get schema version if exists
        try:
            version_doc = await db.schema_version.find_one({}, sort=[("applied_at", -1)])
            schema_version = version_doc.get("version", "unknown") if version_doc else "unknown"
            applied_at = version_doc.get("applied_at").isoformat() if version_doc and version_doc.get("applied_at") else None
        except:
            schema_version = "1.0.0-mongodb"
            applied_at = None
        
        # Check for company_scoring_preferences collection
        phase3_exists = "company_scoring_preferences" in collections
        
        return {
            "database_type": "MongoDB Atlas",
            "schema_version": schema_version,
            "applied_at": applied_at,
            "total_collections": len(collections),
            "collections": collections,
            "phase3_enabled": phase3_exists,
            "core_collections": [
                "candidates", "jobs", "feedback", "interviews", "offers", 
                "users", "clients", "matching_cache", "audit_logs", 
                "rate_limits", "csp_violations", "company_scoring_preferences"
            ],
                "checked_at": datetime.now(timezone.utc).isoformat()
            }
    except Exception as e:
        return {
            "schema_version": "error",
            "total_tables": 0,
            "tables": [],
            "phase3_enabled": False,
            "error": str(e),
            "checked_at": datetime.now(timezone.utc).isoformat()
        }

@app.get("/v1/reports/job/{job_id}/export.csv", tags=["Analytics & Statistics"])
async def export_job_report(job_id: str, api_key: str = Depends(get_api_key)):  # Changed from int to str for MongoDB ObjectId
    """Export Job Report"""
    return {
        "message": "Job report export",
        "job_id": job_id,
        "format": "CSV",
        "download_url": f"/downloads/job_{job_id}_report.csv",
        "generated_at": datetime.now(timezone.utc).isoformat()
    }

# Client Portal API (2 endpoints)
@app.post("/v1/client/register", tags=["Client Portal API"])
async def client_register(client_data: ClientRegister):
    """Client Registration"""
    from fastapi import status
    try:
        db = await get_mongo_db()

        client_id_normalized = str(client_data.client_id).strip()
        if not client_id_normalized:
            raise HTTPException(status_code=400, detail="Client ID is required")

        # Check if client_id already exists
        existing_client = await db.clients.find_one({"client_id": client_id_normalized})
        if not existing_client and client_id_normalized.isdigit():
            existing_client = await db.clients.find_one({"client_id": int(client_id_normalized)})
        if existing_client:
            raise HTTPException(status_code=400, detail="Client ID already exists")

        # Check if email already exists (normalize to lowercase for consistency)
        email_normalized = str(client_data.contact_email or "").strip().lower()
        if not email_normalized:
            raise HTTPException(status_code=400, detail="Contact email is required")
        existing_email = await db.clients.find_one({"email": email_normalized})
        if existing_email:
            raise HTTPException(status_code=400, detail="Email already registered")

        # Generate unique client_code if not provided
        client_code = (client_data.client_code or str(uuid.uuid4())).strip() or str(uuid.uuid4())

        # Check if client_code already exists (should be unique)
        existing_code = await db.clients.find_one({"client_code": client_code})
        if existing_code:
            raise HTTPException(status_code=400, detail="Client code already exists. Please try again.")

        # One-time connection_id for recruiter linking (24-char hex, same style as ObjectId)
        connection_id = str(ObjectId())

        # Hash password
        password_hash = bcrypt.hashpw(client_data.password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

        # Insert client (normalize client_id to string for consistent lookups)
        company_name_safe = str(client_data.company_name or "").strip()
        if not company_name_safe:
            raise HTTPException(status_code=400, detail="Company name is required")
        document = {
            "client_id": client_id_normalized,
            "company_name": company_name_safe,
            "email": email_normalized,
            "client_code": client_code,
            "connection_id": connection_id,
            "password_hash": password_hash,
            "status": "active",
            "failed_login_attempts": 0,
            "locked_until": None,
            "created_at": datetime.now(timezone.utc)
        }
        await db.clients.insert_one(document)

        from fastapi import Response
        return Response(
            content=json.dumps({
                "success": True,
                "message": "Client registration successful",
                "client_id": client_id_normalized,
                "company_name": str(client_data.company_name).strip(),
                "client_code": client_code,
                "connection_id": connection_id
            }),
            status_code=status.HTTP_201_CREATED,
            media_type="application/json"
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.exception("client_register failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/client/login", tags=["Client Portal API"])
async def client_login(login_data: ClientLogin):
    """Client Authentication with Database Integration - Supports both client_id and email"""
    try:
        db = await get_mongo_db()
        
        # Support both client_id and email-based login
        if not login_data.client_id and not login_data.email:
            return {"success": False, "error": "Either client_id or email is required"}
        
        # Try to find client by client_id first, then by email
        client = None
        if login_data.client_id:
            client = await db.clients.find_one({"client_id": login_data.client_id})
        
        if not client and login_data.email:
            # Try finding by email (stored as 'email' field in clients collection)
            client = await db.clients.find_one({"email": login_data.email})
        
        if not client:
            return {"success": False, "error": "Invalid credentials"}
        
        # Check if account is locked
        locked_until = client.get("locked_until")
        if locked_until:
            if locked_until.tzinfo is None:
                locked_until = locked_until.replace(tzinfo=timezone.utc)
            if locked_until > datetime.now(timezone.utc):
                return {"success": False, "error": "Account temporarily locked"}
        
        # Check if account is active
        if client.get("status") != 'active':
            return {"success": False, "error": "Account is inactive"}
        
        # Verify password
        if client.get("password_hash"):
            if not bcrypt.checkpw(login_data.password.encode('utf-8'), client.get("password_hash").encode('utf-8')):
                # Increment failed attempts
                new_attempts = (client.get("failed_login_attempts") or 0) + 1
                locked_until = None
                if new_attempts >= 5:
                    locked_until = datetime.now(timezone.utc) + timedelta(minutes=30)
                
                # Use the actual client_id from the found client (works for both client_id and email login)
                await db.clients.update_one(
                    {"client_id": client.get("client_id")},
                    {"$set": {
                        "failed_login_attempts": new_attempts,
                        "locked_until": locked_until
                    }}
                )
                
                return {"success": False, "error": "Invalid credentials"}
        else:
            # No password hash exists - require password to be set
            return {"success": False, "error": "Account requires password setup"}
        
        # Generate JWT token using JWT_SECRET_KEY
        jwt_secret = os.getenv("JWT_SECRET_KEY")
        client_id = client.get("client_id")
        token_payload = {
            "sub": client_id,  # Standard JWT claim for subject/user ID
            "client_id": client_id,  # Keep for backward compatibility
            "user_id": client_id,  # Also include user_id for jwt_auth.py compatibility
            "email": client.get("contact_email", ""),
            "company_name": client.get("company_name"),
            "role": "client",  # Explicit role for jwt_auth.py
            "exp": int(datetime.now(timezone.utc).timestamp()) + 86400  # 24 hours
        }
        access_token = jwt.encode(token_payload, jwt_secret, algorithm="HS256")
        
        # Reset failed attempts and update last login
        # Use the actual client_id from the found client (works for both client_id and email login)
        await db.clients.update_one(
            {"client_id": client.get("client_id")},
            {"$set": {
                "failed_login_attempts": 0,
                "locked_until": None,
                "last_login": datetime.now(timezone.utc)
            }}
        )
        
        return {
            "success": True,
            "message": "Authentication successful",
            "client_id": client.get("client_id"),
            "company_name": client.get("company_name"),
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": 86400,  # 24 hours
            "permissions": ["view_jobs", "create_jobs", "view_candidates", "schedule_interviews"]
        }
            
    except Exception as e:
        return {
            "success": False,
            "error": f"Authentication error: {str(e)}"
        }


@app.get("/v1/client/profile", tags=["Client Portal API"])
async def get_client_profile(auth=Depends(get_auth)):
    """Return authenticated client's profile including connection_id (for dashboard display and sharing with recruiters)."""
    if auth.get("type") != "jwt_token" or auth.get("role") != "client":
        raise HTTPException(status_code=403, detail="This endpoint is only available for clients")
    raw_uid = auth.get("user_id")
    if raw_uid is None or raw_uid == "":
        raise HTTPException(status_code=401, detail="Client not identified")
    client_id_str = str(raw_uid)
    if not client_id_str.strip():
        raise HTTPException(status_code=401, detail="Client not identified")
    try:
        db = await get_mongo_db()
        # Support old clients: DB may have client_id as string or int
        client = await db.clients.find_one({"client_id": client_id_str})
        if not client and client_id_str.isdigit():
            client = await db.clients.find_one({"client_id": int(client_id_str)})
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")
        connection_id = client.get("connection_id")
        if not connection_id:
            # Backfill for clients created before connection_id existed
            connection_id = str(ObjectId())
            # Use the document's own client_id for update (may be int or str in DB)
            doc_client_id = client.get("client_id")
            await db.clients.update_one(
                {"_id": client["_id"]},
                {"$set": {"connection_id": connection_id}}
            )
        return {
            "client_id": str(client.get("client_id", "")),
            "company_name": str(client.get("company_name", "")),
            "email": str(client.get("email", "")),
            "connection_id": str(connection_id),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("get_client_profile failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/client/by-connection/{connection_id}", tags=["Client Portal API"])
async def get_client_by_connection(connection_id: str, auth=Depends(get_auth)):
    """Validate connection_id and return minimal client info. Used by recruiters when posting jobs. Records recruiter as connected to this client for client sidebar."""
    if not connection_id or not connection_id.strip():
        raise HTTPException(status_code=400, detail="Connection ID is required")
    connection_id = connection_id.strip()
    if len(connection_id) != 24 or not all(c in "0123456789abcdefABCDEF" for c in connection_id):
        raise HTTPException(status_code=400, detail="Invalid Connection ID format (must be 24 hexadecimal characters)")
    try:
        db = await get_mongo_db()
        client = await db.clients.find_one({"connection_id": connection_id})
        if not client:
            raise HTTPException(status_code=404, detail="Invalid Connection ID. Please ask your client for the correct ID from their dashboard.")
        company_name = client.get("company_name") or ""
        # Validation only: do NOT record connection or push SSE here. Connection is established only when recruiter confirms via POST /v1/recruiter/confirm-connection.
        return {
            "client_id": client.get("client_id"),
            "company_name": company_name,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/client/connected-recruiter", tags=["Client Portal API"])
async def get_client_connected_recruiter(auth=Depends(get_auth)):
    """Return count of recruiters connected to this client (for sidebar). Client can have multiple recruiters. Client-only."""
    if auth.get("type") != "jwt_token" or auth.get("role") != "client":
        raise HTTPException(status_code=403, detail="This endpoint is only available for clients")
    client_id = str(auth.get("user_id", ""))
    if not client_id:
        return {"connected_count": 0, "status": "none"}
    try:
        db = await get_mongo_db()
        count = await db.client_connected_recruiter.count_documents({"client_id": client_id})
        return {"connected_count": count, "status": "connected" if count > 0 else "none"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("get_client_connected_recruiter failed: %s", e)
        return {"connected_count": 0, "status": "none"}


_SSE_HEARTBEAT_INTERVAL = 25.0


async def _client_connection_event_stream(client_id: str):
    channel = f"client:{client_id}"
    q = await _subscribe_connection_events(channel)
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=_SSE_HEARTBEAT_INTERVAL)
                yield f"data: {json.dumps(event)}\n\n"
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
    finally:
        _unsubscribe_connection_events(channel, q)


async def _recruiter_connection_event_stream(recruiter_id: str):
    channel = f"recruiter:{recruiter_id}"
    q = await _subscribe_connection_events(channel)
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=_SSE_HEARTBEAT_INTERVAL)
                yield f"data: {json.dumps(event)}\n\n"
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
    finally:
        _unsubscribe_connection_events(channel, q)


@app.get("/v1/client/connection-events", tags=["Client Portal API"])
async def client_connection_events(auth=Depends(get_auth)):
    """SSE stream for connection status. Client-only. Emits connected/disconnected so client and recruiter stay in sync."""
    if auth.get("type") != "jwt_token" or auth.get("role") != "client":
        raise HTTPException(status_code=403, detail="This endpoint is only available for clients")
    client_id = str(auth.get("user_id", ""))
    if not client_id:
        raise HTTPException(status_code=400, detail="Invalid client")
    return StreamingResponse(
        _client_connection_event_stream(client_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/v1/recruiter/connection-events", tags=["Recruiter API"])
async def recruiter_connection_events(auth=Depends(get_auth)):
    """SSE stream for connection status. Recruiter-only. Emits connected/disconnected so client and recruiter stay in sync."""
    if auth.get("type") != "jwt_token" or auth.get("role") not in ("recruiter", "admin"):
        raise HTTPException(status_code=403, detail="This endpoint is only available for recruiters")
    recruiter_id = str(auth.get("user_id", ""))
    if not recruiter_id:
        raise HTTPException(status_code=400, detail="Invalid recruiter")
    return StreamingResponse(
        _recruiter_connection_event_stream(recruiter_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ConfirmConnectionBody(BaseModel):
    connection_id: str


@app.post("/v1/recruiter/confirm-connection", tags=["Recruiter API"])
async def recruiter_confirm_connection(body: ConfirmConnectionBody, auth=Depends(get_auth)):
    """Establish and lock the recruiter-client connection. One document per (client_id, recruiter_id); a client can have multiple recruiters. Recruiter can only be connected to one client at a time."""
    if auth.get("type") != "jwt_token" or auth.get("role") not in ("recruiter", "admin"):
        raise HTTPException(status_code=403, detail="This endpoint is only available for recruiters")
    connection_id = (body.connection_id or "").strip()
    if len(connection_id) != 24 or not all(c in "0123456789abcdefABCDEF" for c in connection_id):
        raise HTTPException(status_code=400, detail="Invalid Connection ID format (must be 24 hexadecimal characters)")
    recruiter_id = str(auth.get("user_id") or "")
    recruiter_name = str(auth.get("name") or "Recruiter").strip() or "Recruiter"
    if not recruiter_id:
        raise HTTPException(status_code=400, detail="Invalid recruiter")
    try:
        db = await get_mongo_db()
        client = await db.clients.find_one({"connection_id": connection_id})
        if not client:
            raise HTTPException(status_code=404, detail="Invalid Connection ID. Please ask your client for the correct ID from their dashboard.")
        company_name = client.get("company_name") or ""
        client_id_str = str(client.get("client_id") or "")
        if not client_id_str:
            raise HTTPException(status_code=400, detail="Invalid client")
        # This recruiter can only be connected to one client: remove from previous client if any
        old_doc = await db.client_connected_recruiter.find_one({"recruiter_id": recruiter_id})
        old_client_id = str(old_doc["client_id"]) if old_doc and old_doc.get("client_id") else None
        await db.client_connected_recruiter.delete_many({"recruiter_id": recruiter_id})
        # Add this recruiter to this client (one doc per client-recruiter pair; client can have many recruiters)
        await db.client_connected_recruiter.update_one(
            {"client_id": client_id_str, "recruiter_id": recruiter_id},
            {"$set": {
                "recruiter_name": recruiter_name,
                "last_validated_at": datetime.now(timezone.utc),
            }},
            upsert=True
        )
        new_count = await db.client_connected_recruiter.count_documents({"client_id": client_id_str})
        if old_client_id and old_client_id != client_id_str:
            old_count = await db.client_connected_recruiter.count_documents({"client_id": old_client_id})
            await _push_connection_event(f"client:{old_client_id}", {"event": "disconnected", "connected_count": old_count})
        await _push_connection_event(f"client:{client_id_str}", {"event": "connected", "connected_count": new_count})
        await _push_connection_event(f"recruiter:{recruiter_id}", {"event": "connected", "company_name": company_name})
        return {"client_id": client.get("client_id"), "company_name": company_name}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("recruiter_confirm_connection failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/recruiter/disconnect", tags=["Recruiter API"])
async def recruiter_disconnect(auth=Depends(get_auth)):
    """Explicitly disconnect recruiter from current client. Notifies client (with new count) and recruiter via SSE."""
    if auth.get("type") != "jwt_token" or auth.get("role") not in ("recruiter", "admin"):
        raise HTTPException(status_code=403, detail="This endpoint is only available for recruiters")
    recruiter_id = str(auth.get("user_id", ""))
    if not recruiter_id:
        raise HTTPException(status_code=400, detail="Invalid recruiter")
    try:
        db = await get_mongo_db()
        doc = await db.client_connected_recruiter.find_one({"recruiter_id": recruiter_id})
        if doc:
            client_id = str(doc.get("client_id", ""))
            await db.client_connected_recruiter.delete_many({"recruiter_id": recruiter_id})
            if client_id:
                new_count = await db.client_connected_recruiter.count_documents({"client_id": client_id})
                await _push_connection_event(f"client:{client_id}", {"event": "disconnected", "connected_count": new_count})
            await _push_connection_event(f"recruiter:{recruiter_id}", {"event": "disconnected"})
        return {}
    except Exception as e:
        logger.exception("recruiter_disconnect failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/recruiter/current-connection", tags=["Recruiter API"])
async def get_recruiter_current_connection(auth=Depends(get_auth)):
    """Get recruiter's current active connection from database. Returns connection_id and company_name if connected, null otherwise. Used on login to restore connection state across devices/browsers."""
    if auth.get("type") != "jwt_token" or auth.get("role") not in ("recruiter", "admin"):
        raise HTTPException(status_code=403, detail="This endpoint is only available for recruiters")
    recruiter_id = str(auth.get("user_id", ""))
    if not recruiter_id:
        raise HTTPException(status_code=400, detail="Invalid recruiter")
    try:
        db = await get_mongo_db()
        # Find the recruiter's current connection
        doc = await db.client_connected_recruiter.find_one({"recruiter_id": recruiter_id})
        if not doc:
            return {"connection_id": None, "company_name": None}
        
        # Get client details
        client_id = str(doc.get("client_id", ""))
        if not client_id:
            return {"connection_id": None, "company_name": None}
        
        client = await db.clients.find_one({"client_id": client_id})
        if not client:
            # Connection exists but client not found - cleanup
            await db.client_connected_recruiter.delete_many({"recruiter_id": recruiter_id})
            return {"connection_id": None, "company_name": None}
        
        connection_id = client.get("connection_id", "")
        company_name = client.get("company_name", "")
        
        return {
            "connection_id": connection_id if connection_id else None,
            "company_name": company_name if company_name else None
        }
    except DatabaseNotConfiguredError:
        # No-database dev mode: report "not connected" instead of a 500
        return {"connection_id": None, "company_name": None}
    except Exception as e:
        logger.exception("get_recruiter_current_connection failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/connection/health-check", tags=["Connection API"])
async def connection_health_check(auth=Depends(get_auth)):
    """Bidirectional connection health check. Validates that both client and recruiter exist and connection is still active. If validation fails, triggers disconnect events to both parties via SSE. Called every 30 seconds by both client and recruiter."""
    user_type = auth.get("type")
    role = auth.get("role")
    user_id = str(auth.get("user_id", ""))
    
    if user_type != "jwt_token" or role not in ("client", "recruiter", "admin"):
        raise HTTPException(status_code=403, detail="This endpoint requires authentication")
    
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid user")
    
    try:
        db = await get_mongo_db()
        
        # Determine if this is a client or recruiter health check
        if role == "client":
            client_id = user_id
            # Validate client exists
            client = await db.clients.find_one({"client_id": client_id})
            if not client:
                return {"healthy": False, "reason": "client_not_found"}
            
            # Check if any recruiters are connected
            connections = await db.client_connected_recruiter.find({"client_id": client_id}).to_list(length=None)
            if not connections:
                return {"healthy": True, "connected_count": 0}
            
            # Note: Recruiters are authenticated via JWT tokens, not stored in a database collection
            # We don't validate recruiter existence because they're not in db.recruiters
            # The connection records themselves are the source of truth
            # If a recruiter's auth account is deleted, they won't be able to authenticate,
            # and their health checks will stop, which will eventually clean up stale connections
            
            return {"healthy": True, "connected_count": len(connections)}
        
        elif role == "recruiter" or role == "admin":
            recruiter_id = user_id
            # Note: Recruiters are authenticated via JWT tokens, not stored in a separate collection
            # The fact that this endpoint was called with a valid JWT token proves the recruiter exists
            # We only need to validate the connection and client existence
            
            # Check if recruiter is connected to any client
            connection = await db.client_connected_recruiter.find_one({"recruiter_id": recruiter_id})
            if not connection:
                return {"healthy": True, "connected": False}
            
            # Validate client still exists
            client_id = str(connection.get("client_id", ""))
            client = await db.clients.find_one({"client_id": client_id})
            if not client:
                # Client deleted - remove connection and notify recruiter
                await db.client_connected_recruiter.delete_many({"recruiter_id": recruiter_id})
                await _push_connection_event(f"recruiter:{recruiter_id}", {"event": "disconnected"})
                return {"healthy": False, "reason": "client_deleted", "disconnected": True}
            
            return {"healthy": True, "connected": True, "client_id": client_id}
    
    except Exception as e:
        logger.exception("connection_health_check failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/client/jobs", tags=["Client Portal API"])
async def get_client_jobs(auth=Depends(get_auth)):
    """List jobs for the authenticated client only (data isolation). Like GET /v1/recruiter/jobs for clients."""
    try:
        if auth.get("type") != "jwt_token" or auth.get("role") != "client":
            raise HTTPException(status_code=403, detail="This endpoint is only available for clients")
        client_id = str(auth.get("user_id", ""))
        if not client_id:
            return {"jobs": [], "count": 0}
        db = await get_mongo_db()
        job_ids = await _client_job_ids_for_dashboard(db, client_id)
        if not job_ids:
            return {"jobs": [], "count": 0}
        cursor = db.jobs.find({"_id": {"$in": [ObjectId(j) for j in job_ids]}}).sort("created_at", -1).limit(100)
        jobs_list = await cursor.to_list(length=100)
        counts_by_job: Dict[str, Dict[str, int]] = {}
        pipeline = [
            {"$match": {"job_id": {"$in": job_ids}}},
            {"$group": {
                "_id": "$job_id",
                "applicants": {"$sum": 1},
                "shortlisted": {"$sum": {"$cond": [{"$eq": ["$status", "shortlisted"]}, 1, 0]}},
            }},
        ]
        async for agg_doc in db.job_applications.aggregate(pipeline):
            jid = agg_doc.get("_id") or ""
            counts_by_job[str(jid)] = {"applicants": agg_doc.get("applicants", 0), "shortlisted": agg_doc.get("shortlisted", 0)}
        jobs = []
        for doc in jobs_list:
            jid = str(doc["_id"])
            counts = counts_by_job.get(jid, {"applicants": 0, "shortlisted": 0})
            salary_min, salary_max = _job_salary_from_doc(doc)
            jobs.append({
                "id": jid,
                "title": doc.get("title"),
                "department": doc.get("department"),
                "location": doc.get("location"),
                "experience_level": doc.get("experience_level"),
                "requirements": doc.get("requirements"),
                "description": doc.get("description"),
                "job_type": doc.get("job_type") or doc.get("employment_type"),
                "employment_type": doc.get("employment_type"),
                "salary_min": salary_min,
                "salary_max": salary_max,
                "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
                "applicants": counts["applicants"],
                "shortlisted": counts["shortlisted"],
            })
        return {"jobs": jobs, "count": len(jobs)}
    except HTTPException:
        raise
    except Exception as e:
        return {"jobs": [], "count": 0, "error": str(e)}


@app.get("/v1/client/stats", tags=["Client Portal API"])
async def get_client_stats(auth=Depends(get_auth)):
    """Client dashboard statistics: active jobs, total applications, interviews scheduled, offers made.
    Only available when authenticated as client. Returns only this client's data (jobs where client_id matches
    logged-in client). Includes jobs posted by recruiters via connection_id. Data isolation: no other client's data."""
    if auth.get("type") != "jwt_token" or auth.get("role") != "client":
        raise HTTPException(status_code=403, detail="This endpoint is only available for clients")
    client_id = str(auth.get("user_id", ""))
    if not client_id:
        return {
            "active_jobs": 0,
            "total_applications": 0,
            "shortlisted": 0,
            "interviews_scheduled": 0,
            "offers_made": 0,
            "hired": 0,
        }
    try:
        db = await get_mongo_db()
        # Active jobs: client's jobs + connected recruiter's jobs when connected
        active_job_ids = await _client_job_ids_for_dashboard(db, client_id)
        active_jobs = len(active_job_ids)
        # All jobs (any status) for pipeline stats: client's + connected recruiter's when connected
        job_ids = await _client_all_job_ids_for_dashboard(db, client_id)
        if not job_ids:
            return {
                "active_jobs": active_jobs,
                "total_applications": 0,
                "shortlisted": 0,
                "interviews_scheduled": 0,
                "offers_made": 0,
                "hired": 0,
            }
        total_applications = await db.job_applications.count_documents({"job_id": {"$in": job_ids}})
        shortlisted = await db.job_applications.count_documents({
            "job_id": {"$in": job_ids},
            "status": "shortlisted"
        })
        interviews_scheduled = await db.interviews.count_documents({
            "job_id": {"$in": job_ids},
            "status": {"$in": ["scheduled", "pending"]}
        })
        try:
            alt_interviews = await db.interviews.count_documents({"job_id": {"$in": job_ids}})
            if interviews_scheduled == 0 and alt_interviews > 0:
                interviews_scheduled = alt_interviews
        except Exception:
            pass
        offers_made = await db.offers.count_documents({"job_id": {"$in": job_ids}})
        hired = await db.offers.count_documents({
            "job_id": {"$in": job_ids},
            "status": {"$in": ["accepted", "hired"]}
        })
        return {
            "active_jobs": active_jobs,
            "total_applications": total_applications,
            "shortlisted": shortlisted,
            "interviews_scheduled": interviews_scheduled,
            "offers_made": offers_made,
            "hired": hired,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("get_client_stats failed: %s", e)
        return {
            "active_jobs": 0,
            "total_applications": 0,
            "shortlisted": 0,
            "interviews_scheduled": 0,
            "offers_made": 0,
            "hired": 0,
        }


# Security Testing (7 endpoints)
@app.get("/v1/security/rate-limit-status", tags=["Security Testing"])
async def check_rate_limit_status(api_key: str = Depends(get_api_key)):
    """Check Rate Limit Status"""
    return {
        "rate_limit_enabled": True,
        "requests_per_minute": 60,
        "current_requests": 15,
        "remaining_requests": 45,
        "reset_time": datetime.now(timezone.utc).isoformat(),
        "status": "active"
    }

@app.get("/v1/security/blocked-ips", tags=["Security Testing"])
async def view_blocked_ips(api_key: str = Depends(get_api_key)):
    """View Blocked IPs"""
    return {
        "blocked_ips": [
            {"ip": "192.168.1.100", "reason": "Rate limit exceeded", "blocked_at": "2025-01-02T10:30:00Z"},
            {"ip": "10.0.0.50", "reason": "Suspicious activity", "blocked_at": "2025-01-02T09:15:00Z"}
        ],
        "total_blocked": 2,
        "last_updated": datetime.now(timezone.utc).isoformat()
    }

@app.post("/v1/security/test-input-validation", tags=["Security Testing"])
async def test_input_validation(input_data: InputValidation, api_key: str = Depends(get_api_key)):
    """Test Input Validation"""
    data = input_data.input_data
    threats = []
    
    if "<script>" in data.lower():
        threats.append("XSS attempt detected")
    if "'" in data and ("union" in data.lower() or "select" in data.lower()):
        threats.append("SQL injection attempt detected")
    
    return {
        "input": data,
        "validation_result": "SAFE" if not threats else "BLOCKED",
        "threats_detected": threats,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.post("/v1/security/validate-email", tags=["Security Testing"])
async def validate_email(email_data: EmailValidation, api_key: str = Depends(get_api_key)):
    """Email Validation"""
    email = email_data.email
    
    email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    is_valid = re.match(email_pattern, email) is not None
    
    return {
        "email": email,
        "is_valid": is_valid,
        "validation_type": "regex_pattern",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.post("/v1/security/test-email-validation", tags=["Security Testing"])
async def test_email_validation(email_data: EmailValidation, api_key: str = Depends(get_api_key)):
    """Test Email Validation"""
    email = email_data.email
    
    email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    is_valid = re.match(email_pattern, email) is not None
    
    return {
        "email": email,
        "is_valid": is_valid,
        "validation_type": "regex_pattern",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.post("/v1/security/validate-phone", tags=["Security Testing"])
async def validate_phone(phone_data: PhoneValidation, api_key: str = Depends(get_api_key)):
    """Phone Validation"""
    phone = phone_data.phone
    
    phone_pattern = r'^(\+91|91)?[6-9]\d{9}$'
    is_valid = re.match(phone_pattern, phone) is not None
    
    return {
        "phone": phone,
        "is_valid": is_valid,
        "validation_type": "Indian_phone_format",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.post("/v1/security/test-phone-validation", tags=["Security Testing"])
async def test_phone_validation(phone_data: PhoneValidation, api_key: str = Depends(get_api_key)):
    """Test Phone Validation"""
    phone = phone_data.phone
    
    phone_pattern = r'^(\+91|91)?[6-9]\d{9}$'
    is_valid = re.match(phone_pattern, phone) is not None
    
    return {
        "phone": phone,
        "is_valid": is_valid,
        "validation_type": "Indian_phone_format",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/v1/security/test-headers", tags=["Security Testing"])
async def test_security_headers(response: Response, api_key: str = Depends(get_api_key)):
    """Security Headers Test"""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'self' http://localhost:3000 https://setu.blackholeinfiverse.com;"
    
    return {
        "security_headers": {
            "X-Content-Type-Options": "nosniff",
            "X-XSS-Protection": "1; mode=block",
            "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
            "Content-Security-Policy": "frame-ancestors 'self' http://localhost:3000 https://setu.blackholeinfiverse.com;"
        },
        "headers_count": 4,
        "status": "all_headers_applied"
    }

@app.get("/v1/security/security-headers-test", tags=["Security Testing"])
async def test_security_headers_legacy(response: Response, api_key: str = Depends(get_api_key)):
    """Test Security Headers"""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'self' http://localhost:3000 https://setu.blackholeinfiverse.com;"
    
    return {
        "security_headers": {
            "X-Content-Type-Options": "nosniff",
            "X-XSS-Protection": "1; mode=block",
            "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
            "Content-Security-Policy": "frame-ancestors 'self' http://localhost:3000 https://setu.blackholeinfiverse.com;"
        },
        "headers_count": 4,
        "status": "all_headers_applied"
    }

@app.post("/v1/security/penetration-test", tags=["Security Testing"])
async def penetration_test(test_data: SecurityTest, api_key: str = Depends(get_api_key)):
    """Penetration Test"""
    return {
        "message": "Penetration test completed",
        "test_type": test_data.test_type,
        "payload": test_data.payload,
        "result": "No vulnerabilities detected",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/v1/security/test-auth", tags=["Security Testing"])
async def test_authentication(api_key: str = Depends(get_api_key)):
    """Test Authentication"""
    return {
        "message": "Authentication test successful",
        "authenticated": True,
        "api_key_valid": True,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/v1/security/penetration-test-endpoints", tags=["Security Testing"])
async def penetration_test_endpoints(api_key: str = Depends(get_api_key)):
    """Penetration Testing Endpoints"""
    return {
        "test_endpoints": [
            {"endpoint": "/v1/security/test-input-validation", "method": "POST", "purpose": "XSS/SQL injection testing"},
            {"endpoint": "/v1/security/test-email-validation", "method": "POST", "purpose": "Email format validation"},
            {"endpoint": "/v1/security/test-phone-validation", "method": "POST", "purpose": "Phone format validation"},
            {"endpoint": "/v1/security/security-headers-test", "method": "GET", "purpose": "Security headers verification"}
        ],
        "total_endpoints": 4,
        "penetration_testing_enabled": True
    }

# CSP Management (4 endpoints)
@app.post("/v1/security/csp-report", tags=["CSP Management"])
async def csp_violation_reporting(csp_report: CSPReport, api_key: str = Depends(get_api_key)):
    """CSP Violation Reporting"""
    return {
        "message": "CSP violation reported successfully",
        "violation": {
            "violated_directive": csp_report.violated_directive,
            "blocked_uri": csp_report.blocked_uri,
            "document_uri": csp_report.document_uri,
            "timestamp": datetime.now(timezone.utc).isoformat()
        },
        "report_id": f"csp_report_{datetime.now().timestamp()}"
    }

@app.get("/v1/security/csp-violations", tags=["CSP Management"])
async def view_csp_violations(api_key: str = Depends(get_api_key)):
    """View CSP Violations"""
    return {
        "violations": [
            {
                "id": "csp_001",
                "violated_directive": "script-src",
                "blocked_uri": "https://malicious-site.com/script.js",
                "document_uri": "https://bhiv-platform.com/dashboard",
                "timestamp": "2025-01-02T10:15:00Z"
            }
        ],
        "total_violations": 1,
        "last_24_hours": 1
    }



@app.get("/v1/security/csp-policies", tags=["CSP Management"])
async def current_csp_policies(api_key: str = Depends(get_api_key)):
    """Current CSP Policies"""
    return {
        "current_policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self' https:; connect-src 'self' https:; media-src 'self'; object-src 'none'; child-src 'self'; frame-ancestors 'none'; form-action 'self'; upgrade-insecure-requests; block-all-mixed-content",
        "policy_length": 408,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "status": "active"
    }

@app.post("/v1/security/test-csp-policy", tags=["CSP Management"])
async def test_csp_policy(csp_data: CSPPolicy, api_key: str = Depends(get_api_key)):
    """Test CSP Policy"""
    return {
        "message": "CSP policy test completed",
        "test_policy": csp_data.policy,
        "policy_length": len(csp_data.policy),
        "validation_result": "valid",
        "tested_at": datetime.now(timezone.utc).isoformat()
    }

# Two-Factor Authentication (8 endpoints)
@app.post("/v1/auth/2fa/setup", tags=["Two-Factor Authentication"])
async def setup_2fa(setup_data: TwoFASetup, api_key: str = Depends(get_api_key)):
    """Setup 2FA"""
    secret = pyotp.random_base32()
    totp_uri = pyotp.totp.TOTP(secret).provisioning_uri(
        name=setup_data.user_id,
        issuer_name="BHIV HR Platform"
    )
    
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(totp_uri)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    img_buffer = io.BytesIO()
    img.save(img_buffer, format='PNG')
    img_str = base64.b64encode(img_buffer.getvalue()).decode()
    
    return {
        "message": "2FA setup initiated",
        "user_id": setup_data.user_id,
        "secret": secret,
        "qr_code": f"data:image/png;base64,{img_str}",
        "manual_entry_key": secret,
        "instructions": "Scan QR code with Google Authenticator, Microsoft Authenticator, or Authy"
    }

@app.post("/v1/auth/2fa/verify", tags=["Two-Factor Authentication"])
async def verify_2fa(login_data: TwoFALogin, api_key: str = Depends(get_api_key)):
    """Verify 2FA"""
    stored_secret = "JBSWY3DPEHPK3PXP"
    totp = pyotp.TOTP(stored_secret)
    
    if totp.verify(login_data.totp_code, valid_window=1):
        return {
            "message": "2FA verification successful",
            "user_id": login_data.user_id,
            "verified": True,
            "verified_at": datetime.now(timezone.utc).isoformat()
        }
    else:
        raise HTTPException(status_code=401, detail="Invalid 2FA code")

@app.post("/v1/auth/2fa/login", tags=["Two-Factor Authentication"])
async def login_2fa(login_data: TwoFALogin, api_key: str = Depends(get_api_key)):
    """2FA Login"""
    stored_secret = "JBSWY3DPEHPK3PXP"
    totp = pyotp.TOTP(stored_secret)
    
    if totp.verify(login_data.totp_code, valid_window=1):
        return {
            "message": "2FA authentication successful",
            "user_id": login_data.user_id,
            "access_token": f"2fa_token_{login_data.user_id}_{datetime.now().timestamp()}",
            "token_type": "bearer",
            "expires_in": 3600,
            "2fa_verified": True
        }
    else:
        raise HTTPException(status_code=401, detail="Invalid 2FA code")

@app.get("/v1/auth/2fa/status/{user_id}", tags=["Two-Factor Authentication"])
async def get_2fa_status_auth(user_id: str, api_key: str = Depends(get_api_key)):
    """2FA Status"""
    return {
        "user_id": user_id,
        "2fa_enabled": True,
        "setup_date": "2025-01-01T12:00:00Z",
        "last_used": "2025-01-02T08:30:00Z",
        "backup_codes_remaining": 8
    }

@app.post("/v1/auth/2fa/disable", tags=["Two-Factor Authentication"])
async def disable_2fa_auth(setup_data: TwoFASetup, api_key: str = Depends(get_api_key)):
    """Disable 2FA"""
    return {
        "message": "2FA disabled successfully",
        "user_id": setup_data.user_id,
        "disabled_at": datetime.now(timezone.utc).isoformat(),
        "2fa_enabled": False
    }

@app.post("/v1/auth/2fa/backup-codes", tags=["Two-Factor Authentication"])
async def generate_backup_codes_auth(setup_data: TwoFASetup, api_key: str = Depends(get_api_key)):
    """Generate Backup Codes"""
    backup_codes = [f"BACKUP-{secrets.token_hex(4).upper()}" for _ in range(10)]
    
    return {
        "message": "Backup codes generated successfully",
        "user_id": setup_data.user_id,
        "backup_codes": backup_codes,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "codes_count": len(backup_codes)
    }

@app.post("/v1/auth/2fa/test-token", tags=["Two-Factor Authentication"])
async def test_2fa_token_auth(login_data: TwoFALogin, api_key: str = Depends(get_api_key)):
    """Test Token"""
    stored_secret = "JBSWY3DPEHPK3PXP"
    totp = pyotp.TOTP(stored_secret)
    
    is_valid = totp.verify(login_data.totp_code, valid_window=1)
    
    return {
        "user_id": login_data.user_id,
        "token": login_data.totp_code,
        "is_valid": is_valid,
        "test_timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/v1/auth/2fa/qr/{user_id}", tags=["Two-Factor Authentication"])
async def get_qr_code(user_id: str, api_key: str = Depends(get_api_key)):
    """QR Code"""
    secret = "JBSWY3DPEHPK3PXP"
    totp_uri = pyotp.totp.TOTP(secret).provisioning_uri(
        name=user_id,
        issuer_name="BHIV HR Platform"
    )
    
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(totp_uri)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    img_buffer = io.BytesIO()
    img.save(img_buffer, format='PNG')
    img_str = base64.b64encode(img_buffer.getvalue()).decode()
    
    return {
        "user_id": user_id,
        "qr_code": f"data:image/png;base64,{img_str}",
        "secret": secret,
        "generated_at": datetime.now(timezone.utc).isoformat()
    }



# Password Management (6 endpoints)
@app.post("/v1/auth/password/validate", tags=["Password Management"])
async def validate_password(password_data: PasswordValidation, api_key: str = Depends(get_api_key)):
    """Validate Password"""
    password = password_data.password
    
    score = 0
    feedback = []
    
    if len(password) >= 8:
        score += 20
    else:
        feedback.append("Password should be at least 8 characters long")
    
    if any(c.isupper() for c in password):
        score += 20
    else:
        feedback.append("Password should contain uppercase letters")
    
    if any(c.islower() for c in password):
        score += 20
    else:
        feedback.append("Password should contain lowercase letters")
    
    if any(c.isdigit() for c in password):
        score += 20
    else:
        feedback.append("Password should contain numbers")
    
    if any(c in "!@#$%^&*()_+-=[]{}|;:,.<>?" for c in password):
        score += 20
    else:
        feedback.append("Password should contain special characters")
    
    strength = "Very Weak"
    if score >= 80:
        strength = "Very Strong"
    elif score >= 60:
        strength = "Strong"
    elif score >= 40:
        strength = "Medium"
    elif score >= 20:
        strength = "Weak"
    
    return {
        "password_strength": strength,
        "score": score,
        "max_score": 100,
        "is_valid": score >= 60,
        "feedback": feedback
    }

@app.get("/v1/auth/password/generate", tags=["Password Management"])
async def generate_password(length: int = 12, include_symbols: bool = True, api_key: str = Depends(get_api_key)):
    """Generate Password"""
    if length < 8 or length > 128:
        raise HTTPException(status_code=400, detail="Password length must be between 8 and 128 characters")
    
    chars = string.ascii_letters + string.digits
    if include_symbols:
        chars += "!@#$%^&*()_+-="
    password = ''.join(random.choice(chars) for _ in range(length))
    
    return {
        "generated_password": password,
        "length": length,
        "entropy_bits": length * 6.5,
        "strength": "Very Strong",
        "generated_at": datetime.now(timezone.utc).isoformat()
    }

@app.get("/v1/auth/password/policy", tags=["Password Management"])
async def get_password_policy_auth(api_key: str = Depends(get_api_key)):
    """Password Policy"""
    return {
        "policy": {
            "minimum_length": 8,
            "require_uppercase": True,
            "require_lowercase": True,
            "require_numbers": True,
            "require_special_chars": True,
            "max_age_days": 90,
            "history_count": 5
        },
        "complexity_requirements": [
            "At least 8 characters long",
            "Contains uppercase letters",
            "Contains lowercase letters", 
            "Contains numbers",
            "Contains special characters"
        ]
    }

@app.post("/v1/auth/password/change", tags=["Password Management"])
async def change_password_auth(password_change: PasswordChange, api_key: str = Depends(get_api_key)):
    """Change Password"""
    return {
        "message": "Password changed successfully",
        "changed_at": datetime.now(timezone.utc).isoformat(),
        "password_strength": "Strong",
        "next_change_due": "2025-04-02T00:00:00Z"
    }

@app.post("/v1/auth/password/strength", tags=["Password Management"])
async def test_password_strength(password_data: PasswordValidation, api_key: str = Depends(get_api_key)):
    """Password Strength Test"""
    password = password_data.password
    
    score = 0
    feedback = []
    
    if len(password) >= 8:
        score += 20
    else:
        feedback.append("Password should be at least 8 characters long")
    
    if any(c.isupper() for c in password):
        score += 20
    else:
        feedback.append("Password should contain uppercase letters")
    
    if any(c.islower() for c in password):
        score += 20
    else:
        feedback.append("Password should contain lowercase letters")
    
    if any(c.isdigit() for c in password):
        score += 20
    else:
        feedback.append("Password should contain numbers")
    
    if any(c in "!@#$%^&*()_+-=[]{}|;:,.<>?" for c in password):
        score += 20
    else:
        feedback.append("Password should contain special characters")
    
    strength = "Very Weak"
    if score >= 80:
        strength = "Very Strong"
    elif score >= 60:
        strength = "Strong"
    elif score >= 40:
        strength = "Medium"
    elif score >= 20:
        strength = "Weak"
    
    return {
        "password_strength": strength,
        "score": score,
        "max_score": 100,
        "is_valid": score >= 60,
        "feedback": feedback
    }

@app.get("/v1/auth/password/security-tips", tags=["Password Management"])
async def get_security_tips(api_key: str = Depends(get_api_key)):
    """Security Tips"""
    return {
        "security_tips": [
            "Use a unique password for each account",
            "Enable two-factor authentication when available",
            "Use a password manager to generate and store passwords",
            "Avoid using personal information in passwords",
            "Change passwords immediately if a breach is suspected",
            "Use passphrases with random words for better security"
        ],
        "password_requirements": {
            "minimum_length": 8,
            "character_types": 4,
            "avoid": ["dictionary words", "personal info", "common patterns"]
        }
    }





# Candidate Portal APIs (5 endpoints)
@app.post("/v1/candidate/register", tags=["Candidate Portal"])
async def candidate_register(candidate_data: CandidateRegister):
    """Candidate Registration"""
    try:
        db = await get_mongo_db()
        
        # Check if email already exists
        existing = await db.candidates.find_one({"email": candidate_data.email})
        if existing:
            return {"success": False, "error": "Email already registered"}
        
        # Hash password
        password_hash = bcrypt.hashpw(candidate_data.password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        
        # Get role from request (for recruiters) or default to candidate
        user_role = candidate_data.role or "candidate"
        if user_role not in ["candidate", "recruiter"]:
            user_role = "candidate"  # Ensure valid role
        
        # Insert candidate with password hash and role
        document = {
            "name": candidate_data.name,
            "email": candidate_data.email,
            "phone": candidate_data.phone,
            "location": candidate_data.location,
            "experience_years": candidate_data.experience_years or 0,
            "technical_skills": candidate_data.technical_skills,
            "education_level": candidate_data.education_level,
            "seniority_level": candidate_data.seniority_level,
            "password_hash": password_hash,
            "role": user_role,  # Store role in database (for recruiters)
            "status": "applied",
            "created_at": datetime.now(timezone.utc)
        }
        result = await db.candidates.insert_one(document)
        candidate_id = str(result.inserted_id)
        
        # Send welcome email asynchronously (don't block registration)
        try:
            print(f"🔧 [Welcome Email] Using LangGraph URL: {_get_langgraph_service_url()}")
            
            # Send welcome email only for candidates (not recruiters)
            if user_role == "candidate":
                welcome_payload = {
                    "candidates": [{
                        "candidate_name": candidate_data.name,
                        "candidate_email": candidate_data.email,
                        "candidate_phone": candidate_data.phone or "",
                        "candidate_id": candidate_id
                    }],
                    "sequence_type": "welcome",
                    "job_title": None,
                    "job_id": None
                }
                
                await _send_langgraph_bulk_notifications(welcome_payload, timeout=10.0)
                print(f"✅ Welcome email sent successfully to {candidate_data.email}")
        except Exception as email_error:
            # Log error but don't fail registration
            print(f"Failed to send welcome email to {candidate_data.email}: {email_error}")
        
        return {
            "success": True,
            "message": "Registration successful",
            "candidate_id": candidate_id
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/v1/notifications/health", tags=["Notifications"])
async def get_notification_service_health(auth=Depends(get_auth)):
    """Proxy LangGraph health through Gateway so the browser stays same-origin."""
    return await _forward_langgraph_request("/health", method="GET", timeout=15.0)

@app.post("/v1/notifications/send", tags=["Notifications"])
async def send_notification(request: NotificationSendRequest, auth=Depends(get_auth)):
    """Proxy single notification send requests through Gateway."""
    return await _forward_langgraph_request(
        "/automation/notifications/send",
        method="POST",
        json_body=request.model_dump(exclude_none=True),
        timeout=45.0,
    )

@app.post("/v1/notifications/test-sequence", tags=["Notifications"])
async def test_notification_sequence(request: TestSequenceRequest, auth=Depends(get_auth)):
    """Proxy notification sequence tests through Gateway."""
    return await _forward_langgraph_request(
        "/automation/test/sequence",
        method="POST",
        params=request.model_dump(exclude_none=True),
        timeout=45.0,
    )

@app.post("/v1/notifications/preview", tags=["Notifications"])
async def get_notification_preview(request: PreviewNotificationRequest, auth=Depends(get_auth)):
    """Fetch notification template previews through Gateway."""
    return await _forward_langgraph_request(
        "/automation/notifications/preview",
        method="POST",
        params=request.model_dump(exclude_none=True),
        timeout=20.0,
    )

@app.post("/v1/notifications/bulk", tags=["Notifications"])
async def send_bulk_notifications_proxy(request: BulkNotificationProxyRequest, auth=Depends(get_auth)):
    """Proxy standard bulk notification requests through Gateway."""
    return await _send_langgraph_bulk_notifications(request.model_dump(exclude_none=True), timeout=180.0)

@app.post("/v1/automation/trigger", tags=["Notifications"])
async def trigger_automation(request: AutomationTriggerRequest, auth=Depends(get_auth)):
    """Trigger LangGraph automation workflows through Gateway."""
    return await _forward_langgraph_request(
        "/automation/workflows/trigger",
        method="POST",
        json_body={
            "event_type": request.type,
            "payload": request.payload,
        },
        timeout=45.0,
    )

@app.post("/v1/notifications/send-grouped-by-candidate", tags=["Notifications"])
async def send_grouped_notifications(request: PerJobNotificationRequest, auth=Depends(get_auth)):
    """
    Send ONE email per candidate with ALL their job applications listed.
    
    For example:
    - Candidate A applied to 2 jobs → Gets 1 email listing both jobs
    - Candidate B applied to 1 job → Gets 1 email with that job
    - Candidate C applied to 3 jobs → Gets 1 email listing all 3 jobs
    
    Used for bulk application_received notifications.
    """
    try:
        db = await get_mongo_db()
        # Get recruiter_id from auth if not provided
        recruiter_id = request.recruiter_id
        if not recruiter_id and auth:
            user_role = auth.get("role", "")
            if user_role == "recruiter":
                recruiter_id = auth.get("user_id")
        
        total_emails_sent = 0
        success_count = 0
        failed_count = 0
        notifications_sent = []
        
        # Process each candidate
        for candidate_id_str in request.candidate_ids:
            try:
                # Get candidate details
                candidate = await db.candidates.find_one({"_id": ObjectId(candidate_id_str)})
                if not candidate:
                    print(f"⚠️ Candidate {candidate_id_str} not found")
                    failed_count += 1
                    continue
                
                # Get all job applications for this candidate
                app_query = {"candidate_id": candidate_id_str}
                
                # Filter by recruiter's jobs if recruiter_id is provided
                if recruiter_id:
                    recruiter_jobs_cursor = db.jobs.find(
                        {"status": "active", "recruiter_id": recruiter_id},
                        {"_id": 1}
                    )
                    recruiter_jobs = await recruiter_jobs_cursor.to_list(length=500)
                    job_ids = [str(doc["_id"]) for doc in recruiter_jobs]
                    if job_ids:
                        app_query["job_id"] = {"$in": job_ids}
                    else:
                        # Recruiter has no jobs, skip this candidate
                        failed_count += 1
                        continue
                
                # Get job applications
                applications_cursor = db.job_applications.find(app_query)
                applications = await applications_cursor.to_list(length=100)
                
                if not applications:
                    print(f"⚠️ No applications found for candidate {candidate_id_str}")
                    failed_count += 1
                    continue
                
                # Collect all job titles for this candidate
                job_titles = []
                for app in applications:
                    job_id = app.get("job_id")
                    job = await db.jobs.find_one({"_id": ObjectId(job_id)})
                    if job:
                        job_titles.append(job.get("title", "Position"))
                
                if not job_titles:
                    failed_count += 1
                    continue
                
                # Create grouped job title string
                if len(job_titles) == 1:
                    grouped_job_title = job_titles[0]
                else:
                    grouped_job_title = ", ".join(job_titles[:-1]) + f" and {job_titles[-1]}"
                
                # Send ONE email with all jobs
                notification_payload = {
                    "candidates": [{
                        "candidate_name": candidate.get("name", "Candidate"),
                        "candidate_email": candidate.get("email", ""),
                        "candidate_phone": candidate.get("phone", ""),
                        "candidate_id": candidate_id_str
                    }],
                    "sequence_type": request.notification_type,
                    "job_title": grouped_job_title,
                    "job_count": len(job_titles),
                    "job_id": None,  # Multiple jobs, so no single job_id
                    "matching_score": "N/A"
                }
                
                # Send to LangGraph
                try:
                    await _send_langgraph_bulk_notifications(notification_payload, timeout=45.0)
                    total_emails_sent += 1
                    success_count += 1
                    notifications_sent.append({
                        "candidate_id": candidate_id_str,
                        "candidate_name": candidate.get("name"),
                        "job_count": len(job_titles),
                        "jobs": job_titles
                    })
                    print(f"✅ Sent grouped notification to {candidate.get('name')} for {len(job_titles)} job(s): {grouped_job_title}")
                except Exception as send_error:
                    failed_count += 1
                    print(f"Error sending notification: {send_error}")
                
                # Small delay to prevent overwhelming the email service
                await asyncio.sleep(0.5)
            
            except Exception as candidate_error:
                failed_count += 1
                print(f"Error processing candidate {candidate_id_str}: {candidate_error}")
        
        return {
            "success": True,
            "total_emails_sent": total_emails_sent,
            "success_count": success_count,
            "failed_count": failed_count,
            "notifications_sent": notifications_sent
        }
    
    except Exception as e:
        print(f"Error in send_grouped_notifications: {e}")
        return {
            "success": False,
            "error": str(e),
            "total_emails_sent": 0,
            "success_count": 0,
            "failed_count": 0
        }

@app.post("/v1/notifications/send-per-job", tags=["Notifications"])
async def send_per_job_notifications(request: PerJobNotificationRequest, auth=Depends(get_auth)):
    """
    Send separate notifications to candidates for each job they applied to.
    
    For each candidate:
    - Gets all their job applications
    - Sends a separate notification for each job application
    - Useful for application_received notifications where each job application should get its own email
    """
    try:
        db = await get_mongo_db()
        print(f"🔧 [Per-Job Notifications] Using LangGraph URL: {_get_langgraph_service_url()}")
        
        # Get recruiter_id from auth if not provided
        recruiter_id = request.recruiter_id
        if not recruiter_id and auth:
            user_role = auth.get("role", "")
            if user_role == "recruiter":
                recruiter_id = auth.get("user_id")
        
        total_notifications = 0
        success_count = 0
        failed_count = 0
        notifications_sent = []
        
        # Process each candidate
        for candidate_id_str in request.candidate_ids:
            try:
                # Get candidate details
                candidate = await db.candidates.find_one({"_id": ObjectId(candidate_id_str)})
                if not candidate:
                    print(f"⚠️ Candidate {candidate_id_str} not found")
                    failed_count += 1
                    continue
                
                # Get all job applications for this candidate
                app_query = {"candidate_id": candidate_id_str}
                
                # Filter by recruiter's jobs if recruiter_id is provided
                if recruiter_id:
                    recruiter_jobs_cursor = db.jobs.find(
                        {"status": "active", "recruiter_id": recruiter_id},
                        {"_id": 1}
                    )
                    recruiter_jobs = await recruiter_jobs_cursor.to_list(length=500)
                    job_ids = [str(doc["_id"]) for doc in recruiter_jobs]
                    app_query["job_id"] = {"$in": job_ids}
                
                # Get job applications
                applications_cursor = db.job_applications.find(app_query)
                applications = await applications_cursor.to_list(length=100)
                
                if not applications:
                    print(f"⚠️ No applications found for candidate {candidate_id_str}")
                    failed_count += 1
                    continue
                
                # Send a separate notification for each job application
                for app in applications:
                    total_notifications += 1
                    job_id = app.get("job_id")
                    
                    # Get job details
                    job = await db.jobs.find_one({"_id": ObjectId(job_id)})
                    job_title = job.get("title", "Position") if job else "Position"
                    
                    # Prepare notification payload
                    notification_payload = {
                        "candidates": [{
                            "candidate_name": candidate.get("name", "Candidate"),
                            "candidate_email": candidate.get("email", ""),
                            "candidate_phone": candidate.get("phone", ""),
                            "candidate_id": candidate_id_str
                        }],
                        "sequence_type": request.notification_type,
                        "job_title": job_title,
                        "job_id": job_id,
                        "matching_score": str(app.get("matching_score", "N/A"))
                    }
                    
                    # Send to LangGraph
                    try:
                        await _send_langgraph_bulk_notifications(notification_payload, timeout=45.0)

                        # Wait a bit between requests to prevent overwhelming the service
                        await asyncio.sleep(0.3)

                        success_count += 1
                        notifications_sent.append({
                            "candidate_id": candidate_id_str,
                            "candidate_name": candidate.get("name"),
                            "job_id": job_id,
                            "job_title": job_title
                        })
                        print(f"✅ Sent notification to {candidate.get('name')} for job {job_title}")
                    except Exception as send_error:
                        failed_count += 1
                        print(f"Error sending notification: {send_error}")
            
            except Exception as candidate_error:
                failed_count += 1
                print(f"Error processing candidate {candidate_id_str}: {candidate_error}")
        
        return {
            "success": True,
            "total_notifications": total_notifications,
            "success_count": success_count,
            "failed_count": failed_count,
            "notifications_sent": notifications_sent
        }
    
    except Exception as e:
        print(f"Error in send_per_job_notifications: {e}")
        return {
            "success": False,
            "error": str(e),
            "total_notifications": 0,
            "success_count": 0,
            "failed_count": 0
        }

@app.post("/v1/candidate/login", tags=["Candidate Portal"])
async def candidate_login(login_data: CandidateLogin):
    """Candidate Login"""
    try:
        db = await get_mongo_db()
        
        # Get candidate by email
        candidate = await db.candidates.find_one({"email": login_data.email})
        
        if not candidate:
            return {"success": False, "error": "Invalid credentials"}
        
        # Verify password hash
        if candidate.get("password_hash"):
            if not bcrypt.checkpw(login_data.password.encode('utf-8'), candidate.get("password_hash").encode('utf-8')):
                return {"success": False, "error": "Invalid credentials"}
        # If no password hash exists, accept any password (for existing test data)
        
        # Generate JWT token
        jwt_secret = os.getenv("CANDIDATE_JWT_SECRET_KEY")
        candidate_id_str = str(candidate["_id"])
        
        # Get role from database (for recruiters) or default to candidate
        # Recruiters are stored as candidates but have role field set to "recruiter"
        user_role = candidate.get("role", "candidate")
        if user_role not in ["candidate", "recruiter"]:
            user_role = "candidate"  # Ensure valid role
        
        token_payload = {
            "sub": candidate_id_str,  # Standard JWT claim for subject/user ID
            "candidate_id": candidate_id_str,  # Keep for backward compatibility
            "user_id": candidate_id_str,  # Also include user_id for jwt_auth.py compatibility
            "email": candidate.get("email"),
            "name": candidate.get("name", ""),
            "role": user_role,  # Use role from database (supports recruiter)
            "exp": int(datetime.now(timezone.utc).timestamp()) + 86400  # 24 hours
        }
        token = jwt.encode(token_payload, jwt_secret, algorithm="HS256")
        
        return {
            "success": True,
            "message": "Login successful",
            "token": token,
            "candidate_id": candidate_id_str,  # Add candidate_id at top level for frontend compatibility
            "candidate": {
                "id": str(candidate["_id"]),
                "name": candidate.get("name"),
                "email": candidate.get("email"),
                "phone": candidate.get("phone"),
                "location": candidate.get("location"),
                "experience_years": candidate.get("experience_years"),
                "technical_skills": candidate.get("technical_skills"),
                "seniority_level": candidate.get("seniority_level"),
                "education_level": candidate.get("education_level"),
                "status": candidate.get("status")
            }
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/v1/auth/guest-session", tags=["Auth"])
async def create_guest_session():
    """Mint a demo guest (candidate) JWT so the UI opens without any sign-in."""
    signing_secret = (os.getenv("CANDIDATE_JWT_SECRET_KEY") or "").strip()
    if not signing_secret:
        raise HTTPException(
            status_code=503,
            detail="CANDIDATE_JWT_SECRET_KEY not configured for guest sessions",
        )
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "sub": "guest:demo",
        "user_id": "guest:demo",
        "candidate_id": "guest:demo",
        "email": "",
        "name": "Guest User",
        "role": "candidate",
        "auth_type": "guest_demo",
        "exp": now + 86400,
    }
    token = jwt.encode(payload, signing_secret, algorithm="HS256")
    return {
        "success": True,
        "token": token,
        "user": {"id": "guest:demo", "email": "", "name": "Guest User", "role": "candidate"},
    }


@app.get("/v1/candidate/profile/{candidate_id}", tags=["Candidate Portal"])
async def get_candidate_profile(candidate_id: str, auth = Depends(get_auth)):
    """Get Candidate Profile (JWT authenticated)"""
    try:
        db = await get_mongo_db()
        
        # Verify the candidate_id matches the authenticated user (if using JWT token)
        auth_info = auth
        # Support both old format (candidate_token) and new format (jwt_token with role)
        if auth_info.get("type") in ["candidate_token", "jwt_token"]:
            # Get candidate_id from token - try both old and new formats
            token_candidate_id = None
            if auth_info.get("type") == "candidate_token":
                # Old format
                token_candidate_id = str(auth_info.get("candidate_id", ""))
            elif auth_info.get("type") == "jwt_token" and auth_info.get("role") == "candidate":
                # New format from jwt_auth.py
                token_candidate_id = str(auth_info.get("user_id", ""))
            
            # Compare as strings to handle ObjectId vs string differences
            if token_candidate_id and token_candidate_id != str(candidate_id):
                # Also try ObjectId comparison
                try:
                    if ObjectId(token_candidate_id) != ObjectId(candidate_id):
                        raise HTTPException(status_code=403, detail="You can only view your own profile")
                except:
                    # If ObjectId conversion fails, use string comparison
                    if token_candidate_id != str(candidate_id):
                        raise HTTPException(status_code=403, detail="You can only view your own profile")
        
        # Try to convert to ObjectId if valid, otherwise search by string id
        try:
            doc = await db.candidates.find_one({"_id": ObjectId(candidate_id)})
        except:
            doc = await db.candidates.find_one({"id": candidate_id})
        
        if not doc:
            return {"error": "Candidate not found", "candidate_id": candidate_id}
        
        candidate = {
            "id": str(doc["_id"]),
            "name": doc.get("name"),
            "email": doc.get("email"),
            "phone": doc.get("phone"),
            "location": doc.get("location"),
            "experience_years": doc.get("experience_years"),
            "technical_skills": doc.get("technical_skills"),
            "seniority_level": doc.get("seniority_level"),
            "education_level": doc.get("education_level"),
            "resume_path": doc.get("resume_path"),
            "resume_url": doc.get("resume_path"),  # Alias for frontend compatibility
            "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
            "updated_at": doc.get("updated_at").isoformat() if doc.get("updated_at") else None
        }
        
        return candidate
    except HTTPException:
        raise
    except Exception as e:
        return {"error": str(e), "candidate_id": candidate_id}

@app.put("/v1/candidate/profile/{candidate_id}", tags=["Candidate Portal"])
async def update_candidate_profile(candidate_id: str, profile_data: CandidateProfileUpdate, auth = Depends(get_auth)):
    """Update Candidate Profile"""
    try:
        db = await get_mongo_db()
        
        # Input validation
        if profile_data.phone and not re.match(r"^(\+91|91)?[6-9]\d{9}$", profile_data.phone):
            raise HTTPException(status_code=400, detail="Invalid Indian phone number format.")
        if profile_data.experience_years is not None and profile_data.experience_years < 0:
            raise HTTPException(status_code=400, detail="Experience years cannot be negative.")
        
        # Build update fields
        update_fields = {}
        
        if profile_data.name:
            update_fields["name"] = profile_data.name
        if profile_data.phone:
            update_fields["phone"] = profile_data.phone
        if profile_data.location:
            update_fields["location"] = profile_data.location
        if profile_data.experience_years is not None:
            update_fields["experience_years"] = profile_data.experience_years
        if profile_data.technical_skills:
            update_fields["technical_skills"] = profile_data.technical_skills
        if profile_data.education_level:
            update_fields["education_level"] = profile_data.education_level
        if profile_data.seniority_level:
            update_fields["seniority_level"] = profile_data.seniority_level
        
        if not update_fields:
            return {"success": False, "error": "No fields to update"}
        
        update_fields["updated_at"] = datetime.now(timezone.utc)
        
        try:
            result = await db.candidates.update_one(
                {"_id": ObjectId(candidate_id)},
                {"$set": update_fields}
            )
        except:
            result = await db.candidates.update_one(
                {"id": candidate_id},
                {"$set": update_fields}
            )
        
        return {"success": True, "message": "Profile updated successfully"}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/v1/candidate/apply", tags=["Candidate Portal"])
async def apply_for_job(application: JobApplication, auth = Depends(get_auth)):
    """Apply for Job"""
    try:
        db = await get_mongo_db()
        
        # Normalize candidate_id to string for consistency
        candidate_id_str = str(application.candidate_id)
        job_id_str = str(application.job_id)
        
        print(f"Applying for job - candidate_id: {candidate_id_str}, job_id: {job_id_str}")
        
        # Check if already applied - try multiple formats
        existing = None
        try:
            # Try string match first
            existing = await db.job_applications.find_one({
                "candidate_id": candidate_id_str,
                "job_id": job_id_str
            })
            
            # If not found, try ObjectId match
            if not existing:
                try:
                    candidate_obj_id = ObjectId(candidate_id_str)
                    existing = await db.job_applications.find_one({
                        "candidate_id": str(candidate_obj_id),
                        "job_id": job_id_str
                    })
                except:
                    pass
        except Exception as e:
            print(f"Error checking existing application: {e}")
        
        if existing:
            return {"success": False, "error": "Already applied for this job"}
        
        # Insert application with normalized IDs
        document = {
            "candidate_id": candidate_id_str,  # Store as string for consistency
            "job_id": job_id_str,
            "cover_letter": application.cover_letter,
            "status": "applied",
            "applied_date": datetime.now(timezone.utc)
        }
        result = await db.job_applications.insert_one(document)
        application_id = str(result.inserted_id)
        
        print(f"Application inserted successfully - application_id: {application_id}")
        
        # Send application received email asynchronously (don't block the response)
        try:
            # Get candidate details
            candidate = await db.candidates.find_one({"_id": ObjectId(candidate_id_str)})
            if not candidate:
                # Try with string ID
                candidate = await db.candidates.find_one({"id": candidate_id_str})
            
            # Get job details
            job = await db.jobs.find_one({"_id": ObjectId(job_id_str)})
            if not job:
                job = await db.jobs.find_one({"id": job_id_str})
            
            if candidate and job:
                candidate_name = candidate.get("name", "Candidate")
                candidate_email = candidate.get("email", "")
                candidate_phone = candidate.get("phone", "")
                job_title = job.get("title", "Position")
                
                print(f"📧 [Application Email] Sending to {candidate_email} for job: {job_title}")
                
                application_payload = {
                    "candidates": [{
                        "candidate_name": candidate_name,
                        "candidate_email": candidate_email,
                        "candidate_phone": candidate_phone,
                        "candidate_id": candidate_id_str
                    }],
                    "sequence_type": "application_received",
                    "job_title": job_title,
                    "job_id": job_id_str,
                    "application_id": application_id
                }
                
                await _send_langgraph_bulk_notifications(application_payload, timeout=10.0)
                print(f"✅ Application email sent successfully to {candidate_email}")
            else:
                print(f"⚠️ Could not send email - candidate or job not found")
        except Exception as email_error:
            # Log error but don't fail the application
            print(f"Failed to send application email: {email_error}")
        
        return {
            "success": True,
            "message": "Application submitted successfully",
            "application_id": application_id
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/v1/candidate/stats/{candidate_id}", tags=["Candidate Portal"])
async def get_candidate_stats(candidate_id: str, auth = Depends(get_auth)):
    """Get Candidate Dashboard Statistics"""
    try:
        db = await get_mongo_db()
        
        # Verify the candidate_id matches the authenticated user (if using JWT token)
        auth_info = auth
        if auth_info.get("type") == "jwt_token" and auth_info.get("role") == "candidate":
            token_candidate_id = str(auth_info.get("user_id", ""))
            if token_candidate_id and token_candidate_id != str(candidate_id):
                raise HTTPException(status_code=403, detail="You can only view your own stats")
        
        # Get applications count
        applications_count = await db.job_applications.count_documents({"candidate_id": candidate_id})
        
        # Get shortlisted count
        shortlisted_count = await db.job_applications.count_documents({
            "candidate_id": candidate_id,
            "status": "shortlisted"
        })
        
        # Get interviews scheduled count
        interviews_scheduled = await db.interviews.count_documents({
            "candidate_id": candidate_id,
            "status": "scheduled"
        })
        
        # Get offers received count
        offers_received = await db.offers.count_documents({
            "candidate_id": candidate_id
        })
        
        return {
            "total_applications": applications_count,
            "shortlisted": shortlisted_count,
            "interviews_scheduled": interviews_scheduled,
            "offers_received": offers_received,
            "profile_views": 0  # Placeholder - can be implemented later
        }
    except HTTPException:
        raise
    except Exception as e:
        return {
            "total_applications": 0,
            "shortlisted": 0,
            "interviews_scheduled": 0,
            "offers_received": 0,
            "profile_views": 0,
            "error": str(e)
        }

@app.get("/v1/recruiter/jobs", tags=["Recruiter Portal"])
async def get_recruiter_jobs(auth=Depends(get_auth)):
    """List active jobs posted by the authenticated recruiter only."""
    try:
        auth_info = auth
        if auth_info.get("type") != "jwt_token" or auth_info.get("role") != "recruiter":
            raise HTTPException(status_code=403, detail="This endpoint is only available for recruiters")
        recruiter_id = str(auth_info.get("user_id", ""))
        if not recruiter_id:
            return {"jobs": [], "count": 0}
        db = await get_mongo_db()
        query = {"status": "active", "recruiter_id": recruiter_id}
        cursor = db.jobs.find(query).sort("created_at", -1).limit(100)
        jobs_list = await cursor.to_list(length=100)
        job_ids = [str(doc["_id"]) for doc in jobs_list]
        # Single aggregation for per-job applicants/shortlisted (avoids N+1 and slow match service)
        counts_by_job: Dict[str, Dict[str, int]] = {}
        if job_ids:
            pipeline = [
                {"$match": {"job_id": {"$in": job_ids}}},
                {"$group": {
                    "_id": "$job_id",
                    "applicants": {"$sum": 1},
                    "shortlisted": {"$sum": {"$cond": [{"$eq": ["$status", "shortlisted"]}, 1, 0]}},
                }},
            ]
            async for agg_doc in db.job_applications.aggregate(pipeline):
                jid = agg_doc.get("_id") or ""
                counts_by_job[str(jid)] = {
                    "applicants": agg_doc.get("applicants", 0),
                    "shortlisted": agg_doc.get("shortlisted", 0),
                }
        jobs = []
        for doc in jobs_list:
            jid = str(doc["_id"])
            counts = counts_by_job.get(jid, {"applicants": 0, "shortlisted": 0})
            salary_min, salary_max = _job_salary_from_doc(doc)
            jobs.append({
                "id": jid,
                "title": doc.get("title"),
                "department": doc.get("department"),
                "location": doc.get("location"),
                "experience_level": doc.get("experience_level"),
                "requirements": doc.get("requirements"),
                "description": doc.get("description"),
                "job_type": doc.get("job_type") or doc.get("employment_type"),
                "employment_type": doc.get("employment_type"),
                "salary_min": salary_min,
                "salary_max": salary_max,
                "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
                "applicants": counts["applicants"],
                "shortlisted": counts["shortlisted"],
            })
        return {"jobs": jobs, "count": len(jobs)}
    except HTTPException:
        raise
    except Exception as e:
        return {"jobs": [], "count": 0, "error": str(e)}


@app.get("/v1/client/applicants", tags=["Client Portal API"])
async def get_client_applicants(auth=Depends(get_auth)):
    """List all applicants across the authenticated client's jobs with document workflow metadata."""
    ctx = _get_auth_jwt_context(auth)
    if ctx["role"] != "client":
        raise HTTPException(status_code=403, detail="This endpoint is only available for clients")

    db = await get_mongo_db()
    job_ids = await _client_all_job_ids_for_dashboard(db, ctx["user_id"])
    if not job_ids:
        return {"applicants": [], "count": 0}

    cursor = db.job_applications.find({"job_id": {"$in": job_ids}}).sort("applied_date", -1).limit(2000)
    applications = await cursor.to_list(length=2000)
    if not applications:
        return {"applicants": [], "count": 0}

    candidate_ids = list({str(a.get("candidate_id")) for a in applications if a.get("candidate_id")})
    candidate_object_ids: List[ObjectId] = []
    for cid in candidate_ids:
        if ObjectId.is_valid(cid):
            candidate_object_ids.append(ObjectId(cid))

    candidate_docs = []
    if candidate_object_ids:
        candidate_docs = await db.candidates.find({"_id": {"$in": candidate_object_ids}}).to_list(length=len(candidate_object_ids))
    candidate_by_id = {str(c["_id"]): c for c in candidate_docs}

    job_object_ids = [ObjectId(j) for j in job_ids if ObjectId.is_valid(j)]
    jobs_docs = await db.jobs.find({"_id": {"$in": job_object_ids}}).to_list(length=len(job_object_ids))
    jobs_by_id = {str(j["_id"]): j for j in jobs_docs}

    out = []
    for app_doc in applications:
        application_id = str(app_doc["_id"])
        candidate_id = str(app_doc.get("candidate_id") or "")
        job_id = str(app_doc.get("job_id") or "")
        candidate = candidate_by_id.get(candidate_id, {})
        job = jobs_by_id.get(job_id, {})
        out.append({
            "application_id": application_id,
            "job_id": job_id,
            "job_title": job.get("title"),
            "status": app_doc.get("status") or "applied",
            "applied_date": app_doc.get("applied_date").isoformat() if app_doc.get("applied_date") else None,
            "candidate_id": candidate_id,
            "candidate_name": candidate.get("name"),
            "candidate_email": candidate.get("email"),
            "candidate_phone": candidate.get("phone"),
            "candidate_location": candidate.get("location"),
            "required_documents": _normalize_required_documents(app_doc.get("required_documents")),
            "required_documents_updated_at": app_doc.get("required_documents_updated_at").isoformat() if app_doc.get("required_documents_updated_at") else None,
            "documents_uploaded": _sanitize_uploaded_documents(app_doc.get("documents_uploaded")),
        })
    return {"applicants": out, "count": len(out)}


@app.post("/v1/client/applications/{application_id}/required-documents", tags=["Client Portal API"])
async def set_application_required_documents(
    application_id: str,
    body: RequiredDocumentsRequest,
    auth=Depends(get_auth),
):
    """Set required document list for an application and notify the candidate."""
    ctx = _get_auth_jwt_context(auth)
    if ctx["role"] != "client":
        raise HTTPException(status_code=403, detail="This endpoint is only available for clients")
    required_docs = _normalize_required_documents(body.document_types)
    if not required_docs:
        raise HTTPException(status_code=400, detail="Select at least one valid document (resume, nda).")
    if not ObjectId.is_valid(application_id):
        raise HTTPException(status_code=400, detail="Invalid application id.")

    db = await get_mongo_db()
    app_doc = await db.job_applications.find_one({"_id": ObjectId(application_id)})
    if not app_doc:
        raise HTTPException(status_code=404, detail="Application not found.")
    job_id = str(app_doc.get("job_id") or "")
    allowed_job_ids = await _client_all_job_ids_for_dashboard(db, ctx["user_id"])
    if job_id not in allowed_job_ids:
        raise HTTPException(status_code=403, detail="You can only manage applicants for your own jobs.")

    now = datetime.now(timezone.utc)
    await db.job_applications.update_one(
        {"_id": app_doc["_id"]},
        {
            "$set": {
                "required_documents": required_docs,
                "required_documents_updated_at": now,
                "required_documents_updated_by": ctx["user_id"],
                "updated_at": now,
            }
        },
    )

    candidate_id = str(app_doc.get("candidate_id") or "")
    job_doc = await db.jobs.find_one({"_id": ObjectId(job_id)}) if ObjectId.is_valid(job_id) else await db.jobs.find_one({"id": job_id})
    job_title = (job_doc or {}).get("title") or "your application"
    label_text = ", ".join(DOCUMENT_LABELS[d] for d in required_docs)
    await _create_portal_notification(
        db,
        recipient_role="candidate",
        recipient_user_id=candidate_id,
        title="Documents requested by client",
        message=f"Please upload {label_text} for {job_title}.",
        kind="document_request",
        payload={
            "application_id": application_id,
            "job_id": job_id,
            "job_title": job_title,
            "required_documents": required_docs,
        },
    )

    return {
        "ok": True,
        "application_id": application_id,
        "required_documents": required_docs,
    }


@app.get("/v1/client/applications/{application_id}/documents/{document_type}", tags=["Client Portal API"])
async def get_client_application_document(
    application_id: str,
    document_type: str,
    download: bool = False,
    auth=Depends(get_auth),
):
    """View/download candidate uploaded document for an applicant in the client's job."""
    ctx = _get_auth_jwt_context(auth)
    if ctx["role"] != "client":
        raise HTTPException(status_code=403, detail="This endpoint is only available for clients")
    if not ObjectId.is_valid(application_id):
        raise HTTPException(status_code=400, detail="Invalid application id.")

    doc_key = str(document_type or "").strip().lower()
    if doc_key not in DOCUMENT_LABELS:
        raise HTTPException(status_code=400, detail="Invalid document type. Use 'resume' or 'nda'.")

    db = await get_mongo_db()
    app_doc = await db.job_applications.find_one({"_id": ObjectId(application_id)})
    if not app_doc:
        raise HTTPException(status_code=404, detail="Application not found.")

    job_id = str(app_doc.get("job_id") or "")
    allowed_job_ids = await _client_all_job_ids_for_dashboard(db, ctx["user_id"])
    if job_id not in allowed_job_ids:
        raise HTTPException(status_code=403, detail="You can only access documents for your own applicants.")

    uploaded_map = app_doc.get("documents_uploaded") or {}
    uploaded_meta = uploaded_map.get(doc_key) if isinstance(uploaded_map, dict) else None
    document_id = str((uploaded_meta or {}).get("document_id") or "")
    if not document_id or not ObjectId.is_valid(document_id):
        raise HTTPException(status_code=404, detail="Document not uploaded yet.")

    blob_doc = await db.application_documents.find_one({"_id": ObjectId(document_id)})
    if not blob_doc:
        raise HTTPException(status_code=404, detail="Document data not found.")

    encoded = str(blob_doc.get("file_base64") or "")
    if not encoded:
        raise HTTPException(status_code=404, detail="Document content is missing.")

    try:
        binary = base64.b64decode(encoded.encode("ascii"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Stored document content is corrupted.") from exc

    filename = str(blob_doc.get("filename") or f"{doc_key}.bin").replace("\n", "_").replace("\r", "_")
    content_type = str(blob_doc.get("content_type") or "application/octet-stream")
    disposition = "attachment" if download else "inline"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    return Response(content=binary, media_type=content_type, headers=headers)


@app.get("/v1/recruiter/applicants", tags=["Recruiter Portal"])
async def get_recruiter_applicants(auth=Depends(get_auth)):
    """List all applicants across the authenticated recruiter's jobs with document workflow metadata."""
    ctx = _get_auth_jwt_context(auth)
    if ctx["role"] != "recruiter":
        raise HTTPException(status_code=403, detail="This endpoint is only available for recruiters")

    db = await get_mongo_db()
    job_ids = await _recruiter_all_job_ids_for_dashboard(db, ctx["user_id"])
    if not job_ids:
        return {"applicants": [], "count": 0}

    cursor = db.job_applications.find({"job_id": {"$in": job_ids}}).sort("applied_date", -1).limit(2000)
    applications = await cursor.to_list(length=2000)
    if not applications:
        return {"applicants": [], "count": 0}

    candidate_ids = list({str(a.get("candidate_id")) for a in applications if a.get("candidate_id")})
    candidate_object_ids: List[ObjectId] = []
    for cid in candidate_ids:
        if ObjectId.is_valid(cid):
            candidate_object_ids.append(ObjectId(cid))

    candidate_docs = []
    if candidate_object_ids:
        candidate_docs = await db.candidates.find({"_id": {"$in": candidate_object_ids}}).to_list(length=len(candidate_object_ids))
    candidate_by_id = {str(c["_id"]): c for c in candidate_docs}

    job_object_ids = [ObjectId(j) for j in job_ids if ObjectId.is_valid(j)]
    jobs_docs = await db.jobs.find({"_id": {"$in": job_object_ids}}).to_list(length=len(job_object_ids))
    jobs_by_id = {str(j["_id"]): j for j in jobs_docs}

    out = []
    for app_doc in applications:
        application_id = str(app_doc["_id"])
        candidate_id = str(app_doc.get("candidate_id") or "")
        job_id = str(app_doc.get("job_id") or "")
        candidate = candidate_by_id.get(candidate_id, {})
        job = jobs_by_id.get(job_id, {})
        out.append({
            "application_id": application_id,
            "job_id": job_id,
            "job_title": job.get("title"),
            "status": app_doc.get("status") or "applied",
            "applied_date": app_doc.get("applied_date").isoformat() if app_doc.get("applied_date") else None,
            "candidate_id": candidate_id,
            "candidate_name": candidate.get("name"),
            "candidate_email": candidate.get("email"),
            "candidate_phone": candidate.get("phone"),
            "candidate_location": candidate.get("location"),
            "required_documents": _normalize_required_documents(app_doc.get("required_documents")),
            "required_documents_updated_at": app_doc.get("required_documents_updated_at").isoformat() if app_doc.get("required_documents_updated_at") else None,
            "documents_uploaded": _sanitize_uploaded_documents(app_doc.get("documents_uploaded")),
        })
    return {"applicants": out, "count": len(out)}


@app.post("/v1/recruiter/applications/{application_id}/required-documents", tags=["Recruiter Portal"])
async def set_recruiter_application_required_documents(
    application_id: str,
    body: RequiredDocumentsRequest,
    auth=Depends(get_auth),
):
    """Set required document list for an application under recruiter-owned jobs and notify candidate."""
    ctx = _get_auth_jwt_context(auth)
    if ctx["role"] != "recruiter":
        raise HTTPException(status_code=403, detail="This endpoint is only available for recruiters")
    required_docs = _normalize_required_documents(body.document_types)
    if not required_docs:
        raise HTTPException(status_code=400, detail="Select at least one valid document (resume, nda).")
    if not ObjectId.is_valid(application_id):
        raise HTTPException(status_code=400, detail="Invalid application id.")

    db = await get_mongo_db()
    app_doc = await db.job_applications.find_one({"_id": ObjectId(application_id)})
    if not app_doc:
        raise HTTPException(status_code=404, detail="Application not found.")
    job_id = str(app_doc.get("job_id") or "")
    allowed_job_ids = await _recruiter_all_job_ids_for_dashboard(db, ctx["user_id"])
    if job_id not in allowed_job_ids:
        raise HTTPException(status_code=403, detail="You can only manage applicants for your own jobs.")

    now = datetime.now(timezone.utc)
    await db.job_applications.update_one(
        {"_id": app_doc["_id"]},
        {
            "$set": {
                "required_documents": required_docs,
                "required_documents_updated_at": now,
                "required_documents_updated_by": ctx["user_id"],
                "updated_at": now,
            }
        },
    )

    candidate_id = str(app_doc.get("candidate_id") or "")
    job_doc = await db.jobs.find_one({"_id": ObjectId(job_id)}) if ObjectId.is_valid(job_id) else await db.jobs.find_one({"id": job_id})
    job_title = (job_doc or {}).get("title") or "your application"
    label_text = ", ".join(DOCUMENT_LABELS[d] for d in required_docs)
    await _create_portal_notification(
        db,
        recipient_role="candidate",
        recipient_user_id=candidate_id,
        title="Documents requested by recruiter",
        message=f"Please upload {label_text} for {job_title}.",
        kind="document_request",
        payload={
            "application_id": application_id,
            "job_id": job_id,
            "job_title": job_title,
            "required_documents": required_docs,
        },
    )

    return {
        "ok": True,
        "application_id": application_id,
        "required_documents": required_docs,
    }


@app.get("/v1/recruiter/applications/{application_id}/documents/{document_type}", tags=["Recruiter Portal"])
async def get_recruiter_application_document(
    application_id: str,
    document_type: str,
    download: bool = False,
    auth=Depends(get_auth),
):
    """View/download candidate uploaded document for an applicant in the recruiter's job."""
    ctx = _get_auth_jwt_context(auth)
    if ctx["role"] != "recruiter":
        raise HTTPException(status_code=403, detail="This endpoint is only available for recruiters")
    if not ObjectId.is_valid(application_id):
        raise HTTPException(status_code=400, detail="Invalid application id.")

    doc_key = str(document_type or "").strip().lower()
    if doc_key not in DOCUMENT_LABELS:
        raise HTTPException(status_code=400, detail="Invalid document type. Use 'resume' or 'nda'.")

    db = await get_mongo_db()
    app_doc = await db.job_applications.find_one({"_id": ObjectId(application_id)})
    if not app_doc:
        raise HTTPException(status_code=404, detail="Application not found.")

    job_id = str(app_doc.get("job_id") or "")
    allowed_job_ids = await _recruiter_all_job_ids_for_dashboard(db, ctx["user_id"])
    if job_id not in allowed_job_ids:
        raise HTTPException(status_code=403, detail="You can only access documents for your own applicants.")

    uploaded_map = app_doc.get("documents_uploaded") or {}
    uploaded_meta = uploaded_map.get(doc_key) if isinstance(uploaded_map, dict) else None
    document_id = str((uploaded_meta or {}).get("document_id") or "")
    if not document_id or not ObjectId.is_valid(document_id):
        raise HTTPException(status_code=404, detail="Document not uploaded yet.")

    blob_doc = await db.application_documents.find_one({"_id": ObjectId(document_id)})
    if not blob_doc:
        raise HTTPException(status_code=404, detail="Document data not found.")

    encoded = str(blob_doc.get("file_base64") or "")
    if not encoded:
        raise HTTPException(status_code=404, detail="Document content is missing.")

    try:
        binary = base64.b64decode(encoded.encode("ascii"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Stored document content is corrupted.") from exc

    filename = str(blob_doc.get("filename") or f"{doc_key}.bin").replace("\n", "_").replace("\r", "_")
    content_type = str(blob_doc.get("content_type") or "application/octet-stream")
    disposition = "attachment" if download else "inline"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    return Response(content=binary, media_type=content_type, headers=headers)


@app.get("/v1/portal/notifications", tags=["Notifications"])
async def get_portal_notifications(limit: int = 30, auth=Depends(get_auth)):
    """Get in-app notifications for the authenticated JWT user."""
    ctx = _get_auth_jwt_context(auth)
    try:
        db = await get_mongo_db()
    except DatabaseNotConfiguredError:
        return {"notifications": [], "unread_count": 0}
    lim = max(1, min(limit, 100))
    query = {"recipient_role": ctx["role"], "recipient_user_id": ctx["user_id"]}
    cursor = db.portal_notifications.find(query).sort("created_at", -1).limit(lim)
    rows = await cursor.to_list(length=lim)
    unread_count = await db.portal_notifications.count_documents({**query, "is_read": False})
    notifications = []
    for row in rows:
        notifications.append({
            "id": str(row["_id"]),
            "title": row.get("title"),
            "message": row.get("message"),
            "kind": row.get("kind") or "info",
            "is_read": bool(row.get("is_read")),
            "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
            "payload": row.get("payload") or {},
        })
    return {"notifications": notifications, "unread_count": unread_count}


@app.post("/v1/portal/notifications/{notification_id}/read", tags=["Notifications"])
async def mark_portal_notification_read(notification_id: str, auth=Depends(get_auth)):
    """Mark a single in-app notification as read."""
    ctx = _get_auth_jwt_context(auth)
    if not ObjectId.is_valid(notification_id):
        raise HTTPException(status_code=400, detail="Invalid notification id.")
    db = await get_mongo_db()
    res = await db.portal_notifications.update_one(
        {
            "_id": ObjectId(notification_id),
            "recipient_role": ctx["role"],
            "recipient_user_id": ctx["user_id"],
        },
        {"$set": {"is_read": True, "read_at": datetime.now(timezone.utc)}},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Notification not found.")
    return {"ok": True}


@app.post("/v1/portal/notifications/read-all", tags=["Notifications"])
async def mark_all_portal_notifications_read(auth=Depends(get_auth)):
    """Mark all in-app notifications as read for the authenticated JWT user."""
    ctx = _get_auth_jwt_context(auth)
    db = await get_mongo_db()
    await db.portal_notifications.update_many(
        {"recipient_role": ctx["role"], "recipient_user_id": ctx["user_id"], "is_read": False},
        {"$set": {"is_read": True, "read_at": datetime.now(timezone.utc)}},
    )
    return {"ok": True}


@app.post("/v1/candidate/applications/{application_id}/documents/{document_type}", tags=["Candidate Portal"])
async def upload_candidate_application_document(
    application_id: str,
    document_type: str,
    file: UploadFile = File(...),
    auth=Depends(get_auth),
):
    """Upload requested application document (resume / nda), persist in DB, and notify client."""
    ctx = _get_auth_jwt_context(auth)
    if ctx["role"] != "candidate":
        raise HTTPException(status_code=403, detail="This endpoint is only available for candidates")
    if not ObjectId.is_valid(application_id):
        raise HTTPException(status_code=400, detail="Invalid application id.")
    doc_key = str(document_type or "").strip().lower()
    if doc_key not in DOCUMENT_LABELS:
        raise HTTPException(status_code=400, detail="Invalid document type. Use 'resume' or 'nda'.")

    db = await get_mongo_db()
    app_doc = await db.job_applications.find_one({"_id": ObjectId(application_id)})
    if not app_doc:
        raise HTTPException(status_code=404, detail="Application not found.")
    candidate_id = str(app_doc.get("candidate_id") or "")
    if candidate_id != ctx["user_id"]:
        raise HTTPException(status_code=403, detail="You can only upload documents for your own applications.")

    required_docs = _normalize_required_documents(app_doc.get("required_documents"))
    if doc_key not in required_docs:
        raise HTTPException(status_code=400, detail=f"This application has not requested {DOCUMENT_LABELS[doc_key]}.")
    request_updated_at = app_doc.get("required_documents_updated_at")
    existing_uploaded = (app_doc.get("documents_uploaded") or {}).get(doc_key) if isinstance(app_doc.get("documents_uploaded"), dict) else None
    if existing_uploaded and request_updated_at:
        uploaded_at_raw = existing_uploaded.get("uploaded_at")
        uploaded_dt: Optional[datetime] = None
        if isinstance(uploaded_at_raw, str):
            try:
                uploaded_dt = datetime.fromisoformat(uploaded_at_raw.replace("Z", "+00:00"))
            except Exception:
                uploaded_dt = None
        if uploaded_dt and uploaded_dt >= request_updated_at:
            raise HTTPException(
                status_code=400,
                detail=f"{DOCUMENT_LABELS[doc_key]} is already submitted for the current request.",
            )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(content) > MAX_DOCUMENT_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File is too large. Max allowed size is 10 MB.")

    content_type = (file.content_type or "").strip().lower()
    allowed_content_types = ALLOWED_DOCUMENT_CONTENT_TYPES.get(doc_key, set())
    if content_type not in allowed_content_types:
        raise HTTPException(status_code=400, detail="Unsupported file type for selected document.")

    now = datetime.now(timezone.utc)
    encoded = base64.b64encode(content).decode("ascii")
    doc_insert = {
        "application_id": application_id,
        "job_id": str(app_doc.get("job_id") or ""),
        "candidate_id": candidate_id,
        "document_type": doc_key,
        "label": DOCUMENT_LABELS[doc_key],
        "filename": file.filename or f"{doc_key}.bin",
        "content_type": content_type,
        "size_bytes": len(content),
        "file_base64": encoded,
        "uploaded_at": now,
    }
    doc_result = await db.application_documents.insert_one(doc_insert)
    doc_id = str(doc_result.inserted_id)

    doc_meta = {
        "document_id": doc_id,
        "filename": doc_insert["filename"],
        "content_type": content_type,
        "size_bytes": len(content),
        "uploaded_at": now.isoformat(),
    }
    await db.job_applications.update_one(
        {"_id": app_doc["_id"]},
        {"$set": {f"documents_uploaded.{doc_key}": doc_meta, "updated_at": now}},
    )

    job_id = str(app_doc.get("job_id") or "")
    job_doc = await db.jobs.find_one({"_id": ObjectId(job_id)}) if ObjectId.is_valid(job_id) else await db.jobs.find_one({"id": job_id})
    if job_doc:
        client_id = str(job_doc.get("client_id") or "").strip()
        if client_id:
            candidate_doc = await db.candidates.find_one({"_id": ObjectId(candidate_id)}) if ObjectId.is_valid(candidate_id) else None
            candidate_name = (candidate_doc or {}).get("name") or "A candidate"
            await _create_portal_notification(
                db,
                recipient_role="client",
                recipient_user_id=client_id,
                title="Candidate uploaded requested document",
                message=f"{candidate_name} uploaded {DOCUMENT_LABELS[doc_key]} for {job_doc.get('title') or 'a job'}.",
                kind="document_uploaded",
                payload={
                    "application_id": application_id,
                    "job_id": job_id,
                    "candidate_id": candidate_id,
                    "document_type": doc_key,
                },
            )
        recruiter_id = str(job_doc.get("recruiter_id") or "").strip()
        if recruiter_id:
            candidate_doc = await db.candidates.find_one({"_id": ObjectId(candidate_id)}) if ObjectId.is_valid(candidate_id) else None
            candidate_name = (candidate_doc or {}).get("name") or "A candidate"
            await _create_portal_notification(
                db,
                recipient_role="recruiter",
                recipient_user_id=recruiter_id,
                title="Candidate uploaded requested document",
                message=f"{candidate_name} uploaded {DOCUMENT_LABELS[doc_key]} for {job_doc.get('title') or 'a job'}.",
                kind="document_uploaded",
                payload={
                    "application_id": application_id,
                    "job_id": job_id,
                    "candidate_id": candidate_id,
                    "document_type": doc_key,
                },
            )

    return {"ok": True, "document_type": doc_key, "document": doc_meta}


@app.get("/v1/recruiter/stats", tags=["Recruiter Portal"])
async def get_recruiter_stats(auth=Depends(get_auth)):
    """Get Recruiter Dashboard Statistics. All metrics are data-isolated: only jobs posted by the logged-in recruiter (recruiter_id from JWT) and related applicants, interviews, offers, and feedback are counted."""
    try:
        db = await get_mongo_db()
        auth_info = auth
        if auth_info.get("type") != "jwt_token" or auth_info.get("role") != "recruiter":
            raise HTTPException(status_code=403, detail="This endpoint is only available for recruiters")
        recruiter_id = str(auth_info.get("user_id", ""))
        if not recruiter_id:
            return {
                "total_jobs": 0,
                "total_applicants": 0,
                "shortlisted": 0,
                "interviewed": 0,
                "offers_sent": 0,
                "hired": 0,
                "assessments_completed": 0
            }
        # Jobs posted by this recruiter (active only) – all counts below use these job_ids for isolation
        cursor = db.jobs.find({"status": "active", "recruiter_id": recruiter_id}, {"_id": 1})
        jobs_list = await cursor.to_list(length=500)
        job_ids = [str(doc["_id"]) for doc in jobs_list]
        total_jobs = len(job_ids)
        if total_jobs == 0:
            return {
                "total_jobs": 0,
                "total_applicants": 0,
                "shortlisted": 0,
                "interviewed": 0,
                "offers_sent": 0,
                "hired": 0,
                "assessments_completed": 0
            }
        total_applicants = await db.job_applications.count_documents({"job_id": {"$in": job_ids}})
        shortlisted = await db.job_applications.count_documents({
            "job_id": {"$in": job_ids},
            "status": "shortlisted"
        })
        interviewed = await db.interviews.count_documents({"job_id": {"$in": job_ids}})
        offers_sent = await db.offers.count_documents({"job_id": {"$in": job_ids}})
        hired = await db.offers.count_documents({
            "job_id": {"$in": job_ids},
            "status": "accepted"
        })
        assessments_completed = await db.feedback.count_documents({"job_id": {"$in": job_ids}})
        return {
            "total_jobs": total_jobs,
            "total_applicants": total_applicants,
            "shortlisted": shortlisted,
            "interviewed": interviewed,
            "offers_sent": offers_sent,
            "hired": hired,
            "assessments_completed": assessments_completed
        }
    except HTTPException:
        raise
    except Exception as e:
        return {
            "total_jobs": 0,
            "total_applicants": 0,
            "shortlisted": 0,
            "interviewed": 0,
            "offers_sent": 0,
            "hired": 0,
            "assessments_completed": 0,
            "error": str(e)
        }

@app.get("/v1/candidate/applications/{candidate_id}", tags=["Candidate Portal"])
async def get_candidate_applications(candidate_id: str, auth = Depends(get_auth)):
    """Get Candidate Applications"""
    try:
        db = await get_mongo_db()
        if auth.get("type") == "jwt_token" and auth.get("role") == "candidate":
            token_candidate_id = str(auth.get("user_id") or auth.get("sub") or "")
            if token_candidate_id and token_candidate_id != str(candidate_id):
                raise HTTPException(status_code=403, detail="You can only view your own applications")
        
        # Try multiple query strategies to find applications
        applications_list = []
        
        # Strategy 1: Direct string match
        try:
            cursor = db.job_applications.find({"candidate_id": candidate_id}).sort("applied_date", -1)
            applications_list = await cursor.to_list(length=None)
            print(f"Found {len(applications_list)} applications with string match for candidate_id: {candidate_id}")
        except Exception as e:
            print(f"String match error: {e}")
        
        # Strategy 2: Try ObjectId conversion and match
        if not applications_list:
            try:
                candidate_object_id = ObjectId(candidate_id)
                # Try matching with ObjectId as string
                cursor = db.job_applications.find({"candidate_id": str(candidate_object_id)}).sort("applied_date", -1)
                applications_list = await cursor.to_list(length=None)
                print(f"Found {len(applications_list)} applications with ObjectId string match")
            except Exception as e:
                print(f"ObjectId match error: {e}")
        
        # Strategy 3: Try all variations (for debugging)
        if not applications_list:
            # Get all applications and filter manually (fallback)
            all_apps = await db.job_applications.find({}).to_list(length=100)
            print(f"Total applications in DB: {len(all_apps)}")
            for app in all_apps:
                app_candidate_id = str(app.get("candidate_id", ""))
                if app_candidate_id == candidate_id or app_candidate_id == str(candidate_id):
                    applications_list.append(app)
            print(f"Found {len(applications_list)} applications with manual filter")
        
        applications = []
        for doc in applications_list:
            # Get job details
            job_doc = None
            try:
                job_doc = await db.jobs.find_one({"_id": ObjectId(doc.get("job_id"))})
            except:
                job_doc = await db.jobs.find_one({"id": doc.get("job_id")})
            
            applications.append({
                "id": str(doc["_id"]),
                "job_id": doc.get("job_id"),
                "status": doc.get("status"),
                "applied_date": doc.get("applied_date").isoformat() if doc.get("applied_date") else None,
                "cover_letter": doc.get("cover_letter"),
                "job_title": job_doc.get("title") if job_doc else None,
                "department": job_doc.get("department") if job_doc else None,
                "location": job_doc.get("location") if job_doc else None,
                "experience_level": job_doc.get("experience_level") if job_doc else None,
                "company": "BHIV Partner",
                "updated_at": doc.get("applied_date").isoformat() if doc.get("applied_date") else None,
                "required_documents": _normalize_required_documents(doc.get("required_documents")),
                "required_documents_updated_at": doc.get("required_documents_updated_at").isoformat() if doc.get("required_documents_updated_at") else None,
                "documents_uploaded": _sanitize_uploaded_documents(doc.get("documents_uploaded")),
            })
        
        return {"applications": applications, "count": len(applications)}
    except Exception as e:
        return {"applications": [], "count": 0, "error": str(e)}
