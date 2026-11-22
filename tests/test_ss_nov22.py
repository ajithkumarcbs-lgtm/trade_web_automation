"""SS sports 22 Nov 2026: Gulf Giants' entry sent to a later fixture, a result post read as a loss cut, an entry skipped
for the balance, and a loss cut the site refused ('Rate over 10'). The fixes for each."""
import asyncio
from datetime import datetime, timedelta

import pytest

from conftest import make_settings, signal
from test_channels_rules import make, msg
from tradebot import exchange_bot, site_selectors as sel
from tradebot.channels import Profile
from tradebot.exchange_bot import (BetAborted, BetResult, ExchangeBot, _balance_share, _cut_stake, _parse_match_start,
                                   _rate_over, _team_matches, hedge_on_other)
from tradebot.risk import Decision, RiskManager

LISTED = ["Desert Vipers v Abu Dhabi Knight Riders", "Gulf Giants v MI Emirates", "Sharjah Warriors v Gulf Giants"]
LATER, LIVE = "Gulf Giants v MI Emirates", "Sharjah Warriors v Gulf Giants"


async def nothing(*args, **kwargs):
    return None


def bot_for(store, **settings):
    s = make_settings(**{"dry_run": False, **settings})
    return ExchangeBot(s, RiskManager(s, store, "dry_run" if s.dry_run else "live"))


# --- the wrong fixture -------------------------------------------------------------------------------------

def test_the_page_header_time_reads_as_the_start():
    now = datetime(2026, 11, 22, 18, 16)
    assert _parse_match_start("Gulf Giants v MI Emirates (Nov 24, 8:00 PM)", now) == datetime(2026, 11, 24, 20, 0)
    assert _parse_match_start("(Jan 2, 1:30 AM)", datetime(2026, 12, 30, 22, 0)) == datetime(2027, 1, 2, 1, 30)
    assert _parse_match_start("(Nov 22, 4:00 PM)", now) == datetime(2026, 11, 22, 16, 0)  # started: in the past
    assert _parse_match_start("Sharjah Warriors v Gulf Giants", now) is None


def test_every_listed_match_of_the_team_is_a_candidate():
    assert _team_matches(LISTED, "Gulf Giants") == [1, 2]
    assert _team_matches(LISTED, "gg") == [1, 2]  # SS's code
    assert _team_matches(LISTED, "") == []


class Names:
    def __init__(self, names):
        self.names = names

    async def all_inner_texts(self):
        return list(self.names)


class ListPage:
    """The home list: page.locator(EVENT_ROW).locator(EVENT_NAME).all_inner_texts()."""

    def __init__(self, names):
        self.names = names

    def locator(self, selector):
        return self

    async def all_inner_texts(self):
        return list(self.names)


def open_match(store, starts, listed=LISTED, selection="Gulf Giants", event="Gulf Giants v ?"):
    """Runs _open_match on the list with each match page saying it starts at starts[match] (None: no time shown)."""
    bot = bot_for(store)
    bot._page = ListPage(listed)
    opened = []
    bot.start_reads = 0

    async def enter(match, reload):
        if match not in listed:
            return False
        opened.append(match)
        bot._match_name = match.strip()
        return True

    async def start():
        bot.start_reads += 1
        return starts.get(bot._match_name)

    bot._open_list, bot._enter_listed, bot._match_start = nothing, enter, start
    name = asyncio.run(bot._open_match(event, selection, "gg"))
    return bot, opened, name


def test_a_tip_on_a_team_with_a_later_fixture_listed_opens_the_match_being_played(store):
    """22 Nov 18:16 'Gg back 4.2' opened Gulf Giants v MI Emirates (24 Nov, suspended until then) for 12 minutes while
    Sharjah Warriors v Gulf Giants was on. Its page says it starts in 2 days: the team's other match is opened."""
    bot, opened, name = open_match(store, {LATER: datetime.now() + timedelta(days=2)})
    assert opened == [LATER, LIVE]
    assert bot._match_name == LIVE and name == "Gulf Giants"


