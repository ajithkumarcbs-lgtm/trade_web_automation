import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradebot.config import Settings  # noqa: E402
from tradebot.models import BetSignal  # noqa: E402
from tradebot.store import Store  # noqa: E402


def make_settings(**overrides) -> Settings:
    values = dict(
        tg_api_id=1, tg_api_hash="x", tg_phone="+0", tg_chat=-1,
        openai_api_key="test", openai_model="test", openai_image_detail="high",
        site_url="https://example.invalid", site_username="u", site_password="p", headless=True,
        browser_channel="chrome", dry_run=True, unit_stake=1000, max_units_per_bet=5, max_bets_per_day=0,
        max_daily_exposure=0, max_liability_per_bet=0, min_odds=1.01, max_odds=1000, odds_tolerance=0.05,
        min_confidence=0.7, signal_max_age_s=90, duplicate_window_min=30,
    )
    values.update(overrides)
    return Settings(**values)


def signal(**overrides) -> BetSignal:
    values = dict(action="entry", sport="tennis", event="Florian Broska v Lukas Neumayer", market="match_odds",
                  market_name="", selection="Florian Broska", side="back", odds=2.9, line=None, units=None,
                  amount=None, confidence=0.95)
    values.update(overrides)
    return BetSignal(**values)


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "trades.db")
