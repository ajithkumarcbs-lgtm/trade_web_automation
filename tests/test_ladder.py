"""SK Astro's method: the other team laid in steps, booked at even or as told; SK's jackpot days for SS sports."""
import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta

from pathlib import Path

from conftest import make_settings, signal
from main import Pipeline
from test_channels_rules import CHAT, FakeNotifier, FakeParser, FakeSite, make, msg  # noqa: F401  (the same fakes as the pipeline tests)
from tradebot.channels import Channel, Channels, Profile
from tradebot.exchange_bot import TeamQuote
from tradebot.fancy import Match
from tradebot.ladder import (BOOKSET, EQUAL_EXIT, FELL_BACK, GIVEBACK, WAIT_FAVOURITE, WAIT_PROFIT, Ladder, exit_call, jackpot_days_in,
                             parse_day, read_sk_tip, step_at)
from tradebot.risk import RiskManager, booked_profit
from tradebot.rules import parse_known

UAE = dict(selection="UAE", event="UAE v Netherlands", sport="cricket", odds=None)
MATCH_WIN = "UAE Toss or Match Win"
TODAY = date(2026, 11, 1)


class Scores:
    def __init__(self, matches=()):
        self.matches = list(matches)

    async def refresh_list(self):
        return self.matches


class LadderSite(FakeSite):
    """The other team's prices: live is its lay price, back (else live) its back price; team the tipped team's back price
    (else what a market with no margin would show)."""
    back, team, gone, looks = None, None, False, 0

    async def other_team_quote(self, event, team):
        self.looks += 1
        if self.gone:
            return TeamQuote(gone=True, problem="match not found on site")
        return TeamQuote("Netherlands", "UAE v Netherlands", self.live, self.back or self.live, team_back=self.team)


def sk(store, script=None, live=1.6, mode="dry_run", matches=()):
    profile = Profile("sk", ladder=True, jackpot_source=True, enter_before_start_min=10)
    script = {MATCH_WIN: [signal(**UAE)], **(script or {})}
    pipe, site, notifier = make(store, profile, script, live=live, mode=mode, site_class=LadderSite)
    pipe._cricbuzz = Scores(matches)
    return pipe, site, notifier


def tip(pipe, text):
    asyncio.run(pipe._handle(msg(text)))


def look(pipe, site, *prices, mode="dry_run"):
    async def go():
        for price in prices:
            site.live = price
            for ladder in pipe._store.active_ladders(mode):
                await pipe._ladder_step(ladder)

    asyncio.run(go())


def entries(site):
    return [call for call in site.calls if call[0] == "entry"]


def test_each_price_is_in_one_step():
    assert [step_at(p) for p in (1.51, 1.5, 1.21, 1.2, 1.11, 1.1, 1.02, 1.01, 1.0)] == [
        None, 0, 0, 1, 1, 2, 2, 2, None]


def test_steps_are_taken_once_and_only_going_down():
    ladder = Ladder(1, "", "SK", "UAE", "UAE v Netherlands", "", None, last_step=1)
    assert ladder.step_to_take(1.4) is None  # back up in a step above
    assert ladder.step_to_take(1.15) is None  # this step again
    assert ladder.step_to_take(1.05) == 2
    assert replace(ladder, stopped=True).step_to_take(1.05) is None


def test_a_match_win_tip_lays_the_other_team_in_three_steps(store):
    pipe, site, _ = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.6, 1.45, 1.35, 1.15, 1.05)
    assert entries(site) == [("entry", "Netherlands", 1000), ("entry", "Netherlands", 1000), ("entry", "Netherlands", 1000)]
    trade = store.find_position("Netherlands", "dry_run")
    assert trade.side == "lay" and trade.stake == 3000
    assert round(trade.liability) == 650  # 450 + 150 + 50: all that is lost if Netherlands win
    assert len(store.active_ladders("dry_run")) == 1  # still watched, for the bookset at even


def test_the_first_entry_can_be_a_lower_step(store):
    pipe, site, _ = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.15, 1.05)
    assert len(entries(site)) == 2 and store.find_position("Netherlands", "dry_run").stake == 2000


def test_a_swing_up_stops_the_entries_and_the_trade_stays(store):
    pipe, site, _ = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.45, 1.75, 1.15, 1.05)
    assert len(entries(site)) == 1
    assert store.find_position("Netherlands", "dry_run").stake == 1000
    assert store.active_ladders("dry_run")[0].stopped  # still watched, for the bookset at even