def test_a_match_starting_within_12_hours_is_kept(store):
    bot, opened, _ = open_match(store, {LATER: datetime.now() + timedelta(hours=2)})
    assert opened == [LATER] and bot._match_name == LATER


def test_when_every_listed_match_starts_later_the_first_of_them_is_used(store):
    bot, opened, _ = open_match(store, {LATER: datetime.now() + timedelta(days=1),
                                        LIVE: datetime.now() + timedelta(days=3)})
    assert opened == [LATER, LIVE, LATER] and bot._match_name == LATER


def test_a_team_in_one_listed_match_reads_no_start_time(store):
    listed = ["Desert Vipers v Abu Dhabi Knight Riders", LIVE]
    bot, opened, _ = open_match(store, {LIVE: datetime.now() + timedelta(days=2)}, listed=listed)
    assert opened == [LIVE] and bot.start_reads == 0  # opened at once, as before the fix


def test_a_match_gone_from_the_list_is_not_found_rather_than_left_unopened(store):
    bot = bot_for(store)
    bot._page = ListPage(LISTED)
    bot._open_list = nothing

    async def gone(match, reload):
        return False

    bot._enter_listed = gone
    with pytest.raises(BetAborted) as info:
        asyncio.run(bot._open_match("Gulf Giants v ?", "Gulf Giants"))
    assert info.value.status == "failed" and "not found" in info.value.reason


# --- the result post ---------------------------------------------------------------------------------------

BH = dict(selection="Brisbane Heat", event="Brisbane Heat v Adelaide Strikers", sport="cricket")
BH_ENTRY = "Bh 3.2 odds back limit 1"
RESULT = "First match\n\nBh 1 limit loss 👍"


@pytest.mark.parametrize("text,closes", [
    (RESULT, False),                                  # 22 Nov 13:00, read as LOSS CUT Brisbane Heat
    ("Today 2 limit loss", False),                    # 4 Nov
    ("0.5 limit loss no problem next match", False),  # 1 Nov
    ("Bh 1 limit loss cut now", True),                # an instruction with the result in it still acts
    ("Bh losscut", True),
])
def test_a_result_post_is_not_a_loss_cut(store, text, closes):
    script = {BH_ENTRY: [signal(odds=3.2, units=1, **BH)],
              text: [signal(action="close", odds=None, loss_cut=True, **BH)]}
    pipe, site, _ = make(store, Profile("ss"), script, live=3.2)

    async def go():
        await pipe._handle(msg(BH_ENTRY))
        site.live = 20.0
        await pipe._handle(msg(text))

    asyncio.run(go())
    assert [call[0] for call in site.calls] == (["entry", "close"] if closes else ["entry"])


# --- two matches at once: a close naming no team ----------------------------------------------------------------

IND = dict(selection="India", event="India v New Zealand", sport="cricket")
SYS = dict(selection="Sydney Sixers", event="Sydney Sixers v Hobart Hurricanes", sport="cricket")
IRE = dict(selection="Ireland", event="Ireland v Namibia", sport="cricket")
NZ = dict(selection="New Zealand", event="New Zealand v India", sport="cricket")


def test_a_close_for_a_team_tipped_earlier_in_another_match_does_not_book_the_newest_trade(store):
    """SS 1 Nov: '3 odds back ind' 12:54 (missed: India at 2.47), Sixers 13:54, '2.9 odds Ireland back limit 1' 14:21, then
    'Now bookset' 14:46, read by the AI as India's, and SS's voice at 14:49: 'India... bookset now'. The bot booked Ireland,
    its newest trade, which SS kept (and added to at 6.4). India's bookset now books nothing."""
    script = {"3 odds back ind": [signal(odds=3.0, units=0.5, **IND)],
              "4.5odds sys back limit 1": [signal(odds=4.5, units=1, **SYS)],
              "2.9 odds Ireland back limit 1": [signal(odds=2.9, units=1, **IRE)],
              "Now bookset": [signal(action="close", odds=None, **IND)]}
    pipe, site, _ = make(store, Profile("ss", rules="ss"), script, live=2.47)

    async def go():
        await pipe._handle(msg("3 odds back ind"))
        site.live = 4.5
        await pipe._handle(msg("4.5odds sys back limit 1"))
        site.live = 2.9
        await pipe._handle(msg("2.9 odds Ireland back limit 1"))
        await pipe._handle(msg("Now bookset"))

    asyncio.run(go())
    assert [call[0] for call in site.calls if call[0] == "close"] == []
    assert store.find_position("Ireland", "live") is not None and store.find_position("Sydney Sixers", "live") is not None


