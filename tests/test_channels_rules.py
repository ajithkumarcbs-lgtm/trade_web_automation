"""channels.json, rule-checked reading, stops, top-ups, waiting entries, confirmations and Saved Messages commands."""
import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_settings, signal
from main import Pipeline, TradeLock, result_from_price
from tradebot.channels import Channel, Channels, Profile, apply_aliases
from tradebot.exchange_bot import BetResult, _best_index
from tradebot.models import ParsedMessage
from tradebot.names import contains_player, name_score, same_name
from tradebot.risk import RiskManager, lay_instead_of_back
from tradebot.rules import not_yet, parse_known, parse_strict, reconcile
from tradebot.store import Store, period_start
from tradebot.telegram_listener import IncomingMessage

CHAT = -1001


# --- rules ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,action,name,odds,units", [
    ("Back Shelton 3.25 odds\n1 unit", "entry", "shelton", 3.25, 1),
    ("Back rublev 1 unit 2.30 odds", "entry", "rublev", 2.3, 1),
    ("Tormo back 3 odds 1 unit", "entry", "tormo", 3, 1),
    ("Back daniel 8 odds", "entry", "daniel", 8, None),
    ("Bookset pls quick both side 1 unit 1.55", "close", "", None, None),
    ("Loss cut 2.5 la", "close", "", None, None),
])
def test_tipster_rules(text, action, name, odds, units):
    sig = parse_known(text, "tipster")[0]
    assert (sig.action, sig.selection, sig.odds, sig.units) == (action, name, odds, units)


@pytest.mark.parametrize("text", ["Back goat 🐐 on 3.6 odds 1 unit", "Thanks ra bubi", "Active",
                                  "Guys na already bookset ipo apdiye thoki potrukan ungaluku loss na enaku no loss",
                                  "Berriting 3 odds pona edunga entry"])
def test_tipster_rules_leave_the_rest_to_the_ai(text):
    assert parse_known(text, "tipster") is None


@pytest.mark.parametrize("text,action,name,odds,units,loss_cut", [
    ("6 odds back war limit 1", "entry", "war", 6, 1, False),
    ("3.15 odds pak back", "entry", "pak", 3.15, None, False),
    ("Limit 1.5 use", "stake", "", None, 1.5, False),
    ("Only one limit", "stake", "", None, 1, False),
    ("Half limit", "stake", "", None, 0.5, False),
    ("Losscut now", "close", "", None, None, True),
    ("Good profit means bookset", "close", "", None, None, False),
    ("Eng bookset 🔥💯🔥", "close", "eng", None, None, False),
])
def test_ss_rules(text, action, name, odds, units, loss_cut):
    sig = parse_known(text, "ss")[0]
    assert (sig.action, sig.selection, sig.odds, sig.units, sig.loss_cut) == (action, name, odds, units, loss_cut)


@pytest.mark.parametrize("text", ["Bookset coming be active", "Ready for bookset", "Now back 7 odds uns",
                                  "First entry SL back 8 odds limit 1"])
def test_ss_rules_not_yet_or_unclear(text):
    assert parse_known(text, "ss") is None


def test_ss_stakes_in_words_and_rupees():
    assert parse_known("Try small amount", "ss")[0].units == 0.5
    stake = parse_known("Try 200 or 300", "ss")[0]
    assert (stake.action, stake.amount, stake.units) == ("stake", 200, None)
    assert parse_known("Ready now 5.3 odds back eng limit 2", "ss")[0].units == 2


@pytest.mark.parametrize("text,waiting", [("Ready for bookset", True), ("Zim w fav come   wait for bookset", True),
                                          ("Japan trade possible  your choice any odds back and boookset", True),
                                          ("Bookset now", False), ("Good profit means bookset", False)])
def test_not_yet_bookset(text, waiting):
    assert not_yet(text) is waiting


def test_a_team_code_finds_its_match_only_when_one_fits():
    listed = ["Amsterdam Flames v Belfast Titans", "Rotterdam Dockers v Guernsey"]
    assert _best_index(listed, "af", "af") == 0  # "4.6 odds back af"
    assert _best_index(listed + ["Afghanistan v Zimbabwe"], "af", "af") is None  # two could be meant: no guess


def test_india_is_not_west_indies(store):
    assert name_score("India", "West Indies") == 0 and not same_name("India", "West Indies", short_forms=False)
    assert not contains_player("India", "England v West Indies") and contains_player("India", "India v West Indies")
    store.open_position(signal(selection="India", event="India v ?", sport="cricket"), "live", 6.0, 1000)
    assert store.find_position("West Indies", "live") is None


def test_strict_commands():
    entry = parse_strict("BACK AT Berrettini 3 1u")[0]
    assert (entry.action, entry.selection, entry.when_odds, entry.units) == ("entry", "Berrettini", 3, 1)
    assert parse_strict("lay Sinner 1.55 rs500")[0].amount == 500
    assert parse_strict("/losscut Shelton")[0].loss_cut is True
    assert parse_strict("CANCEL Berrettini")[0].action == "cancel"
    assert parse_strict("EXIT Shelton 1.6")[0].odds == 1.6
    assert parse_strict("hello there") is None


def test_reconcile_takes_the_ai_name_and_the_rule_numbers():
    rule = parse_known("Back ruplev 2.4 odds 1 unit", "tipster")
    ai = [signal(selection="Andrey Rublev", odds=2.4, units=1, confidence=0.8)]
    merged, how = reconcile(rule, ai)
    assert how == "agree" and merged[0].selection == "Andrey Rublev" and merged[0].confidence >= 0.95
    _, how = reconcile(rule, [signal(selection="Andrey Rublev", odds=4.2, units=1)])
    assert how == "disagree"
    assert reconcile(rule, [])[1] == "rule-only" and reconcile(None, ai)[1] == "ai-only"


def test_reconcile_keeps_the_rules_spelling_and_refuses_another_player():
    rule = parse_known("Back gea 5 odds 1 unit", "tipster")
    merged, how = reconcile(rule, [signal(selection="Gaea", event="Gaea v ?", odds=5, units=1)])
    assert how == "agree" and (merged[0].selection, merged[0].aka) == ("Gaea", "gea")  # the site tries both
    merged, how = reconcile(rule, [signal(selection="Garin", event="Garin v ?", odds=5, units=1)])
    assert how == "disagree" and merged[0].selection == "gea"  # the AI named someone else: the text wins


@pytest.mark.parametrize("a,b,same", [("ruplev", "Andrey Rublev", True), ("beaz", "Baez", True),
                                      ("kg", "Knights", True), ("nwd", "North West Dragons", True),
                                      ("war", "Warriors", True), ("gea", "Garin", False), ("rublev", "Rune", False),
                                      ("de minaur", "de Jong", False)])
def test_same_name(a, b, same):
    assert same_name(a, b) is same


def test_aliases():
    assert apply_aliases("rno v TUS", {"rno": "Rhinos", "tus": "Tuskers"}) == "Rhinos v Tuskers"


# --- channels.json --------------------------------------------------------------------------------