def test_a_rise_below_the_stop_keeps_the_steps(store):
    pipe, site, _ = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.45, 1.65, 1.4, 1.15)
    assert len(entries(site)) == 2  # 1.45 and 1.15, not 1.4 again


def test_a_price_high_before_any_entry_stops_nothing(store):
    pipe, site, _ = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.9, 1.45)
    assert len(entries(site)) == 1


def test_the_favourite_tipped_gets_no_entry_until_the_other_team_comes_down(store):
    """31 Oct: SK 'Adelaide' with Adelaide 1.50 and Stars 2.92."""
    pipe, site, notifier = sk(store, live=2.92)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 2.92, 3.4)
    assert entries(site) == []
    site.gone = True
    look(pipe, site, *[3.4] * 15)
    assert store.active_ladders("dry_run") == []
    assert "0 of 3 entries, Netherlands first read at 2.92" in notifier.sent[-1]


def test_a_comeback_at_once_is_one_step_booked_at_even(store):
    """The user's first way, 1 Nov: NZ 1.50, India 3: lay 1k. India comes to equal: out, a quarter of a unit."""
    pipe, site, _ = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.5, 1.75, 1.9)
    trade = store.find_position("Netherlands", "dry_run")
    assert trade.stake == 1000 and round(booked_profit(trade, 2.0)) == 250
    look(pipe, site, 2.0)
    assert ("close", "Netherlands", None) in site.calls and store.find_position("Netherlands", "dry_run") is None
    assert len(entries(site)) == 1 and store.active_ladders("dry_run") == []


def test_falling_behind_takes_the_lower_steps_then_books_at_even(store):
    """The user's second way: India goes to 6 without coming to 1.8-2.0: NZ laid again at 1.2 and 1.1, and equal or India
    favourite books it all: about the one unit they aim for, from at most 0.8 of a unit at risk."""
    pipe, site, _ = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.5, 1.2, 1.1, 1.4, 1.75)
    trade = store.find_position("Netherlands", "dry_run")
    assert len(entries(site)) == 3 and trade.stake == 3000 and round(trade.liability) == 800
    assert round(booked_profit(trade, 2.0)) == 1100
    look(pipe, site, 2.04)
    assert site.calls[-1] == ("close", "Netherlands", None) and store.active_ladders("dry_run") == []


def test_your_own_channels_win_call_runs_the_same_steps_and_books_as_told(store):
    profile = Profile("mine", rules="mine", ladder=True, enter_before_start_min=10)
    pipe, site, _ = make(store, profile, {}, live=1.6, mode="dry_run", site_class=LadderSite)
    pipe._cricbuzz = Scores()
    tip(pipe, "IND vs NZ\nIndia win")
    assert [ladder.team for ladder in store.active_ladders("dry_run")] == ["India"]
    look(pipe, site, 1.5)
    assert entries(site) == [("entry", "Netherlands", 1000)]
    tip(pipe, "Bookset")
    assert site.calls[-1] == ("close", "Netherlands", None) and store.active_ladders("dry_run") == []


def two_channels(store, mode):
    s = make_settings(dry_run=False)
    channels = Channels(s, [Channel("SK", Profile("sk", ladder=True), {CHAT}),
                            Channel("Mine", Profile("mine", rules="mine", ladder=True), {CHAT - 1})])
    risk = RiskManager(s, store, mode, channels)
    site = LadderSite(risk, 1.6)
    notifier = FakeNotifier()
    pipe = Pipeline(s, mode, site, risk, store, notifier, channels)
    pipe._parser = FakeParser({MATCH_WIN: [signal(**UAE)]})
    pipe._cricbuzz = Scores()
    return pipe, site, notifier


def test_sk_and_your_channel_on_one_team_are_one_trade_booked_from_either(store):
    """SIGNAL_CONFIRMED: one match, one ₹3,000 at most, live and on paper; a bookset in either channel books it."""
    for mode in ("live", "dry_run"):
        pipe, site, notifier = two_channels(store, mode)
        tip(pipe, MATCH_WIN)
        asyncio.run(pipe._handle(msg("UAE win", chat=CHAT - 1)))
        assert "SIGNAL_CONFIRMED, SK and Mine both say" in notifier.sent[-1]
        (ladder,) = store.active_ladders(mode)
        assert (ladder.channel, ladder.confirmed_by) == ("SK", "Mine")
        look(pipe, site, 1.5, 1.2, mode=mode)
        assert entries(site) == [("entry", "Netherlands", 1000), ("entry", "Netherlands", 1000)]
        assert "SIGNAL_CONFIRMED, SK + Mine" in asyncio.run(pipe.command("/sk"))
        asyncio.run(pipe._handle(msg("Bookset", chat=CHAT - 1)))
        assert site.calls[-1] == ("close", "Netherlands", None) and store.find_position("Netherlands", mode) is None


