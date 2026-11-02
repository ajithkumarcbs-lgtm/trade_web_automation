"""Fancy (session) tips on paper: reading the session, finding the team, settling from Cricbuzz's over scores."""
import asyncio
from datetime import datetime, timedelta

from conftest import signal
from test_channels_rules import CHAT, FakeNotifier, FakeSite, make, msg  # noqa: F401  (the same fakes as the pipeline tests)
from tradebot.channels import Profile
from tradebot.fancy import (Innings, Match, Session, coming_match, finished_match, parse_matches, parse_overs,
                            parse_scorecard, read_session, settle, site_session_row, team_fits)

LIVE_PAGE = ('<a href="/live-cricket-scores/155562/indw-vs-zimw-2nd-odi">x</a>'
             'self.__next_f.push([1,"{\\"matchInfo\\":{\\"matchId\\":155562,\\"seriesName\\":\\"x\\",'
             '\\"startDate\\":1792900000000,\\"state\\":\\"In Progress\\",'
             '\\"team1\\":{\\"teamId\\":1,\\"teamName\\":\\"India Women\\",\\"teamSName\\":\\"INDW\\"},'
             '\\"team2\\":{\\"teamId\\":2,\\"teamName\\":\\"Zimbabwe Women\\",\\"teamSName\\":\\"ZIMW\\"}},'
             '\\"matchScore\\":{}}"])')
OVERS_PAGE = ('{\\"overSummaryList\\":[{\\"inningsId\\":1,\\"overs\\":7,\\"score\\":35,\\"wickets\\":1,'
              '\\"ovrSummary\\":\\"1 Wd 1 \\",\\"batTeamName\\":\\"ZIMW\\"},'
              '{\\"inningsId\\":1,\\"overs\\":6,\\"score\\":33,\\"wickets\\":1,\\"ovrSummary\\":\\"1 W 4 Wd 6 0 1 \\",'
              '\\"batTeamName\\":\\"ZIMW\\",\\"batStrikerIds\\":[1,2]},{\\"inningsId\\":1,\\"overs\\":5,'
              '\\"score\\":20,\\"wickets\\":0,\\"ovrSummary\\":\\"0 1 2 N1 0 1 0 \\",\\"batTeamName\\":\\"ZIMW\\"},'
              '{\\"inningsId\\":1,\\"overs\\":4,\\"score\\":16,\\"wickets\\":0,\\"ovrSummary\\":\\"1 1 1 1 1 1 \\",'
              '\\"batTeamName\\":\\"ZIMW\\"}],\\"matchId\\":\\"155562\\"}')
SCORECARD_PAGE = ('{\\"scoreCard\\":[{\\"inningsId\\":1,\\"batTeamDetails\\":{\\"batTeamName\\":\\"Zimbabwe Women\\",'
                  '\\"batTeamShortName\\":\\"ZIMW\\"},\\"scoreDetails\\":{\\"runs\\":207,\\"wickets\\":10,'
                  '\\"overs\\":49.4},\\"ppData\\":{\\"pp_1\\":{\\"ppOversTo\\":10,\\"ppType\\":\\"Mandatory\\",'
                  '\\"runsScored\\":48}},\\"wicketsData\\":{\\"wkt_1\\":{\\"wktNbr\\":1,\\"wktRuns\\":33,'
                  '\\"wktOver\\":6.1},\\"wkt_2\\":{\\"wktNbr\\":2,\\"wktRuns\\":81}}}]}')


SITE_ROWS = ["15 Over MLSW 1", "15 Over MLSW", "20 Over MLSW 1", "20 Over MLSW", "1st 4 Wkt MLSW", "4th Wkt MLSW Run Bhav",
             "A Sutherland Runs", "4th Wkt Pship Boundaries MLSW", "16th Over Runs Only MLSW", "MLSW Power Surge Ball 1"]


