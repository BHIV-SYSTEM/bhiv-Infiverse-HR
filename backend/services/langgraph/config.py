from pydantic_settings import BaseSettings
from functools import lru_cache
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class Settings(BaseSettings):
    # API Configuration - Environment Variables
    gateway_service_url: str = "http://localhost:8000"
    gateway_url: str = "http://localhost:8000"  # Alias for tools.py
    api_key_secret: str = "<API_KEY_SECRET>"
    
    # Service URLs
    langgraph_service_url: str = "http://localhost:9001"
    
    # Database
    # Empty by default: an unset DATABASE_URL must mean "no MongoDB", not a
    # placeholder string that later gets resolved as a hostname
    # ("<DATABASE_URL>:27017 getaddrinfo failed"). Set it in backend/.env.
    database_url: str = ""
    
    # JWT Secrets
    jwt_secret_key: str = "<JWT_SECRET_KEY>"
    candidate_jwt_secret_key: str = "<CANDIDATE_JWT_SECRET_KEY>"
    
    # Gemini AI
    gemini_api_key: str = "<GEMINI_API_KEY>"
    gemini_model: str = "gemini-pro"
    
    # Twilio
    twilio_account_sid: str = "<TWILIO_ACCOUNT_SID>"
    twilio_auth_token: str = "<TWILIO_AUTH_TOKEN>"
    twilio_whatsapp_number: str = "+14155238886"
    
    # Gmail
    gmail_email: str = "<GMAIL_EMAIL>"
    gmail_app_password: str = "<GMAIL_APP_PASSWORD>"
    
    # Telegram
    telegram_bot_token: str = "<TELEGRAM_BOT_TOKEN>"
    telegram_bot_username: str = "<TELEGRAM_BOT_USERNAME>"
    
    # Environment
    environment: str = "production"
    log_level: str = "INFO"
    
    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"  # Ignore extra fields from .env

@lru_cache()
def get_settings():
    return Settings()

settings = get_settings()

# Credential validation for production
if settings.environment == "production":
    required = [
        "gemini_api_key",
        "twilio_account_sid",
        "gmail_email",
        "telegram_bot_token"
    ]
    for req in required:
        if not getattr(settings, req):
            logger.warning(f"⚠️ Missing {req} - some features will be unavailable")