def test_channels_file(tmp_path):
    path = tmp_path / "channels.json"
    path.write_text(json.dumps({
        "profiles": {"tipster": {"rules": "tipster", "settings": {"odds_tolerance": 0.15, "auto_bookset_units": 0.8}},
                     "open": {"trade": False, "watch_links": True}},
        "channels": [{"name": "VIP", "profile": "tipster", "ids": [-100], "title_regex": "^(Oct|Nov)\\b"},
                     {"name": "Open", "profile": "open", "ids": [-200]}]}), encoding="utf-8")
    channels = Channels.load(make_settings(), path)
    assert channels.for_chat(-100).name == "VIP" and channels.for_chat(-300) is None
    assert channels.settings_for("VIP").odds_tolerance == 0.15 and channels.settings_for("Open").odds_tolerance == 0.05
    assert channels.by_title("Nov (Paris)").name == "VIP" and channels.by_title("Octopus") is None
    channels.follow(-400, "VIP")  # the tipster's next tournament channel
    assert channels.for_chat(-400).name == "VIP"
    channels.unfollow(-400)
    assert channels.for_chat(-400) is None


def test_channels_file_errors(tmp_path):
    path = tmp_path / "channels.json"
    path.write_text(json.dumps({"profiles": {"p": {"settings": {"site_password": "x"}}},
                                "channels": [{"name": "A", "profile": "p", "ids": [-1]}]}), encoding="utf-8")
    with pytest.raises(SystemExit, match="can't override"):
        Channels.load(make_settings(), path)


def test_back_above_10_as_a_lay_of_the_opponent():
    back_odds, lay_stake, risk = lay_instead_of_back(100, 1.06)
    assert back_odds == pytest.approx(17.6667, abs=1e-3)
    assert lay_stake == pytest.approx(1666.67, abs=0.01)  # paid if our player wins = 100 x (17.67 - 1)
    assert risk == pytest.approx(100, abs=0.01)  # lost if our player loses = the back stake


# --- store / risk ---------------------------------------------------------------------------------

def test_top_up_averages_the_odds(store):
    store.open_position(signal(), "live", 3.0, 1000)
    pos = store.find_position("Broska", "live")
    store.add_to_position(pos.id, 1000, 2.6)
    pos = store.find_position("Broska", "live")
    assert pos.stake == 2000 and pos.price == pytest.approx(2.8)  # same win: 1000x2 + 1000x1.6 = 2000x1.8


def test_daily_stop(store):
    settings = make_settings(daily_stop_units=1, unit_stake=1000)
    risk = RiskManager(settings, store, "live")
    store.open_position(signal(selection="Rublev", event="Rublev v ?"), "live", 2.4, 1000)
    store.set_closed(store.find_position("Rublev", "live").id, how="finished")  # ran to the end: counts as lost
    check = risk.precheck(signal())
    assert not check.ok and "DAY STOP" in check.reason
    assert store.period_result("live", period_start("day")) == -1000


def test_settled_win_lifts_the_stop(store):
    risk = RiskManager(make_settings(daily_stop_units=1, unit_stake=1000), store, "live")
    store.open_position(signal(selection="Bublik", event="Bublik v ?"), "live", 4.1, 1000)
    pos = store.find_position("Bublik", "live")
    store.set_closed(pos.id, how="finished")
    store.settle(pos.id, 3100)
    assert risk.precheck(signal()).ok


def test_waiting_entry_price_check(store):
    risk = RiskManager(make_settings(), store, "live")
    sig = signal(odds=3.0, when_odds=3.0)
    assert "WAITING" in risk.price_check(sig, 2.5).reason
    assert risk.price_check(sig, 2.96).ok and risk.price_check(sig, 3.4).ok


def test_old_database_gets_the_new_columns(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, opened_at TEXT, mode TEXT, sport TEXT, event TEXT,"
               " market TEXT, selection TEXT, side TEXT, price REAL, stake REAL, exit_price REAL, exit_status TEXT)")
    db.commit()
    db.close()
    Store(path)
    columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(positions)")}
    assert {"channel", "closed_pl", "closed_how", "close_pending_since"} <= columns


# --- pipeline -------------------------------------------------------------------------------------

class FakeParser:
    def __init__(self, script):
        self.script = script
        self.calls = 0

    async def parse(self, text, image=None, context=None, reply_to="", hint=""):
        self.calls += 1
        return ParsedMessage(signals=self.script.get(text, []), summary=text)


class FakeSite:
    """Takes bets at `live` when the risk rules allow; a waiting entry below its trigger is retried."""

    def __init__(self, risk, live=3.0):
        self.risk, self.live, self.calls, self.busy = risk, live, [], False

    async def place(self, sig, position, require_green=False):
        self.calls.append((sig.action, sig.selection, sig.amount or sig.units))
        if sig.action == "entry":
            d = self.risk.price_check(sig, self.live)
            if not d.ok:
                return BetResult("retry" if "WAITING" in d.reason else "skipped", d.reason, self.live)
            return BetResult("placed", "ok", self.live, d.stake, d.liability)
        return BetResult("placed", "cashed out", pl=250)

    async def cancel_waiting(self, pos):
        return 0.0

    async def quote(self, sig):
        return self.live

    async def fancy_quote(self, event, session, side):
        return None


class GeaSite(FakeSite):
    """Lists 'Arthur Gea v Tomas Machac' and finds a tip's player by either name it carries, like the site."""
    MATCH = "Arthur Gea v Tomas Machac"

    async def place(self, sig, position, require_green=False):
        if sig.action == "entry" and not any(contains_player(n, self.MATCH) for n in (sig.selection, sig.aka) if n):
            self.calls.append((sig.action, sig.selection, sig.amount or sig.units))
            return BetResult("failed", f"match not found on site: {sig.event}")
        result = await super().place(sig, position, require_green)
        if sig.action == "entry":
            result.name, result.event = "Arthur Gea", self.MATCH
        return result


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, text, image=None):
        self.sent.append(text)


def make(store, profile: Profile, script=None, live=3.0, mode="live", site_class=FakeSite, **settings):
    s = make_settings(dry_run=False, **settings)
    channels = Channels(s, [Channel("Tip", profile, {CHAT})])
    risk = RiskManager(s, store, mode, channels)
    site = site_class(risk, live)
    notifier = FakeNotifier()
    pipe = Pipeline(s, mode, site, risk, store, notifier, channels)
    pipe._parser = FakeParser(script or {})
    return pipe, site, notifier


def msg(text, chat=CHAT, voice=False, reply_to=""):
    return IncomingMessage(id=1, date=datetime.now(timezone.utc), text=text, image=None, chat_id=chat, voice=voice,
                           reply_to=reply_to)


KG = dict(selection="Knights", event="Knights v ?", sport="cricket")


def test_stake_message_completes_a_held_entry(store):
    script = {"5 odds back kg": [signal(odds=5.0, **KG)], "Limit 2": [signal(action="stake", odds=None, units=2, **KG)]}
    pipe, site, _ = make(store, Profile("ss", rules="ss", stake_wait_s=5), script, live=5.0)

    async def go():
        await pipe._handle(msg("5 odds back kg"))
        assert site.calls == []  # held for its stake message
        await pipe._handle(msg("Limit 2"))

    asyncio.run(go())
    assert site.calls == [("entry", "Knights", 2)]
    assert store.find_position("Knights", "live").stake == 2000


def test_held_entry_goes_with_one_unit_when_no_stake_comes(store):
    script = {"5 odds back kg": [signal(odds=5.0, **KG)]}
    pipe, site, _ = make(store, Profile("ss", rules="ss", stake_wait_s=0.1), script, live=5.0)

    async def go():
        await pipe._handle(msg("5 odds back kg"))
        await asyncio.sleep(0.3)

    asyncio.run(go())
    assert site.calls == [("entry", "Knights", None)] and store.find_position("Knights", "live").stake == 1000


