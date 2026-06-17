"""Central configuration. Loads `.env` and exposes typed constants.

Nothing else in the codebase should read environment variables directly — import from here.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# --- Paths ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
PDF_DIR = DATA_DIR / "pdfs"
LOGS_DIR = PROJECT_ROOT / "logs"
DB_PATH = DATA_DIR / "state.db"

# Ensure runtime dirs exist (safe to call repeatedly).
for _d in (PDF_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Load .env from project root (no-op if missing; values may also come from real env).
load_dotenv(PROJECT_ROOT / ".env")


def _get_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logging.getLogger(__name__).warning(
            "Invalid float for %s=%r; using default %s", key, raw, default
        )
        return default


# --- Anthropic ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
EXTRACTION_MODEL = os.getenv("EXTRACTION_MODEL", "claude-haiku-4-5")
EXTRACTION_CONFIDENCE_THRESHOLD = _get_float("EXTRACTION_CONFIDENCE_THRESHOLD", 0.75)

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- JBTC TCMS portal ---
TCMS_BASE_URL = os.getenv("TCMS_BASE_URL", "")
TCMS_USERNAME = os.getenv("TCMS_USERNAME", "")
TCMS_PASSWORD = os.getenv("TCMS_PASSWORD", "")

# --- Synergix ERP ---
SYNERGIX_BASE_URL = os.getenv("SYNERGIX_BASE_URL", "")
SYNERGIX_USERNAME = os.getenv("SYNERGIX_USERNAME", "")
SYNERGIX_PASSWORD = os.getenv("SYNERGIX_PASSWORD", "")
SYNERGIX_TEMPLATE_QUO_ID = os.getenv("SYNERGIX_TEMPLATE_QUO_ID", "")

# --- Behaviour ---
DRY_RUN = _get_bool("DRY_RUN", True)          # defaults to True — never default to live
HEADLESS = _get_bool("HEADLESS", False)
TIMEZONE = os.getenv("TIMEZONE", "Asia/Singapore")

# Default timeout (ms) for Playwright waits. D365/Synergix can be slow to render.
PLAYWRIGHT_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "30000"))


def configure_logging() -> None:
    """Configure stdlib logging: console + a per-process file in logs/."""
    log_file = LOGS_DIR / "run.log"
    fmt = "%(asctime)s %(levelname)-7s %(name)s :: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    except OSError:
        pass  # console-only if file handler can't open
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


def summary() -> str:
    """Human-readable config summary for startup logs (no secrets)."""
    return (
        f"DRY_RUN={DRY_RUN} HEADLESS={HEADLESS} TZ={TIMEZONE} "
        f"model={EXTRACTION_MODEL} conf_threshold={EXTRACTION_CONFIDENCE_THRESHOLD} "
        f"db={DB_PATH}"
    )