def test_sk_and_your_channel_on_two_teams_block_the_entries_until_you_decide(store):
    """SIGNAL_CONFLICT: 'Netherlands win' against SK's UAE, the other team known or named."""
    pipe, site, notifier = two_channels(store, "dry_run")
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.6)  # the other team is read: Netherlands
    asyncio.run(pipe._handle(msg("Netherlands win", chat=CHAT - 1)))
    assert "SIGNAL_CONFLICT: SK says United Arab Emirates, Mine says Netherlands" in notifier.sent[-1]
    (ladder,) = store.active_ladders("dry_run")
    assert ladder.conflict and ladder.state == "SIGNAL_CONFLICT"
    look(pipe, site, 1.45, 1.15)
    assert entries(site) == []
    assert "decided" in asyncio.run(pipe.command("/sk UAE Match Win"))
    look(pipe, site, 1.15)
    assert len(entries(site)) == 1


def test_a_tip_naming_the_other_tips_team_as_its_opponent_is_a_conflict_at_once(store):
    pipe, site, _ = two_channels(store, "dry_run")
    store.add_ladder("dry_run", "Mine", "Netherlands", "Netherlands v ?", datetime.now(), None)
    tip(pipe, MATCH_WIN)  # 'UAE v Netherlands'
    assert len(store.active_ladders("dry_run")) == 1 and store.active_ladders("dry_run")[0].conflict


def test_a_conflict_found_only_when_the_other_team_is_read_blocks_both_in_that_look(store):
    pipe, site, notifier = two_channels(store, "dry_run")
    pipe._parser = FakeParser({MATCH_WIN: [signal(selection="UAE", event="UAE v ?", sport="cricket", odds=None)]})
    tip(pipe, MATCH_WIN)
    store.add_ladder("dry_run", "Mine", "Netherlands", "Netherlands v ?", datetime.now(), None)
    look(pipe, site, 1.45)
    assert [ladder.conflict for ladder in store.active_ladders("dry_run")] == [True, True]
    assert entries(site) == [] and any("SIGNAL_CONFLICT" in text for text in notifier.sent)


def test_your_tip_for_the_other_team_can_replace_a_blocked_one_with_no_entries(store):
    pipe, site, _ = two_channels(store, "dry_run")
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.6)
    asyncio.run(pipe._handle(msg("Netherlands win", chat=CHAT - 1)))
    asyncio.run(pipe.command("/sk Netherlands Match Win"))
    (ladder,) = store.active_ladders("dry_run")
    assert ladder.team == "Netherlands" and not ladder.conflict


def direction(prices):
    """What a lay made at these prices would lock if booked at each later price: a lay gains as the price rises."""
    from tradebot.store import Position
    total = sum(prices)
    trade = Position(1, "", "cricket", "A v B", "match_odds", "NZ", "lay", total / len(prices), 1000.0 * len(prices), None, None)
    return lambda price: round(booked_profit(trade, price))


def test_a_lays_pl_rises_with_the_laid_teams_price_and_the_spec_numbers_hold():
    one = direction([1.5])
    assert one(1.15) == -304 and one(1.5) == 0 and one(1.8) == 167 and one(2.0) == 250 and one(3.0) == 500
    two = direction([1.5, 1.15])
    assert two(1.15) == -304 and two(2.0) == 675 and two(3.0) == 1117
    three = direction([1.5, 1.15, 1.1])
    assert three(1.1) == -409 and three(1.3) == 115 and three(1.95) == 1077 and three(2.0) == 1125 and three(3.0) == 1750


def test_1_a_first_entry_at_1_50_is_a_1000_lay_risking_500(store):
    pipe, site, _ = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.5)
    trade = store.find_position("Netherlands", "dry_run")
    assert (trade.side, trade.stake, trade.liability) == ("lay", 1000, 500)
    (ladder,) = store.active_ladders("dry_run")
    assert ladder.state == "ENTRY_1_ACTIVE" and ladder.entries == 1 and ladder.entry_prices == "1.5"


