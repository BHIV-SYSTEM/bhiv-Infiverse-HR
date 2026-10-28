# Agent Service Configuration
import os
import logging

# Environment Configuration
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
PYTHON_VERSION = os.getenv("PYTHON_VERSION", "3.12.7")
OBSERVABILITY_ENABLED = os.getenv("OBSERVABILITY_ENABLED", "true").lower() == "true"

# Database Configuration
# Required in production. Outside production it is optional: the agent's
# semantic engine logs the missing DB and continues with its fallbacks, so a
# missing URI must not abort startup.
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

# Logging Configuration
def setup_logging():
    """Setup logging based on environment configuration"""
    log_level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler()
        ]
    )
    
    if ENVIRONMENT == "production":
        # Disable debug logging in production
        logging.getLogger("uvicorn").setLevel(logging.WARNING)
        logging.getLogger("transformers").setLevel(logging.WARNING)
        logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)  # Suppress HTTP request logs

# Configuration validation
def validate_config():
    """Validate all required configuration is present"""
    required_vars = [
        "API_KEY_SECRET", 
        "CANDIDATE_JWT_SECRET_KEY"
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
    "setup_logging",
    "validate_config"
]