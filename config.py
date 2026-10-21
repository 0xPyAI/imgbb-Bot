"""Shared configuration loader for production and local environments.

Supports loading from .env or config.env, with real OS environment variables
taking highest priority (ideal for Docker / Kubernetes / Cloud deployments).
Includes alias fallbacks for backwards compatibility and provides explicit
type casting and fail-fast validation at startup.
"""
import logging
import os
from pathlib import Path
from typing import Any, List, Optional

from dotenv import dotenv_values

logger = logging.getLogger(__name__)

# Project directory, so config files and the session are found from any cwd
BASE_DIR = Path(__file__).resolve().parent

# Load file values (.env takes precedence over config.env if both exist)
_env_files = [BASE_DIR / ".env", BASE_DIR / "config.env"]
_file_values = {}
for env_file in _env_files:
    if env_file.is_file():
        # Merge values; files loaded earlier have higher priority. Without
        # interpolation a value is read as written, as Docker Compose and
        # systemd read single-quoted values, so "${" in a password survives.
        loaded = dotenv_values(env_file, interpolate=False)
        for k, v in loaded.items():
            if k not in _file_values and v is not None:
                _file_values[k] = v


def get(
    key: str,
    default: Any = None,
    required: bool = False,
    aliases: Optional[List[str]] = None,
) -> Optional[str]:
    """Return a configuration string value from env vars, file, or default.

    Checks:
      1. Real environment variables (OS environ) for `key` and any `aliases`
      2. File values (.env / config.env) for `key` and any `aliases`
      3. `default`
    """
    candidate_keys = [key] + (aliases or [])

    # 1. OS environment variables (highest priority)
    for k in candidate_keys:
        val = os.environ.get(k)
        if val is not None and val.strip() != "":
            return val.strip()

    # 2. File values (.env / config.env)
    for k in candidate_keys:
        val = _file_values.get(k)
        if val is not None and val.strip() != "":
            return val.strip()

    # 3. Default fallback
    if default is not None:
        return default

    if required:
        keys_str = " or ".join(f"'{k}'" for k in candidate_keys)
        raise RuntimeError(
            f"Missing required configuration key: {keys_str}. "
            f"Please set it in your .env / config.env file or as an environment variable."
        )

    return None


def get_int(
    key: str,
    default: Optional[int] = None,
    required: bool = False,
    aliases: Optional[List[str]] = None,
) -> Optional[int]:
    """Return a configuration value cast to integer."""
    val = get(key, default=None, required=required, aliases=aliases)
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        keys_str = " / ".join([key] + (aliases or []))
        raise RuntimeError(
            f"Configuration key '{keys_str}' must be an integer, got: {val!r}"
        ) from None


def get_bool(
    key: str,
    default: bool = False,
    aliases: Optional[List[str]] = None,
) -> bool:
    """Return a configuration value cast to boolean."""
    val = get(key, default=None, aliases=aliases)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes", "y", "on")


# ==========================================
# Typed Configuration Accessors / Properties
# ==========================================
class Settings:
    """Centralized typed application settings."""

    @property
    def API_ID(self) -> int:
        return get_int("API_ID", aliases=["YOUR_API_ID"], required=True)

    @property
    def API_HASH(self) -> str:
        return get("API_HASH", aliases=["YOUR_API_HASH"], required=True)

    @property
    def BOT_TOKEN(self) -> str:
        return get("BOT_TOKEN", aliases=["token"], required=True)

    @property
    def DATABASE_URL(self) -> str:
        raw = get("DATABASE_URL", aliases=["grespost1"], required=True)
        if raw.startswith("postgres://"):
            return raw.replace("postgres://", "postgresql+asyncpg://", 1)
        if raw.startswith("postgresql://"):
            return raw.replace("postgresql://", "postgresql+asyncpg://", 1)
        return raw

    @property
    def FERNET_KEY(self) -> str:
        return get("FERNET_KEY", required=True)

    @property
    def LOG_LEVEL(self) -> str:
        return get("LOG_LEVEL", default="INFO").upper()

    @property
    def MAX_CONCURRENT_DOWNLOADS(self) -> int:
        return max(1, get_int("MAX_CONCURRENT_DOWNLOADS", default=4))

    @property
    def DOWNLOAD_TIMEOUT(self) -> int:
        # 0 or less would make every download time out at once
        return max(1, get_int("DOWNLOAD_TIMEOUT", default=60))

    @property
    def SESSION_DIR(self) -> str:
        """Directory that holds the Pyrogram bot.session file."""
        return get("SESSION_DIR", default=str(BASE_DIR))

    @property
    def HEARTBEAT_FILE(self) -> Optional[str]:
        """File touched while the bot runs, for container health checks (off if unset)."""
        return get("HEARTBEAT_FILE", default=None)

    @property
    def SENTRY_DSN(self) -> Optional[str]:
        return get("SENTRY_DSN", default=None)


settings = Settings()