def test_2_a_fall_to_1_15_takes_entry_2_though_it_is_a_loss_so_far(store):
    pipe, site, _ = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.5)
    assert round(booked_profit(store.find_position("Netherlands", "dry_run"), 1.15)) == -304
    look(pipe, site, 1.15)
    trade = store.find_position("Netherlands", "dry_run")
    assert trade.stake == 2000 and round(trade.liability) == 650
    (ladder,) = store.active_ladders("dry_run")
    assert ladder.state == "ENTRY_2_ACTIVE" and ladder.entry_prices == "1.5,1.15"


def test_3_and_8_a_rise_to_1_80_stops_the_entries_for_good_though_it_is_a_profit(store):
    pipe, site, notifier = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.5)
    assert round(booked_profit(store.find_position("Netherlands", "dry_run"), 1.8)) == 167
    look(pipe, site, 1.8)
    assert "no more entries in this match" in notifier.sent[-1]
    (ladder,) = store.active_ladders("dry_run")
    assert ladder.stopped and ladder.state == "STOP_LOSS_TRIGGERED"
    look(pipe, site, 1.4, 1.15, 1.05)  # comes back: no re-entry
    assert len(entries(site)) == 1


def test_4_equal_odds_below_the_target_is_an_exit(store):
    """1.95 / 1.95: the two teams' own prices decide, not one team's reaching 2.00."""
    pipe, site, notifier = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.5)
    site.team = 1.95
    look(pipe, site, 1.95)
    assert site.calls[-1] == ("close", "Netherlands", None) and store.active_ladders("dry_run") == []
    assert any("EQUAL-ODDS EXIT: United Arab Emirates 1.95, Netherlands 1.95, P&L +231" in text for text in notifier.sent)


def test_exit_call_is_the_users_strict_and():
    def call(pl, team, other, peak=0.0, was_favourite=False):
        return exit_call(pl, team, other, 1000.0, 200.0, max(peak, pl), was_favourite)

    assert call(1200, 1.4, 3.0) == BOOKSET  # test 5, scenario 3
    assert call(1500, 2.2, 1.8) == WAIT_FAVOURITE  # test 6, scenario 1
    assert call(500, 1.4, 3.0) == WAIT_PROFIT  # test 7, scenario 2
    assert call(5000, 2.5, 1.7) == WAIT_FAVOURITE  # the special scenario: big profit, no favourite
    assert call(231, 1.95, 1.95) == EQUAL_EXIT  # test 4
    assert call(1051, 1.95, 1.95) == WAIT_FAVOURITE  # all three at equal: over the target, a tick from favourite
    assert call(-304, 6.0, 1.15) == "" and call(167, 2.25, 1.8) == ""
    assert call(118, 2.4, 1.7, peak=423, was_favourite=True) == FELL_BACK
    assert call(765, 2.4, 1.7, peak=1021) == GIVEBACK


def test_5_all_three_entries_book_when_the_target_and_the_favourite_are_both_there(store):
    pipe, site, notifier = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.5, 1.15, 1.1)
    site.team = 1.4
    look(pipe, site, 3.0)
    assert site.calls[-1] == ("close", "Netherlands", None) and store.active_ladders("dry_run") == []
    assert any("BOOKSET TRIGGERED, both conditions: P&L +1,750 and United Arab Emirates favourite" in text for text in notifier.sent)
    assert store.recent_ladders("dry_run")[0].state == "BOOKSET_TRIGGERED"


def test_6_the_profit_without_the_favourite_waits_and_says_so_once(store):
    pipe, site, notifier = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.5, 1.15, 1.1)
    site.team = 1.95
    look(pipe, site, 1.95, 1.95)
    assert store.find_position("Netherlands", "dry_run") is not None
    told = [text for text in notifier.sent if "PROFIT CONDITION SATISFIED, WAITING FOR FAVOURITE CONFIRMATION" in text]
    assert len(told) == 1 and store.active_ladders("dry_run")[0].state == "BOOKSET_WAITING"
    site.team = 1.9
    look(pipe, site, 2.0)
    assert store.find_position("Netherlands", "dry_run") is None


