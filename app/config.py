"""Application configuration.

Values are read from environment variables so the same image can run
locally, in Docker, and on a serverless platform without code changes.
"""
from __future__ import annotations

import os
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Settings:
    """Runtime settings, resolved once at import time."""

    APP_NAME: str = "Geospatial File Measurement API"
    VERSION: str = "1.0.0"

    # Serverless platforms expose a read-only filesystem except for /tmp.
    # Detect that and relocate writable paths accordingly.
    IS_SERVERLESS: bool = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))

    _default_data_dir = "/tmp/geoapi" if IS_SERVERLESS else str(Path.cwd() / "data")
    DATA_DIR: Path = Path(os.getenv("DATA_DIR", _default_data_dir))

    DATABASE_URL: str = os.getenv("DATABASE_URL", "")

    # Upload guards. A geospatial upload is attacker-controlled input:
    # every one of these limits exists to bound work done before validation.
    MAX_UPLOAD_BYTES: int = _int("MAX_UPLOAD_BYTES", 50 * 1024 * 1024)        # 50 MB
    MAX_UNCOMPRESSED_BYTES: int = _int("MAX_UNCOMPRESSED_BYTES", 250 * 1024 * 1024)  # 250 MB
    MAX_ZIP_MEMBERS: int = _int("MAX_ZIP_MEMBERS", 200)
    MAX_FEATURES: int = _int("MAX_FEATURES", 50_000)

    ALLOWED_ORIGINS: list[str] = [
        o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()
    ]

    def resolved_database_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{self.DATA_DIR / 'app.db'}"


settings = Settings()