def test_late_limit_message_tops_up(store):
    script = {"5 odds back kg limit 1": [signal(odds=5.0, units=1, **KG)],
              "Limit 2": [signal(action="stake", odds=None, units=2, **KG)]}
    pipe, site, _ = make(store, Profile("ss", rules="ss", limit_followup_s=180), script, live=5.0)

    async def go():
        await pipe._handle(msg("5 odds back kg limit 1"))
        await pipe._handle(msg("Limit 2"))

    asyncio.run(go())
    assert site.calls == [("entry", "Knights", 1), ("entry", "Knights", 1000)]
    assert store.find_position("Knights", "live").stake == 2000


def test_waiting_entry_goes_in_once_the_price_comes(store):
    pipe, site, _ = make(store, Profile("tip"), live=2.4)

    async def go():
        first = await pipe.execute(signal(odds=3.0, when_odds=3.0, selection="Berrettini", event="Berrettini v ?",
                                          channel="Tip"))
        assert first.status == "pending" and "waiting" in first.reason
        await pipe.retry_pending_once()  # still 2.4
        site.live = 3.05
        await pipe.retry_pending_once()

    asyncio.run(go())
    assert store.find_position("Berrettini", "live").price == 3.05


def test_waiting_entry_cancelled(store):
    pipe, site, _ = make(store, Profile("tip"), live=2.4)

    async def go():
        await pipe.execute(signal(odds=3.0, when_odds=3.0, selection="Berrettini", event="Berrettini v ?", channel="Tip"))
        result = await pipe.execute(signal(action="cancel", odds=None, selection="Berrettini", channel="Tip"))
        assert result.status == "placed" and "dropped" in result.reason

    asyncio.run(go())
    assert store.pending_instructions("live") == []


def test_green_money_stakes_a_share_of_the_last_bookset(store):
    script = {"Green 20% back 7 odds sa": [signal(odds=7.0, green_pct=20, selection="South Africa", event="SA v ?")]}
    pipe, site, _ = make(store, Profile("ss", green_money="stake"), script, live=7.0)
    store.open_position(signal(selection="Lions", event="Lions v ?", channel="Tip"), "live", 16, 1000)
    store.set_closed(store.find_position("Lions", "live").id, pl=500)
    asyncio.run(pipe._handle(msg("Green 20% back 7 odds sa")))
    assert site.calls == [("entry", "South Africa", 100)]


def test_green_money_is_off_by_default(store):
    script = {"Green 20% back 7 odds sa": [signal(odds=7.0, green_pct=20, selection="South Africa", event="SA v ?")]}
    pipe, site, _ = make(store, Profile("ss"), script, live=7.0)
    asyncio.run(pipe._handle(msg("Green 20% back 7 odds sa")))
    assert site.calls == [] and "green-money" in store.recent_bets()[0]["reason"]


def test_confirm_yes_and_no(store):
    script = {"Back x": [signal(odds=3.0, selection="Shelton", event="Shelton v ?")],
              "Back y": [signal(odds=3.0, selection="Gea", event="Gea v ?")]}
    pipe, site, notifier = make(store, Profile("tip", confirm={"always": True}), script)

    async def go():
        await pipe._handle(msg("Back x"))
        await pipe._handle(msg("Back y"))
        assert site.calls == [] and sum("CONFIRM #" in t for t in notifier.sent) == 2
        assert "PLACED" in await pipe.command("/yes 1")
        assert "dropped" in await pipe.command("/no 2")
        assert "No open question" in await pipe.command("/yes 2")

    asyncio.run(go())
    assert site.calls == [("entry", "Shelton", None)]


def test_unanswered_question_is_dropped(store):
    pipe, site, _ = make(store, Profile("tip", confirm={"always": True}), {"Back x": [signal(odds=3.0)]},
                         confirm_timeout_s=0)

    async def go():
        await pipe._handle(msg("Back x"))
        await asyncio.sleep(0.01)
        await pipe._expire_asked()

    asyncio.run(go())
    assert site.calls == [] and "not answered" in store.recent_bets()[0]["reason"]


def test_bookings_are_never_held_for_confirmation(store):
    pipe, site, _ = make(store, Profile("tip", confirm={"always": True}), {"Bookset": [signal(action="close", odds=None)]})
    store.open_position(signal(channel="Tip"), "live", 2.9, 1000)
    asyncio.run(pipe._handle(msg("Bookset")))
    assert site.calls == [("close", "Florian Broska", None)]


def test_pause_and_resume(store):
    pipe, site, _ = make(store, Profile("tip"))

    async def go():
        await pipe.command("/pause")
        paused = await pipe.execute(signal(odds=3.0, channel="Tip"))
        assert paused.status == "skipped" and "PAUSED" in paused.reason
        await pipe.command("/resume")
        assert (await pipe.execute(signal(odds=3.0, channel="Tip"))).status == "placed"

    asyncio.run(go())


def test_strict_channel_never_calls_the_ai(store):
    pipe, site, _ = make(store, Profile("test", strict_only=True))

    async def go():
        await pipe._handle(msg("BACK Shelton 3 1u"))
        await pipe._handle(msg("good luck guys"))

    asyncio.run(go())
    assert pipe._parser.calls == 0 and site.calls == [("entry", "Shelton", 1)]


def test_saved_messages_trade_command(store):
    pipe, site, _ = make(store, Profile("tip"))
    reply = asyncio.run(pipe.command("/back Shelton 3 1u"))
    assert reply.startswith("PLACED") and site.calls == [("entry", "Shelton", 1)]


def test_group_link_alert_from_a_watch_only_channel(store):
    pipe, site, notifier = make(store, Profile("open", trade=False, watch_links=True))
    asyncio.run(pipe._handle(msg("Naliku group link https://t.me/+AbCdEf123 join pannunga")))
    assert pipe._parser.calls == 0 and any("t.me/+AbCdEf123" in t for t in notifier.sent)


def test_settle_and_report(store):
    pipe, site, _ = make(store, Profile("tip"))
    store.open_position(signal(selection="Andreeva", event="Andreeva v ?", channel="Tip"), "live", 3.2, 1000)
    pos = store.find_position("Andreeva", "live")
    store.set_closed(pos.id, how="finished")
    report = asyncio.run(pipe.command("/report"))
    assert "Tip" in report and f"/settle {pos.id}" in report
    assert "-1000" in asyncio.run(pipe.command(f"/settle {pos.id} lose"))
    assert "Known result: -1000" in asyncio.run(pipe.command("/report week"))


def test_ai_spelling_still_finds_the_player_and_the_trade_is_kept_under_the_site_name(store):
    """Real case: the AI turned 'Back gea 5 odds 1 unit' into 'Gaea', then 'Loss cut 2.5 la' into close Gaea."""
    script = {"Back gea 5 odds 1 unit": [signal(selection="Gaea", event="Gaea v ?", odds=5.0, units=1)],
              "Loss cut 2.5 la": [signal(action="close", selection="Gaea", event="Gaea v ?", odds=None,
                                         loss_cut=True)]}
    pipe, site, _ = make(store, Profile("tipster", rules="tipster"), script, live=5.0, site_class=GeaSite)
    asyncio.run(pipe._handle(msg("Back gea 5 odds 1 unit")))
    pos = store.find_position("Arthur Gea", "live")
    assert (pos.selection, pos.event) == ("Arthur Gea", "Arthur Gea v Tomas Machac")
    asyncio.run(pipe._handle(msg("Loss cut 2.5 la")))
    assert site.calls[-1][:2] == ("close", "Gaea") and store.find_position("Arthur Gea", "live") is None


