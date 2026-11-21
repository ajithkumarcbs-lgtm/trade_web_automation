"""SS sports 17-21 Nov 2026: a voice part exit, an unconfirmed cashout, closes dropped behind a hung page, and a second
bet placed after a page timeout. The fixes for each."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_settings, signal
from main import Pipeline, voice_part_exit
from test_channels_rules import CHAT, FakeSite, make, msg
from tradebot import exchange_bot, site_selectors as sel
from tradebot.channels import Profile
from tradebot.exchange_bot import BetResult, ExchangeBot
from tradebot.risk import RiskManager
from tradebot.telegram_listener import IncomingMessage

PERTH = dict(selection="Perth Scorchers", event="Sydney Sixers v Perth Scorchers", sport="cricket")
SS = dict(voice=True, act_on_voice=True)
# SS 21 Nov 12:53, as transcribed on the server
PART_EXIT = ("(voice note) நாம வந்து 1 limit யூஸ் பணியிருக்கோம், அதுல 0.5 limit எக்ஸிட் பண்ணிக்கலாம். 0.5 மட்டும் "
             "ஹோல்டு பண்ணுங்க. 50 யார்ட்ஸ்ல இருந்து கம்பாக் இருக்குது, சர்க்கட்டா 0.5 limit ரிமூவ் பண்ணுங்க, 0.5 limit மட்டும் "
             "இருக்கணும்.")
ENTRY = "3.6 odds perth back limit 1"


def closed_pl(store, selection):
    return store._db.execute("SELECT closed_pl, closed_how FROM positions WHERE selection = ? AND closed = 1"
                             " ORDER BY id DESC", (selection,)).fetchone()


# --- the voice part exit -----------------------------------------------------------------------------

def test_sss_part_exit_note_keeps_half():
    assert voice_part_exit(PART_EXIT) == 0.5
    assert voice_part_exit("(voice note) 1 limit exit பண்ணுங்க, 1 limit ஹோல்டு பண்ணுங்க") == 0.5
    assert voice_part_exit("(voice note) 1 limit exit, 0.5 limit hold பண்ணுங்க") == pytest.approx(0.3333)
    # no amount kept, a bookset, or one waiting for its price: not a part exit
    assert voice_part_exit("(voice note) 0.5 limit எக்ஸிட் பண்ணுங்க") is None
    assert voice_part_exit("(voice note) Book set பண்ணுங்க") is None
    assert voice_part_exit("(voice note) 1.8 வந்தா 0.5 limit எக்ஸிட், 0.5 மட்டும் ஹோல்டு பண்ணுங்க") is None


def part_exit_pipe(store, live=4.2, **settings):
    script = {ENTRY: [signal(odds=3.6, units=1, **PERTH)],
              # what the AI read on 21 Nov: EXIT BACK Perth 0.5u (0.90)
              PART_EXIT: [signal(action="exit", odds=None, units=0.5, confidence=0.9, selection="Perth",
                                 event="Sydney Sixers Women v Perth Scorchers Women", sport="cricket")]}
    return make(store, Profile("ss", **SS), script, live=live, **settings)


def test_a_part_exit_books_the_trade_and_puts_the_kept_share_back_at_once(store):
    """1 limit (1000) backed at 4.2; at 3.45 '0.5 exit, 0.5 hold': the site's Cashout books all of it, then 0.5 goes back
    in, sized to return what half the old trade returns if it wins: 0.5 x 1000 x 4.2 / 3.45 = 608.70."""
    pipe, site, notifier = part_exit_pipe(store)

    async def go():
        await pipe._handle(msg(ENTRY))
        site.live = 3.45
        await pipe._handle(msg(PART_EXIT, voice=True))

    asyncio.run(go())
    # the close carries the AI's 'Perth' (it found the open Perth Scorchers trade); the kept share goes in on that trade
    assert site.calls == [("entry", "Perth Scorchers", 1), ("close", "Perth", None),
                          ("entry", "Perth Scorchers", 608.7)]
    assert closed_pl(store, "Perth Scorchers") == (250, "booked")  # the Cashout of the whole trade
    kept = store.find_position("Perth Scorchers", "live")
    assert (kept.stake, kept.price) == (608.7, 3.45)
    assert any("PART EXIT" in text for text in notifier.sent)


def test_after_a_part_exit_the_next_bookset_books_the_kept_part(store):
    """Neither the same-close-again rule (10 min) nor the duplicate check may swallow SS's next 'Bookset'."""
    pipe, site, _ = part_exit_pipe(store)
    pipe._parser.script["Bookset"] = [signal(action="close", odds=None, selection="", event="? v ?", sport="cricket")]

    async def go():
        await pipe._handle(msg(ENTRY))
        site.live = 3.45
        await pipe._handle(msg(PART_EXIT, voice=True))
        site.live = 2.0
        await pipe._handle(msg("Bookset"))

    asyncio.run(go())
    assert [call[0] for call in site.calls] == ["entry", "close", "entry", "close"]
    assert store.find_position("Perth Scorchers", "live") is None  # nothing left open