def test_the_sites_row_of_a_session():
    """The rows of Adelaide Strikers W v Melbourne Stars W, 31 Oct: '15 Over MLSW 1' is the same session at other rates."""
    assert site_session_row(SITE_ROWS, Session("runs", 15, "Melbourne Stars W")) == 1
    assert site_session_row(SITE_ROWS, Session("runs", 15, "MLSW")) == 1
    assert site_session_row(SITE_ROWS, Session("runs", 0, "Melbourne Stars W")) == 3  # innings total: the longest over
    assert site_session_row(SITE_ROWS, Session("runs", 10, "MLSW")) is None  # no 10 over row listed
    assert site_session_row(SITE_ROWS, Session("runs", 15, "Adelaide Strikers W")) is None  # the other side's
    assert site_session_row(SITE_ROWS, Session("wicket", 4, "MLSW")) is None  # 'Run Bhav' is another market
    rows = ["6 Over NZ", "2nd Wkt AFG", "20 Over Runs West Indies"]
    assert site_session_row(rows, Session("runs", 6, "New Zealand")) == 0
    assert site_session_row(rows, Session("wicket", 2, "AFG MEN")) == 1
    assert site_session_row(rows, Session("runs", 20, "WEST IND")) == 2


def test_a_tip_is_also_settled_at_the_line_the_site_showed(store):
    """'10 OVER 102 YES NZ' when the site's YES was 98 @90: at 100 runs the tip's line lost, the site's won 0.9 units."""
    from main import _site_pl
    match = Match(1, "x", [("New Zealand", "NZ"), ("India", "IND")], "Complete", datetime.now(), "T20")
    match.innings[1] = Innings("NZ", {10: (100, 2)})
    tip = dict(kind="runs", number=10, team="NZ", side="yes", line=102.0, units=1.0, created_at=datetime.now().isoformat(),
               site_line=98.0, site_rate=90.0)
    assert _site_pl(tip, match) == 0.9
    assert _site_pl({**tip, "site_line": 104.0}, match) == -1.0
    assert _site_pl({**tip, "site_line": None}, match) is None
    fancy_id = store.add_fancy("live", "BF", None, "New Zealand v India", "NZ", "runs", 10, 102, "yes", None, 1, "10 Over NZ",
                               98, 90)
    assert store.open_fancy("live")[0]["site_line"] == 98
    store.settle_fancy(fancy_id, "lost", -1, "100 after over 10", 0.9)
    assert store.open_fancy("live") == []


def test_every_over_is_read_from_the_commentary_feed():
    """NZ v IND 27 Oct: '10 OVER 102' and '15 over 146' (Boss Fire) stayed unknown: the over-by-over page only
    lists the latest overs. The full-commentary feed has a separator at the end of every over (9.6 = over 10)."""
    from tradebot.fancy import parse_commentary
    feed = ('{"matchId": 161212, "commentary": [{"commentaryList": ['
            '{"commText": "x", "overSeparator": {"score": 146, "wickets": 3, "inningsId": 1, "overNum": 14.6,'
            ' "batTeamName": "NZ"}}, {"commText": "y"},'
            '{"overSeparator": {"score": 100, "wickets": 2, "inningsId": 1, "overNum": 9.6, "batTeamName": "NZ"}}]}]}')
    match = Match(161212, "x", [("New Zealand", "NZ"), ("India", "IND")], "Complete", datetime.now(), "T20")
    assert parse_commentary(feed, match) == 2
    assert match.innings[1].overs == {10: (100, 2), 15: (146, 3)}
    assert settle(Session("runs", 10, "NZ"), "yes", 102, match) == (False, "100 after over 10")
    assert settle(Session("runs", 15, "NZ"), "yes", 146, match) == (True, "146 after over 15")
    assert parse_commentary("not json", match) == 0


def _coming(hours, *teams) -> Match:
    return Match(int(hours * 10), "x", list(teams), "Preview", datetime.now() + timedelta(hours=hours), "ODI")


SL, PAK, OMAN = ("Sri Lanka", "SL"), ("Pakistan", "PAK"), ("Oman", "OMA")


def test_the_coming_match_of_a_tip():
    sixes, odi = _coming(1, SL, OMAN), _coming(3, PAK, SL)  # SK Astro 27 Oct: 'PAK vs SL / SL even or Win'
    now = datetime.now()
    assert coming_match([sixes, odi], "Sri Lanka", "Pakistan v Sri Lanka", now) is odi  # the opponent it names
    assert coming_match([sixes], "Sri Lanka", "Pakistan v Sri Lanka", now) is None  # 31 Oct: that match is on, this is another
    assert coming_match([sixes, odi], "Sri Lanka", "Sri Lanka v ?", now) is sixes  # no opponent: the soonest
    assert coming_match([_coming(-1, PAK, SL)], "Sri Lanka", "Sri Lanka v ?", now) is None  # already on
    assert coming_match([_coming(30, PAK, SL)], "Sri Lanka", "Sri Lanka v ?", now) is None  # not today
    assert coming_match([sixes], "India", "India v ?", now) is None


