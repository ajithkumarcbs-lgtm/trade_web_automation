"""One bookset message is enough: refused bookings are retried until the site takes them (10 Oct, Gaubas)."""
import asyncio

from conftest import make_settings, signal
from main import Pipeline
from tradebot.exchange_bot import BetResult
from tradebot.risk import RiskManager

GAUBAS = dict(selection="Vilius Gaubas", event="Elmer Moller v Vilius Gaubas")


class RefusingSite:
    """Like iob7 on 10 Oct: 'Your bet is still on going.' twice, then the Cashout goes through."""

    def __init__(self, refusals: int):
        self.refusals = refusals
        self.calls = []  # (action, require_green)
        self.busy = False

    async def place(self, sig, position, require_green=False):
        self.calls.append((sig.action, require_green))
        if sig.action == "entry":
            return BetResult("placed", "ok", 4.2, 50, 50)
        if self.refusals:
            self.refusals -= 1
            return BetResult("retry", "cashout -3.34 refused for now: Your bet is still on going.")
        return BetResult("placed", "cashout 5.27: BAL/EXP changed", pl=5.27)


def setup(store, refusals, **settings):
    s = make_settings(dry_run=False, **settings)
    site = RefusingSite(refusals)
    pipe = Pipeline(s, "live", site, RiskManager(s, store, "live"), store)
    return pipe, site


def statuses(store):
    return [b["status"] for b in reversed(store.recent_bets())]


def test_one_bookset_message_is_retried_until_booked(store):
    pipe, site = setup(store, refusals=2)

    async def run():
        await pipe.execute(signal(odds=3.7, units=1, **GAUBAS))                 # "Back Gaubas 3.7 odds 1 unit"
        first = await pipe.execute(signal(action="close", odds=None, **GAUBAS))  # "Bookset guys quick profitt"
        assert first.status == "pending"
        repeat = await pipe.execute(signal(action="close", odds=None, **GAUBAS))  # same message again
        assert repeat.status == "skipped" and "already booking" in repeat.reason
        await pipe.retry_pending_once()  # refused again
        await pipe.retry_pending_once()  # goes through

    asyncio.run(run())
    assert statuses(store) == ["placed", "pending", "skipped", "placed"]
    assert store.find_position("Gaubas", "live") is None and store.pending_closes("live") == []
    assert [a for a, _ in site.calls] == ["entry", "close", "close", "close"]  # never booked twice


def test_bookset_waits_for_green_but_loss_cut_does_not(store):
    pipe, site = setup(store, refusals=0)
    asyncio.run(pipe.execute(signal(odds=3.7, units=1, **GAUBAS)))
    asyncio.run(pipe.execute(signal(action="close", odds=None, **GAUBAS)))
    assert site.calls[-1] == ("close", True)  # bookset: only book when green (within the wait)

    pipe2, site2 = setup(store, refusals=0)
    asyncio.run(pipe2.execute(signal(odds=2.0, units=1, selection="Snigur", event="Snigur v ?")))
    asyncio.run(pipe2.execute(signal(action="close", odds=None, loss_cut=True, selection="Snigur",
                                     event="Snigur v ?")))
    assert site2.calls[-1] == ("close", False)  # loss cut: book now, red or green


class SlowEntrySite(RefusingSite):
    """Entry refused for now (price out of range / suspended) a few times, then taken."""

    def __init__(self, entry_refusals: int):
        super().__init__(refusals=0)
        self.entry_refusals = entry_refusals

    async def place(self, sig, position, require_green=False):
        if sig.action == "entry" and self.entry_refusals:
            self.entry_refusals -= 1
            self.calls.append(("entry", require_green))
            return BetResult("retry", "SLIPPAGE_REJECTED: back odds dropped to 3.4 (tip 3.7, lowest allowed 3.52)")
        return await super().place(sig, position, require_green)


def entry_pipeline(store, refusals, **settings):
    s = make_settings(dry_run=False, **settings)
    site = SlowEntrySite(refusals)
    return Pipeline(s, "live", site, RiskManager(s, store, "live"), store), site


def test_one_entry_message_is_retried_until_placed(store):
    pipe, site = entry_pipeline(store, refusals=2)

    async def run():
        first = await pipe.execute(signal(odds=3.7, units=1, **GAUBAS))
        assert first.status == "pending"
        again = await pipe.execute(signal(odds=3.7, units=1, **GAUBAS))  # same tip posted again
        assert again.status == "skipped" and "already being retried" in again.reason
        await pipe.retry_pending_once()  # price still out of range
        await pipe.retry_pending_once()  # back in range: placed

    asyncio.run(run())
    assert statuses(store) == ["pending", "skipped", "placed"]
    assert store.find_position("Gaubas", "live") is not None and store.pending_instructions("live") == []
    assert [a for a, _ in site.calls].count("entry") == 3  # tried 3 times, placed once


def test_bookset_before_the_entry_went_through_cancels_it(store):
    pipe, site = entry_pipeline(store, refusals=99)

    async def run():
        await pipe.execute(signal(odds=3.7, units=1, **GAUBAS))                 # pending
        close = await pipe.execute(signal(action="close", odds=None, **GAUBAS))  # tipster already booked
        assert close.status == "skipped" and "pending entry cancelled" in close.reason
        await pipe.retry_pending_once()

    asyncio.run(run())
    assert store.pending_instructions("live") == [] and store.find_position("Gaubas", "live") is None
    assert [a for a, _ in site.calls] == ["entry"]  # never entered after the tipster's exit


def test_entry_missed_after_the_retry_window(store):
    pipe, site = entry_pipeline(store, refusals=99, entry_retry_min=0)
    asyncio.run(pipe.execute(signal(odds=3.7, units=1, **GAUBAS)))
    import time
    time.sleep(1.1)
    asyncio.run(pipe.retry_pending_once())
    last = store.recent_bets()[0]
    assert last["status"] == "failed" and "ENTRY MISSED" in last["reason"] and "SLIPPAGE" in last["reason"]


def test_book_only_green_makes_loss_cut_wait_for_green(store):
    pipe, site = setup(store, refusals=0, book_only_green=True)
    asyncio.run(pipe.execute(signal(odds=4.3, units=1, selection="Pedro Martinez", event="Martinez v Djere")))
    asyncio.run(pipe.execute(signal(action="close", odds=None, loss_cut=True, selection="Pedro Martinez",
                                    event="Martinez v Djere")))
    assert site.calls[-1] == ("close", True)  # 10 Oct: the -20.74 loss cut would have waited for green


def test_heartbeat_text(store):
    pipe, _ = setup(store, refusals=0)
    asyncio.run(pipe.execute(signal(odds=3.7, units=1, **GAUBAS)))
    text = pipe.status_text()
    assert "Bot running" in text and "1 entries" in text and "Vilius Gaubas" in text


def test_gives_up_with_an_alert_after_the_retry_window(store):
    pipe, site = setup(store, refusals=99, bookset_retry_min=0)
    asyncio.run(pipe.execute(signal(odds=3.7, units=1, **GAUBAS)))
    asyncio.run(pipe.execute(signal(action="close", odds=None, **GAUBAS)))
    import time
    time.sleep(1.1)
    asyncio.run(pipe.retry_pending_once())
    last = store.recent_bets()[0]
    assert last["status"] == "failed" and "BOOK IT BY HAND" in last["reason"]
    assert store.pending_closes("live") == []
