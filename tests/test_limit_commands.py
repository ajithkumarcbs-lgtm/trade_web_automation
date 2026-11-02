"""/limit and /stop in Saved Messages: for new trades only, each trade counted at its own limit, kept over a restart."""
import asyncio
from datetime import datetime, timedelta

from conftest import make_settings, signal
from test_channels_rules import make, msg
from test_ladder import MATCH_WIN, UAE, LadderSite, Scores, entries, look, sk, tip, two_channels
from tradebot.channels import Channel, Channels, Profile
from tradebot.risk import RiskManager
from tradebot.store import Position
from tradebot.telegram_listener import PLAIN_COMMAND

BROSKA = signal()  # Florian Broska at 2.9, the fake site's price 3.0
SINNER = signal(selection="Sinner", event="Sinner v Alcaraz", odds=3.0)
CLOSE = signal(action="close", odds=None)
FANCY = signal(market="fancy", market_name="6 Over Runs NZ", selection="6 Over Runs NZ", side="yes", line=57.0, odds=None,
               event="New Zealand v India", sport="cricket")
SCRIPT = {"Broska": [BROSKA], "Sinner": [SINNER], "Bookset": [CLOSE], "Broska bookset": [CLOSE],
          "Two limits": [SINNER.model_copy(update={"units": 2.0})], "6 over 57 yes": [FANCY]}
NAMES = ["SS sports", "Kartik Bhai", "Raftaar", "Tennis Professor", "SK Astro", "MY Tips", "Boss Fire 2.0", "CricStatz",
         "Panther", "Panther Toss", "TEST group"]


def cmd(pipe, text):
    return asyncio.run(pipe.command(text))


def post(pipe, text):
    asyncio.run(pipe._handle(msg(text)))


def tipster(store, **settings):
    return make(store, Profile("tip"), SCRIPT, **settings)


def closed_trade(store, selection, event, stake, pl, unit, mode="live"):
    store.open_position(signal(selection=selection, event=event, sport="cricket", channel="Tip"), mode, 3.0, stake, unit)
    store.set_closed(store.find_position(selection, mode).id, pl=pl, how="settled")


# --- /limit ---------------------------------------------------------------------------------------

def test_a_new_limit_is_for_new_trades_and_an_open_trade_keeps_its_own(store):
    pipe, _, _ = tipster(store)
    assert "1 limit = ₹1,000" in cmd(pipe, "/limit")
    post(pipe, "Broska")
    reply = cmd(pipe, "/limit 100")
    assert "1 limit = ₹100" in reply and "was ₹1,000" in reply and "-₹80" in reply
    post(pipe, "Sinner")
    first, second = store.find_position("Florian Broska", "live"), store.find_position("Sinner", "live")
    assert (first.stake, first.unit) == (1000, 1000) and (second.stake, second.unit) == (100, 100)
    cmd(pipe, "/limit 1000")
    assert store.find_position("Sinner", "live").stake == 100


def test_a_tips_limits_are_multiples_of_the_new_limit(store):
    pipe, _, _ = tipster(store)
    cmd(pipe, "/limit 100")
    post(pipe, "Two limits")
    assert store.find_position("Sinner", "live").stake == 200


def test_a_wrong_amount_is_refused_and_the_limit_stays(store):
    pipe, _, _ = tipster(store)
    cmd(pipe, "/limit 500")
    for wrong in ("-100", "abc", "0", "5", "50000", "nan", "100.5", "SK 500"):
        reply = cmd(pipe, f"/limit {wrong}")
        assert reply.startswith("⚠️") and "stays ₹500" in reply
    assert pipe._limit_now() == 500


def test_a_rupee_sign_and_a_comma_are_understood(store):
    pipe, _, _ = tipster(store)
    assert "1 limit = ₹2,000" in cmd(pipe, "/limit ₹2,000")