def _sk(store, start_in_hours):
    class Site(FakeSite):
        async def quote(self, sig):
            return 2.78

    class Scores:
        async def refresh_list(self):
            return [_coming(start_in_hours, PAK, SL)]

    tip = signal(selection="Sri Lanka", event="Pakistan v Sri Lanka", sport="cricket", odds=None)
    script = {"SL even or Win": [tip],
              "Bookset": [signal(action="close", selection="Sri Lanka", event="Pakistan v Sri Lanka", odds=None)]}
    pipe, site, notifier = make(store, Profile("sk", enter_before_start_min=10), script, live=2.6, site_class=Site)
    pipe._cricbuzz = Scores()
    return pipe, site, notifier


def test_a_tip_hours_before_its_match_waits_for_the_start(store):
    pipe, site, notifier = _sk(store, 3)
    asyncio.run(pipe._handle(msg("SL even or Win")))
    assert site.calls == []  # not on the site yet
    assert "price now 2.78" in notifier.sent[-1]
    asyncio.run(pipe.retry_pending_once())
    assert site.calls == []  # hours to go: the site isn't opened
    (pending_id, _, held, _, _), = store.pending_instructions("live")
    earlier = held.model_copy(update={"not_before": (datetime.now() - timedelta(seconds=5)).isoformat(timespec="seconds")})
    store._db.execute("UPDATE pending SET signal_json = ? WHERE id = ?", (earlier.model_dump_json(), pending_id))
    asyncio.run(pipe.retry_pending_once())
    assert site.calls == [("entry", "Sri Lanka", None)] and store.pending_instructions("live") == []
    assert store.find_position("Sri Lanka", "live").price == 2.6  # the price at the match, not when tipped
    assert "price when tipped 2.78" in store._db.execute("SELECT reason FROM bets ORDER BY id DESC").fetchone()[0]


def test_a_tip_for_a_match_under_way_goes_at_once(store):
    pipe, site, _ = _sk(store, -1)
    asyncio.run(pipe._handle(msg("SL even or Win")))
    assert site.calls == [("entry", "Sri Lanka", None)]


def test_a_bookset_before_the_start_drops_the_waiting_tip(store):
    pipe, site, _ = _sk(store, 3)

    async def go():
        await pipe._handle(msg("SL even or Win"))
        await pipe._handle(msg("Bookset"))

    asyncio.run(go())
    assert site.calls == [] and store.pending_instructions("live") == []


def test_a_waiting_tip_is_dropped_while_paused(store):
    pipe, site, _ = _sk(store, 3)
    asyncio.run(pipe._handle(msg("SL even or Win")))
    (pending_id, _, held, _, _), = store.pending_instructions("live")
    earlier = held.model_copy(update={"not_before": datetime.now().isoformat(timespec="seconds")})
    store._db.execute("UPDATE pending SET signal_json = ? WHERE id = ?", (earlier.model_dump_json(), pending_id))
    pipe._paused = True
    asyncio.run(pipe.retry_pending_once())
    assert site.calls == [] and store.pending_instructions("live") == []


def _test_match(status="South Africa won by 120 runs", state="Complete", days_ago=3) -> Match:
    return Match(1, "x", [("South Africa", "RSA"), ("Australia", "AUS")], state, datetime.now() - timedelta(days=days_ago),
                 "TEST", status=status)


def test_the_finished_match_of_a_held_trade():
    now = datetime.now()
    event = "South Africa v Australia"
    assert finished_match([_test_match()], "Australia", event, now) is not None
    assert finished_match([_test_match()], "The Draw", event, now) is not None  # by the event's teams
    assert finished_match([_test_match(state="Stumps")], "Australia", event, now) is None  # still on
    assert finished_match([_test_match(days_ago=9)], "Australia", event, now) is None  # the Test before this one
    assert finished_match([_test_match()], "India", "India v ?", now) is None


