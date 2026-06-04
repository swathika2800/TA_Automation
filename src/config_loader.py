"""Project-wide configuration loader.

Reads `config/.env` and `config/settings.json`, exposes a typed settings object
to all modules. Never logs secrets.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / "config" / ".env")


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _float(value: str | None, default: float | None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    naukri_username: str
    naukri_password: str
    litellm_base_url: str
    litellm_api_key: str
    litellm_model: str

    jd_input_dir: Path
    guidelines_dir: Path
    resumes_dir: Path
    outputs_dir: Path
    logs_dir: Path
    session_dir: Path

    headless: bool
    max_resumes_per_jd: int
    download_timeout_sec: int

    raw: dict[str, Any]


def load_settings() -> Settings:
    settings_path = PROJECT_ROOT / "config" / "settings.json"
    raw: dict[str, Any] = {}
    if settings_path.exists():
        raw = json.loads(settings_path.read_text(encoding="utf-8"))

    return Settings(
        naukri_username=os.environ.get("NAUKRI_USERNAME", ""),
        naukri_password=os.environ.get("NAUKRI_PASSWORD", ""),
        litellm_base_url=os.environ.get("LITELLM_BASE_URL", "").rstrip("/"),
        litellm_api_key=os.environ.get("LITELLM_API_KEY", ""),
        litellm_model=os.environ.get("LITELLM_MODEL", "claude-haiku-4-5-20251001"),
        jd_input_dir=PROJECT_ROOT / os.environ.get("JD_INPUT_DIR", "jd_input"),
        guidelines_dir=PROJECT_ROOT / os.environ.get("GUIDELINES_DIR", "screening_guidelines"),
        resumes_dir=PROJECT_ROOT / os.environ.get("RESUMES_DIR", "resumes"),
        outputs_dir=PROJECT_ROOT / os.environ.get("OUTPUTS_DIR", "outputs"),
        logs_dir=PROJECT_ROOT / os.environ.get("LOGS_DIR", "logs"),
        session_dir=Path(os.environ.get("SESSION_DIR", "config/.session")) if Path(os.environ.get("SESSION_DIR", "config/.session")).is_absolute() else PROJECT_ROOT / os.environ.get("SESSION_DIR", "config/.session"),
        headless=_bool(os.environ.get("HEADLESS"), True),
        max_resumes_per_jd=_int(os.environ.get("MAX_RESUMES_PER_JD"), 50),
        download_timeout_sec=_int(os.environ.get("DOWNLOAD_TIMEOUT_SEC"), 60),
        raw=raw,
    )


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger
