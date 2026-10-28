# Gateway Service Configuration
import os
import logging

# Environment Configuration
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
PYTHON_VERSION = os.getenv("PYTHON_VERSION", "3.12.7")
OBSERVABILITY_ENABLED = os.getenv("OBSERVABILITY_ENABLED", "true").lower() == "true"

# Database Configuration
# Required in production. Outside production it is optional: the gateway and
# its modules degrade to their documented in-memory fallbacks and report
# "storage_mode" honestly, so a missing URI must not abort startup.
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("MONGODB_URI")
if not DATABASE_URL:
    if ENVIRONMENT == "production":
        raise ValueError("DATABASE_URL environment variable is required in production")
    logging.getLogger(__name__).warning(
        "DATABASE_URL/MONGODB_URI not set — running without MongoDB "
        "(in-memory fallbacks). Set it in backend/.env to enable persistence."
    )

# Authentication Configuration - Standardized variable name (see ENVIRONMENT_VARIABLES.md)
API_KEY_SECRET = os.getenv("API_KEY_SECRET")
if not API_KEY_SECRET:
    raise ValueError("API_KEY_SECRET environment variable is required. See ENVIRONMENT_VARIABLES.md")

# JWT Configuration - Standardized variable names (see ENVIRONMENT_VARIABLES.md)
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not JWT_SECRET_KEY:
    raise ValueError("JWT_SECRET_KEY environment variable is required. See ENVIRONMENT_VARIABLES.md")

# Candidate JWT Configuration - Standardized variable name (see ENVIRONMENT_VARIABLES.md)
CANDIDATE_JWT_SECRET_KEY = os.getenv("CANDIDATE_JWT_SECRET_KEY")
if not CANDIDATE_JWT_SECRET_KEY:
    raise ValueError("CANDIDATE_JWT_SECRET_KEY environment variable is required. See ENVIRONMENT_VARIABLES.md")

# Agent Service Configuration
AGENT_SERVICE_URL = os.getenv("AGENT_SERVICE_URL")
if not AGENT_SERVICE_URL:
    raise ValueError("AGENT_SERVICE_URL environment variable is required")

# LangGraph Service Configuration
LANGGRAPH_SERVICE_URL = os.getenv("LANGGRAPH_SERVICE_URL")
if not LANGGRAPH_SERVICE_URL:
    raise ValueError("LANGGRAPH_SERVICE_URL environment variable is required")

# Logging Configuration
def setup_logging():
    """Setup logging based on environment configuration"""
    log_level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    
    # Create logs directory if it doesn't exist
    os.makedirs('logs', exist_ok=True)
    
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('logs/gateway.log'),  # Original path with directory creation
            logging.StreamHandler()
        ]
    )
    
    if ENVIRONMENT == "production":
        # Disable debug logging in production
        logging.getLogger("uvicorn").setLevel(logging.WARNING)
        logging.getLogger("pymongo").setLevel(logging.WARNING)

# Configuration validation
def validate_config():
    """Validate all required configuration is present"""
    required_vars = [
        "API_KEY_SECRET", 
        "CANDIDATE_JWT_SECRET_KEY",
        "AGENT_SERVICE_URL",
        "LANGGRAPH_SERVICE_URL"
    ]
    # MongoDB is mandatory only in production (see DATABASE_URL above).
    if ENVIRONMENT == "production":
        required_vars.insert(0, "DATABASE_URL")
    
    missing_vars = []
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if not JWT_SECRET_KEY:
        missing_vars.append("JWT_SECRET_KEY")
    
    if missing_vars:
        raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")
    
    return True

# Export configuration
__all__ = [
    "ENVIRONMENT",
    "LOG_LEVEL", 
    "PYTHON_VERSION",
    "OBSERVABILITY_ENABLED",
    "DATABASE_URL",
    "API_KEY_SECRET",
    "JWT_SECRET_KEY",
    "CANDIDATE_JWT_SECRET_KEY", 
    "AGENT_SERVICE_URL",
    "LANGGRAPH_SERVICE_URL",
    "setup_logging",
    "validate_config"
]