def test_7_the_favourite_without_the_profit_holds_when_the_price_jumped_past_equal(store):
    pipe, site, _ = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.5)
    site.team = 1.45
    look(pipe, site, 3.0)  # +500, a wicket's jump in one read
    assert store.find_position("Netherlands", "dry_run") is not None


def test_a_jump_past_equal_is_out_when_the_favourite_is_lost(store):
    pipe, site, notifier = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.5)
    site.team = 1.6
    look(pipe, site, 2.6)
    site.team = 2.4
    look(pipe, site, 1.7)
    assert store.find_position("Netherlands", "dry_run") is None
    assert any("was favourite and lost it" in text for text in notifier.sent)


def test_a_profit_over_the_target_falling_back_is_out(store):
    pipe, site, notifier = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.5, 1.2, 1.1)
    site.team = 1.95
    look(pipe, site, 1.92)  # +1,021, not favourite
    site.team = 2.4
    look(pipe, site, 1.7)  # +765
    assert store.find_position("Netherlands", "dry_run") is None
    assert any("profit giving back" in text for text in notifier.sent)


def test_9_a_stale_market_gets_no_entry_until_it_moves(store):
    pipe, site, notifier = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.6)
    for _ in range(20):
        store.log_price(-store.active_ladders("dry_run")[0].id, 1.45)
    look(pipe, site, 1.45, 1.45)
    assert entries(site) == []
    blocked = [text for text in notifier.sent if "BLOCKED: market PAUSED" in text]
    assert len(blocked) == 1
    look(pipe, site, 1.44)
    assert len(entries(site)) == 1


def test_10_a_repeated_tip_makes_no_second_watch_or_entry(store):
    pipe, site, notifier = sk(store)
    tip(pipe, MATCH_WIN)
    tip(pipe, MATCH_WIN)
    assert len(store.active_ladders("dry_run")) == 1
    look(pipe, site, 1.45)
    assert len(entries(site)) == 1


def lost(store, pl, minutes_ago=0):
    store.open_position(signal(selection="Old", event="Old v ?", channel="Tip"), "dry_run", 3.0, 1000)
    position = store.find_position("Old", "dry_run")
    store.set_closed(position.id, pl=pl, how="settled")
    when = (datetime.now() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    store._db.execute("UPDATE positions SET closed_at = ? WHERE id = ?", (when, position.id))


def test_the_limits_block_a_new_match_not_the_steps_of_an_open_one(store):
    pipe, site, notifier = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.45)
    lost(store, -3000)
    look(pipe, site, 1.15)
    assert len(entries(site)) == 2  # the open match goes on


def test_the_days_loss_limit_blocks_a_new_match(store):
    pipe, site, notifier = sk(store)
    lost(store, -3000)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.45)
    assert entries(site) == [] and any("day's loss limit of 3 limits" in text for text in notifier.sent)