def test_a_bet_cap_comes_down_with_a_smaller_limit_but_never_goes_above_env(store):
    channels = Channels(make_settings(max_liability_per_bet=10000), [Channel("Tip", Profile("tip"))])
    channels.limit = 100
    assert channels.settings_for("Tip").max_liability_per_bet == 1000
    assert channels.settings_for("Tip").unit_stake == 100
    channels.limit = 5000
    assert channels.settings_for("Tip").max_liability_per_bet == 10000
    channels.limit = 100
    assert channels.settings_for("Tip", 1000).max_liability_per_bet == 10000  # the trade's own, whatever /limit says now


def test_kartiks_lay_at_75_is_refused_at_any_limit_and_an_old_trade_can_still_be_booked(store):
    settings = make_settings(max_liability_per_bet=10000)
    channels = Channels(settings, [Channel("Tip", Profile("tip"))])
    risk = RiskManager(settings, store, "live", channels)
    channels.limit = 100
    lay = signal(side="lay", odds=75.0, channel="Tip")
    assert risk.price_check(lay, 75.0).reason.startswith("liability 7400")  # 74 limits, as ₹74,000 was at ₹1000
    old = Position(1, "", "cricket", "A v B", "match_odds", "A", "lay", 1.25, 3000.0, None, None, channel="Tip", unit=1000.0)
    assert risk.check_exit_size("back", 1875.0, 1875.0, old).ok


def test_a_channel_with_its_own_unit_stake_keeps_it():
    channels = Channels(make_settings(), [Channel("Tip", Profile("tip", settings={"unit_stake": 300}))])
    channels.limit = 100
    assert channels.settings_for("Tip").unit_stake == 300


# --- reports --------------------------------------------------------------------------------------

def test_the_report_counts_each_trade_at_its_own_limit(store):
    pipe, _, _ = tipster(store)
    closed_trade(store, "Sixers", "Sixers v Heat", 1000, 1000, 1000)
    cmd(pipe, "/limit 100")
    closed_trade(store, "India", "India v New Zealand", 100, -100, 100)
    report = cmd(pipe, "/report year")
    assert "• Tip (Cricket): 2 trades, 1 won 1 lost, 2.0 limits, +0.00" in report
    assert "Known result: +900 (+0.00 limits)" in report
    assert "1 limit = ₹100 now, each trade counted at its own" in report
    daily = pipe.day_report_text()
    assert "All: day +0.00" in daily and "*Tip*: +0.00u (₹+900)" in daily and "This week: Tip +0.00u" in daily


def test_a_trade_from_before_limit_counts_at_the_env_unit(store):
    pipe, _, _ = tipster(store)
    closed_trade(store, "Sixers", "Sixers v Heat", 1000, 2000, None)
    cmd(pipe, "/limit 100")
    report = cmd(pipe, "/report year")
    assert "1 trade, 1 won 0 lost, 1.0 limits, +2.00" in report and "Known result: +2000 (+2.00 limits)" in report


def test_an_unknown_results_worst_case_is_in_its_own_limit(store):
    pipe, _, _ = tipster(store)
    closed_trade(store, "Sixers", "Sixers v Heat", 1000, 1000, 1000)
    store.open_position(signal(selection="India", event="India v NZ", channel="Tip"), "live", 3.0, 100, 100)
    store.set_closed(store.find_position("India", "live").id, pl=None, how="finished")
    assert "If the 1 unknown ones lost: +900 (+0.00 limits)" in cmd(pipe, "/report year")


def test_a_fancy_bet_keeps_its_rupees_when_the_limit_changes(store):
    pipe, _, _ = make(store, Profile("fancy", fancy_paper=True), SCRIPT)
    pipe._bot = None  # no site line to read
    post(pipe, "6 over 57 yes")
    tip_id = store.open_fancy("live")[0]["id"]
    store.settle_fancy(tip_id, "won", 1.0, "58 after over 6")
    cmd(pipe, "/limit 100")
    new = store.add_fancy("live", "Tip", 2, "A v B", "NZ", "runs", 20, 180.0, "yes", None, 1.0, unit=100.0)
    store.settle_fancy(new, "won", 1.0, "198")
    report = cmd(pipe, "/report year")
    assert "2 trades, 2 won 0 lost, 2.0 limits, +2.00" in report and "Known result: +1100 (+2.00 limits)" in report
    assert "*Tip*: +2.00u (₹+1,100)" in pipe.day_report_text()


