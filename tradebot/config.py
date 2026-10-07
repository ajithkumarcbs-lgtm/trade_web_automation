"""Settings loaded from the .env file in the project root."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SAMPLES_DIR = ROOT / "samples"

load_dotenv(ROOT / ".env")


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _float(name: str, default: float) -> float:
    return float(_str(name) or default)


def _int(name: str, default: int) -> int:
    return int(_str(name) or default)


def _bool(name: str, default: bool) -> bool:
    return (_str(name) or str(default)).lower() in {"1", "true", "yes", "on"}


def _chat(value: str) -> str | int:
    # Numeric ids (e.g. -1001234567890) must be passed to Telethon as int.
    return int(value) if value.lstrip("-").isdigit() else value


@dataclass(frozen=True)
class Settings:
    tg_api_id: int
    tg_api_hash: str
    tg_phone: str
    tg_chat: str | int

    openai_api_key: str
    openai_model: str
    openai_image_detail: str

    site_url: str
    site_username: str
    site_password: str
    headless: bool
    browser_channel: str

    dry_run: bool
    stake: float
    max_bets_per_day: int
    max_daily_exposure: float
    max_liability_per_bet: float
    min_odds: float
    max_odds: float
    odds_tolerance: float
    min_confidence: float
    signal_max_age_s: int
    duplicate_window_min: int

    def require(self, *names: str) -> None:
        """Exit with a clear message if any of the named settings is empty."""
        missing = [ENV_NAMES[n] for n in names if getattr(self, n) in ("", 0, None)]
        if missing:
            raise SystemExit(f"Missing settings in .env: {', '.join(missing)} (see .env.example)")


def load_settings() -> Settings:
    settings = Settings(
        tg_api_id=_int("TELEGRAM_API_ID", 0),
        tg_api_hash=_str("TELEGRAM_API_HASH"),
        tg_phone=_str("TELEGRAM_PHONE"),
        tg_chat=_chat(_str("TELEGRAM_CHAT")),
        openai_api_key=_str("OPENAI_API_KEY"),
        openai_model=_str("OPENAI_MODEL", "gpt-5.4-mini"),
        openai_image_detail=_str("OPENAI_IMAGE_DETAIL", "high"),
        site_url=_str("SITE_URL", "https://www.iob7.com").rstrip("/"),
        site_username=_str("SITE_USERNAME"),
        site_password=_str("SITE_PASSWORD"),
        headless=_bool("HEADLESS", False),
        browser_channel=_str("BROWSER_CHANNEL", "chrome"),
        dry_run=_bool("DRY_RUN", True),
        stake=_float("STAKE", 100),
        max_bets_per_day=_int("MAX_BETS_PER_DAY", 20),
        max_daily_exposure=_float("MAX_DAILY_EXPOSURE", 2000),
        max_liability_per_bet=_float("MAX_LIABILITY_PER_BET", 300),
        min_odds=_float("MIN_ODDS", 1.10),
        max_odds=_float("MAX_ODDS", 5.0),
        odds_tolerance=_float("ODDS_TOLERANCE", 0.05),
        min_confidence=_float("MIN_CONFIDENCE", 0.7),
        signal_max_age_s=_int("SIGNAL_MAX_AGE_SECONDS", 90),
        duplicate_window_min=_int("DUPLICATE_WINDOW_MINUTES", 30),
    )
    DATA_DIR.mkdir(exist_ok=True)
    return settings


TELEGRAM_FIELDS = ("tg_api_id", "tg_api_hash", "tg_phone")
SITE_FIELDS = ("site_url", "site_username", "site_password")
ENV_NAMES = {
    "tg_api_id": "TELEGRAM_API_ID",
    "tg_api_hash": "TELEGRAM_API_HASH",
    "tg_phone": "TELEGRAM_PHONE",
    "tg_chat": "TELEGRAM_CHAT",
    "openai_api_key": "OPENAI_API_KEY",
    "site_url": "SITE_URL",
    "site_username": "SITE_USERNAME",
    "site_password": "SITE_PASSWORD",
}
