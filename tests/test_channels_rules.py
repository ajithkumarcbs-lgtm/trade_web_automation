"""channels.json, rule-checked reading, stops, top-ups, waiting entries, confirmations and Saved Messages commands."""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from conftest import make_settings, signal
from main import Pipeline, TradeLock
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


def test_a_stake_in_rupees_completes_the_held_entry(store):
    script = {"8 odds back war": [signal(odds=8.0, **dict(KG, selection="Warriors", event="Warriors v ?"))],
              "Try 200 or 300": [signal(action="stake", odds=None, amount=200, selection="Warriors")]}
    pipe, site, _ = make(store, Profile("ss", rules="ss", stake_wait_s=5), script, live=8.0)

    async def go():
        await pipe._handle(msg("8 odds back war"))
        await pipe._handle(msg("Try 200 or 300"))

    asyncio.run(go())
    assert site.calls == [("entry", "Warriors", 200)]


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


def test_messages_from_other_chats_are_ignored(store):
    pipe, site, _ = make(store, Profile("tip"), {"Back x": [signal(odds=3.0)]})
    asyncio.run(pipe._handle(msg("Back x", chat=-999)))
    assert pipe._parser.calls == 0 and site.calls == []
