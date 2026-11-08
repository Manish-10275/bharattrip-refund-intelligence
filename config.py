import os
from pathlib import Path
from typing import Any, Optional
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


def get_operator_username() -> str:
    """
    Load OPERATOR_USERNAME from the environment.
    - If present, returns the configured operator username.
    - If absent in an explicitly identified testing environment, returns a default test operator username ('operator').
    - If absent in a non-testing environment, raises a clear RuntimeError.
    """
    user = os.environ.get("OPERATOR_USERNAME")
    if user and user.strip():
        return user.strip()

    if is_testing_environment():
        return "operator"

    raise RuntimeError(
        "FATAL: OPERATOR_USERNAME environment variable is not set. "
        "A designated operator username is required for application startup in non-testing environments. "
        "Configure OPERATOR_USERNAME in your environment or .env file."
    )


def get_operator_password_hash() -> str:
    """
    Load OPERATOR_PASSWORD_HASH from the environment.
    - If present, returns the configured password hash.
    - If absent in an explicitly identified testing environment, returns an empty string (tests configure test hashes).
    - If absent in a non-testing environment, raises a clear RuntimeError.
    """
    p_hash = os.environ.get("OPERATOR_PASSWORD_HASH")
    if p_hash and p_hash.strip():
        return p_hash.strip()

    if is_testing_environment():
        return ""

    raise RuntimeError(
        "FATAL: OPERATOR_PASSWORD_HASH environment variable is not set. "
        "A secure password hash is required for application startup in non-testing environments. "
        "Generate a password hash with: python -c \"from werkzeug.security import generate_password_hash; print(generate_password_hash('YourPassword'))\" "
        "and configure OPERATOR_PASSWORD_HASH in your environment or .env file."
    )


def get_session_cookie_secure() -> bool:
    """
    Determine SESSION_COOKIE_SECURE.
    - Defaults to True in production (non-testing) environments.
    - Defaults to False in testing environments.
    - Respects explicit boolean string values from SESSION_COOKIE_SECURE environment variable.
    """
    raw = os.environ.get("SESSION_COOKIE_SECURE")
    if raw is None or not str(raw).strip():
        return not is_testing_environment()
    return parse_bool(raw, default=not is_testing_environment())


def normalize_database_url(url: Optional[str]) -> str:
    """
    Normalize DATABASE_URL for PostgreSQL (psycopg 3) and Neon pooled connections.
    - Converts postgres:// to postgresql://
    - Enforces sslmode=require for Neon hosted endpoints if missing.
    """
    if not url or not str(url).strip():
        return ""
    u = str(url).strip()
    if u.startswith("postgres://"):
        u = "postgresql://" + u[len("postgres://"):]
    if "neon.tech" in u and "sslmode=" not in u:
        sep = "&" if "?" in u else "?"
        u = f"{u}{sep}sslmode=require"
    return u


def get_database_url() -> str:
    """Retrieve normalized DATABASE_URL from environment."""
    return normalize_database_url(os.environ.get("DATABASE_URL"))


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
    OPERATOR_USERNAME = get_operator_username()
    OPERATOR_PASSWORD_HASH = get_operator_password_hash()

    @classmethod
    def load_operator_username(cls) -> str:
        """Retrieve the validated operator username."""
        return get_operator_username()

    @classmethod
    def load_operator_password_hash(cls) -> str:
        """Retrieve the validated operator password hash."""
        return get_operator_password_hash()

    # Security & Session Cookie Hardening
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = get_session_cookie_secure()
    PERMANENT_SESSION_LIFETIME = 3600

    @classmethod
    def load_session_cookie_secure(cls) -> bool:
        """Retrieve the validated session cookie secure setting."""
        return get_session_cookie_secure()

    # CSRF Protection (Flask-WTF)
    WTF_CSRF_ENABLED = parse_bool(os.environ.get("WTF_CSRF_ENABLED"), default=True)
    WTF_CSRF_TIME_LIMIT = 3600

    DATABASE = os.environ.get("DATABASE", "bharattrip.db")
    DATABASE_URL = get_database_url()

    @classmethod
    def load_database_url(cls) -> str:
        """Retrieve the normalized DATABASE_URL."""
        return get_database_url()

    DEBUG = parse_bool(os.environ.get("FLASK_DEBUG"), default=False)
    HOST = os.environ.get("FLASK_RUN_HOST", "0.0.0.0" if os.environ.get("RENDER") else "127.0.0.1")
    PORT = int(os.environ.get("PORT", os.environ.get("FLASK_RUN_PORT", 5000)))

    # Groq AI Intake Service Configuration
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
    GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")
    GROQ_REQUEST_TIMEOUT = float(os.environ.get("GROQ_REQUEST_TIMEOUT", "30.0"))
