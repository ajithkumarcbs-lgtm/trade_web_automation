"""Panther's calls on paper: a player's runs or balls and the toss, settled from Cricbuzz's scorecard; edited posts."""
import asyncio
import json
from datetime import datetime, timedelta

from conftest import signal
from pathlib import Path

from conftest import make_settings
from test_channels_rules import CHAT, make, msg
from tradebot.channels import Channels, Profile
from tradebot.fancy import Batter, Match, Session, parse_scorecard, read_session, same_batter, settle, site_session_row, team_fits
from tradebot.rules import parse_known

STARS = [("Melbourne Stars Women", "MLSW"), ("Hobart Hurricanes Women", "HHW")]
THUNDER = [("Sydney Thunder Women", "SYTW"), ("Melbourne Renegades Women", "MLRW")]
CARD = json.dumps({
    "tossResults": {"tossWinnerId": 357, "tossWinnerName": "Hobart Hurricanes Women", "decision": "Bowling"},
    "scoreCard": [{"inningsId": 1, "scoreDetails": {"runs": 192, "wickets": 4, "overs": 20}, "ppData": {}, "wicketsData": {},
                   "batTeamDetails": {"batTeamName": "Melbourne Stars Women", "batTeamShortName": "MLSW", "batsmenData": {
                       "bat_1": {"batName": "Meg Lanning", "runs": 71, "balls": 52, "outDesc": "lbw b Heather Graham"},
                       "bat_2": {"batName": "Danielle Gibson", "runs": 22, "balls": 11, "outDesc": "batting"},
                       "bat_3": {"batName": "Naomi Stalenberg", "runs": 0, "balls": 0, "outDesc": ""}}}}]},
    separators=(",", ":"))
TOSS = "𝐓𝐎𝐒𝐒 👉 #SYDNEY_THUNDER_W 🇦🇺\n\n➡️PLAY Only 5 % Of Your limit ✔️\n\n#PantherToss 🪙"


class Scores:
    def __init__(self, *matches):
        self.matches = list(matches)

    async def refresh_list(self):
        return self.matches

    async def refresh_overs(self, match):
        return 0

    async def refresh_scorecard(self, match):
        return 1

    async def refresh_commentary(self, match):
        return 0


def test_toss_calls_are_read_in_his_three_forms():
    call = parse_known(TOSS, "panther_toss")[0]
    assert (call.market, call.market_name, call.selection, call.side, call.units) == ("fancy", "Toss", "Sydney Thunder W", "yes", 0.05)
    call = parse_known("PANTHER TOSS PREDICTION 🇧🇷\n\n🎯  TOSS WINNER : #Sydney Sixers 🇦🇺\n\n➡️ [  SMALL LIMIT ONLY ]", "panther_toss")[0]
    assert (call.selection, call.units) == ("Sydney Sixers", 0.1)
    calls = parse_known("Hello Next  3 Match Tosses\n\n1. NZ 🇳🇿🇳🇿\n\n2. IRELAND 🇮🇪🇮🇪\n\n3. Hobart W 🇦🇺🇦🇺\n\nPlay all with Small Limits ✔️", "panther_toss")
    assert [c.selection for c in calls] == ["NZ", "Ireland", "Hobart W"] and all(c.units == 0.1 for c in calls)
    assert parse_known("𝐓𝐎𝐒𝐒 👉 #SRILANKA 🇱🇰🇱🇰\n\n➡️PLAY Only 5% Of Your limit", "panther_toss")[0].selection == "Sri Lanka"
    assert parse_known("𝐓𝐎𝐒𝐒 👉 #MELBOURNE_R 🇦🇺🇦🇺", "panther_toss")[0].selection == "Melbourne Renegades"


def test_his_toss_results_are_not_calls():
    for text in ("🇦🇺 SYDNEY THUNDER - W 🇦🇺 WON THE TOSS AND OPTED TO BOWL FIRST ✔️✔️", "3/3 TOSS PASS ❤️‍🔥", "Today 3/4 Toss ❤️👍🏻",
                 "Today Toss Work 🔂\n\n3/4 ❤️❤️", "Fail", "Pass ❤️✅️", "3/3 Toss is Same Limit\nEach 200-300/- Is enough"):
        assert parse_known(text, "panther_toss") is None