def test_a_close_read_as_the_latest_trades_opponent_books_the_latest_trade(store):
    """SS 24 Oct 'Bookset enjoy jackpot 🔥🔥✅' was read as India's (the other side of New Zealand v India) while New Zealand
    was SS's trade: the latest entry is booked, as before."""
    script = {"3.8 odds nz back limit 1": [signal(odds=3.8, units=1, **NZ)],
              "Bookset enjoy jackpot 🔥🔥✅": [signal(action="close", odds=None, **IND)]}
    pipe, site, _ = make(store, Profile("ss", rules="ss"), script, live=3.8)

    async def go():
        await pipe._handle(msg("3.8 odds nz back limit 1"))
        await pipe._handle(msg("Bookset enjoy jackpot 🔥🔥✅"))

    asyncio.run(go())
    assert ("close", "New Zealand") in [call[:2] for call in site.calls]
    assert store.find_position("New Zealand", "live") is None


@pytest.mark.parametrize("text,closes", [
    ("3 rd jackpot done ✅ booksey", False),         # 1 Nov 14:22, about an earlier match, a minute after Ireland's entry
    ("Perfect entry point and bookset point 🔥🔥✅", False),
    ("Bookset 🔥💯🔥 done ✅✅ jackpot", False),
    ("Jackpot biggest done bookset now", True),     # 6 Nov: 'now' asks for it
    ("Booksey", True),
])
def test_a_booking_told_as_done_is_a_report(store, text, closes):
    script = {"2.9 odds Ireland back limit 1": [signal(odds=2.9, units=1, **IRE)]}
    pipe, site, _ = make(store, Profile("ss", rules="ss"), script, live=2.9)

    async def go():
        await pipe._handle(msg("2.9 odds Ireland back limit 1"))
        await pipe._handle(msg(text))  # the AI reads nothing (as on 1 Nov): the rules' reading

    asyncio.run(go())
    assert [call[0] for call in site.calls] == (["entry", "close"] if closes else ["entry"])


@pytest.mark.parametrize("text,loss_cut", [
    ("Losscit 🔥", True), ("Booksey", False), ("Bookse", False), ("Booksert", False), ("Loss ut now", True),
    ("Losscat", True), ("Bookset", False), ("Losscut 🔥🔥✅", True),
])
def test_sss_typo_closes_are_read_without_the_ai(text, loss_cut):
    from tradebot.rules import parse_known
    sig = parse_known(text, "ss")[0]
    assert (sig.action, sig.loss_cut) == ("close", loss_cut)


@pytest.mark.parametrize("text", ["Bookset coming be active", "Nab vs irl", "Booksellers", "lossless"])
def test_words_like_the_typos_are_not_closes(text):
    from tradebot.rules import parse_known
    assert parse_known(text, "ss") is None


# --- the balance -------------------------------------------------------------------------------------------

def test_a_short_balance_cuts_the_stake_instead_of_skipping():
    """22 Nov 12:36 'try 3.2 odds sco' found less than its stake left and was skipped. 98% of the balance now goes in."""
    assert _balance_share(250, 300) == pytest.approx(0.8167)  # 245 of 300
    assert _balance_share(50, 300) is None  # under a quarter of the stake: skipped as before
    assert _balance_share(1000, 300) == 1.0
    back = _cut_stake(Decision(True, "ok", stake=300, liability=300), 0.66)
    assert (back.stake, back.liability, back.ok) == (198, 198, True)
    lay = _cut_stake(Decision(True, "ok", stake=300, liability=600), 0.5)  # a lay at 3.0
    assert (lay.stake, lay.liability) == (150, 300)


