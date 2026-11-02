"""Boss Fire 2.0's session calls, read by rules; and what 2 Nov showed: a market not open yet. The posts are its own."""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from conftest import make_settings, signal
from test_channels_rules import FakeSite, make, msg
from tradebot.channels import Channels, Profile
from tradebot.exchange_bot import BetResult
from tradebot.fancy import Innings, Match, Session, settle, site_session_row
from tradebot.rules import parse_known

TICKS = "\n\n✔️✔️✔️✔️✔️✔️✔️✔️✔️✔️"
CROSSES = "\n\n❌❌❌❌❌❌❌❌❌"
ALIASES = {"prs": "Perth Scorchers", "mls": "Melbourne Stars"}
BOSS = Profile("bf", rules="bossfire", rules_first=True, ai_filter=True, bare_photos=False, fancy_paper=True, aliases=ALIASES,
               entry_words="(over|wicket|wkt|yes|not|under|upar)")
TEAMS = [("Perth Scorchers Women", "PRSW"), ("Melbourne Stars Women", "MLSW")]
SHUT = "the site's Max Bet is 0: this market takes no bets"


def one(text):
    (call,) = parse_known(text, "bossfire")
    return call


def perth_v_stars(state="Complete"):
    """The real numbers of 2 Nov: Perth 124/9, Stars 109."""
    first = Innings("PRSW", overs={6: (53, 1), 10: (75, 5), 15: (92, 7), 20: (124, 9)}, total=(124, 9, 20.0), fow={1: 37})
    second = Innings("MLSW", overs={6: (38, 3)}, total=(109, 10, 18.1))
    return Match(162753, "x", list(TEAMS), state, datetime(2026, 11, 2, 13, 40), "T20", innings={1: first, 2: second})


def test_its_over_calls_are_read_as_written():
    for text, name, side, line, team in (
            ("06 OVER 42 YES PRS " + TICKS, "6 Over Runs PRS", "yes", 42, "PRS"),
            ("10 OVER 84  NOT PRS W" + CROSSES, "10 Over Runs PRS W", "no", 84, "PRS W"),
            ("15 OVER 98 NOT PRS W" + CROSSES, "15 Over Runs PRS W", "no", 98, "PRS W"),
            ("06 OVER 39 YES MLR - W " + TICKS, "6 Over Runs MLR W", "yes", 39, "MLR W"),
            ("10 OVER 102 YES NZ 🇳🇿 " + TICKS, "10 Over Runs NZ", "yes", 102, "NZ"),
            ("20 OVER 164 NOT AFG MEN " + CROSSES, "20 Over Runs AFG MEN", "no", 164, "AFG MEN"),
            ("06 OVER 46 NOT ZIMBABWE 🇿🇼 " + CROSSES, "6 Over Runs ZIMBABWE", "no", 46, "ZIMBABWE")):
        call = one(text)
        assert (call.action, call.market, call.selection, call.side, call.line, call.event, call.units) == (
            "entry", "fancy", name, side, line, f"{team} v ?", None), text


def test_its_wicket_call_naming_a_side_is_read_and_one_naming_none_goes_to_the_ai():
    call = one("1ST WICKET CALL PRS   🚽 \n\n34 K UNDER JAYEGA 🚾 \n\nWicket Call 📞 Not Karte H")
    assert (call.selection, call.side, call.line) == ("1 Wkt PRS", "no", 34)
    assert parse_known("2nd WICKET CALL  🚽 \n\n42 K UNDER JAYEGA 🚾", "bossfire") is None


def test_results_and_talk_are_no_call():
    for text in ("20 Over 179 Not Pass✔️✔️", "10 Over 80 Yes Pass ✔️✔️", "06 Over 49 Not  Fail❗️", "38 MILA HOGA DIRECT YE WKT CALL 📞",
                 "BOSSFIRE WORKSHEET \n\nTODAY SESSIONS ⬇️⬇️⬇️\n\n06 Over 42 Yes Pass✔️✔️\n06 Over 49 Not  Fail❗️",
                 "SESSION PASS PASS PASS 💰💰💰", "1 RUN SE BET LOOSE HUYI HAI", "NORMAL LIMIT", "MLS VS PRS", ""):
        assert parse_known(text, "bossfire") is None, text