def test_the_kept_share_never_goes_above_the_bet_cap(store):
    """At 2.0 half of the 1000 @ 4.2 trade would be 0.5 x 1000 x 4.2 / 2 = 1050: cut to the cap, 1000."""
    pipe, site, _ = part_exit_pipe(store, max_liability_per_bet=1000)

    async def go():
        await pipe._handle(msg(ENTRY))
        site.live = 2.0
        await pipe._handle(msg(PART_EXIT, voice=True))

    asyncio.run(go())
    assert site.calls[-1] == ("entry", "Perth Scorchers", 1000)


class RefusingKeepSite(FakeSite):
    async def place(self, sig, position, require_green=False):
        if sig.action == "entry" and sig.amount:  # the kept share
            self.calls.append((sig.action, sig.selection, sig.amount))
            return BetResult("retry", "Cannot place bet. Odds is not correct.")
        return await super().place(sig, position, require_green)


def test_a_kept_share_the_site_refuses_is_tried_3_times_then_you_are_told(store):
    pipe, site, notifier = part_exit_pipe(store)
    pipe._bot = site = RefusingKeepSite(pipe._risk, 4.2)

    async def go():
        await pipe._handle(msg(ENTRY))
        site.live = 3.45
        await pipe._handle(msg(PART_EXIT, voice=True))

    asyncio.run(go())
    assert [call[0] for call in site.calls] == ["entry", "close", "entry", "entry", "entry"]
    assert store.find_position("Perth Scorchers", "live") is None
    assert any("could NOT go back in" in text for text in notifier.sent)


def test_a_voice_exit_without_the_part_kept_still_does_nothing(store):
    """Only a part exit with both amounts said acts: a plain voice 'exit' stays a no-action, as before."""
    pipe, site, _ = part_exit_pipe(store)
    text = "(voice note) 0.5 limit எக்ஸிட் பண்ணிக்கலாம்"
    pipe._parser.script[text] = pipe._parser.script[PART_EXIT]

    async def go():
        await pipe._handle(msg(ENTRY))
        await pipe._handle(msg(text, voice=True))

    asyncio.run(go())
    assert [call[0] for call in site.calls] == ["entry"]


# --- an unconfirmed cashout ---------------------------------------------------------------------------

class UnconfirmedSite(FakeSite):
    """20 Nov 16:09: a Cashout in profit showed no confirmation; the next look finds the match booked."""

    def __init__(self, risk, live=3.0, after="already booked on the site: every result pays +0", pl=0.0):
        super().__init__(risk, live)
        self.after, self.after_pl, self.closes = after, pl, 0

    async def place(self, sig, position, require_green=False):
        if sig.action != "close":
            return await super().place(sig, position, require_green)
        self.calls.append((sig.action, sig.selection, None))
        self.closes += 1
        if self.closes == 1:
            return BetResult("unconfirmed", "cashout 70: no confirmation within 20s, check Open Bets", pl=70.0)
        return BetResult("placed", self.after, pl=self.after_pl)