def balance_bot(store, balance, **settings):
    bot = bot_for(store, **settings)

    async def read():
        return balance, 0.0

    bot._balance = read
    return bot


def test_the_balance_check_returns_the_share_to_place(store):
    assert asyncio.run(balance_bot(store, 250)._check_balance(300, 3.2)) == pytest.approx(0.8167)
    assert asyncio.run(balance_bot(store, 900)._check_balance(300, 3.2)) == 1.0
    assert asyncio.run(balance_bot(store, None)._check_balance(300, 3.2)) == 1.0  # balance not read: as before
    assert asyncio.run(balance_bot(store, 50, dry_run=True)._check_balance(300, 3.2)) == 1.0
    with pytest.raises(BetAborted) as info:
        asyncio.run(balance_bot(store, 50)._check_balance(300, 3.2))
    assert info.value.status == "skipped" and "INSUFFICIENT_BALANCE" in info.value.reason


# --- 'Rate over 10' --------------------------------------------------------------------------------------

def test_booking_on_the_other_runner_pays_the_same_on_both_results():
    """BH backed 1000 @ 3.2 (win +2200, lose -1000) and now 21: the lay is refused above 10. Adelaide backed at 1.05."""
    stake, locked = hedge_on_other(2200, -1000, 1.05)
    assert stake == pytest.approx(3047.62)
    assert locked == pytest.approx(2200 - stake)
    assert -1000 + stake * 0.05 == pytest.approx(locked, abs=0.01)  # Adelaide wins
    assert _rate_over("Bet Not Accepted. Rate over 10.")
    assert not _rate_over("Cannot place bet. Odds is not correct.")


class Box:
    def __init__(self, text="", count=1, log=None, name=""):
        self.text, self._count, self.log, self.name = text, count, log, name

    def filter(self, **kwargs):
        return self

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def inner_text(self, **kwargs):
        return self.text

    async def click(self, **kwargs):
        if self.log is not None:
            self.log.append(self.name)

    async def wait_for(self, **kwargs):
        return None


class CashoutPage:
    def __init__(self):
        self.clicks = []

    def locator(self, selector):
        if selector == sel.CASHOUT_BUTTON:
            return Box("Cashout: -880", log=self.clicks, name="cashout")
        if selector == sel.CASHOUT_CONFIRM:
            return Box(log=self.clicks, name="confirm")
        if selector == sel.CASHOUT_CANCEL:
            return Box(log=self.clicks, name="cancel")
        return Box(count=0)


def test_a_cashout_refused_for_its_rate_books_by_hand_instead(store, monkeypatch):
    """22 Nov 13:00: Cashout of the BH back (BH at 20+) -> 'Bet Not Accepted. Rate over 10.' and the close failed."""
    bot = bot_for(store)
    page = CashoutPage()
    bot._page = page
    hedged = []

    async def opened(*args, **kwargs):
        return "Brisbane Heat"

    async def refused(p):
        return "failed", "Bet Not Accepted. Rate over 10."

    async def same(status, reason, before):
        return status, reason

    async def hedge(sig, name, pos, require_green=False):
        hedged.append(name)
        return BetResult("placed", "booked by hand (back Adelaide Strikers 3047.62 @ 1.05)", pl=-847.62)

    async def balance():
        return 1000.0, 0.0

    monkeypatch.setattr(exchange_bot, "_wait_for_outcome", refused)
    bot._open_match, bot._confirm_by_balance, bot._hedge_now = opened, same, hedge
    bot._screenshot, bot._clear_toasts, bot._balance = nothing, nothing, balance
    result = asyncio.run(bot._cash_out(signal(action="close", odds=None, loss_cut=True, **BH), None))
    assert hedged == ["Brisbane Heat"]
    assert page.clicks == ["cashout", "confirm", "cancel"]  # the refused confirm is closed before the booking
    assert (result.status, result.pl) == ("placed", -847.62)