# --- /limit on the SK method ----------------------------------------------------------------------

def test_the_next_step_of_a_trade_keeps_its_limit(store):
    pipe, site, _ = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.45)
    cmd(pipe, "/limit 100")
    look(pipe, site, 1.15)
    assert entries(site) == [("entry", "Netherlands", 1000), ("entry", "Netherlands", 1000)]
    assert store.find_position("Netherlands", "dry_run").stake == 2000


def test_a_tip_with_no_entry_yet_takes_the_new_limit_and_its_target(store):
    pipe, site, _ = sk(store)
    tip(pipe, MATCH_WIN)
    cmd(pipe, "/limit 100")
    look(pipe, site, 1.45, 1.15, 1.05)
    trade = store.find_position("Netherlands", "dry_run")
    assert (trade.stake, trade.unit) == (300, 100)
    site.back, site.team = 1.9, 1.8  # 300 - 365 / 1.9 = +108, over 1 limit of ₹100 and the favourite
    look(pipe, site, 1.9)
    assert site.calls[-1][0] == "close" and store.find_position("Netherlands", "dry_run") is None


def test_a_loss_limit_counts_each_trade_at_its_own_limit(store):
    profile = Profile("sk", ladder=True, ladder_rules={"daily_loss": 3.0, "cooldown_min": 0})
    pipe, site, _ = make(store, profile, {MATCH_WIN: [signal(**UAE)]}, live=1.6, mode="dry_run", site_class=LadderSite)
    pipe._cricbuzz = Scores()
    closed_trade(store, "India", "India v NZ", 3000, -2000, 1000, mode="dry_run")
    cmd(pipe, "/limit 100")
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.45)
    assert entries(site) != []  # -2000 is 2 limits of its own ₹1000, not 20 limits of ₹100
    closed_trade(store, "Pakistan", "Pakistan v NZ", 100, -100, 100, mode="dry_run")  # -1 limit more
    store.add_ladder("dry_run", "Tip", "Pakistan", "Pakistan v NZ", datetime.now(), None)
    new = next(ladder for ladder in store.active_ladders("dry_run") if not ladder.entries)
    assert pipe._ladder_block(new) == "the day's loss limit of 3 limits is reached (-3.00)"


# --- /stop, /start --------------------------------------------------------------------------------

def test_a_stopped_channel_opens_nothing_and_its_posts_cost_no_ai_until_started(store):
    pipe, site, notifier = tipster(store)
    reply = cmd(pipe, "/stop tip")
    assert "Tip stopped" in reply
    post(pipe, "Broska")
    assert site.calls == [] and pipe._parser.calls == 0
    assert store.rows("SELECT summary FROM messages")[-1] == ("channel stopped, not read",)
    assert "Tip started" in cmd(pipe, "/start tip")
    post(pipe, "Broska")
    assert entries(site) != [] and pipe._parser.calls == 1


def test_a_stopped_channels_open_trade_stays_and_its_bookset_still_works(store):
    pipe, site, notifier = tipster(store)
    post(pipe, "Broska")
    reply = cmd(pipe, "/stop tip")
    assert "Open trades stay, its booksets still work: Florian Broska" in reply
    post(pipe, "Sinner")
    assert store.find_position("Sinner", "live") is None and any("STOPPED: no new entries from Tip" in text for text in notifier.sent)
    post(pipe, "Broska bookset")
    assert site.calls[-1][0] == "close" and store.find_position("Florian Broska", "live") is None


def test_a_bare_bookset_books_a_stopped_channels_trade(store):
    pipe, site, _ = tipster(store)
    post(pipe, "Broska")
    cmd(pipe, "/stop tip")
    post(pipe, "Bookset")
    assert site.calls[-1][0] == "close" and store.find_position("Florian Broska", "live") is None


