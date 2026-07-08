"""Central configuration. Loads `.env` into a typed `Settings` class.

Nothing else in the codebase should read environment variables directly — import from here.

A single instance is created as `settings` (alias of `get_settings()`), and the most-used
fields are re-exported as module-level constants so existing `settings.ATTR` access keeps
working. New code may prefer `from config.settings import settings` and `settings.field`.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Paths (static; not env-driven) ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
PDF_DIR = DATA_DIR / "pdfs"
INCOMING_EMAIL_DIR = DATA_DIR / "incoming_emails"  # .eml files pulled from the IMAP mailbox
TCMS_SESSION_DIR = PROJECT_ROOT / ".tcms_session"  # persisted D365 browser session (login cookies)
LOGS_DIR = PROJECT_ROOT / "logs"
DB_PATH = DATA_DIR / "state.db"

# Ensure runtime dirs exist (safe to call repeatedly).
for _d in (PDF_DIR, INCOMING_EMAIL_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    """Typed application settings loaded from environment / `.env`."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # --- Anthropic ---
    ANTHROPIC_API_KEY: str = ""
    EXTRACTION_MODEL: str = "claude-haiku-4-5"
    # Extractions below this confidence (0..1) are flagged for manual verification.
    EXTRACTION_CONFIDENCE_THRESHOLD: float = Field(default=0.75, ge=0.0, le=1.0)

    # --- Telegram ---
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # --- Email intake (IMAP) ---
    # A dedicated mailbox WE control. The client auto-forwards Work Order emails here; we poll it,
    # save each new message as .eml into data/incoming_emails/, and feed it into the ingest funnel.
    IMAP_HOST: str = ""                 # e.g. "imap.gmail.com" / "outlook.office365.com"
    IMAP_PORT: int = 993               # 993 = IMAP over SSL (the normal case)
    IMAP_USERNAME: str = ""            # the mailbox login (often the full email address)
    IMAP_PASSWORD: str = ""            # an APP PASSWORD, not the main account password (Gmail/M365)
    IMAP_MAILBOX: str = "INBOX"        # folder to read (e.g. "INBOX" or a label/sub-folder)
    # Optional server-side filters (IMAP SEARCH). Leave blank to take every unseen message.
    IMAP_FROM_FILTER: str = ""         # only fetch mail FROM this address/substring, e.g. "@sktc.sg"
    IMAP_SUBJECT_FILTER: str = ""      # only fetch mail whose SUBJECT contains this, e.g. "Work Order"
    # If true, mark fetched messages \Seen so they aren't pulled again. If false, dedup is by Message-ID.
    IMAP_MARK_SEEN: bool = True

    # --- JBTC TCMS portal ---
    TCMS_BASE_URL: str = ""
    TCMS_USERNAME: str = ""
    TCMS_PASSWORD: str = ""

    # --- Synergix ERP ---
    SYNERGIX_BASE_URL: str = ""
    SYNERGIX_USERNAME: str = ""
    SYNERGIX_PASSWORD: str = ""
    SYNERGIX_TEMPLATE_QUO_ID: str = ""
    # Which WO field to search Synergix on when checking "already invoiced?". The JBTC un-invoiced
    # list is maintained by hand and can be stale, so this check is what actually prevents double
    # billing. "wo_po" | "job_sheet" — confirm against the real Synergix records.
    SYNERGIX_DEDUP_KEY: str = "wo_po"

    # --- Behaviour ---
    DRY_RUN: bool = True          # defaults to True — never default to live
    # DEV ONLY. When Synergix is not configured, the dedup check can't verify invoiced status, so it
    # returns UNCERTAIN (fail-safe) and WOs land in NEEDS_REVIEW. Set true to instead assume
    # NOT_DUPLICATE in stub mode, so the approval flow can be demoed locally. NEVER true with real
    # billing — it defeats the double-invoice guard.
    DEDUP_STUB_ASSUME_SAFE: bool = False
    HEADLESS: bool = False
    TIMEZONE: str = "Asia/Singapore"
    # Default timeout (ms) for Playwright waits. D365/Synergix can be slow to render.
    PLAYWRIGHT_TIMEOUT_MS: int = 30000

    # --- Paths (exposed on the instance for convenience) ---
    @property
    def PROJECT_ROOT(self) -> Path:
        return PROJECT_ROOT

    @property
    def DATA_DIR(self) -> Path:
        return DATA_DIR

    @property
    def PDF_DIR(self) -> Path:
        return PDF_DIR

    @property
    def INCOMING_EMAIL_DIR(self) -> Path:
        return INCOMING_EMAIL_DIR

    @property
    def TCMS_SESSION_DIR(self) -> Path:
        return TCMS_SESSION_DIR

    @property
    def LOGS_DIR(self) -> Path:
        return LOGS_DIR

    @property
    def DB_PATH(self) -> Path:
        return DB_PATH

    @field_validator("EXTRACTION_CONFIDENCE_THRESHOLD", mode="before")
    @classmethod
    def _empty_threshold_to_default(cls, v: object) -> object:
        # Treat an empty/blank env value as "unset" so the default applies.
        if isinstance(v, str) and v.strip() == "":
            return 0.75
        return v

    def configure_logging(self) -> None:
        """Configure stdlib logging: console + a per-process file in logs/."""
        log_file = LOGS_DIR / "run.log"
        fmt = "%(asctime)s %(levelname)-7s %(name)s :: %(message)s"
        handlers: list[logging.Handler] = [logging.StreamHandler()]
        try:
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError:
            pass  # console-only if file handler can't open
        logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)

    def summary(self) -> str:
        """Human-readable config summary for startup logs (no secrets)."""
        return (
            f"DRY_RUN={self.DRY_RUN} HEADLESS={self.HEADLESS} TZ={self.TIMEZONE} "
            f"model={self.EXTRACTION_MODEL} "
            f"conf_threshold={self.EXTRACTION_CONFIDENCE_THRESHOLD} "
            f"db={DB_PATH}"
        )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings singleton."""
    return Settings()


# Process-wide instance. Import this for the class-based API: `settings.DRY_RUN`.
settings = get_settings()

# --- Backwards-compatible module-level constants ---
# Existing code does `from config import settings; settings.ANTHROPIC_API_KEY`.
# Those resolve to the instance below via the module's `settings` name, but several
# modules also expect these as plain module attributes, so we mirror them here.
ANTHROPIC_API_KEY = settings.ANTHROPIC_API_KEY
EXTRACTION_MODEL = settings.EXTRACTION_MODEL
EXTRACTION_CONFIDENCE_THRESHOLD = settings.EXTRACTION_CONFIDENCE_THRESHOLD

TELEGRAM_BOT_TOKEN = settings.TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID = settings.TELEGRAM_CHAT_ID

IMAP_HOST = settings.IMAP_HOST
IMAP_PORT = settings.IMAP_PORT
IMAP_USERNAME = settings.IMAP_USERNAME
IMAP_PASSWORD = settings.IMAP_PASSWORD
IMAP_MAILBOX = settings.IMAP_MAILBOX
IMAP_FROM_FILTER = settings.IMAP_FROM_FILTER
IMAP_SUBJECT_FILTER = settings.IMAP_SUBJECT_FILTER
IMAP_MARK_SEEN = settings.IMAP_MARK_SEEN

TCMS_BASE_URL = settings.TCMS_BASE_URL
TCMS_USERNAME = settings.TCMS_USERNAME
TCMS_PASSWORD = settings.TCMS_PASSWORD

SYNERGIX_BASE_URL = settings.SYNERGIX_BASE_URL
SYNERGIX_USERNAME = settings.SYNERGIX_USERNAME
SYNERGIX_PASSWORD = settings.SYNERGIX_PASSWORD
SYNERGIX_TEMPLATE_QUO_ID = settings.SYNERGIX_TEMPLATE_QUO_ID
SYNERGIX_DEDUP_KEY = settings.SYNERGIX_DEDUP_KEY

DRY_RUN = settings.DRY_RUN
DEDUP_STUB_ASSUME_SAFE = settings.DEDUP_STUB_ASSUME_SAFE
HEADLESS = settings.HEADLESS
TIMEZONE = settings.TIMEZONE
PLAYWRIGHT_TIMEOUT_MS = settings.PLAYWRIGHT_TIMEOUT_MS


def configure_logging() -> None:
    """Module-level shim delegating to the Settings instance."""
    settings.configure_logging()


def summary() -> str:
    """Module-level shim delegating to the Settings instance."""
    return settings.summary()