def test_a_new_match_cools_down_after_a_losing_trade(store):
    pipe, site, notifier = sk(store)
    lost(store, -500, minutes_ago=10)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.45)
    assert entries(site) == [] and any("cooling down for 30 min" in text for text in notifier.sent)
    store._db.execute("UPDATE positions SET closed_at = ?", ((datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"),))
    look(pipe, site, 1.44)
    assert len(entries(site)) == 1


def test_the_status_shows_everything_about_a_tip(store):
    pipe, site, _ = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.5, 1.15)
    text = asyncio.run(pipe.command("/sk"))
    for part in ("UAE v Netherlands", "lays Netherlands", "ENTRY_2_ACTIVE", "Favourite: Netherlands", "1 @1.5 · 2 @1.15 · 3 waiting",
                 "Stake 2,000, liability 650, P&L now", "Target 1,000: no", "Bookset: no", "Entries stopped: no"):
        assert part in text, part


def test_skes_usual_calls_are_read_without_the_ai():
    for text, name in (("UAE Toss or Match Win", "UAE"), ("Adelaide Toss or Match Win", "Adelaide"), ("Sri Lanka Even or Win\n\nEqual Match", "Sri Lanka"),
                       ("IND Even or 50p", "IND"), ("Eng win or Bookset", "Eng"), ("Sydney Match Win", "Sydney")):
        (sig,) = parse_known(text, "sk")
        assert (sig.action, sig.selection, sig.side) == ("entry", name, "back"), text
    assert [s.selection for s in parse_known("UAE Toss or Match Win\nAdelaide Toss or Match Win", "sk")] == ["UAE", "Adelaide"]
    assert parse_known("Sixers Bookset", "sk")[0].action == "close"


def test_skes_results_and_talk_are_left_to_the_ai():
    for text in ("Today \n\nAds Win ✅\nPRS Win ✅ (YouTube)\nUAE Win ✅\nSL Even ✅ & bookset", "Sydney Toss Loss \nSydney Match Win ✅",
                 "SL 50p to 50p ✅", "Hong Kong Sixes \nNot available in sites", "If Enter Bookset"):
        assert parse_known(text, "sk") is None, text


def test_your_channel_and_skes_usual_calls_need_no_ai_at_all(store):
    """The AI's free limit can run out in a day. The channels the strategy rests on must not stop with it."""
    channels = Channels.load(make_settings(), Path(__file__).resolve().parent.parent / "channels.example.json")
    assert channels.get("MY Tips").profile.rules_only and channels.get("SK Astro").profile.rules_first
    mine = Profile("mine", rules="mine", rules_only=True, ladder=True)
    pipe, site, _ = make(store, mine, {}, live=1.6, mode="dry_run", site_class=LadderSite)
    pipe._cricbuzz = Scores()
    tip(pipe, "IND vs NZ\nIndia win")
    tip(pipe, "Good morning all")
    assert pipe._parser.calls == 0 and [ladder.team for ladder in store.active_ladders("dry_run")] == ["India"]
    look(pipe, site, 1.5)
    tip(pipe, "Bookset")
    assert site.calls[-1] == ("close", "Netherlands", None) and pipe._parser.calls == 0


def test_sks_call_goes_in_without_the_ai_and_other_posts_still_go_to_it(store):
    sk_channel = Profile("sk", rules="sk", rules_first=True, ladder=True)
    pipe, site, _ = make(store, sk_channel, {"UAE plays well today": []}, live=1.6, mode="dry_run", site_class=LadderSite)
    pipe._cricbuzz = Scores()
    tip(pipe, MATCH_WIN)
    assert pipe._parser.calls == 0 and len(store.active_ladders("dry_run")) == 1
    tip(pipe, "UAE plays well today")
    assert pipe._parser.calls == 1


def test_a_match_win_call_goes_in_when_the_ai_cant_read_it(store):
    match = Match(2, "x", SIXERS, "Preview", datetime.now() + timedelta(minutes=35), "T20")
    pipe, site, _ = sk(store, matches=[match])

    async def out_of_limit(*args, **kwargs):
        return None

    pipe._parse_while_fresh = out_of_limit
    tip(pipe, "Sydney Toss Loss\nSydney Match Win ✅")
    assert [ladder.team for ladder in store.active_ladders("dry_run")] == ["Sydney"]


def test_a_limit_on_units_ends_the_steps_quietly(store):
    profile = Profile("sk", ladder=True, settings={"max_units_per_bet": 2})
    pipe, site, notifier = make(store, profile, {MATCH_WIN: [signal(**UAE)]}, live=1.6, mode="dry_run", site_class=LadderSite)
    pipe._cricbuzz = Scores()
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.5, 1.2)
    sent = len(notifier.sent)
    look(pipe, site, 1.1, 1.05, 1.03)
    assert len(entries(site)) == 2 and len(notifier.sent) == sent and store.active_ladders("dry_run")[0].stopped


def test_a_live_run_ignores_the_paper_only_channels(store):
    """DRY_RUN=false is one switch for the whole bot: only the channels chosen for production may reach real money."""
    script = {"Back x": [signal(odds=3.0)]}
    pipe, site, _ = make(store, Profile("ss", paper_only=True), script, mode="live", live=3.0)
    tip(pipe, "Back x")
    assert site.calls == []
    pipe, site, _ = make(store, Profile("ss", paper_only=True), script, mode="dry_run", live=3.0)
    tip(pipe, "Back x")
    assert len(site.calls) == 1
    pipe, site, _ = make(store, Profile("sk"), {"Back y": [signal(odds=3.0, selection="Y One")]}, mode="live", live=3.0)
    tip(pipe, "Back y")
    assert len(site.calls) == 1


def test_only_sk_and_your_channel_can_go_live():
    channels = Channels.load(make_settings(), Path(__file__).resolve().parent.parent / "channels.example.json")
    assert sorted(c.name for c in channels.all() if not c.profile.paper_only and c.profile.trade) == [
        "MY Tips", "SK Astro", "TEST group"]


def test_your_calls_are_read_by_rule():
    for text in ("India win", "India Match Win", "IND vs NZ\nIndia win", "IND vs NZ India win 3 limit", "India Even or Win",
                 "India Toss or Match Win", "India win or bookset"):
        (sig,) = parse_known(text, "mine")
        assert (sig.action, sig.selection, sig.side) == ("entry", "India", "back"), text
    assert parse_known("Sinner win", "mine")[0].selection == "Sinner"
    assert parse_known("Bookset", "mine")[0].action == "close"
    assert parse_known("Loss cut", "mine")[0].loss_cut
    assert parse_known("Good morning", "mine") is None


def test_a_tip_before_its_match_opens_nothing_until_then(store):
    match = Match(1, "x", [("United Arab Emirates", "UAE"), ("Netherlands", "NED")], "Preview",
                  datetime.now() + timedelta(hours=3), "ODI")
    pipe, site, notifier = sk(store, matches=[match])
    tip(pipe, MATCH_WIN)
    assert "SK method" in notifier.sent[-1]
    look(pipe, site, 1.45)
    assert site.looks == 0 and entries(site) == []
    ladder = store.active_ladders("dry_run")[0]
    assert datetime.fromisoformat(ladder.start_at) == (match.start - timedelta(minutes=10)).replace(microsecond=0)
    earlier = (datetime.now() - timedelta(seconds=5)).isoformat(timespec="seconds")
    store.update_ladder(ladder.id, start_at=earlier)
    look(pipe, site, 1.45)
    assert len(entries(site)) == 1


SIXERS = [("Sydney Sixers Women", "SYSW"), ("Hobart Hurricanes Women", "HBHW")]


def test_a_match_win_call_shortly_before_the_match_starts_a_watch(store):
    """1 Nov 13:05: 'Sydney Toss Loss / Sydney Match Win ✅', read as a result, 35 min before the match."""
    match = Match(2, "x", SIXERS, "Preview", datetime.now() + timedelta(minutes=35), "T20")
    pipe, site, _ = sk(store, matches=[match])
    tip(pipe, "Sydney Toss Loss\nSydney Match Win ✅")
    assert [ladder.team for ladder in store.active_ladders("dry_run")] == ["Sydney"]


def test_a_match_win_after_the_match_is_a_result(store):
    over = Match(2, "x", SIXERS, "Complete", datetime.now() - timedelta(hours=3), "T20")
    pipe, site, _ = sk(store, matches=[over])
    tip(pipe, "Sydney Toss Loss\nSydney Match Win ✅")
    tomorrow = Match(3, "x", SIXERS, "Preview", datetime.now() + timedelta(hours=20), "T20")
    pipe._cricbuzz = Scores([tomorrow])  # the same team's next match is too far off for this to be a call on it
    tip(pipe, "Sydney Match Win ✅")
    assert store.active_ladders("dry_run") == []


def test_the_tipsters_bookset_books_the_lay_of_the_other_team(store):
    bookset = dict(UAE, action="close")
    pipe, site, _ = sk(store, {"UAE Bookset": [signal(**bookset)]})
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.45)
    tip(pipe, "UAE Bookset")
    assert site.calls[-1] == ("close", "Netherlands", None)
    assert store.find_position("Netherlands", "dry_run") is None and store.active_ladders("dry_run") == []
    look(pipe, site, 1.15)
    assert len(entries(site)) == 1