def test_trades_are_matched_loosely_only_when_one_fits(store):
    store.open_position(signal(selection="Arthur Gea", event="Arthur Gea v ?", channel="Tip"), "live", 5, 1000)
    assert store.find_position("Gaea", "live", "Tip").selection == "Arthur Gea"
    assert store.find_position("Garin", "live", "Tip") is None
    store.open_position(signal(selection="Gaia Sanesi", event="Gaia Sanesi v ?", channel="Tip"), "live", 3, 1000)
    assert store.find_position("Gaea", "live", "Tip") is None  # two could be meant: neither is picked


def test_a_second_trade_in_the_same_match_is_refused(store):
    """The site's Cashout books the whole match, so the other channel's bookset would close this trade too."""
    risk = RiskManager(make_settings(), store, "live")
    store.open_position(signal(selection="Jannik Sinner", event="Jannik Sinner v Carlos Alcaraz", channel="Tip"),
                        "live", 2.0, 1000)
    check = risk.precheck(signal(selection="Alcaraz", event="Alcaraz v ?", odds=2.1, channel="SS"))
    assert not check.ok and "SAME MATCH" in check.reason
    assert risk.precheck(signal(selection="Rublev", event="Rublev v ?", odds=2.1, channel="SS")).ok


def test_a_channels_messages_keep_their_order_when_a_voice_note_is_slow(store):
    script = {"5 odds back kg": [signal(odds=5.0, **KG)], "Limit 2": [signal(action="stake", odds=None, units=2, **KG)]}
    pipe, site, _ = make(store, Profile("ss", rules="ss", stake_wait_s=5), script, live=5.0)

    async def voice_note():  # still being transcribed when the "Limit 2" after it arrives
        await asyncio.sleep(0.2)
        return msg("5 odds back kg")

    async def text():
        return msg("Limit 2")

    async def go():
        await pipe.enqueue(CHAT, voice_note())
        await pipe.enqueue(CHAT, text())
        await asyncio.sleep(0.5)

    asyncio.run(go())
    assert site.calls == [("entry", "Knights", 2)]  # not "Limit 2" first and then an entry of 1 unit


def test_bookings_go_before_entries_waiting_for_the_browser():
    lock, order = TradeLock(), []

    async def action(name, priority):
        async with lock(priority):
            order.append(name)
            await asyncio.sleep(0.01)

    async def go():
        async with lock(TradeLock.OTHER):  # one channel's entry is on the site
            waiting = [asyncio.create_task(action("entry SS", TradeLock.OTHER)),
                       asyncio.create_task(action("auto-bookset check", TradeLock.OTHER)),
                       asyncio.create_task(action("bookset Tipster", TradeLock.BOOKING))]
            await asyncio.sleep(0.01)
        await asyncio.gather(*waiting)

    asyncio.run(go())
    assert order == ["bookset Tipster", "entry SS", "auto-bookset check"]


def test_a_cancelled_wait_does_not_block_the_lock():
    lock, order = TradeLock(), []

    async def action(name):
        async with lock():
            order.append(name)

    async def go():
        async with lock():
            first, second = asyncio.create_task(action("first")), asyncio.create_task(action("second"))
            await asyncio.sleep(0.01)
            first.cancel()
        await second

    asyncio.run(go())
    assert order == ["second"]


def test_a_voice_note_alone_is_context_not_an_instruction(store):
    """Transcribed Tamil "be ready for the bookset" was read as a bookset: voice notes inform, text instructs."""
    voice = "(voice note) bookset ready-aa irunga"
    script = {voice: [signal(action="close", odds=None, channel="Tip")],
              "Bookset": [signal(action="close", odds=None, channel="Tip")]}  # the AI is given the caption only
    pipe, site, _ = make(store, Profile("ss", voice=True), script)
    store.open_position(signal(channel="Tip"), "live", 2.9, 1000)
    asyncio.run(pipe._handle(msg(voice, voice=True)))
    assert site.calls == [] and pipe._parser.calls == 0
    asyncio.run(pipe._handle(msg("Bookset\n" + voice, voice=True)))  # its caption is an instruction
    assert site.calls == [("close", "Florian Broska", None)]


SA_AUS = dict(event="South Africa v Australia", sport="cricket")
BOTH_TEAMS = {"orders": [signal(selection="South Africa", odds=3.0, units=2, **SA_AUS),
                         signal(selection="Australia", odds=4.0, units=1.5, **SA_AUS)]}


def _trades_after_both_teams_tipped(store, settings, mode):
    pipe, _, _ = make(store, Profile("cs", settings=settings), BOTH_TEAMS, live=4.0, mode=mode)
    asyncio.run(pipe._handle(msg("orders")))
    return store._db.execute("SELECT COUNT(*) FROM positions").fetchone()[0]


def test_a_system_channel_backs_both_teams_on_paper(store):
    """CricStatz's Test system: 'Back South Africa 2pts @ 3.0 / Back Australia 1.5pts @ 4.0'."""
    assert _trades_after_both_teams_tipped(store, {"both_sides_in_match": True}, "dry_run") == 2


def test_other_channels_stay_on_one_team(store):
    assert _trades_after_both_teams_tipped(store, {}, "dry_run") == 1


def test_both_teams_is_never_for_live(store):
    assert _trades_after_both_teams_tipped(store, {"both_sides_in_match": True}, "live") == 1


def test_a_long_waiting_order_reads_its_price_once_a_minute(store):
    """Up to 5 days of Test match orders: every 10 s would keep the browser busy all that time."""
    order = signal(selection="South Africa", odds=5.0, when_odds=5.0, units=2, **SA_AUS)
    pipe, site, _ = make(store, Profile("cs", settings={"conditional_entry_min": 7200}), {"order": [order]}, live=3.0,
                         mode="dry_run")

    async def go():
        await pipe._handle(msg("order"))
        looks = len(site.calls)
        await pipe.retry_pending_once()
        assert len(site.calls) == looks + 1
        await pipe.retry_pending_once()
        assert len(site.calls) == looks + 1  # within the minute
        pending_id = store.pending_instructions("dry_run")[0][0]
        pipe._last_look[pending_id] = datetime.now() - timedelta(seconds=61)
        await pipe.retry_pending_once()
        assert len(site.calls) == looks + 2

    asyncio.run(go())


def test_a_short_waiting_order_still_reads_its_price_every_time(store):
    order = signal(selection="South Africa", odds=5.0, when_odds=5.0, units=2, **SA_AUS)
    pipe, site, _ = make(store, Profile("ss"), {"order": [order]}, live=3.0, mode="dry_run", conditional_entry_min=90)

    async def go():
        await pipe._handle(msg("order"))
        looks = len(site.calls)
        await pipe.retry_pending_once()
        await pipe.retry_pending_once()
        assert len(site.calls) == looks + 2

    asyncio.run(go())


def test_if_enter_bookset_is_a_close_now(store):
    """SK Astro 31 Oct 11:27: dropped as a bookset still to come because of its 'if'."""
    script = {"If Enter Bookset": [signal(action="close", odds=None)]}
    pipe, site, _ = make(store, Profile("sk"), script)
    store.open_position(signal(channel="Tip"), "live", 2.9, 1000)
    asyncio.run(pipe._handle(msg("If Enter Bookset")))
    assert site.calls == [("close", "Florian Broska", None)]


