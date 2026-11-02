"""The league or tournament of a trade, and /report by channel, sport, league and period."""
import asyncio
import json
from datetime import datetime, timedelta

from conftest import signal
from main import _date_range
from test_channels_rules import make
from tradebot.channels import Profile
from tradebot.fancy import Match, parse_matches
from tradebot.leagues import Draw, cricket_league, leagues_of, parse_draws, series_name, sport_of, tennis_league
from tradebot.notifier import _parts
from tradebot.store import period_start

NOW = datetime(2026, 11, 1, 14, 0)
WOMEN = [("Melbourne Stars Women", "MLSW"), ("Hobart Hurricanes Women", "HHW")]
ESPN_PAGE = json.dumps({"events": [{"name": "Erste Bank Open", "groupings": [
    {"grouping": {"displayName": "Men's Singles"}, "competitions": [
        {"competitors": [{"athlete": {"displayName": "Daniil Medvedev"}}, {"athlete": {"displayName": "Cameron Norrie"}}]},
        {"competitors": [{"athlete": {"displayName": "Felix Auger-Aliassime"}}, {"athlete": {"displayName": "Vít Kopřiva"}}]}]},
    {"grouping": {"displayName": "Men's Doubles"}, "competitions": [
        {"competitors": [{"roster": {"displayName": "Salisbury / Skupski"}}, {"roster": {"displayName": "Arevalo / Pavic"}}]}]},
]}]})


def match(teams, series, hours_ago=2.0):
    return Match(1, "x", teams, "Complete", NOW - timedelta(hours=hours_ago), "T20", series=series)


def test_a_series_is_named_without_its_year_and_the_womens_big_bash_is_one_league():
    assert series_name("Womens Big Bash League 2026") == series_name("WBBL 2026") == "Big Bash Women"
    assert series_name("Ireland tour of Namibia, 2026") == "Ireland tour of Namibia"
    assert series_name("ICC Cricket World Cup League Two 2023-27") == "ICC Cricket World Cup League Two"
    assert series_name("Big Bash League 2026-27") == "Big Bash League"


def test_cricbuzz_gives_a_matchs_series():
    page = '"matchInfo":{"matchId":5,"seriesName":"WBBL 2026","startDate":"1793000000000","team1":{"teamName":"A"},"team2":{"teamName":"B"}}'
    assert parse_matches(page)[0].series == "WBBL 2026"


def test_a_cricket_trade_is_named_by_its_match():
    stars = match(WOMEN, "Womens Big Bash League 2026")
    assert cricket_league([stars], "Melbourne Stars W v Hobart Hurricanes W", NOW) == "Big Bash Women"
    assert cricket_league([stars], "Hobart Hurricanes W v Melbourne Stars W", NOW) == "Big Bash Women"
    assert cricket_league([stars], "Toronto Kings v Montreal Warriors", NOW) is None
    assert cricket_league([stars], "Melbourne Stars W v Hobart Hurricanes W", NOW + timedelta(days=30)) is None


def test_the_senior_match_is_chosen_over_the_a_sides_and_the_nearest_one_over_an_earlier():
    senior = match([("New Zealand", "NZ"), ("India", "IND")], "India tour of New Zealand 2026")
    a_sides = match([("India A", "INDA"), ("New Zealand A", "NZA")], "India A tour of New Zealand, 2026", hours_ago=1)
    assert cricket_league([a_sides, senior], "New Zealand v India", NOW) == "India tour of New Zealand"
    tri_series = match([("Pakistan", "PAK"), ("Sri Lanka", "SL")], "Pakistan ODI Tri-Series, 2026")
    sixes = match([("Pakistan", "PAK"), ("Sri Lanka", "SL")], "Hong Kong Sixes 2026", hours_ago=48)
    assert cricket_league([sixes, tri_series], "Pakistan v Sri Lanka", NOW) == "Pakistan ODI Tri-Series"


def test_a_tennis_trade_is_named_by_its_tournament_singles_only():
    draws = parse_draws(ESPN_PAGE, "atp")
    assert len(draws) == 2 and draws[0].tournament == "ATP Erste Bank Open"
    assert tennis_league(draws, "Norrie v Medvedev") == "ATP Erste Bank Open"
    assert tennis_league(draws, "Fe Auger Aliassime v Kopriva") == "ATP Erste Bank Open"
    assert tennis_league(draws, "Medvedev v Kopriva") is None  # both are there, but not in one match
    assert tennis_league(draws, "Donski v Jasika") is None


def test_only_tennis_names_make_a_tennis_trade():
    assert sport_of("tennis", None) == "Tennis"
    assert sport_of("other", "WTA Guangzhou Open") == "Tennis"
    assert sport_of("other", "Big Bash Women") == sport_of("cricket", None) == "Cricket"


