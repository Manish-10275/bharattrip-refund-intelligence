import os
from pathlib import Path
from typing import Any
from dotenv import load_dotenv

# Load .env before reading environment-based configuration.
# override=False guarantees existing process environment variables have higher priority than .env values.
project_root = Path(__file__).resolve().parent
dotenv_path = project_root / ".env"
if dotenv_path.exists():
    load_dotenv(dotenv_path=dotenv_path, override=False)
else:
    load_dotenv(override=False)


def parse_bool(val: Any, default: bool = False) -> bool:
    """
    Safely parse boolean environment variable string.
    Ensures that strings like "False", "false", "0" resolve to False and never become Python True.
    """
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("true", "1", "t", "yes", "y", "on"):
        return True
    if s in ("false", "0", "f", "no", "n", "off", ""):
        return False
    return default


def is_testing_environment() -> bool:
    """
    Return True if explicitly identified as an automated testing environment.
    Only explicit environment configuration activates test mode.
    Presence of unittest or other modules in sys.modules never activates test mode.
    """
    if parse_bool(os.environ.get("TESTING"), default=False):
        return True
    if os.environ.get("FLASK_ENV", "").strip().lower() == "testing":
        return True
    return False


def get_secret_key() -> str:
    """
    Load SECRET_KEY from the environment.
    - If present, returns the configured secret key.
    - If absent in an explicitly identified testing environment, returns a dedicated test secret.
    - If absent in a non-testing environment, raises a clear RuntimeError.
    - Never silently generates a random production secret or uses a weak hardcoded fallback.
    """
    key = os.environ.get("SECRET_KEY")
    if key and key.strip():
        return key.strip()

    if is_testing_environment():
        return "bharattrip-test-only-secret-key-not-for-production"

    raise RuntimeError(
        "FATAL: SECRET_KEY environment variable is not set. "
        "A secure, persistent SECRET_KEY is required for application startup in non-testing environments. "
        "Generate a key with: python -c \"import secrets; print(secrets.token_hex(32))\" "
        "and configure it in your environment or .env file."
    )


class Config:
    """Application configuration for BharatTrip Refund Intelligence."""
    APP_NAME = os.environ.get("APP_NAME", "BharatTrip Refund Intelligence")

    # Secret Key evaluated via get_secret_key()
    SECRET_KEY = get_secret_key()

    @classmethod
    def load_secret_key(cls) -> str:
        """Retrieve the validated secret key."""
        return get_secret_key()

    # Operator Authentication Credentials (from environment only)
    OPERATOR_USERNAME = os.environ.get("OPERATOR_USERNAME", "")
    OPERATOR_PASSWORD_HASH = os.environ.get("OPERATOR_PASSWORD_HASH", "")

    # Security & Session Cookie Hardening
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = parse_bool(os.environ.get("SESSION_COOKIE_SECURE"), default=False)
    PERMANENT_SESSION_LIFETIME = 3600

    # CSRF Protection (Flask-WTF)
    WTF_CSRF_ENABLED = parse_bool(os.environ.get("WTF_CSRF_ENABLED"), default=True)
    WTF_CSRF_TIME_LIMIT = 3600

    DATABASE = os.environ.get("DATABASE", "bharattrip.db")
    DEBUG = parse_bool(os.environ.get("FLASK_DEBUG"), default=False)
    HOST = os.environ.get("FLASK_RUN_HOST", "127.0.0.1")
    PORT = int(os.environ.get("FLASK_RUN_PORT", 5000))

    # Groq AI Intake Service Configuration
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
    GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")
    GROQ_REQUEST_TIMEOUT = float(os.environ.get("GROQ_REQUEST_TIMEOUT", "30.0"))