def test_a_voice_note_acts_when_the_channel_says_so(store):
    """SS gives most exits in Tamil voice notes. Only a confident reading acts; plain talk doesn't reach the AI."""
    now = "(voice note) அதையே profit ஆருந்தா loss cut பண்ணிக்கோங்க."
    coming = "(voice note) புக்செட் ரெடியா இருக்குங்கள். சொல்றேன் சொல்றேன்."
    unsure = "(voice note) bookset பண்ணிடுங்க"
    talk = "(voice note) நல்ல match, பாத்துக்கலாம்"
    close = signal(action="close", odds=None)
    script = {now: [close], coming: [close], unsure: [signal(action="close", odds=None, confidence=0.6)]}
    pipe, site, _ = make(store, Profile("ss", voice=True, act_on_voice=True, entry_words=r"\b(back)\b"), script)
    store.open_position(signal(channel="Tip"), "live", 2.9, 1000)

    async def go():
        await pipe._handle(msg(talk, voice=True))
        assert pipe._parser.calls == 0
        await pipe._handle(msg(coming, voice=True))
        await pipe._handle(msg(unsure, voice=True))
        assert site.calls == []
        await pipe._handle(msg(now, voice=True))

    asyncio.run(go())
    assert site.calls == [("close", "Florian Broska", None)]


def test_voice_talk_about_a_later_bookset_books_nothing(store):
    """SS's old notes the AI read as a close: a price, an over, or 'should' still to come."""
    notes = ["(voice note) 1.8 அந்த Rangeன்னா நம்ம புக்க்செட் பண்ணினா போதும்.",
             "(voice note) விக்கெட் போய்ட்டுச்சு அப்படின்னா, 1.9, 1.8 ரேஞ்ச்ல வந்தாலும் லாஸ்கட் பண்ணிடுங்க.",
             "(voice note) 35ஆவது ஓவர்லே நம bookset பண்ணியனும்.",
             "(voice note) Bookset கண்டிப்பா லோவான் தான் புக்க்சட் பண்ணனும்."]
    script = {note: [signal(action="close", odds=None)] for note in notes}
    pipe, site, _ = make(store, Profile("ss", voice=True, act_on_voice=True), script)
    store.open_position(signal(channel="Tip"), "live", 2.9, 1000)

    async def go():
        for note in notes:
            await pipe._handle(msg(note, voice=True))

    asyncio.run(go())
    assert site.calls == []


def test_a_voice_entry_is_only_logged(store):
    spoken = "(voice note) இப்போ ஒரு limit இந்தியா back பண்ணிக்கோங்க"
    script = {spoken: [signal(selection="India", event="India v ?", odds=None, units=1)]}
    pipe, site, _ = make(store, Profile("ss", voice=True, act_on_voice=True), script)
    asyncio.run(pipe._handle(msg(spoken, voice=True)))
    assert site.calls == []


def test_entries_need_an_entry_word_and_bookings_not_yet_are_dropped(store):
    script = {"Sl 1.2 coming": [signal(odds=1.2, selection="Sri Lanka", event="Sri Lanka v ?")],
              "Sl back 5.7 odds": [signal(odds=5.7, selection="Sri Lanka", event="Sri Lanka v ?")],
              "Ready for bookset": [signal(action="close", odds=None, selection="Sri Lanka")]}
    pipe, site, _ = make(store, Profile("ss", entry_words=r"\b(back|lay|entry|take|try)\b"), script, live=5.7)

    async def go():
        await pipe._handle(msg("Sl 1.2 coming"))
        await pipe._handle(msg("Ready for bookset"))
        await pipe._handle(msg("Sl back 5.7 odds"))

    asyncio.run(go())
    assert site.calls == [("entry", "Sri Lanka", None)]


def test_a_close_naming_another_team_acts_on_the_latest_entry(store):
    """The AI took 'Australia' from the context for a bare 'Bookset'; the channel's latest entry was Sri Lanka."""
    script = {"Sl back 5.7 odds": [signal(odds=5.7, selection="Sri Lanka", event="Sri Lanka v England")],
              "Bookset enjoy double side jackpot": [signal(action="close", odds=None, selection="Australia",
                                                           event="Australia v South Africa")]}
    pipe, site, _ = make(store, Profile("ss"), script, live=5.7)

    async def go():
        await pipe._handle(msg("Sl back 5.7 odds"))
        await pipe._handle(msg("Bookset enjoy double side jackpot"))

    asyncio.run(go())
    assert site.calls == [("entry", "Sri Lanka", None), ("close", "Sri Lanka", None)]


def test_no_close_when_the_latest_entry_was_not_placed(store):
    script = {"Pak back 30 odds": [signal(odds=30.0, selection="Pakistan", event="Pakistan v India")],
              "Bookset now": [signal(action="close", odds=None, selection="Zimbabwe", event="Zimbabwe v ?")]}
    pipe, site, _ = make(store, Profile("ss", settings={"max_odds": 20}), script, live=30.0)
    store.open_position(signal(selection="Zimbabwe", event="Zimbabwe v ?", channel="Tip"), "live", 4.0, 1000)

    async def go():
        await pipe._handle(msg("Pak back 30 odds"))  # skipped: above the channel's max odds
        await pipe._handle(msg("Bookset now"))

    asyncio.run(go())
    assert site.calls == [("entry", "Pakistan", None)]  # tried, refused by the odds range; and no close after it
    assert store.find_position("Zimbabwe", "live") is not None


def test_a_close_after_a_skipped_re_entry_books_the_open_trade(store):
    """Tennis Tv 22 Oct: Uchijima entry placed, '35P entry kr lena' skipped (already in it), then 'LOSS CUT ONLY'."""
    uchi = dict(selection="Moyuka Uchijima", event="Moyuka Uchijima v ?")
    script = {"UCHIJIMA Win Karo": [signal(odds=2.5, **uchi)], "35P ENTRY KR LENA": [signal(odds=1.35, **uchi)],
              "LOSS CUT ONLY": [signal(action="close", odds=None, loss_cut=True, selection="Mary Stoiana",
                                       event="Mary Stoiana v ?")]}
    pipe, site, _ = make(store, Profile("tv"), script, live=2.5)

    async def go():
        for text in script:
            await pipe._handle(msg(text))

    asyncio.run(go())
    # '35P entry' at once is the same entry (a re-entry needs the trade 10 min old), so skipped: already in it
    assert site.calls == [("entry", "Moyuka Uchijima", None), ("close", "Moyuka Uchijima", None)]


def test_add_tops_up_the_open_trade(store):
    """SS 22 Oct: '4.3 odds ind back 0.5 limit', then 'Add one more 1 limit'."""
    ind = dict(selection="India", event="India v New Zealand", sport="cricket")
    script = {"4.3 odds ind back 0.5 limit": [signal(odds=4.3, units=0.5, **ind)],
              "Add one more 1 limit": [signal(action="add", odds=None, units=1, **ind)]}
    pipe, site, _ = make(store, Profile("ss"), script, live=4.3)

    async def go():
        for text in script:
            await pipe._handle(msg(text))

    asyncio.run(go())
    assert site.calls == [("entry", "India", 0.5), ("entry", "India", 1000)]  # 1 unit more, at 1000 a unit
    assert store.find_position("India", "live").stake == 1500