def test_what_cant_be_named_waits_two_days_then_gets_the_other_name():
    async def no_matches():
        return []

    async def one_draw(day):
        return [Draw("WTA Guangzhou Open", ["Xin Sun", "Harriet Dart"])]

    rows = [(1, NOW.isoformat(), "tennis", "Xin Sun v H Dart"), (2, NOW.isoformat(), "tennis", "Donski v Jasika"),
            (3, NOW.isoformat(), "cricket", "Toronto Kings v Montreal Warriors"),
            (4, (NOW - timedelta(days=3)).isoformat(), "tennis", "Donski v Jasika"),
            (5, (NOW - timedelta(days=3)).isoformat(), "cricket", "Toronto Kings v Montreal Warriors")]
    names = asyncio.run(leagues_of(rows, NOW, no_matches, one_draw))
    assert names == {1: "WTA Guangzhou Open", 4: "Tennis other", 5: "Other cricket"}


def test_espn_being_down_stops_only_the_tennis_names_and_gives_up_on_nothing():
    async def one_match():
        return [match([("New Zealand", "NZ"), ("India", "IND")], "India tour of New Zealand 2026")]

    async def no_page(day):
        raise OSError("ESPN is down")

    rows = [(1, (NOW - timedelta(days=3)).isoformat(), "tennis", "Donski v Jasika"),
            (2, NOW.isoformat(), "cricket", "New Zealand v India")]
    assert asyncio.run(leagues_of(rows, NOW, one_match, no_page)) == {2: "India tour of New Zealand"}


def test_a_long_message_goes_in_parts_cut_at_line_ends():
    text = "\n".join(f"line {i}" for i in range(1500))
    parts = _parts(text)
    assert len(parts) > 1 and all(len(part) <= 4000 for part in parts)
    assert "\n".join(parts) == text
    assert _parts("short") == ["short"]


def trade(store, selection, event, sport, channel, stake, pl, league=None, how="settled"):
    store.open_position(signal(selection=selection, event=event, sport=sport, channel=channel), "live", 3.0, stake)
    position = store.find_position(selection, "live")
    store.set_closed(position.id, pl=pl, how=how)
    if league:
        store.set_league(position.id, league)
    return position


def test_the_report_gives_channels_sports_and_leagues_in_limits_for_running_channels_only(store):
    pipe, _, _ = make(store, Profile("tip"))
    trade(store, "Sixers", "Sixers v Heat", "cricket", "Tip", 1000, 2900, "Big Bash Women")
    trade(store, "India", "India v New Zealand", "cricket", "Tip", 1000, -1000, "India tour of New Zealand")
    trade(store, "Jasika", "Donski v Jasika", "tennis", "Tip", 2000, 2146, "Tennis other")
    trade(store, "Sudheer pick", "X v Y", "cricket", "Removed channel", 1000, -1000, "Big Bash Women")
    report = asyncio.run(pipe.command("/report year"))
    assert "Removed channel" not in report and "1 limit = ₹1,000" in report
    assert "• Tip (Cricket, Tennis): 3 trades, 2 won 1 lost, 4.0 limits, +4.05" in report
    assert "*Cricket match*: 2 trades, 1 won 1 lost, 2.0 limits, +1.90" in report
    assert "• Big Bash Women: 1 trade, 1 won 0 lost, 1.0 limits, +2.90" in report
    assert "*Tennis*: 1 trade, 1 won 0 lost, 2.0 limits, +2.15" in report
    assert report.index("Big Bash Women") < report.index("India tour of New Zealand")  # the best first
    assert "Known result: +4046 (+4.05 limits)" in report


def test_the_report_leaves_out_voids_lists_idle_channels_and_keeps_unknowns_apart(store):
    pipe, _, _ = make(store, Profile("tip"))
    assert "No trades: Tip" in asyncio.run(pipe.command("/report"))
    trade(store, "Void pick", "A v B", "cricket", "Tip", 1000, 0.0, how="booked")
    assert "No trades: Tip" in asyncio.run(pipe.command("/report"))
    unknown = trade(store, "Andreeva", "Andreeva v ?", "tennis", "Tip", 1000, None, how="finished")
    report = asyncio.run(pipe.command("/report"))
    assert "1 trade, 0 won 0 lost, 1 unknown, 1.0 limits, +0.00" in report and "Not named yet" in report
    assert "If the 1 unknown ones lost: -1000" in report and f"/settle {unknown.id}" in report