def test_a_stopped_channel_adds_no_fancy_bet(store):
    pipe, _, _ = make(store, Profile("fancy", fancy_paper=True), SCRIPT)
    post(pipe, "Broska")
    cmd(pipe, "/stop tip")
    post(pipe, "6 over 57 yes")
    assert store.open_fancy("live") == []


def test_a_paused_bot_adds_no_fancy_bet_either(store):
    pipe, _, _ = make(store, Profile("fancy", fancy_paper=True), SCRIPT)
    cmd(pipe, "/pause")
    post(pipe, "6 over 57 yes")
    assert store.open_fancy("live") == []


def test_a_stake_message_is_new_money_and_is_stopped_too(store):
    script = {**SCRIPT, "Limit 2": [signal(action="stake", odds=None, units=2)]}
    pipe, site, _ = make(store, Profile("ss", limit_followup_s=180), script)
    post(pipe, "Broska")
    cmd(pipe, "/stop tip")
    post(pipe, "Limit 2")
    assert store.find_position("Florian Broska", "live").stake == 1000


def test_a_waiting_entry_of_a_stopped_channel_is_dropped_whenever_it_was_due(store):
    pipe, site, _ = tipster(store)
    post(pipe, "Broska")
    later = (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds")
    store.add_pending(SINNER.model_copy(update={"channel": "Tip", "not_before": later}), "live", None, "waiting for its match")
    store.add_pending(signal(action="exit", odds=1.5, channel="Tip"), "live", None, "a limit exit")
    cmd(pipe, "/stop tip")
    asyncio.run(pipe.retry_pending_once())
    assert store.pending_instructions("live") == []
    assert [call[0] for call in site.calls] == ["entry", "exit"]  # the exit on the open trade went on


def test_your_own_trade_goes_in_while_a_channel_is_stopped(store):
    pipe, site, _ = tipster(store)
    cmd(pipe, "/stop tip")
    cmd(pipe, "/back Shelton 3 1u")
    assert entries(site) != []


def test_all_entries_paused_stands_over_a_channels_start(store):
    pipe, site, _ = tipster(store)
    cmd(pipe, "/stop tip")
    cmd(pipe, "/pause")
    assert "But all entries are paused" in cmd(pipe, "/start tip")
    post(pipe, "Broska")
    assert entries(site) == []
    cmd(pipe, "/resume")
    post(pipe, "Broska")
    assert entries(site) != []


def test_stop_all_and_start_all_are_pause_and_resume(store):
    pipe, _, _ = tipster(store)
    assert "Paused" in cmd(pipe, "/stop all") and pipe._paused
    cmd(pipe, "/stop tip")
    reply = cmd(pipe, "/start all")
    assert "Resumed" in reply and "Still stopped: Tip" in reply and not pipe._paused


def test_a_restart_keeps_the_limit_the_stopped_channels_and_the_pause(store):
    pipe, _, _ = tipster(store)
    cmd(pipe, "/limit 100")
    cmd(pipe, "/stop tip")
    cmd(pipe, "/pause")
    again, _, _ = tipster(store)
    assert again._limit_now() == 100 and again._stopped == {"Tip"} and again._paused
    assert "1 limit = ₹100" in cmd(again, "/status") and "Stopped: Tip" in cmd(again, "/status")


def test_a_stopped_channel_since_removed_from_channels_json_is_forgotten(store):
    store.set_setting("stopped", '["Removed", "Tip"]')
    pipe, _, _ = tipster(store)
    assert pipe._stopped == {"Tip"}


def test_stop_says_what_is_stopped_and_stopping_twice_changes_nothing(store):
    pipe, _, _ = tipster(store)
    assert "Nothing is stopped" in cmd(pipe, "/stop") and "Tip" in cmd(pipe, "/stop")
    cmd(pipe, "/stop tip")
    assert "already stopped" in cmd(pipe, "/stop tip")
    assert "Stopped: Tip" in cmd(pipe, "/stop") and "⏹ STOPPED" in cmd(pipe, "/channels")
    assert "/limit" in cmd(pipe, "/start") and "/limit" in cmd(pipe, "/help")


# --- /stop on the SK method -----------------------------------------------------------------------

def test_a_stopped_tips_trade_takes_no_more_entries_but_still_exits(store):
    pipe, site, _ = sk(store)
    tip(pipe, MATCH_WIN)
    look(pipe, site, 1.45)
    cmd(pipe, "/stop tip")
    look(pipe, site, 1.15)
    assert len(entries(site)) == 1 and len(store.active_ladders("dry_run")) == 1
    site.back, site.team = 1.95, 1.95
    look(pipe, site, 1.95)
    assert site.calls[-1][0] == "close" and store.find_position("Netherlands", "dry_run") is None


def test_a_tip_waiting_for_its_first_entry_is_dropped_by_stop(store):
    pipe, site, _ = sk(store)
    tip(pipe, MATCH_WIN)
    assert "1 tip(s) waiting for an entry dropped" in cmd(pipe, "/stop tip")
    assert store.active_ladders("dry_run") == []


def test_a_tip_two_channels_gave_goes_on_until_both_are_stopped(store):
    pipe, site, _ = two_channels(store, "dry_run")
    tip(pipe, MATCH_WIN)
    asyncio.run(pipe._handle(msg("UAE win", chat=-1002)))
    cmd(pipe, "/stop sk")
    assert len(store.active_ladders("dry_run")) == 1
    look(pipe, site, 1.45)
    assert len(entries(site)) == 1
    cmd(pipe, "/stop mine")
    look(pipe, site, 1.15)
    assert len(entries(site)) == 1


def test_sk_by_hand_is_refused_while_its_channel_is_stopped(store):
    pipe, site, notifier = sk(store)
    cmd(pipe, "/stop tip")
    cmd(pipe, "/sk UAE Toss or Match Win")
    assert store.active_ladders("dry_run") == [] and "STOPPED" in notifier.sent[-1]


# --- what can be typed ----------------------------------------------------------------------------

def test_a_channel_is_found_by_any_word_of_its_name():
    channels = Channels(make_settings(), [Channel(name, Profile("p")) for name in NAMES])

    def found(text):
        return [c.name for c in channels.named(text)]

    assert found("sk") == ["SK Astro"] and found("SK ASTRO") == ["SK Astro"] and found("my") == ["MY Tips"]
    assert found("boss fire") == found("bossfire") == found("boss") == ["Boss Fire 2.0"]
    assert found("professor") == ["Tennis Professor"] and found("kartik") == ["Kartik Bhai"]
    assert found("panther") == ["Panther"] and found("panther toss") == found("toss") == ["Panther Toss"]
    assert set(found("s")) == {"SS sports", "SK Astro"}
    assert found("tennis") == [] and found("cricket") == [] and found("???") == [] and found("manual") == []


def test_a_name_that_fits_none_or_two_is_refused_and_lists_them(store):
    pipe, _, _ = tipster(store)
    reply = cmd(pipe, "/stop nobody")
    assert reply.startswith("⚠️") and "no channel 'nobody'" in reply and "Tip" in reply and pipe._stopped == set()
    assert "no channel 'tennis'" in cmd(pipe, "/stop tennis") and pipe._stopped == set()


def test_limit_stop_and_start_need_no_slash():
    for text in ("LIMIT 100", "limit 1000", "Limit", "STOP SK", "stop boss fire", "Start my tips", "start SK Astro"):
        assert PLAIN_COMMAND.match(text), text
    for text in ("stopped sk", "limit 2 use kro", "Stop", "start", "hello", "stop sk\nnow", "India win"):
        assert not PLAIN_COMMAND.match(text), text


def test_the_commands_work_in_capitals_without_the_slash(store):
    pipe, _, _ = tipster(store)
    assert "1 limit = ₹100" in cmd(pipe, "LIMIT 100")
    assert "Tip stopped" in cmd(pipe, "STOP TIP") and "Tip started" in cmd(pipe, "START TIP")
