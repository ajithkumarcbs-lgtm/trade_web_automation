"""tools.try_bet builds the same instructions as Telegram tips and runs them through the same pipeline."""
import asyncio

import pytest

from conftest import make_settings
from main import Pipeline
from test_pipeline import FakeSite
from tools.try_bet import make_signal
from tradebot.risk import RiskManager


def test_signals_built_by_the_tool():
    back = make_signal("back", "Svrcina", 10, None)
    assert (back.action, back.side, back.amount, back.odds, back.selection) == ("entry", "back", 10, None, "Svrcina")
    assert make_signal("lay", "Svrcina", 10, 2.1).side == "lay"
    limit = make_signal("limit", "Svrcina", 1.5, None)
    assert (limit.action, limit.odds) == ("exit", 1.5)
    assert make_signal("cashout", "Svrcina", None, None).action == "close"


def test_back_limit_cashout_sequence(store):
    settings = make_settings(dry_run=False, max_liability_per_bet=300)  # Rs 10 test fits the old Rs 300 cap
    risk = RiskManager(settings, store, "live")
    site = FakeSite(risk, live_price=2.12)
    pipe = Pipeline(settings, "live", site, risk, store)

    async def run():
        return [await pipe.execute(make_signal(*step)) for step in
                [("back", "Svrcina", 10, None), ("limit", "Svrcina", 1.5, None), ("cashout", "Svrcina", None, None)]]

    results = asyncio.run(run())
    assert [r.status for r in results] == ["placed", "placed", "placed"]
    assert results[0].stake == 10 and results[1].stake == pytest.approx(10 * 2.12 / 1.5, abs=0.01)
    assert site.calls == [("entry", "Svrcina"), ("exit", "Svrcina"), ("close", "Svrcina")]
    assert store.find_position("Svrcina", "live") is None  # closed by the cashout
