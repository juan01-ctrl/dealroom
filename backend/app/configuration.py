import os

import psycopg
from psycopg.conninfo import conninfo_to_dict


class ConfigurationError(ValueError):
    """Configuration problems that can be printed without revealing values."""


def validate_database_url(url):
    if not url or not url.startswith(("postgresql://", "postgres://")):
        raise ConfigurationError("DATABASE_URL must be a PostgreSQL connection URI.")
    try:
        conninfo_to_dict(url)
    except psycopg.Error:
        raise ConfigurationError("DATABASE_URL is not a valid PostgreSQL connection URI.") from None


def daily_model_budget(env):
    raw = env.get("LIVE_DAILY_MODEL_CALLS")
    if not raw and env.get("APP_ENV", "local") != "production":
        return None
    if not raw or len(raw) > 10 or not raw.isascii() or not raw.isdecimal():
        raise ConfigurationError("Set LIVE_DAILY_MODEL_CALLS to a positive integer.")
    budget = int(raw)
    if not 0 < budget <= 2_147_483_647:
        raise ConfigurationError("Set LIVE_DAILY_MODEL_CALLS to a positive 32-bit integer.")
    return budget


def validate_configuration(role, env=None):
    values = os.environ if env is None else env
    validate_database_url(values.get("DATABASE_URL"))
    app_env = values.get("APP_ENV", "local")
    mode = values.get("MODEL_MODE", "scripted")
    if app_env not in {"local", "production"}:
        raise ConfigurationError("APP_ENV must be local or production.")
    if mode not in {"scripted", "live"}:
        raise ConfigurationError("MODEL_MODE must be scripted or live.")
    if app_env == "production":
        if mode != "live":
            raise ConfigurationError("Production requires MODEL_MODE=live.")
        if role == "api":
            if values.get("COOKIE_SECURE") != "1":
                raise ConfigurationError("Production requires COOKIE_SECURE=1.")
            secret = values.get("API_PROXY_SECRET", "")
            if len(secret) < 32 or not secret.isascii():
                raise ConfigurationError("Set an ASCII API_PROXY_SECRET of at least 32 characters.")
    if role == "worker" and mode == "live":
        daily_model_budget(values)
        provider = values.get("MODEL_PROVIDER")
        allowed = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
        if provider not in allowed:
            raise ConfigurationError("MODEL_PROVIDER must be openai or anthropic.")
        if not values.get("MODEL_NAME", "").strip():
            raise ConfigurationError("Set MODEL_NAME in the worker environment.")
        key_name = allowed[provider]
        if not values.get(key_name, "").strip():
            raise ConfigurationError(f"Set {key_name} in the worker environment.")
    return mode