@pytest.mark.parametrize("after,pl,expected", [
    ("already booked on the site: every result pays +0", 0.0, 70.0),  # a booked match shown at 0: the cashout's figure
    ("already booked on the site: every result pays +70", 70.0, 70.0),
    ("cashout 68.5: Bet Placed", 68.5, 68.5),  # it had not gone through: booked now
])
def test_an_unconfirmed_cashout_is_looked_at_again_until_the_site_shows_it_booked(store, after, pl, expected):
    script = {"4 odds back ads": [signal(odds=4.0, units=1, selection="Adelaide Strikers",
                                         event="Melbourne Renegades v Adelaide Strikers", sport="cricket")],
              "Bookset": [signal(action="close", odds=None, selection="", event="? v ?", sport="cricket")]}
    pipe, site, _ = make(store, Profile("ss"), script, live=4.0,
                         site_class=lambda risk, live: UnconfirmedSite(risk, live, after, pl))

    async def go():
        await pipe._handle(msg("4 odds back ads"))
        await pipe._handle(msg("Bookset"))
        assert store.pending_closes("live")  # not left as it was on 20 Nov
        await pipe.retry_pending_once()

    asyncio.run(go())
    assert site.closes == 2
    assert closed_pl(store, "Adelaide Strikers") == (expected, "booked")
    assert not store.pending_closes("live")


# --- closes that waited behind a hung page --------------------------------------------------------------

def old_msg(text, seconds):
    return IncomingMessage(id=2, date=datetime.now(timezone.utc) - timedelta(seconds=seconds), text=text, image=None,
                           chat_id=CHAT)


def test_a_late_bookset_still_books_but_a_late_entry_is_not_taken(store):
    """21 Nov 10:21-10:24 the page hung for 5 min on an entry; SS's posts behind it were dropped as 117-273s old."""
    script = {ENTRY: [signal(odds=3.6, units=1, **PERTH)],
              "Bookset now": [signal(action="close", odds=None, selection="", event="? v ?", sport="cricket")],
              "3.5 odds back aus limit 1": [signal(odds=3.5, units=1, selection="Australia", event="Australia v England",
                                                   sport="cricket")]}
    pipe, site, _ = make(store, Profile("ss"), script, live=3.6)

    async def go():
        await pipe._handle(msg(ENTRY))
        await pipe._handle(old_msg("3.5 odds back aus limit 1", 200))
        await pipe._handle(old_msg("Bookset now", 200))

    asyncio.run(go())
    assert [call[0] for call in site.calls] == ["entry", "close"]  # the Australia entry was not taken
    assert store.find_position("Perth Scorchers", "live") is None  # the bookset booked the Perth trade


def test_a_bookset_older_than_10_minutes_is_still_dropped(store):
    script = {ENTRY: [signal(odds=3.6, units=1, **PERTH)],
              "Bookset now": [signal(action="close", odds=None, selection="", event="? v ?", sport="cricket")]}
    pipe, site, _ = make(store, Profile("ss"), script, live=3.6)

    async def go():
        await pipe._handle(msg(ENTRY))
        await pipe._handle(old_msg("Bookset now", 11 * 60))

    asyncio.run(go())
    assert [call[0] for call in site.calls] == ["entry"]


# --- a page timeout after the Placebet click ------------------------------------------------------------

def test_a_page_timeout_after_the_placebet_click_is_unconfirmed_and_never_placed_again(store, monkeypatch):
    """21 Nov 10:20:59 Perth: 'Placebet clicked (3.65)', the page hung 5 min, 'timed out: Locator.click' came back as a
    retry, and a second bet went in at 4.2. A timeout after the click is now 'unconfirmed': counted, never retried."""
    settings = make_settings(dry_run=False)
    bot = ExchangeBot(settings, RiskManager(settings, store, "live"))
    clicks = []

    class Box:
        async def is_enabled(self):
            return True

        async def click(self):
            clicks.append("Placebet")

        async def input_value(self):
            return "3.65"

    class Slip:
        def locator(self, selector):
            return Box()

    async def limits(slip):
        return 10.0, 50000.0

    async def hung(page):
        raise exchange_bot.PlaywrightTimeout("Locator.click: Timeout 30000ms exceeded.")

    async def nothing(*args, **kwargs):
        return None

    async def balance():
        return 1000.0, 0.0

    monkeypatch.setattr(exchange_bot, "_slip_limits", limits)
    monkeypatch.setattr(exchange_bot, "_wait_for_outcome", hung)
    bot._enter_stake, bot._clear_toasts, bot._screenshot, bot._balance = nothing, nothing, nothing, balance
    result = asyncio.run(bot._submit(signal(odds=3.6, **PERTH), Slip(), 50, 50, 3.65))
    assert clicks == ["Placebet"]
    assert result.status == "unconfirmed" and "NOT tried again" in result.reason
    assert (result.price, result.stake) == (3.65, 50)


