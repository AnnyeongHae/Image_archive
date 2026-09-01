from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse


PLATFORM_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PLATFORM_ROOT / ".env"


class DatabaseConfigurationError(RuntimeError):
    """Raised when a usable Postgres connection string is unavailable."""


def _read_env_file(path: Path = ENV_PATH) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def database_dsn(*, env_path: Path = ENV_PATH) -> str:
    """Return the DSN without logging or otherwise exposing it.

    ``DATABASE_URL`` is the production name. ``NEON_DATABASE_KEY`` remains a
    temporary compatibility alias for the current local file.
    """

    file_values = _read_env_file(env_path)
    value = (
        os.environ.get("DATABASE_URL")
        or os.environ.get("NEON_DATABASE_KEY")
        or file_values.get("DATABASE_URL")
        or file_values.get("NEON_DATABASE_KEY")
        or ""
    ).strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise DatabaseConfigurationError(
            "DATABASE_URL is missing or is not a PostgreSQL DSN; "
            "NEON_DATABASE_KEY is accepted only as a temporary local alias"
        )
    return value


def connect(*, dsn: str | None = None, connect_timeout: int = 10):
    try:
        import psycopg2
    except ImportError as exc:  # pragma: no cover - environment gate
        raise DatabaseConfigurationError("psycopg2 is required for Neon operations") from exc
    return psycopg2.connect(dsn or database_dsn(), connect_timeout=connect_timeout)
