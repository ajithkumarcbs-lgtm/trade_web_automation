"""SS sports calls the live bot missed or misread, 4-16 Nov 2026, and the fixes for them."""
import asyncio
from datetime import datetime, timedelta, timezone

from conftest import make_settings, signal
from main import AI_WORDS, Pipeline
from test_channels_rules import CHAT, make, msg
from tradebot.channels import Profile
from tradebot.risk import RiskManager
from tradebot.telegram_listener import IncomingMessage

MLS = dict(selection="Melbourne Stars", event="Melbourne Stars v Sydney Thunder", sport="cricket")
MLR = dict(selection="Melbourne Renegades", event="Melbourne Renegades v Sydney Sixers", sport="cricket")
AUS = dict(selection="Australia", event="Australia v England", sport="cricket")
IND = dict(selection="India", event="India v New Zealand", sport="cricket")


def test_an_add_above_10_odds_goes_in_instead_of_being_skipped(store):
    """16 Nov: Stars backed at 4.4, then '20 odds back mls try small amount'. The site takes no back above 10, so it
    goes in as a lay of the opponent (exchange_bot); the pipeline used to skip it ('can't add ... at 20')."""
    script = {"3.9 odds mps back limit one": [signal(odds=4.4, units=1, **MLS)],
              "20 odds back mls try small amount": [signal(action="add", odds=20.0, units=None, **MLS)]}
    pipe, site, _ = make(store, Profile("ss"), script, live=4.4, max_back_odds=10)

    async def go():
        await pipe._handle(msg("3.9 odds mps back limit one"))
        site.live = 20.0
        await pipe._handle(msg("20 odds back mls try small amount"))

    asyncio.run(go())
    assert site.calls == [("entry", "Melbourne Stars", 1), ("entry", "Melbourne Stars", 500)]  # 0.5 unit 'small'
    pos = store.find_position("Melbourne Stars", "live")
    assert pos.stake == 1500  # 1000 @ 4.4 + 500 @ 20: the add counts at the back it stands for


def test_ten_odds_after_an_entry_is_its_price_and_try_small_its_stake(store):
    """15 Nov: 'Back mlr', '10 odds', 'Try small amount' within a minute. '10 odds' was read as a 10-unit stake,
    capped to 3 and refused by the liability cap; it is the entry's price, and 'small' is its stake."""
    script = {"Back mlr": [signal(odds=None, units=None, **MLR)],
              "10 odds": [signal(action="stake", odds=None, units=10, **MLR)],
              "Try small amount": [signal(action="stake", odds=None, units=None, **MLR)]}
    pipe, site, _ = make(store, Profile("ss", stake_wait_s=5), script, live=8.6)

    async def go():
        for text in script:
            await pipe._handle(msg(text))

    asyncio.run(go())
    assert site.calls == [("entry", "Melbourne Renegades", 0.5)]  # at 8.6: a 10-odds long shot takes 25% less
    assert store.find_position("Melbourne Renegades", "live").price == 8.6


def test_a_bare_price_with_no_entry_waiting_is_not_a_stake(store):
    script = {"10 odds": [signal(action="stake", odds=None, units=10, **MLR)]}
    pipe, site, _ = make(store, Profile("ss"), script)
    asyncio.run(pipe._handle(msg("10 odds")))
    assert site.calls == []


def test_a_close_naming_no_team_right_after_a_close_is_that_close_again(store):
    """15 Nov: Australia backed; 'Ind match losscut', 'Bookset fav 1.38 ind' (its India entry was never placed), then
    'Perfect entry perfect bookset', which booked the Australia trade at a loss. The last one is the India close again."""
    script = {"3.6 odds back aus limit 1": [signal(odds=3.6, units=1, **AUS)],
              "Ind match losscut": [signal(action="close", odds=None, loss_cut=True, **IND)],
              "Perfect entry perfect bookset": [signal(action="close", odds=None, selection="", event="? v ?",
                                                       sport="cricket")]}
    pipe, site, _ = make(store, Profile("ss"), script, live=3.6)

    async def go():
        for text in script:
            await pipe._handle(msg(text))

    asyncio.run(go())
    assert ("close", "Australia", None) not in site.calls
    assert store.find_position("Australia", "live") is not None


def test_a_close_naming_no_team_later_still_books(store):
    """The rule is only for the same close posted again: half an hour later 'Bookset now' books the open trade."""
    script = {"3.6 odds back aus limit 1": [signal(odds=3.6, units=1, **AUS)],
              "Bookset now": [signal(action="close", odds=None, selection="", event="? v ?", sport="cricket")]}
    pipe, site, _ = make(store, Profile("ss"), script, live=3.6)
    pipe._last_close["Tip"] = datetime.now(timezone.utc) - timedelta(minutes=30)

    async def go():
        for text in script:
            await pipe._handle(msg(text))

    asyncio.run(go())
    assert site.calls[-1][0] == "close"  # the site books the channel's latest trade
    assert store.find_position("Australia", "live") is None


def test_sss_typo_closes_reach_the_ai():
    for text in ("Losscit 🔥", "Booksey", "Bookse", "Now booksert", "Loss ut now", "Losscut 🔥💯💯🔥"):
        assert AI_WORDS.search(text), text
    assert not AI_WORDS.search("booksellers market")


def test_a_long_shot_takes_a_price_up_to_25_percent_below_the_tip(store):
    risk = RiskManager(make_settings(odds_tolerance=0.1), store, "live")
    assert risk.price_check(signal(odds=20.0, units=0.5), 17.7).ok  # lay 1.06 = back 17.7
    assert risk.price_check(signal(odds=10.0, units=0.5), 8.6).ok
    assert not risk.price_check(signal(odds=10.0, units=0.5), 7.0).ok
    assert not risk.price_check(signal(odds=5.0, units=0.5), 4.4).ok  # below 10 the channel's 10% stays


def test_a_suspended_cricket_market_is_retried_12_minutes(store):
    pipe, _, _ = make(store, Profile("ss"), {})
    assert pipe._retry_minutes(signal(**IND), "market suspended") == 12
    assert pipe._retry_minutes(signal(**IND), "SLIPPAGE_REJECTED: back odds dropped") == 5
