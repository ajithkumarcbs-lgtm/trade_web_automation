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
    ai_provider: str  # groq | openai
    groq_api_key: str
    groq_model: str

    alerts: bool
    alert_chat: str | int  # "me" = your Telegram Saved Messages
    auto_bookset_units: float  # book automatically once locked profit reaches this many units; 0 = off
    bookset_green_wait_s: int  # a bookset (not a loss cut) waits this long for the Cashout to be green
    book_only_green: bool  # never book at a loss: booksets AND loss cuts wait for green, else the trade runs on
    bookset_retry_min: int  # keep retrying a bookset / loss cut the site refuses for this long
    entry_retry_min: int  # keep retrying an entry / limit exit the site can't take yet for this long
    monitor_interval_s: int
    keepalive_min: int
    heartbeat_hours: float  # "bot is running" message to your Telegram every N hours; 0 = off

    site_url: str
    site_username: str
    site_password: str
    headless: bool
    browser_channel: str

    dry_run: bool
    unit_stake: float
    max_units_per_bet: float  # the limits below are off when 0
    max_bets_per_day: int
    max_daily_exposure: float
    max_liability_per_bet: float
    min_odds: float
    max_odds: float
    odds_tolerance: float
    odds_tolerance_abs: float  # e.g. 0.4: accept a back down to tip - 0.4; 0 = use the percentage instead
    ignore_slippage: bool  # testing only: never applies to live bets
    min_confidence: float
    signal_max_age_s: int
    duplicate_window_min: int

    # Added with channels.json; defaults keep older .env files working.
    max_back_odds: float = 0  # a back above these odds is placed as a lay of the opponent (same result); 0 = off
    conditional_entry_min: int = 90  # "3 odds pona edunga": wait this long for the price, then report it missed
    daily_stop_units: float = 0  # no new entries once today's result reaches -N units; 0 = off
    weekly_stop_units: float = 0
    monthly_stop_units: float = 0
    confirm_timeout_s: int = 90  # a tip waiting for your /yes is dropped after this long
    transcribe_model: str = "gpt-4o-transcribe"  # voice notes -> text (OpenAI)
    confirm_in_dry_run: bool = False  # paper trading asks /yes too (else it goes ahead, noting live would ask)

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
        ai_provider=_str("AI_PROVIDER", "openai").lower(),
        groq_api_key=_str("GROQ_API_KEY"),
        groq_model=_str("GROQ_MODEL", "llama-3.3-70b-versatile"),
        alerts=_bool("ALERTS", True),
        alert_chat=_chat(_str("ALERT_CHAT", "me")),
        auto_bookset_units=_float("AUTO_BOOKSET_UNITS", 0),
        bookset_green_wait_s=_int("BOOKSET_GREEN_WAIT_SECONDS", 120),
        book_only_green=_bool("BOOK_ONLY_GREEN", False),
        bookset_retry_min=_int("BOOKSET_RETRY_MINUTES", 10),
        entry_retry_min=_int("ENTRY_RETRY_MINUTES", 5),
        monitor_interval_s=_int("MONITOR_SECONDS", 20),
        keepalive_min=_int("KEEPALIVE_MINUTES", 4),
        heartbeat_hours=_float("HEARTBEAT_HOURS", 6),
        site_url=_str("SITE_URL", "https://www.iob7.com").rstrip("/"),
        site_username=_str("SITE_USERNAME"),
        site_password=_str("SITE_PASSWORD"),
        headless=_bool("HEADLESS", False),
        browser_channel=_str("BROWSER_CHANNEL", "chrome"),
        dry_run=_bool("DRY_RUN", True),
        unit_stake=_float("UNIT_STAKE", _float("STAKE", 100)),  # STAKE: the setting's name before units
        max_units_per_bet=_float("MAX_UNITS_PER_BET", 5),
        max_bets_per_day=_int("MAX_BETS_PER_DAY", 0),
        max_daily_exposure=_float("MAX_DAILY_EXPOSURE", 0),
        max_liability_per_bet=_float("MAX_LIABILITY_PER_BET", 0),
        min_odds=_float("MIN_ODDS", 1.01),
        max_odds=_float("MAX_ODDS", 1000),
        odds_tolerance=_float("ODDS_TOLERANCE", 0.05),
        odds_tolerance_abs=_float("ODDS_TOLERANCE_ABS", 0),
        ignore_slippage=_bool("IGNORE_SLIPPAGE", False),
        min_confidence=_float("MIN_CONFIDENCE", 0.7),
        signal_max_age_s=_int("SIGNAL_MAX_AGE_SECONDS", 90),
        duplicate_window_min=_int("DUPLICATE_WINDOW_MINUTES", 30),
        max_back_odds=_float("MAX_BACK_ODDS", 0),
        conditional_entry_min=_int("CONDITIONAL_ENTRY_MINUTES", 90),
        daily_stop_units=_float("DAILY_STOP_UNITS", 0),
        weekly_stop_units=_float("WEEKLY_STOP_UNITS", 0),
        monthly_stop_units=_float("MONTHLY_STOP_UNITS", 0),
        confirm_timeout_s=_int("CONFIRM_SECONDS", 90),
        transcribe_model=_str("TRANSCRIBE_MODEL", "gpt-4o-transcribe"),
        confirm_in_dry_run=_bool("CONFIRM_IN_DRY_RUN", False),
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
    "groq_api_key": "GROQ_API_KEY",
    "site_url": "SITE_URL",
    "site_username": "SITE_USERNAME",
    "site_password": "SITE_PASSWORD",
}