# --- a channel set to paper_only while live --------------------------------------------------------------

def sk_paper(store, mode="live"):
    """SK Astro became paper_only at 22:52 on 21 Nov; its live ladder from 20:33 (Emirates) was still watched after the
    restart, ready to lay the other team at 1.50."""
    from test_ladder import LadderSite, Scores
    pipe, site, notifier = make(store, Profile("sk", ladder=True, paper_only=True), {}, live=1.45, mode=mode,
                                site_class=LadderSite)
    pipe._cricbuzz = Scores()
    return pipe, site, notifier


def test_a_ladder_waiting_for_its_first_lay_is_dropped_once_its_channel_is_paper_only(store):
    pipe, site, notifier = sk_paper(store)
    store.add_ladder("live", "Tip", "UAE", "UAE v Netherlands", datetime.now(), None)

    async def go():
        await pipe._drop_paper_ladders()
        for ladder in store.active_ladders("live"):
            await pipe._ladder_step(ladder)

    asyncio.run(go())
    assert store.active_ladders("live") == []
    assert [call for call in site.calls if call[0] == "entry"] == []  # no lay at 1.45
    assert any("PAPER ONLY" in text for text in notifier.sent)


def test_a_paper_only_ladder_with_lays_in_is_kept_for_its_booking_but_takes_no_more_steps(store):
    pipe, _, _ = sk_paper(store)
    lid = store.add_ladder("live", "Tip", "UAE", "UAE v Netherlands", datetime.now(), None)
    store.update_ladder(lid, entries=1)
    asyncio.run(pipe._drop_paper_ladders())
    kept = store.active_ladders("live")
    assert [ladder.id for ladder in kept] == [lid]
    assert pipe._ladder_entries_off(kept[0]).startswith("PAPER ONLY")


def test_on_paper_a_paper_only_channels_ladder_goes_on(store):
    pipe, _, _ = sk_paper(store, mode="dry_run")
    store.add_ladder("dry_run", "Tip", "UAE", "UAE v Netherlands", datetime.now(), None)
    asyncio.run(pipe._drop_paper_ladders())
    assert len(store.active_ladders("dry_run")) == 1


def test_a_waiting_entry_of_a_channel_now_paper_only_is_dropped_not_placed(store):
    pipe, site, _ = make(store, Profile("raftaar", paper_only=True), {}, live=3.0)
    store.add_pending(signal(odds=3.0, units=1, channel="Tip", **PERTH), "live", None, "market suspended")
    asyncio.run(pipe.retry_pending_once())
    assert site.calls == [] and store.pending_instructions("live") == []


class HungSite(FakeSite):
    async def place(self, sig, position, require_green=False):
        self.calls.append((sig.action, sig.selection, sig.amount or sig.units))
        return BetResult("unconfirmed", "Placebet was clicked, then the page timed out: NOT tried again", 3.65, 1000,
                         1000)


def test_an_unconfirmed_entry_is_counted_as_a_trade_and_not_retried(store):
    pipe, site, _ = make(store, Profile("ss"), {ENTRY: [signal(odds=3.6, units=1, **PERTH)]}, live=3.65,
                         site_class=HungSite)

    async def go():
        await pipe._handle(msg(ENTRY))
        await pipe.retry_pending_once()
        await pipe.retry_pending_once()

    asyncio.run(go())
    assert site.calls == [("entry", "Perth Scorchers", 1)]
    assert store.find_position("Perth Scorchers", "live").stake == 1000
    assert sel.PLACE_BET  # the selector the real click uses