def test_a_player_and_a_toss_session_are_read():
    assert read_session("Player Runs", "Meg Lanning") == Session("player_runs", 0, "Meg Lanning")
    assert read_session("Player Runs", "Meg Lanning 87 Runs") == Session("player_runs", 0, "Meg Lanning")
    assert read_session("Player Balls", "Bavuma") == Session("player_balls", 0, "Bavuma")
    assert read_session("Toss", "Sydney Thunder W") == Session("toss", 0, "Sydney Thunder W")
    assert read_session("Runs", "Sri Lanka") == Session("runs", 0, "Sri Lanka")  # a team's runs with no over: its innings
    assert read_session("", "168 runs Titans") == Session("runs", 0, "Titans")


def test_a_tips_misspelt_player_is_found():
    for tip, name in (("G Readmayne", "Georgia Redmayne"), ("D singh Aree", "Dipendra Singh Airee"), ("M Bracewell", "Michael Bracewell"),
                      ("Meg Lanning", "Meg Lanning"), ("I Zadran", "Ibrahim Zadran"), ("S ali agha", "Salman Agha"), ("Gill", "Shubman Gill"),
                      ("Annabel Sutherland", "A Sutherland")):
        assert same_batter(tip, name), (tip, name)
    assert not same_batter("G Redmayne", "Harry Redmayne") and not same_batter("Meg Lanning", "Annabel Sutherland")


def test_the_sites_row_of_a_players_runs():
    rows = ["15 Over MLSW", "A Sutherland Runs", "A Sutherland Boundaries", "4th Wkt Pship Boundaries MLSW", "MLSW Power Surge Ball 1"]
    assert site_session_row(rows, Session("player_runs", 0, "Annabel Sutherland")) == 1
    assert site_session_row(rows, Session("player_runs", 0, "Meg Lanning")) is None
    assert site_session_row(rows, Session("player_balls", 0, "Annabel Sutherland")) is None


def test_the_scorecard_gives_the_toss_and_who_has_batted():
    match = Match(1, "x", list(STARS), "In Progress", datetime.now(), "T20")
    assert parse_scorecard(CARD, match) == 1
    assert match.toss == "Hobart Hurricanes Women"
    assert match.batters == {"Meg Lanning": Batter(71, 52, True, 1), "Danielle Gibson": Batter(22, 11, False, 1)}


def test_a_players_line_is_settled_once_it_is_known():
    match = Match(1, "x", list(STARS), "In Progress", datetime.now(), "T20")
    parse_scorecard(CARD, match)
    match.innings[1].total = (100, 2, 12.0)  # the innings is still on
    assert settle(Session("player_runs", 0, "Meg Lanning"), "yes", 87, match) == (False, "Meg Lanning 71 runs")  # out
    assert settle(Session("player_runs", 0, "Meg Lanning"), "no", 87, match)[0] is True
    assert settle(Session("player_balls", 0, "Lanning"), "yes", 50, match) == (True, "Meg Lanning 52 balls")
    gibson = Session("player_runs", 0, "D Gibson")
    assert settle(gibson, "yes", 20, match)[0] is True  # past the line while batting
    assert settle(gibson, "yes", 30, match) == (None, "")  # still batting, below it
    assert settle(Session("player_runs", 0, "N Stalenberg"), "yes", 20, match) == (None, "")  # hasn't batted
    match.state = "Complete"
    assert settle(gibson, "yes", 30, match)[0] is False  # not out at the end, below it


def test_a_toss_call_is_settled_by_the_scorecard():
    match = Match(1, "x", list(STARS), "Preview", datetime.now())
    assert settle(Session("toss", 0, "Hobart Hurricanes W"), "yes", 0, match) == (None, "")
    match.toss = "Hobart Hurricanes Women"
    assert settle(Session("toss", 0, "Hobart Hurricanes W"), "yes", 0, match)[0] is True
    assert settle(Session("toss", 0, "Melbourne Stars W"), "yes", 0, match)[0] is False
    assert team_fits("Melbourne Renegades", ["Melbourne Renegades Women"]) and team_fits("Sydney Thunder W", ["Sydney Thunder Women"])


def toss_channel(store):
    pipe, site, notifier = make(store, Profile("toss", rules="panther_toss", rules_only=True, fancy_paper=True))
    return pipe, site, notifier


def test_a_toss_call_goes_on_paper_without_the_ai_or_the_site(store):
    pipe, site, _ = toss_channel(store)
    asyncio.run(pipe._handle(msg(TOSS)))
    asyncio.run(pipe._handle(msg("🇦🇺 SYDNEY THUNDER - W 🇦🇺 WON THE TOSS AND OPTED TO BOWL FIRST ✔️✔️")))
    assert pipe._parser.calls == 0 and site.calls == []
    tips = store.open_fancy("live")
    assert [(t["team"], t["kind"], t["units"], t["rate"], t["line"]) for t in tips] == [("Sydney Thunder W", "toss", 0.05, 98, 0.0)]