def test_again_bookset_closes_the_earlier_open_trade(store):
    """SS 22 Oct: India trade open, a newer Sri Lanka entry, then 'Again bookset enjoy' about India."""
    script = {"4.4 odds back ind": [signal(odds=4.4, units=0.5, selection="India", event="India v New Zealand")],
              "7.6 odds sl back limit 0.5": [signal(odds=4.4, units=0.5, selection="Sri Lanka",  # the fake site's price
                                                    event="England v Sri Lanka")],
              "Again bookset enjoy": [signal(action="close", odds=None, selection="India",
                                             event="India v New Zealand")]}
    pipe, site, _ = make(store, Profile("ss"), script, live=4.4)

    async def go():
        for text in script:
            await pipe._handle(msg(text))

    asyncio.run(go())
    assert site.calls[-1] == ("close", "India", None)
    assert store.find_position("Sri Lanka", "live") is not None


def test_again_bookset_never_books_the_newest_trade_when_the_re_entry_was_skipped(store):
    """SS 22 Oct as replayed: India booked, its green-money re-entry skipped, Sri Lanka entered, then 'Again
    bookset enjoy' (India's second trade, by the tipster's day summary) read by the AI as Sri Lanka."""
    ind = dict(selection="India", event="India v New Zealand")
    script = {"4.3 odds ind back 0.5 limit": [signal(odds=4.4, units=0.5, **ind)],
              "Losscut": [signal(action="close", odds=None, loss_cut=True, **ind)],
              "4.4 odds back ind green money only 10% or 15% try": [signal(odds=4.4, green_pct=10, **ind)],
              "7.6 odds sl back limit 0.5": [signal(odds=4.4, units=0.5, selection="Sri Lanka",
                                                    event="England v Sri Lanka")],
              "Again bookset enjoy": [signal(action="close", odds=None, selection="Sri Lanka",
                                             event="England v Sri Lanka")]}
    pipe, site, _ = make(store, Profile("ss"), script, live=4.4)

    async def go():
        for text in script:
            await pipe._handle(msg(text))

    asyncio.run(go())
    assert site.calls[-1] == ("entry", "Sri Lanka", 0.5)  # no close after it
    assert store.find_position("Sri Lanka", "live") is not None


def test_a_deleted_tip_is_recorded_and_reported(store):
    """Tennis Tv 23 Oct deleted every losing tip after the match: the bot keeps them and tells you."""
    pipe, site, notifier = make(store, Profile("tv"), {"BEJLEK Win Karo": [signal(odds=2.0)]}, live=2.0)
    asyncio.run(pipe._handle(msg("BEJLEK Win Karo")))
    asyncio.run(pipe.deleted(CHAT, [1, 999]))  # 999: never received
    assert "deleted 1 message" in notifier.sent[-1] and "ENTRY placed" in notifier.sent[-1]
    assert store.rows("SELECT COUNT(*) FROM messages WHERE deleted_at IS NOT NULL")[0][0] == 1


def test_the_daily_report_lists_each_channels_trades(store):
    pipe, _, _ = make(store, Profile("tv"))
    store.open_position(signal(selection="Bejlek", event="Bejlek v ?", channel="Tip"), "live", 2.32, 2700)
    store.open_position(signal(selection="Kawa", event="Kawa v ?", channel="Tip"), "live", 3.85, 1800)
    bejlek = store.find_position("Bejlek", "live")
    store.set_closed(bejlek.id, how="finished")
    store.settle(bejlek.id, -2700)
    text = pipe.day_report_text()
    assert "*Tip*: -2.70u" in text and "Bejlek 2.7u @ 2.32 → -2.70u (lost at the end)" in text
    assert "Kawa 1.8u @ 3.85 → open" in text and "⏳ 1 open" in text


def test_a_trade_left_to_the_end_settles_from_its_last_price(store):
    store.open_position(signal(selection="Jarry", event="Jarry v ?"), "live", 1.89, 60)
    back = store.find_position("Jarry", "live")
    assert result_from_price(back, 1.02) == "win"
    assert result_from_price(back, 25.0) == "lose"
    assert result_from_price(back, 1.6) == ""  # not clear: /settle by hand
    assert result_from_price(back, None) == ""
    assert result_from_price(replace(back, side="lay"), 1.02) == "lose"  # laid the runner that won


def test_a_stake_in_rupees_completes_the_held_entry(store):
    script = {"8 odds back war": [signal(odds=8.0, **dict(KG, selection="Warriors", event="Warriors v ?"))],
              "Try 200 or 300": [signal(action="stake", odds=None, amount=200, selection="Warriors")]}
    pipe, site, _ = make(store, Profile("ss", rules="ss", stake_wait_s=5), script, live=8.0)

    async def go():
        await pipe._handle(msg("8 odds back war"))
        await pipe._handle(msg("Try 200 or 300"))

    asyncio.run(go())
    assert site.calls == [("entry", "Warriors", 200)]


def test_the_tipsters_rupees_become_units(store):
    script = {"8 odds back war": [signal(odds=8.0, **dict(KG, selection="Warriors", event="Warriors v ?"))],
              "Try 200 or 300": [signal(action="stake", odds=None, amount=200, selection="Warriors")]}
    pipe, site, _ = make(store, Profile("ss", rules="ss", stake_wait_s=5, rupees_per_unit=1000), script, live=8.0)

    async def go():
        await pipe._handle(msg("8 odds back war"))
        await pipe._handle(msg("Try 200 or 300"))

    asyncio.run(go())
    assert site.calls == [("entry", "Warriors", 0.2)]  # 200 of the tipster's 1000-rupee unit


def test_a_tip_above_the_most_units_is_capped_not_skipped(store):
    script = {"Back x": [signal(odds=3.0, units=2.8)]}
    pipe, site, _ = make(store, Profile("tip", settings={"max_units_per_bet": 2}), script, live=3.0)
    asyncio.run(pipe._handle(msg("Back x")))
    assert [units for _, _, units in site.calls] == [2]


TENNIS_TV_TIP = ("🎾 Live Running Tennis Match 🎾\n\nSHEVCHENKO Win Karo...🏆\n\nPlay Smartly 👍\nPlay In Limit 👍\n\n"
                 "1.70 limit use kro 4 limit rhega SHEVCHENKO GAANDU pr✅\n\nPLACE YOUR BETS ON 👇 \n\nhttps://x.org")


def test_tennis_tv_template():
    sig = parse_known(TENNIS_TV_TIP, "tennistv")[0]
    assert (sig.action, sig.selection, sig.units, sig.odds) == ("entry", "Shevchenko", 1.7, 3.35)  # 1 + 4 / 1.7
    assert parse_known("Moj kro ✅✅\n\nBookset lifeset 😍❤️\n\nCashout done 2 - 2 limit😎", "tennistv")[0].action == "close"
    assert not_yet("CASHOUT KHUDSE KR LENA BHAIYON 👆")  # cash out yourself: no exit given


def test_forwarded_testimonials_are_context(store):
    pipe, site, _ = make(store, Profile("tv", act_on_forwards=False),
                         {"Book set maar diya": [signal(action="close", odds=None)]})
    store.open_position(signal(channel="Tip"), "live", 2.9, 1000)
    testimonial = msg("Book set maar diya")
    testimonial.forwarded = True
    asyncio.run(pipe._handle(testimonial))
    assert site.calls == [] and pipe._parser.calls == 0