def test_a_fancy_bet_counts_at_the_sites_line_when_it_was_read_else_at_the_tips(store):
    pipe, _, _ = make(store, Profile("tip"))
    tip = store.add_fancy("live", "Tip", 1, "A v B", "ZIM", "runs", 10, 58.0, "no", None, 1.0, site_line=56.0)
    store.settle_fancy(tip, "won", 1.0, "57 after over 10", -1.0)  # tip's line won, the site's lost: 1 Nov
    other = store.add_fancy("live", "Tip", 2, "A v B", "NZ", "runs", 6, 57.0, "yes", None, 1.0)
    store.settle_fancy(other, "won", 1.0, "58 after over 6")
    void = store.add_fancy("live", "Tip", 3, "A v B", "NZ", "runs", 20, 179.0, "no", None, 1.0)
    store.settle_fancy(void, "void", 0.0, "a result post, not a call")
    store.set_fancy_league(tip, "Afghanistan vs Zimbabwe in UAE")
    report = asyncio.run(pipe.command("/report"))
    assert "• Tip (Fancy): 2 trades, 1 won 1 lost, 2.0 limits, +0.00" in report
    assert "📐 Tip: at the tip's line +2.00; the site's line read on 1 bets, -1.00" in report
    assert "*Cricket fancy*: 2 trades" in report and "Afghanistan vs Zimbabwe in UAE: 1 trade" in report
    assert "Known result: +0 (+0.00 limits)" in report
    assert store.fancy_without_league("live", period_start("year"))[0][:1] == (other,)


def test_a_toss_call_has_its_own_line_at_its_small_stake(store):
    pipe, _, _ = make(store, Profile("tip"))
    tip = store.add_fancy("live", "Tip", 1, "Sydney Thunder W v ?", "Sydney Thunder W", "toss", 0, 0.0, "yes", 98, 0.05)
    store.settle_fancy(tip, "won", 0.049, "Sydney Thunder Women won the toss")
    report = asyncio.run(pipe.command("/report"))
    assert "• Tip (Toss): 1 trade, 1 won 0 lost, 0.1 limits, +0.05" in report and "*Cricket toss*: 1 trade" in report
    assert "📐 Tip" not in report and "Known result: +49" in report


def test_the_daily_report_opens_with_the_limits_of_each_period_and_leaves_out_removed_channels(store):
    pipe, _, _ = make(store, Profile("tip"))
    trade(store, "Sixers", "Sixers v Heat", "cricket", "Tip", 1000, 2900, "Big Bash Women")
    trade(store, "Jasika", "Donski v Jasika", "tennis", "Tip", 1000, -1000, "Tennis other")
    trade(store, "Other pick", "X v Y", "cricket", "Removed channel", 1000, -1000)
    text = pipe.day_report_text()
    assert "🏏 Match: day +2.90 · week +2.90 · month +2.90 · year +2.90" in text
    assert "🎾 Tennis: day -1.00 · week -1.00 · month -1.00 · year -1.00" in text
    assert "All: day +1.90 · week +1.90" in text
    assert text.index("Limits") < text.index("*Tip*") and "Removed channel" not in text


def test_a_past_month_or_day_can_be_asked_for(store):
    pipe, _, _ = make(store, Profile("tip"))
    old = trade(store, "Sixers", "Sixers v Heat", "cricket", "Tip", 1000, 2900, "Big Bash Women")
    store._db.execute("UPDATE positions SET opened_at = '2026-10-29T14:00:00' WHERE id = ?", (old.id,))
    trade(store, "Jasika", "Donski v Jasika", "tennis", "Tip", 1000, -1000, "Tennis other")
    october = asyncio.run(pipe.command("/report 2026-10"))
    assert "Known result: +2900" in october and "Tennis other" not in october and "from 26 Oct 19:00" in october
    assert "Known result: +2900" in asyncio.run(pipe.command("/report 2026-10-29"))
    assert "No trades: Tip" in asyncio.run(pipe.command("/report 2026-10-30"))
    assert "Big Bash Women" in asyncio.run(pipe.command("/report 2026"))
    assert "period must be" in asyncio.run(pipe.command("/report soon"))
    assert "month must be" in asyncio.run(pipe.command("/report 2026-13"))


def test_a_date_range_ends_with_its_month_or_year():
    assert _date_range("2026-10-29") == (datetime(2026, 10, 29), datetime(2026, 10, 30))
    assert _date_range("2026-12") == (datetime(2026, 12, 1), datetime(2027, 1, 1))
    assert _date_range("2026") == (datetime(2026, 1, 1), datetime(2027, 1, 1))
    assert _date_range("week") is None


def test_a_year_starts_on_the_first_of_january():
    assert period_start("year", datetime(2026, 11, 1, 21, 50)) == datetime(2026, 1, 1)
    assert period_start("month", datetime(2026, 11, 1, 21, 50)) == datetime(2026, 11, 1)