def _held_trades(store, status, state="Complete"):
    class Scores:
        async def refresh_list(self):
            return [_test_match(status, state)]

    pipe, _, notifier = make(store, Profile("cs", settings={"both_sides_in_match": True}), mode="dry_run")
    pipe._cricbuzz = Scores()
    for team, price, stake in (("South Africa", 3.0, 2000), ("Australia", 4.0, 1500)):
        store.open_position(signal(channel="Tip", selection=team, event="South Africa v Australia", sport="cricket"),
                            "dry_run", price, stake)
    store._db.execute("UPDATE positions SET opened_at = ?", ((datetime.now() - timedelta(hours=13)).isoformat(),))
    asyncio.run(pipe._settle_long_holds())
    rows = store._db.execute("SELECT closed, closed_pl, closed_how FROM positions ORDER BY id").fetchall()
    return rows, notifier


def test_a_held_test_trade_settles_from_the_result(store):
    rows, notifier = _held_trades(store, "South Africa won by 120 runs")
    assert rows == [(1, 4000.0, "settled"), (1, -1500.0, "settled")]
    assert "WON" in notifier.sent[0] and "LOST" in notifier.sent[1]


def test_a_draw_loses_both_teams(store):
    assert [r[1] for r in _held_trades(store, "Match drawn")[0]] == [-2000.0, -1500.0]


def test_an_abandoned_match_is_void(store):
    assert [r[1] for r in _held_trades(store, "Match abandoned due to rain")[0]] == [0.0, 0.0]


def test_a_test_still_on_stays_open(store):
    assert [r[0] for r in _held_trades(store, "Day 4: Stumps", "Stumps")[0]] == [0, 0]


def test_an_order_unmatched_when_the_match_ends_is_no_bet(store):
    class Scores:
        async def refresh_list(self):
            return [_test_match()]

    pipe, site, _ = make(store, Profile("cs", settings={"both_sides_in_match": True}), mode="dry_run")
    pipe._cricbuzz = Scores()
    order = signal(channel="Tip", selection="Australia", event="South Africa v Australia", sport="cricket", odds=4.0,
                   when_odds=4.0, units=2)
    store.add_pending(order, "dry_run", None, "WAITING")
    asyncio.run(pipe._settle_long_holds())
    assert store.pending_instructions("dry_run") == [] and site.calls == []
    assert store._db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0


def test_reading_the_session():
    assert read_session("06 OVER WEST IND", "06 Over Runs WEST IND") == Session("runs", 6, "WEST IND")
    assert read_session("", "1st Wkt Zimbabwe") == Session("wicket", 1, "Zimbabwe")
    assert read_session("", "Lambi India") == Session("runs", 0, "India")
    assert read_session("", "Match Odds") is None


def test_team_names():
    assert team_fits("WEST IND", ["West Indies", "WI"])
    assert team_fits("ZIM", ["Zimbabwe Women", "ZIMW"])
    assert team_fits("ENG CHAMP", ["England Champions", "ENGC"])
    assert team_fits("AFG MEN", ["Afghanistan", "AFG"])
    assert not team_fits("IND", ["West Indies", "WI"])  # India is not West Indies
    assert not team_fits("ZIM", ["India Women", "INDW"])


def test_cricbuzz_pages_are_read():
    match = parse_matches(LIVE_PAGE)[0]
    assert (match.match_id, match.slug, match.state) == (155562, "indw-vs-zimw-2nd-odi", "In Progress")
    assert match.has_team("ZIM") and not match.has_team("PAK")
    assert parse_overs(OVERS_PAGE, match) == 4
    overs = match.innings[1].overs
    assert overs[6] == (33, 1) and overs[5] == (20, 0) and match.innings[1].team == "ZIMW"
    assert 7 not in overs  # being bowled: its score so far is no over score (NZ v IND 27 Oct: "108 after 6" was 118)
    assert parse_scorecard(SCORECARD_PAGE, match) == 1
    inn = match.innings[1]
    assert inn.total == (207, 10, 49.4) and inn.powerplay == (10, 48) and inn.fow == {1: 33, 2: 81}


def test_the_side_named_best_is_preferred():
    nz = Match(1, "x", [("New Zealand", "NZ"), ("India", "IND")], "In Progress", datetime.now(), "T20")
    nz_a = Match(2, "x", [("New Zealand A", "NZA"), ("India A", "INDA")], "Stumps", datetime.now(), "TEST")
    assert nz.team_score("NZ") > nz_a.team_score("NZ") > 0