def test_paper_trading_goes_ahead_instead_of_asking(store):
    pipe, site, notifier = make(store, Profile("tip", confirm={"always": True}), {"Back x": [signal(odds=3.0)]},
                                mode="dry_run")
    asyncio.run(pipe._handle(msg("Back x")))
    assert site.calls == [("entry", "Florian Broska", None)] and not any("CONFIRM" in t for t in notifier.sent)
    assert "live would ask" in store.recent_bets()[0]["reason"]


def test_an_entry_must_name_its_team(store):
    """'Now 8.4 odds back' (the team came in the next message) was read as India from the context."""
    script = {"Now 8.4 odds back": [signal(odds=8.4, selection="India", event="India v West Indies")],
              "Back beaz 4 odds": [signal(odds=4.0, selection="Sebastian Baez", event="Baez v ?")]}
    pipe, site, _ = make(store, Profile("ss", entry_words=r"\b(back|entry)\b"), script, live=4.0)

    async def go():
        await pipe._handle(msg("Now 8.4 odds back"))
        await pipe._handle(msg("Back beaz 4 odds"))  # misspelt, but named

    asyncio.run(go())
    assert site.calls == [("entry", "Sebastian Baez", None)]


def test_an_unnamed_entry_takes_the_team_of_an_earlier_tip(store):
    """Tennis King Free 26 Oct: 'Navone WIN KRO' came before the bot restarted (context only), then
    '18p se entry kro guys'."""
    script = {"18p se entry kro guys": [signal(odds=1.18, selection="Mariano Navone", event="Navone v ?")]}
    pipe, site, _ = make(store, Profile("tv", entry_words=r"(win kro|entry)"), script, live=1.18)
    pipe.preload([msg("80p Darderi FAV / KHAOOOO / Navone WIN KRO")])
    asyncio.run(pipe._handle(msg("18p se entry kro guys")))
    assert site.calls == [("entry", "Mariano Navone", None)]


def test_texts_without_instruction_words_skip_the_ai(store):
    """Free AI tier: 'Good morning', zone lists and result chatter aren't sent to the AI; tips and closes are."""
    pipe, site, _ = make(store, Profile("ss", ai_filter=True), {"Losscut must": [signal(action="close", odds=None)]})

    async def go():
        for text in ("Good morning 🙏", "1 to 3 over afg \n4 to 13 over zim", "S-AYUB 06 RUN BANAKER OUT ✔️",
                     "Losscut must"):
            await pipe._handle(msg(text))

    asyncio.run(go())
    assert pipe._parser.calls == 1  # only "Losscut must"


def test_photos_without_instruction_words_skip_the_ai(store):
    """Kartik Bhai 27 Oct: an album of ~100 result photos used up the free AI. A bare photo, or one whose caption
    has no instruction word, isn't read; a captioned tip is."""
    pipe, site, _ = make(store, Profile("kb", ai_filter=True, bare_photos=False),
                         {"68P - ENGLAND Khaooo": [signal(odds=1.68)]})

    def photo(text):
        return IncomingMessage(id=1, date=datetime.now(timezone.utc), text=text, image=b"jpeg", chat_id=CHAT)

    async def go():
        for text in ("", "🏆 PAKISTAN PASS 🏆", "68P - ENGLAND Khaooo"):
            await pipe._handle(photo(text))

    asyncio.run(go())
    assert pipe._parser.calls == 1


def test_a_lay_on_the_channels_open_back_books_it(store):
    """Tennis Tips King 27 Oct: 'Rublev win karo', then '60P LAY KARO Rublev' = trade out of it."""
    rublev = dict(selection="Andrey Rublev", event="Andrey Rublev v ?")
    script = {"Rublev win karo": [signal(odds=2.0, **rublev)],
              "60P LAY KARO Rublev": [signal(odds=1.6, side="lay", **rublev)]}
    pipe, site, _ = make(store, Profile("tk"), script, live=2.0)

    async def go():
        for text in script:
            await pipe._handle(msg(text))

    asyncio.run(go())
    assert site.calls == [("entry", "Andrey Rublev", None), ("close", "Andrey Rublev", None)]


def test_a_lay_at_the_other_runners_price_adds_to_the_back(store):
    """Tennis Tips King 31 Oct: 'Adolfo PLUS KARO', then '45P LAY KARO, Adolfo PLUS KARO' read as a lay of Adolfo, who is
    3.2 by then: the favourite is laid at 1.45. More of the trade, sized as that lay; booking it locked -212."""
    adolfo = dict(selection="Adolfo", event="Adolfo v ?")
    script = {"Adolfo PLUS KARO": [signal(odds=None, **adolfo)],
              "45P LAY KARO": [signal(odds=1.45, side="lay", **adolfo)]}
    pipe, site, _ = make(store, Profile("tk"), script, live=3.2)

    async def go():
        for text in script:
            await pipe._handle(msg(text))

    asyncio.run(go())
    assert site.calls == [("entry", "Adolfo", None), ("entry", "Adolfo", 450)]
    assert store.find_position("Adolfo", "live").stake == 1450


def test_a_call_for_those_not_in_yet_adds_nothing(store):
    """Tennis Professor 31 Oct: 'Hugo win karo', then '50p h jiska entry nahi hua hai kar lo' doubled the stake."""
    hugo = dict(selection="Hugo", event="Hugo v ?")
    script = {"Hugo win karo": [signal(odds=None, **hugo)],
              "50p h jiska entry nahi hua hai kar lo": [signal(odds=1.5, **hugo)]}
    pipe, site, _ = make(store, Profile("tk"), script, live=2.5)

    async def go():
        for text in script:
            await pipe._handle(msg(text))

    asyncio.run(go())
    assert site.calls == [("entry", "Hugo", None)]
    assert store.find_position("Hugo", "live").stake == 1000


def test_your_own_telegram_channel_is_followed_by_its_title():
    """You make a private Telegram channel and post your tips there: its title is enough, no id to copy."""
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "channels.example.json"
    channels = Channels.load(make_settings(), path)
    assert channels.by_title("MY Tips").name == "MY Tips"
    assert channels.by_title("WA Tamil Sports") is None


def test_your_own_post_in_a_followed_channel_is_read_as_its_tip(store):
    pipe, site, _ = make(store, Profile("wa", pasted=True), {"Back x": [signal(odds=3.0)]}, mode="dry_run")
    asyncio.run(pipe._handle(msg("Back x")))
    assert site.calls == [("entry", "Florian Broska", None)]


def test_a_close_naming_the_other_team_closes_the_trade_in_that_match(store):
    """Tamil Sports 1 Nov: 'BACKED INDIA 3.05', then 'Loss cut @1.74 NZ': the team named is where the price is."""
    match = dict(event="India v New Zealand", sport="cricket")
    script = {"BACKED INDIA 3.05": [signal(selection="India", odds=3.05, **match)],
              "Loss cut @1.74 NZ": [signal(action="close", selection="New Zealand", side="lay", odds=1.74, loss_cut=True,
                                           **match)]}
    pipe, site, _ = make(store, Profile("wa"), script, live=3.05)

    async def go():
        for text in script:
            await pipe._handle(msg(text))

    asyncio.run(go())
    assert site.calls[-1] == ("close", "India", None) and store.find_position("India", "live") is None