def test_a_bookset_before_any_entry_ends_the_watch(store):
    pipe, site, _ = sk(store, {"UAE Bookset": [signal(**dict(UAE, action="close"))]})
    tip(pipe, MATCH_WIN)
    tip(pipe, "UAE Bookset")
    assert not [call for call in site.calls if call[0] == "close"] and store.active_ladders("dry_run") == []


def test_your_cancel_stops_the_entries_and_keeps_the_trade(store):
    pipe, site, _ = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.45)
    reply = asyncio.run(pipe.command("/cancel UAE"))
    assert "no more entries" in reply and store.find_position("Netherlands", "dry_run") is not None
    assert store.active_ladders("dry_run") == []


def test_your_bookset_reaches_the_trade_by_the_tipped_team(store):
    pipe, site, _ = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.45)
    asyncio.run(pipe.command("/bookset UAE"))
    assert site.calls[-1] == ("close", "Netherlands", None)


def test_a_tip_you_send_from_your_id_is_treated_as_sks(store):
    pipe, site, _ = sk(store)
    asyncio.run(pipe.command("/sk Sri Lanka Even or Win"))
    ladder = store.active_ladders("dry_run")[0]
    assert ladder.team == "Sri Lanka"
    assert "Sri Lanka" in asyncio.run(pipe.command("/sk"))