def test_a_toss_call_waits_for_its_match_then_wins_at_98_paise(store):
    pipe, _, notifier = toss_channel(store)
    asyncio.run(pipe._handle(msg(TOSS)))
    match = Match(7, "x", list(THUNDER), "Preview", datetime.now() + timedelta(minutes=30), "T20")
    other_day = Match(8, "x", [("Sydney Thunder Women", "SYTW"), ("Perth Scorchers Women", "PRSW")], "Preview",
                      datetime.now() + timedelta(days=2), "T20")
    asyncio.run(pipe._settle_fancy(Scores(other_day, match)))
    tip = store.open_fancy("live")[0]
    assert tip["match_id"] == 7 and tip["event"] == "Sydney Thunder Women v Melbourne Renegades Women"
    match.toss = "Sydney Thunder Women"
    asyncio.run(pipe._settle_fancy(Scores(other_day, match)))
    assert store.open_fancy("live") == []
    outcome, pl = store.rows("SELECT outcome, pl FROM fancy")[0]
    assert outcome == "won" and pl == 0.049 and "Sydney Thunder W toss" in notifier.sent[-1]


def player_tip(text, name="Meg Lanning", line=87.0, event="? v ?", kind="Player Runs", units=1.0):
    return {text: [signal(market="fancy", market_name=kind, selection=name, side="yes", line=line, odds=None, units=units,
                          sport="cricket", event=event)]}


def test_a_player_tip_finds_its_match_by_the_scorecard_when_it_names_none(store):
    text = "➡️ Meg Lanning 87 Runs Yes ✔️\n\n1x limit Only"
    pipe, _, _ = make(store, Profile("p", fancy_paper=True), player_tip(text))
    asyncio.run(pipe._handle(msg(text)))

    class Cards(Scores):
        async def refresh_scorecard(self, match):
            return parse_scorecard(CARD, match) if match.match_id == 2 else 1

    near = [Match(1, "x", list(THUNDER), "In Progress", datetime.now(), "T20"), Match(2, "x", list(STARS), "In Progress", datetime.now(), "T20")]
    asyncio.run(pipe._settle_fancy(Cards(*near)))
    outcome, pl, event = store.rows("SELECT outcome, pl, event FROM fancy")[0]
    assert outcome == "lost" and pl == -1.0 and event == "Melbourne Stars Women v Hobart Hurricanes Women"


def test_a_player_tip_naming_its_match_stays_there(store):
    text = "M Bracewell 22 Runs Yes"
    pipe, _, _ = make(store, Profile("p", fancy_paper=True), player_tip(text, "M Bracewell", 22.0, "New Zealand v India"))
    asyncio.run(pipe._handle(msg(text)))
    nz = Match(3, "x", [("New Zealand", "NZ"), ("India", "IND")], "In Progress", datetime.now(), "T20")
    asyncio.run(pipe._settle_fancy(Scores(Match(1, "x", list(THUNDER), "In Progress", datetime.now(), "T20"), nz)))
    assert store.open_fancy("live")[0]["match_id"] == 3


def test_an_edited_post_is_kept_and_not_acted_on(store):
    script = {"Back Sinner 3.2": [signal(odds=3.2, selection="Sinner", event="Sinner v ?", sport="tennis")]}
    pipe, site, _ = make(store, Profile("tip"), script, live=3.2)
    asyncio.run(pipe._handle(msg("Back Sinner 3.2")))
    calls = len(site.calls)
    asyncio.run(pipe.edited(CHAT, 1, "Back Sinner 3.2"))  # the same words: a button, a reaction count
    assert store.rows("SELECT edited_text FROM messages")[0][0] is None
    asyncio.run(pipe.edited(CHAT, 1, "Back Sinner 2.9"))
    text, edited = store.rows("SELECT text, edited_text FROM messages")[0]
    assert text == "Back Sinner 3.2" and edited == "Back Sinner 2.9" and len(site.calls) == calls
    asyncio.run(pipe.edited(CHAT, 999, "never received"))


def test_both_channels_are_followed_on_paper_only():
    channels = Channels.load(make_settings(), Path(__file__).resolve().parent.parent / "channels.example.json")
    panther, toss = channels.get("Panther"), channels.get("Panther Toss")
    assert -1002294396193 in panther.ids and -1003749997860 in toss.ids
    assert panther.profile.paper_only and toss.profile.paper_only and panther.profile.fancy_paper and toss.profile.fancy_paper
    assert toss.profile.rules_only and panther.profile.small_units == 0.1
