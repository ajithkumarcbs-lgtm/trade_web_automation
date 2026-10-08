"""Replays Telegram instruction sequences through the real pipeline with a fake AI and a fake site."""
import asyncio
from datetime import datetime, timezone

import pytest
from openai import APIConnectionError

import main as main_module
from conftest import make_settings, signal
from main import Pipeline
from tradebot.exchange_bot import BetResult
from tradebot.models import ParsedMessage
from tradebot.risk import RiskManager, exit_plan
from tradebot.telegram_listener import IncomingMessage

BRO = dict(selection="Florian Broska")


class FakeParser:
    def __init__(self, script):
        self.script = script

    async def parse(self, text, image=None, context=None):
        return ParsedMessage(signals=self.script[text], summary=text)


class FakeSite:
    """Stands in for ExchangeBot: accepts every bet, sizes exits like the real bot."""

    def __init__(self, risk: RiskManager, live_price: float):
        self.risk = risk
        self.live_price = live_price
        self.calls = []

    async def place(self, sig, position):
        self.calls.append((sig.action, sig.selection))
        if sig.action == "entry":
            d = self.risk.price_check(sig, self.live_price)
            if not d.ok:
                return BetResult("skipped", d.reason, self.live_price)
            return BetResult("placed", "ok", self.live_price, d.stake, d.liability)
        if sig.action == "exit":
            side, stake, liab = exit_plan(position, sig.odds)  # earlier waiting exit fully cancelled
            return BetResult("placed", f"{side} {stake:g}", sig.odds, stake, liab)
        return BetResult("placed", "cashed out")


def run(pipe, texts):
    for text in texts:
        msg = IncomingMessage(id=1, date=datetime.now(timezone.utc), text=text, image=None)
        asyncio.run(pipe._handle(msg))


def make_pipeline(store, script, live_price=2.9, mode="live"):
    settings = make_settings(dry_run=False)
    risk = RiskManager(settings, store, mode)
    site = FakeSite(risk, live_price)
    pipe = Pipeline(settings, mode, site, risk, store)
    pipe._parser = FakeParser(script)
    return pipe, site


def bets(store):
    return [(b["status"], b["stake"], b["reason"]) for b in reversed(store.recent_bets())]


def test_full_trade_entry_limit_replace_bookset_reentry(store):
    script = {
        "Broska back 2.9 2 units": [signal(units=2, **BRO)],
        "Limit 1.5": [signal(action="exit", side="lay", odds=1.5, **BRO)],
        "Total 3.5 limit": [signal(action="exit", side="lay", odds=3.5, **BRO)],
        "Book set": [signal(action="close", odds=None, **BRO)],
        "Broska back 3": [signal(odds=3.0, **BRO)],
        "Again back 2 limit add": [signal(action="add", odds=2.0, **BRO)],
    }
    pipe, site = make_pipeline(store, script)
    run(pipe, script)
    got = bets(store)
    assert [g[0] for g in got] == ["placed", "placed", "placed", "placed", "placed", "skipped"]
    assert got[0][1] == 2000                                   # 2 units
    assert got[1][1] == pytest.approx(2000 * 2.9 / 1.5, abs=0.01)  # book set at 1.5
    assert got[2][1] == pytest.approx(2000 * 2.9 / 3.5, abs=0.01)  # replaced at 3.5
    assert got[4][1] == 1000                                   # new trade after the cashout, 1 unit default
    assert "adds" in got[5][2]
    assert site.calls == [("entry", "Florian Broska"), ("exit", "Florian Broska"), ("exit", "Florian Broska"),
                          ("close", "Florian Broska"), ("entry", "Florian Broska")]


def test_loss_cut_on_a_trade_placed_by_hand_goes_to_the_site(store):
    script = {"Loss cut": [signal(action="close", odds=None, **BRO)]}
    pipe, site = make_pipeline(store, script)
    run(pipe, script)
    assert bets(store)[0][0] == "placed" and site.calls == [("close", "Florian Broska")]


def test_rupee_stake_tip(store):
    script = {"Back Namibia 2.88 Rs10": [signal(selection="Namibia", event="Namibia v ?", odds=2.88, amount=10)]}
    pipe, _ = make_pipeline(store, script, live_price=3.0)
    run(pipe, script)
    assert bets(store)[0][:2] == ("placed", 10)


def test_entry_skipped_when_price_moved_too_far(store):
    script = {"Broska back 2.9": [signal(**BRO)]}
    pipe, _ = make_pipeline(store, script, live_price=2.5)
    run(pipe, script)
    status, _, reason = bets(store)[0]
    assert status == "skipped" and "dropped" in reason
    assert store.find_position("Broska", "live") is None


def test_repeated_tip_is_not_bet_twice(store):
    script = {"Broska back 2.9": [signal(**BRO)], "Broska back 2.9 again": [signal(**BRO)]}
    pipe, site = make_pipeline(store, script)
    run(pipe, script)
    assert [b[0] for b in bets(store)] == ["placed", "skipped"]
    assert len(site.calls) == 1


class FlakyParser(FakeParser):
    """Network down for the first `failures` calls."""

    def __init__(self, script, failures):
        super().__init__(script)
        self.failures = failures

    async def parse(self, text, image=None, context=None):
        if self.failures:
            self.failures -= 1
            raise APIConnectionError(request=None)
        return await super().parse(text, image, context)


@pytest.fixture
def no_wait(monkeypatch):
    async def instant(_seconds):
        return None
    monkeypatch.setattr(main_module.asyncio, "sleep", instant)


def test_tip_survives_a_short_network_drop(store, no_wait):
    script = {"Broska back 2.9": [signal(**BRO)]}
    pipe, site = make_pipeline(store, script)
    pipe._parser = FlakyParser(script, failures=2)
    run(pipe, script)
    assert bets(store)[0][0] == "placed" and len(site.calls) == 1


def test_tip_is_dropped_when_network_is_down_too_long(store, monkeypatch, caplog):
    real_sleep = asyncio.sleep

    async def short(_seconds):
        await real_sleep(0.2)
    monkeypatch.setattr(main_module.asyncio, "sleep", short)

    script = {"Broska back 2.9": [signal(**BRO)]}
    pipe, site = make_pipeline(store, script)
    pipe._s = make_settings(signal_max_age_s=1)  # fresh when it arrives, stale after ~1 s of retries
    parser = FlakyParser(script, failures=10**6)
    pipe._parser = parser
    asyncio.run(pipe._handle(IncomingMessage(id=1, date=datetime.now(timezone.utc), text="Broska back 2.9",
                                             image=None)))
    assert parser.failures < 10**6 - 2, "the AI call was retried"
    assert site.calls == [] and store.recent_bets() == []
    assert "MISSED" in caplog.text


def test_signals_only_mode_simulates_everything(store):
    script = {
        "Broska back 2.9": [signal(**BRO)],
        "Limit 1.5": [signal(action="exit", side="lay", odds=1.5, **BRO)],
        "Loss cut": [signal(action="close", odds=None, **BRO)],
    }
    settings = make_settings()
    risk = RiskManager(settings, store, "signals_only")
    pipe = Pipeline(settings, "signals_only", None, risk, store)
    pipe._parser = FakeParser(script)
    run(pipe, script)
    got = bets(store)
    assert [g[0] for g in got] == ["simulated", "simulated", "simulated"]
    assert "lay 1933.33" in got[1][2]
    assert store.find_position("Broska", "signals_only") is None  # closed by the loss cut