def _match(overs: dict[int, tuple[int, int]], state="In Progress", second_innings=False) -> Match:
    match = Match(1, "x", [("India Women", "INDW"), ("Zimbabwe Women", "ZIMW")], state, datetime.now())
    match.innings[1] = Innings("ZIMW", dict(overs))
    if second_innings:
        match.innings[2] = Innings("INDW", {1: (8, 0)})
    return match


def test_runs_sessions_settle_from_the_over_score():
    match = _match({5: (20, 0), 6: (33, 1)})
    assert settle(Session("runs", 6, "ZIM"), "no", 34, match) == (True, "33 after over 6")  # '06 Over 34 Not'
    assert settle(Session("runs", 6, "ZIM"), "yes", 33, match)[0] is True  # yes wins at the line
    assert settle(Session("runs", 10, "ZIM"), "yes", 55, match) == (None, "")  # over 10 not bowled yet
    ended = _match({5: (20, 0), 6: (33, 10)}, second_innings=True)  # all out in over 6
    assert settle(Session("runs", 10, "ZIM"), "no", 38, ended) == (True, "33 (innings ended at over 6)")


def test_a_powerplay_still_on_settles_nothing():
    """1 Nov: '06 OVER 46 NOT ZIM' was won 3 min into the match, on the 2 runs of the powerplay so far."""
    teams = [("Zimbabwe", "ZIM"), ("Afghanistan", "AFG")]
    inn = Innings("ZIM", total=(2, 0, 0.5), powerplay=(6, 2))
    match = Match(1, "x", teams, "In Progress", datetime.now(), "T20", innings={1: inn})
    assert settle(Session("runs", 6, "ZIM"), "no", 46, match) == (None, "")
    inn.total, inn.powerplay = (51, 1, 6.0), (6, 51)
    assert settle(Session("runs", 6, "ZIM"), "no", 46, match) == (False, "51 after over 6 (powerplay)")


def test_wicket_sessions_settle_when_the_range_is_clear():
    match = _match({4: (16, 0), 5: (20, 0), 6: (33, 1)})  # the 1st wicket fell between 20 and 33
    assert settle(Session("wicket", 1, "ZIM"), "no", 18, match)[0] is False  # 'under 18': it fell after 20
    assert settle(Session("wicket", 1, "ZIM"), "no", 40, match)[0] is True  # fell by 33, under 40
    assert settle(Session("wicket", 1, "ZIM"), "no", 25, match)[0] is None  # 20-33 spans 25: not clear


def test_a_wicket_call_is_about_the_side_batting_when_it_was_given():
    """Boss Fire 27 Oct 20:14, Zim v AFG (T20, 20:00): '2nd WICKET CALL 42 K UNDER' while AFG batted; the AI
    named Zimbabwe from the match line. AFG's 2nd wicket fell at 14: the no call won."""
    start = datetime(2026, 10, 27, 20, 0)
    match = Match(172701, "x", [("Zimbabwe", "ZIM"), ("Afghanistan", "AFG")], "In Progress", start, "T20")
    match.innings[1] = Innings("AFG", fow={1: 11, 2: 14})
    match.innings[2] = Innings("ZIM", fow={1: 12})
    won, note = settle(Session("wicket", 2, "Zimbabwe"), "no", 42, match, datetime(2026, 10, 27, 20, 14))
    assert won is True and note == "wicket 2 fell at 14"


def test_a_fancy_tip_is_kept_on_paper_not_traded(store):
    script = {"06 OVER 56 YES WEST IND": [signal(market="fancy", market_name="06 Over Runs WEST IND",
                                                 selection="06 Over Runs WEST IND", side="yes", line=56, odds=None,
                                                 sport="cricket", event="West Indies v India")]}
    pipe, site, notifier = make(store, Profile("bf", fancy_paper=True), script)
    asyncio.run(pipe._handle(msg("06 OVER 56 YES WEST IND")))
    asyncio.run(pipe._handle(msg("06 OVER 56 YES WEST IND")))  # posted twice: kept once
    assert site.calls == [] and store.open_positions("live") == []
    tips = store.open_fancy("live")
    assert [(t["team"], t["kind"], t["number"], t["line"], t["side"], t["units"]) for t in tips] == \
        [("WEST IND", "runs", 6, 56, "yes", 1.0)]
    assert "FANCY on paper" in notifier.sent[0]