def test_a_loss_cut_on_a_suspended_market_is_retried_not_dropped(store):
    """Booking by hand reads the runner's row first; a suspended market there came back 'skipped', which drops a close."""
    bot = bot_for(store)
    bot._page, bot._page_since = ListPage([]), datetime.now()

    async def suspended(sig, pos, require_green=False):
        raise BetAborted("skipped", "market suspended")

    async def page_saved(sig, tag):
        return None

    bot._cash_out, bot._save_page, bot._close_slip, bot._park = suspended, page_saved, nothing, nothing
    result = asyncio.run(bot.place(signal(action="close", odds=None, loss_cut=True, **BH), None))
    assert result.status == "retry"


def hedge_bot(store, lay_price):
    bot = bot_for(store, max_back_odds=10)  # MAX_BACK_ODDS=10 on the server
    called = []

    async def row(name, side, need_price=True):
        return None, None

    async def book():
        return 2200.0, [-1000.0]

    async def box(name, side, need_price=True):
        called.append(("box", side, need_price))
        return None, lay_price

    async def lay_as_back(sig, win, lose, require_green):
        called.append(("lay_as_back", win, lose))
        return BetResult("placed", "booked", pl=-847.62)

    async def two_runners():
        return "Adelaide Strikers W"

    bot._odds_box, bot._site_book, bot._odds_box_when_open, bot._hedge_lay_as_back = row, book, box, lay_as_back
    bot._other_runner = two_runners
    return bot, called


@pytest.mark.parametrize("lay_price", [21.0, None])  # above 10, or no lay price shown above it
def test_a_booking_lay_above_10_backs_the_other_runner(store, lay_price):
    bot, called = hedge_bot(store, lay_price)
    result = asyncio.run(bot._hedge_now(signal(action="close", odds=None, **BH), "Brisbane Heat", None))
    assert called == [("box", "lay", False), ("lay_as_back", 2200.0, -1000.0)]
    assert result.pl == -847.62


def test_a_booking_with_no_price_in_a_market_with_a_draw_waits_for_one(store):
    bot, called = hedge_bot(store, None)

    async def three_runners():
        return None

    bot._other_runner = three_runners
    with pytest.raises(BetAborted) as info:
        asyncio.run(bot._hedge_now(signal(action="close", odds=None, **BH), "Brisbane Heat", None))
    assert info.value.status == "retry" and called == [("box", "lay", False)]


def test_the_lay_as_back_booking_bets_the_other_runner(store):
    bot = bot_for(store, max_back_odds=10)
    bot._row_name = "Brisbane Heat W"
    placed = []

    async def other():
        return "Adelaide Strikers W"

    async def box(name, side, need_price=True):
        return None, 1.05

    class Slip:
        def locator(self, selector):
            return self

        async def input_value(self):
            return "1.05"

    async def slip(cell):
        return Slip()

    async def submit(sig, slip, stake, liability, price, hedge=False):
        placed.append((sig.side, sig.selection, stake, liability, price, hedge))
        return BetResult("placed", "Bet placed", price, stake, liability)

    bot._other_runner, bot._odds_box_when_open, bot._open_slip = other, box, slip
    bot._set_unmatch, bot._submit = nothing, submit
    result = asyncio.run(bot._hedge_lay_as_back(signal(action="close", odds=None, **BH), 2200.0, -1000.0, False))
    assert placed == [("back", "Adelaide Strikers W", 3047.62, 3047.62, 1.05, True)]
    assert result.pl == pytest.approx(-847.62)
    assert "back Adelaide Strikers W 3047.62 @ 1.05 = lay Brisbane Heat W @ 21.00" in result.reason