def test_david_reports_entries_are_read_by_rule():
    """1 Nov: the AI found nothing in '59 india / Lay-60% / NEWZLEAND plus', his usual entry."""
    from tradebot.rules import parse_known
    (sig,) = parse_known("59 india\n\nLay-60% \n\n *NEWZLEAND plus*", "david")
    assert (sig.action, sig.selection, sig.side, sig.odds, sig.units) == ("entry", "India", "lay", 1.59, 0.6)
    assert sig.event == "India v Newzleand"
    (sig,) = parse_known("WBBL \nHobartw -50p\nUse 60% limit \n\n *Sixers plus*", "david")
    assert (sig.selection, sig.side, sig.odds, sig.units) == ("Hobartw", "lay", 1.5, 0.6)
    assert parse_known("NEWZLEAND VS INDIA 5TH T20I\n\nNEWZLEAND WIN OR 37P", "david") is None
    assert parse_known("Wait no ball", "david") is None
    (sig,) = parse_known("Now loss cut", "david")
    assert sig.action == "close" and sig.loss_cut


def test_a_call_before_the_entry_is_no_trade(store):
    """David Reports 1 Nov: 'NEWZLEAND WIN OR 37P' was read as a back at 3.7; '59 india / Lay-60% / NEWZLEAND plus' is the entry."""
    nz = dict(event="New Zealand v India", sport="cricket")
    script = {"NEWZLEAND WIN OR 37P": [signal(selection="New Zealand", odds=3.7, **nz)],
              "59 india Lay-60% NEWZLEAND plus": [signal(selection="India", side="lay", odds=1.59, units=0.6, **nz)]}
    pipe, site, _ = make(store, Profile("wa", entry_words="(lay|limit|plus)"), script, live=1.59, mode="dry_run")

    async def go():
        for text in script:
            await pipe._handle(msg(text))

    asyncio.run(go())
    assert site.calls == [("entry", "India", 0.6)]


def test_only_the_new_lines_of_a_pasted_block_are_tips():
    """1 Nov: a block of WhatsApp posts from 29 Oct on, copied together, each stamped '[10/29, 9:56 AM] null: '."""
    from datetime import timedelta
    from tradebot.rules import fresh_part_of_paste
    now, within = datetime(2026, 11, 1, 14, 4), timedelta(minutes=3)
    block = ("[10/29, 9:56 AM] null: Backed Melbourne  Renegades 30% stake @2.74\n"
             "[11/1, 1:07 PM] +91 98765 43210: *Loss cut* @1.74 NZ\n"
             "[11/1, 2:03 PM] null: Backed Sydney 2.08\n\n50% stake\n"
             "[11/1, 2:04 PM] null: Back another 50% @4")
    assert fresh_part_of_paste(block, now, within) == "Backed Sydney 2.08\n\n50% stake\nBack another 50% @4"
    assert fresh_part_of_paste("[11/1, 1:07 PM] null: Loss cut", now, within) == ""
    assert fresh_part_of_paste("Backed Sydney 2.08", now, within) == "Backed Sydney 2.08"  # one message has no stamp
    assert fresh_part_of_paste("[11/1, 12:02 PM] null: x", datetime(2026, 11, 1, 12, 3), within) == "x"  # noon is PM


def test_a_block_of_old_posts_is_kept_but_never_traded(store):
    old = "[10/29, 9:56 AM] null: Back x"
    now = datetime.now()
    new = f"[{now.month}/{now.day}, {now.hour % 12 or 12}:{now.minute:02d} {'PM' if now.hour >= 12 else 'AM'}] null: Back x"
    pipe, site, _ = make(store, Profile("wa", pasted=True), {"Back x": [signal(odds=3.0)]}, mode="dry_run")
    asyncio.run(pipe._handle(msg(old)))
    assert site.calls == []
    assert store._db.execute("SELECT summary FROM messages").fetchone()[0].startswith("pasted history")
    asyncio.run(pipe._handle(msg(new)))
    assert site.calls == [("entry", "Florian Broska", None)]


def test_fav_bookset_books_the_channels_trade(store):
    """SS 1 Nov: 'Fav bookset' looked for a team called Fav and failed, leaving the trade open 48 min more."""
    heat = dict(selection="Brisbane Heat W", event="Brisbane Heat W v ?", sport="cricket")
    script = {"3.3 odds back bh": [signal(odds=3.3, units=0.5, **heat)],
              "Fav bookset": [signal(action="close", odds=None, **heat)]}
    pipe, site, _ = make(store, Profile("ss", rules="ss"), script, live=3.3)

    async def go():
        for text in script:
            await pipe._handle(msg(text))

    asyncio.run(go())
    assert site.calls[-1] == ("close", "Brisbane Heat W", None)
    assert store.find_position("Brisbane Heat W", "live") is None


def test_a_low_confidence_add_adds_nothing(store):
    """SS 1 Nov: '3.3 odds back bh', 'Add 0.5 again. Limit', then '3.7' alone, read as one more add at confidence 0.40."""
    heat = dict(selection="Brisbane Heat W", event="Brisbane Heat W v ?", sport="cricket")
    script = {"3.3 odds back bh": [signal(odds=3.3, units=0.5, **heat)],
              "3.7": [signal(action="add", odds=3.7, confidence=0.4, **heat)]}
    pipe, site, _ = make(store, Profile("ss"), script, live=3.3)

    async def go():
        for text in script:
            await pipe._handle(msg(text))

    asyncio.run(go())
    assert len(site.calls) == 1 and store.find_position("Brisbane Heat W", "live").stake == 500


def test_book_set_compulsory_in_an_entry_is_no_close(store):
    """Tennis Expert 1 Nov: 'Leylah WIN KRO / BOOK SET COMPALSARY' read as both: the trade was booked 5 s after it opened."""
    leylah = dict(selection="Leylah Fernandez", event="Leylah Fernandez v ?")
    script = {"Leylah WIN KRO": [signal(odds=None, **leylah), signal(action="close", odds=None, **leylah)]}
    pipe, site, _ = make(store, Profile("tk"), script, live=6.6)
    asyncio.run(pipe._handle(msg("Leylah WIN KRO")))
    assert site.calls == [("entry", "Leylah Fernandez", None)]


def test_on_paper_each_channels_trades_are_its_own(store):
    """Paper: SK Astro's Sri Lanka trade must not block Kartik's Pakistan tip in the same match; live it does
    (the site holds one position per match)."""
    for mode in ("dry_run", "live"):
        store.open_position(signal(selection="Sri Lanka", event="Pakistan v Sri Lanka", sport="cricket",
                                   channel="SK Astro"), mode, 2.78, 1000)
        tip = signal(selection="Pakistan", event="Pakistan v Sri Lanka", sport="cricket", channel="Kartik",
                     odds=1.55)
        decision = RiskManager(make_settings(), store, mode).precheck(tip)
        assert decision.ok == (mode == "dry_run"), decision.reason


def test_first_names_and_initials_find_the_player():
    """Tennis Tips King 28 Oct 'Tallon v Brandon'; Raftaar 'NAKASHIMA B'."""
    from tradebot.exchange_bot import _first_name_of
    listed = ["Tallon Griekspoor v Brandon Nakashima", "Le Tien v Alex de Minaur"]
    assert _best_index(listed, "Tallon v Brandon", "Tallon") == 0
    assert _first_name_of("Brandon", "Brandon Nakashima") and not _first_name_of("Nakashima", "Brandon Nakashima")


def test_messages_from_other_chats_are_ignored(store):
    pipe, site, _ = make(store, Profile("tip"), {"Back x": [signal(odds=3.0)]})
    asyncio.run(pipe._handle(msg("Back x", chat=-999)))
    assert pipe._parser.calls == 0 and site.calls == []