def test_the_same_tip_twice_is_watched_once(store):
    pipe, site, _ = sk(store)
    tip(pipe, MATCH_WIN)
    tip(pipe, MATCH_WIN)
    assert len(store.active_ladders("dry_run")) == 1


def test_other_channels_keep_their_entries(store):
    pipe, site, _ = make(store, Profile("tk"), {"Back x": [signal(odds=3.0)]}, mode="dry_run", site_class=LadderSite)
    tip(pipe, "Back x")
    assert entries(site) == [("entry", "Florian Broska", None)] and store.active_ladders("dry_run") == []


def test_the_days_in_sks_posts():
    assert jackpot_days_in("Nov 5 Jackpot Alert ⚠️", TODAY) == [date(2026, 11, 5)]
    assert jackpot_days_in("Oct 11  Jackpot Alert ⚠️", TODAY) == [date(2026, 10, 11)]
    assert jackpot_days_in("Fetched Wrong Match\nTomorrow Jackpot Day 👍🏻", TODAY) == [date(2026, 11, 2)]
    assert jackpot_days_in("Jan 2 Jackpot Alert", date(2026, 12, 30)) == [date(2027, 1, 2)]
    for text in ("November Month Jackpot Days\nNot open post", "12p Jackpot ✅", "Next Jackpot day", "Jackpot Day Given ✅",
                 "October Month Jackpot Summary\nOct 3 📅\n12p SL vs BAN"):
        assert jackpot_days_in(text, TODAY) == []


def test_days_typed_in_the_command():
    assert parse_day(["5", "Nov"], TODAY) == parse_day(["Nov", "5"], TODAY) == date(2026, 11, 5)
    assert parse_day(["today"], TODAY) == TODAY and parse_day(["tomorrow"], TODAY) == date(2026, 11, 2)
    assert parse_day(["2026-11-05"], TODAY) == date(2026, 11, 5)


def test_tips_typed_in_the_command():
    assert read_sk_tip("UAE Toss or Match Win") == ("UAE", "?")
    assert read_sk_tip("Sri Lanka Even or Win") == ("Sri Lanka", "?")
    assert read_sk_tip("UAE v Netherlands") == ("UAE", "Netherlands")


def test_sks_post_and_your_command_set_the_jackpot_days(store):
    pipe, site, notifier = sk(store)
    today = datetime.now().date()
    tip(pipe, f"{today:%b} {today.day} Jackpot Alert ⚠️")
    assert store.is_jackpot_day(today) and "jackpot day" in notifier.sent[-1]
    tomorrow = today + timedelta(days=1)
    assert "Jackpot day" in asyncio.run(pipe.command("/jackpot tomorrow")) and store.is_jackpot_day(tomorrow)
    asyncio.run(pipe.command("/jackpot off tomorrow"))
    assert not store.is_jackpot_day(tomorrow)
    assert "⚠️" in asyncio.run(pipe.command("/jackpot someday"))


def _ss(store, mode):
    return make(store, Profile("ss", jackpot_days_only=True), {"Back x": [signal(odds=3.0)]}, mode=mode)


def test_ss_goes_live_only_on_jackpot_days(store):
    pipe, site, notifier = _ss(store, "live")
    tip(pipe, "Back x")
    assert site.calls == [] and "not an SK jackpot day" in notifier.sent[-1]
    store.add_jackpot_day(datetime.now().date(), "test")
    tip(pipe, "Back x")
    assert entries(site) == [("entry", "Florian Broska", None)]


def test_on_paper_ss_trades_every_day_and_its_jackpot_days_are_reported_apart(store):
    pipe, site, _ = _ss(store, "dry_run")
    tip(pipe, "Back x")
    assert len(entries(site)) == 1
    assert "none yet" in asyncio.run(pipe.command("/jackpot"))
    store.add_jackpot_day(datetime.now().date(), "test")
    text = asyncio.run(pipe.command("/jackpot"))
    assert "Tip on those days: 1 trades" in text and "Tip on other days: 0 trades" in text