def test_a_call_goes_on_paper_with_no_ai_under_its_full_team(store):
    pipe, site, _ = make(store, BOSS)
    asyncio.run(pipe._handle(msg("15 OVER 98 NOT PRS W" + CROSSES)))
    asyncio.run(pipe._handle(msg("06 OVER 42 YES PRS " + TICKS)))
    tips = store.open_fancy("live")
    assert [(t["team"], t["kind"], t["number"], t["line"], t["side"], t["units"]) for t in tips] == [
        ("Perth Scorchers W", "runs", 15, 98, "no", 1.0), ("Perth Scorchers", "runs", 6, 42, "yes", 1.0)]
    assert pipe._parser.calls == 0 and site.calls == []


def test_any_other_post_still_goes_to_the_ai(store):
    pipe, _, _ = make(store, BOSS)
    asyncio.run(pipe._handle(msg("2nd WICKET CALL  🚽 \n\n42 K UNDER JAYEGA 🚾")))
    assert pipe._parser.calls == 1


def test_the_days_calls_settle_on_the_real_numbers():
    match = perth_v_stars()
    assert settle(Session("runs", 15, "Perth Scorchers W"), "no", 98, match) == (True, "92 after over 15")
    assert settle(Session("runs", 6, "Perth Scorchers"), "yes", 42, match) == (True, "53 after over 6")
    assert settle(Session("wicket", 1, "Perth Scorchers"), "no", 34, match) == (False, "wicket 1 fell at 37")
    assert settle(Session("runs", 6, "MLR W"), "yes", 39, match) == (False, "38 after over 6")  # its slip for Melbourne Stars


def test_the_sites_row_is_found_with_or_without_the_tips_w():
    rows = ["15 Over MLSW", "15 Over PSW", "6 Over PRSW", "1st Wkt PSW"]
    assert site_session_row(rows, Session("runs", 15, "Perth Scorchers")) == 1
    assert site_session_row(rows, Session("runs", 15, "Perth Scorchers W")) == 1
    assert site_session_row(rows, Session("runs", 6, "Perth Scorchers")) == 2
    assert site_session_row(rows, Session("runs", 15, "Melbourne Stars")) == 0
    assert site_session_row(rows, Session("wicket", 1, "Perth Scorchers")) == 3


def test_the_channel_list_reads_it_by_rules_first():
    channels = Channels.load(make_settings(), Path(__file__).resolve().parent.parent / "channels.example.json")
    profile = channels.get("Boss Fire 2.0").profile
    assert profile.rules == "bossfire" and profile.rules_first and profile.paper_only and profile.fancy_paper
    assert profile.aliases["prs"] == "Perth Scorchers" and "mlr" not in profile.aliases


class ShutSite(FakeSite):
    """A market that takes no bets until it opens."""
    opened = False

    async def place(self, sig, position, require_green=False):
        if not self.opened:
            self.calls.append(("shut", sig.selection, None))
            return BetResult("retry", SHUT)
        return await super().place(sig, position, require_green)


def _waited(store, minutes):
    at = (datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    store._db.execute("UPDATE pending SET created_at = ?", (at,))


def test_an_entry_waits_for_a_market_not_open_yet(store):
    """2 Nov: Panther's 'Back MLSw @ 1.97' at 13:17 was given up at 13:22; the site's market took bets by the 13:40 start."""
    tip = signal(selection="Melbourne Stars W", event="Melbourne Stars W v Perth Scorchers W", sport="cricket", odds=1.97)
    pipe, site, _ = make(store, Profile("tip"), {"Back MLSw @ 1.97": [tip]}, live=1.97, site_class=ShutSite)
    asyncio.run(pipe._handle(msg("Back MLSw @ 1.97")))
    assert len(store.pending_instructions("live")) == 1
    _waited(store, 20)
    asyncio.run(pipe.retry_pending_once())
    assert len(store.pending_instructions("live")) == 1  # past the usual 5 min, still tried
    site.opened = True
    asyncio.run(pipe.retry_pending_once())
    assert store.pending_instructions("live") == [] and store.find_position("Melbourne Stars W", "live").price == 1.97


def test_a_market_that_never_opens_is_given_up_in_time(store):
    tip = signal(selection="Melbourne Stars W", event="Melbourne Stars W v Perth Scorchers W", sport="cricket", odds=1.97)
    pipe, _, notifier = make(store, Profile("tip"), {"Back": [tip]}, live=1.97, site_class=ShutSite)
    asyncio.run(pipe._handle(msg("Back")))
    _waited(store, 31)
    asyncio.run(pipe.retry_pending_once())
    assert store.pending_instructions("live") == [] and "MISSED after 30 min" in notifier.sent[-1]
