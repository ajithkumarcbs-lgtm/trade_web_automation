"""Auto-bookset at a profit target, Telegram alerts and the Groq reader."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from conftest import make_settings, signal
from main import Pipeline
from tradebot.exchange_bot import BetResult
from tradebot.risk import RiskManager, booked_profit
from tradebot.signal_parser import SignalParser
from tradebot.store import Position


def position(price=2.9, stake=1000.0, side="back"):
    return Position(1, "2026-10-09T12:00:00", "tennis", "Broska v Neumayer", "match_odds", "Broska", side,
                    price, stake, None, None)


@pytest.mark.parametrize("lay_price,profit", [(1.5, 933.33), (1.4, 1071.43), (2.9, 0.0), (4.0, -275.0)])
def test_booked_profit(lay_price, profit):
    assert booked_profit(position(), lay_price) == pytest.approx(profit, abs=0.01)


class PricedSite:
    def __init__(self, price):
        self.price = price
        self.busy = False
        self.calls = []

    async def exit_price(self, pos):
        return self.price

    async def place(self, sig, pos, require_green=False):
        self.calls.append(sig.action)
        return BetResult("placed", "cashed out")


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, text, image=None):
        self.sent.append(text)


def run_monitor(pipe, seconds=0.3):
    async def go():
        try:
            await asyncio.wait_for(pipe.monitor(), seconds)
        except asyncio.TimeoutError:
            pass
    asyncio.run(go())


def monitored_pipeline(store, price):
    settings = make_settings(dry_run=False, auto_bookset_units=1, monitor_interval_s=0, unit_stake=1000)
    store.open_position(signal(selection="Broska"), "live", 2.9, 1000)
    site, notifier = PricedSite(price), FakeNotifier()
    pipe = Pipeline(settings, "live", site, RiskManager(settings, store, "live"), store, notifier)
    return pipe, site, notifier


def test_auto_bookset_books_once_target_reached(store):
    pipe, site, notifier = monitored_pipeline(store, price=1.4)  # locks +1071 >= 1 unit (1000)
    run_monitor(pipe)
    assert site.calls == ["close"]  # once only: the trade is closed afterwards
    assert store.open_positions("live") == []
    assert "auto-bookset" in store.recent_bets()[0]["reason"]
    assert any("BOOKSET" in text for text in notifier.sent)


def test_auto_bookset_waits_below_target(store):
    pipe, site, _ = monitored_pipeline(store, price=1.5)  # locks +933 < 1000
    run_monitor(pipe)
    assert site.calls == [] and len(store.open_positions("live")) == 1


def test_alert_sent_for_each_decision(store):
    settings = make_settings()
    notifier = FakeNotifier()
    pipe = Pipeline(settings, "signals_only", None, RiskManager(settings, store, "signals_only"), store, notifier)

    class Parser:
        async def parse(self, text, image=None, context=None, reply_to=""):
            from tradebot.models import ParsedMessage
            return ParsedMessage(signals=[signal(), signal(action="add")], summary=text)

    pipe._parser = Parser()
    from datetime import datetime, timezone
    from tradebot.telegram_listener import IncomingMessage
    asyncio.run(pipe._handle(IncomingMessage(1, datetime.now(timezone.utc), "Broska back 2.9", None)))
    assert len(notifier.sent) == 2
    assert "ENTRY SIMULATED" in notifier.sent[0] and "SKIPPED" in notifier.sent[1]


def test_trade_summary_after_bookset(store):
    pipe, site, notifier = monitored_pipeline(store, price=1.4)
    run_monitor(pipe)
    summary = [t for t in notifier.sent if "TRADE SUMMARY" in t]
    assert len(summary) == 1 and "Broska" in summary[0] and "2.9" in summary[0]


class ListingSite(PricedSite):
    def __init__(self, listings):
        super().__init__(price=2.0)
        self.listings = iter(listings)

    async def listed_matches(self):
        return next(self.listings)


def test_match_finished_needs_two_misses_in_a_row(store):
    settings = make_settings(dry_run=False)
    store.open_position(signal(selection="Broska", event="Florian Broska v Lukas Neumayer"), "live", 2.9, 10)
    site = ListingSite([["Other v Match"], ["Fl Broska v Neumayer"], ["Other v Match"], ["Other v Match"]])
    notifier = FakeNotifier()
    pipe = Pipeline(settings, "live", site, RiskManager(settings, store, "live"), store, notifier)

    async def checks(n):
        for _ in range(n):
            await pipe.check_finished_matches()

    asyncio.run(checks(2))  # missing once, then listed again: still open
    assert len(store.unclosed_positions("live")) == 1 and not notifier.sent
    asyncio.run(checks(2))  # missing twice in a row: finished
    assert store.unclosed_positions("live") == []
    assert len(notifier.sent) == 1 and "MATCH FINISHED" in notifier.sent[0]


def test_reply_context_reaches_the_ai(store):
    settings = make_settings()
    pipe = Pipeline(settings, "signals_only", None, RiskManager(settings, store, "signals_only"), store)
    seen = {}

    class Parser:
        async def parse(self, text, image=None, context=None, reply_to=""):
            from tradebot.models import ParsedMessage
            seen["reply_to"] = reply_to
            return ParsedMessage(signals=[], summary="")

    pipe._parser = Parser()
    from datetime import datetime, timezone
    from tradebot.telegram_listener import IncomingMessage
    msg = IncomingMessage(1, datetime.now(timezone.utc), "Boookset guys", None, reply_to="[19:31] Back djokovic 2.56")
    asyncio.run(pipe._handle(msg))
    assert seen["reply_to"] == "[19:31] Back djokovic 2.56"


def groq_reply(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                           usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


def test_groq_reader_fills_missing_fields():
    parser = SignalParser("", "unused", groq_key="test-key")
    reply = {"summary": "entry", "signals": [{"action": "entry", "selection": "Wi", "side": "back", "odds": 6,
                                              "confidence": 0.9}]}

    async def create(**kwargs):
        assert kwargs["response_format"] == {"type": "json_object"}
        return groq_reply(json.dumps(reply))

    parser._groq.chat.completions.create = create
    parsed = asyncio.run(parser.parse("Wi back 6"))
    sig = parsed.signals[0]
    assert (sig.selection, sig.market, sig.odds, sig.amount) == ("West Indies", "match_odds", 6, None)


def test_groq_reader_retries_unreadable_reply():
    parser = SignalParser("", "unused", groq_key="test-key")
    replies = iter(["not json", json.dumps({"summary": "chat", "signals": []})])

    async def create(**kwargs):
        return groq_reply(next(replies))

    parser._groq.chat.completions.create = create
    assert asyncio.run(parser.parse("hello")).summary == "chat"


def test_groq_reads_the_screenshot_too():
    parser = SignalParser("", "unused", groq_key="test-key")
    seen = {}

    async def create(**kwargs):
        seen["content"] = kwargs["messages"][1]["content"]
        return groq_reply(json.dumps({"summary": "chat", "signals": []}))

    parser._groq.chat.completions.create = create
    asyncio.run(parser.parse("Back him", b"\xff\xd8 jpeg bytes"))
    assert seen["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_groq_wait_times_are_read():
    from tradebot.signal_parser import _retry_after
    assert _retry_after("Limit 8000, Used 7900, Requested 3100. Please try again in 23.5s. Need more") == 23.5
    assert _retry_after("Please try again in 20m16.512s.") == 20 * 60 + 16.512
    assert _retry_after("Please try again in 830ms.") == 0.83
    assert _retry_after("no hint") is None


def test_groq_is_sent_the_short_format_not_the_schema():
    parser = SignalParser("", "unused", groq_key="test-key")
    seen = {}

    async def create(**kwargs):
        seen["system"] = kwargs["messages"][0]["content"]
        return groq_reply(json.dumps({"summary": "chat", "signals": []}))

    parser._groq.chat.completions.create = create
    asyncio.run(parser.parse("hello"))
    assert "Reply with one JSON object only" in seen["system"] and '"properties"' not in seen["system"]


def test_missing_confidence_counts_as_unreadable():
    parser = SignalParser("", "unused", groq_key="test-key")

    async def create(**kwargs):
        return groq_reply(json.dumps({"signals": [{"action": "entry", "selection": "Broska", "side": "back"}]}))

    parser._groq.chat.completions.create = create
    assert asyncio.run(parser.parse("Broska")).signals[0].confidence == 0.0


def test_a_null_side_is_filled_in_not_read_again():
    """A voice close naming no team came back with side and selection null: 8 readings of one note, and the free tier."""
    parser = SignalParser("", "unused", groq_key="test-key")
    calls = []

    async def create(**kwargs):
        calls.append(1)
        return groq_reply(json.dumps({"summary": "close", "signals": [
            {"action": "close", "selection": None, "side": None, "odds": None, "confidence": 0.9}]}))

    parser._groq.chat.completions.create = create
    sig = asyncio.run(parser.parse("(voice note) bookset")).signals[0]
    assert (sig.action, sig.selection, sig.side) == ("close", "", "back") and len(calls) == 1


def test_the_ai_picks_the_listed_side_a_first_name_means():
    parser = SignalParser("", "unused", groq_key="test-key")
    listed = ["Musetti v Tiafoe", "T Griekspoor v Nakashima"]
    replies = {"Tallon": [{"full_name": "Tallon Griekspoor", "index": 1, "side": "Griekspoor"}],
               "Frances": [{"full_name": "", "index": None, "side": ""},  # the first model doesn't know him
                           {"full_name": "Frances Tiafoe", "index": "0", "side": "Tiafoe"}],
               "Zizou": [{"index": 0, "side": "Bergs"}, {"index": 7, "side": "Bergs"}, {}]}  # never a listed side
    asked = []

    async def create(**kwargs):
        prompt = kwargs["messages"][1]["content"]
        assert "1. T Griekspoor v Nakashima" in prompt
        name = prompt.split(":")[1].split("(")[0].strip()
        asked.append(name)
        return groq_reply(json.dumps(replies[name].pop(0)))

    parser._groq.chat.completions.create = create
    assert asyncio.run(parser.resolve_name("Tallon", "Tallon v Brandon", listed)) == "T Griekspoor"  # as listed
    assert asyncio.run(parser.resolve_name("Frances", "Frances v ?", listed)) == "Tiafoe"  # the next model knew
    assert asyncio.run(parser.resolve_name("Zizou", "Zizou v ?", listed)) is None
    assert asked == ["Tallon", "Frances", "Frances", "Zizou", "Zizou", "Zizou"]


def test_the_ai_answer_on_a_womens_match_is_not_taken_for_the_side_listed_first():
    """31 Oct: SS '3.1 odds star back': the AI said Melbourne Stars W, the bot backed Adelaide Strikers W."""
    from tradebot.signal_parser import _listed_side
    listed = ["Adelaide Strikers W v Melbourne Stars W"]
    assert _listed_side({"index": 0, "side": "Melbourne Stars W"}, listed) == "Melbourne Stars W"
    assert _listed_side({"index": 0, "side": "Melbourne Stars"}, listed) == "Melbourne Stars W"
    assert _listed_side({"index": 0, "side": "Women"}, listed) is None
    assert _listed_side({"index": 0, "side": "W"}, listed) is None


def test_a_first_name_the_site_doesnt_list_is_found_by_the_ai(store):
    """28 Oct: 'Tallon PLUS KARO' and 'Quentin Win' were missed: the site lists 'T Griekspoor v Nakashima'. The
    AI's pick must fit the initial the site shows, and is kept for the retries (every 10 s)."""
    from tradebot.exchange_bot import ExchangeBot, _initials_fit
    settings = make_settings()
    bot = ExchangeBot(settings, RiskManager(settings, store, "dry_run"))
    listed = ["Musetti v Tiafoe", "T Griekspoor v Nakashima", "Ha Shi v D Yastremska"]
    asked = []

    async def resolve(name, event, names):
        asked.append(name)
        return {"Tallon": "T Griekspoor", "Dayana": "Ha Shi"}.get(name)

    bot.resolve_name = resolve
    assert asyncio.run(bot._found_by_ai(listed, "Tallon v Brandon", ["Tallon"])) == {"T Griekspoor": 1}
    assert asyncio.run(bot._found_by_ai(listed, "Tallon v ?", ["Tallon"])) == {"T Griekspoor": 1}
    assert asked == ["Tallon"]  # kept: the retries don't ask again
    assert asyncio.run(bot._found_by_ai(listed, "Dayana v ?", ["Dayana"])) == {}  # Dayana doesn't start with 'Ha'
    assert asyncio.run(bot._found_by_ai(listed, "Diane v ?", ["Diane"])) == {}  # the AI can't tell
    assert asyncio.run(bot._found_by_ai(listed, "Diane v ?", ["Diane"])) == {} and asked.count("Diane") == 1
    assert asyncio.run(bot._found_by_ai(listed, "Rafael Nadal v ?", ["Rafael Nadal"])) == {}  # full names: not asked
    assert _initials_fit("pkr", "Pokhara Avengers") and _initials_fit("han", "Ha Shi")


def test_womens_teams_are_found_by_their_own_name():
    """31 Oct: SK Astro 'Adelaide Strikers Women' wasn't found on the site's 'Adelaide Strikers W v Melbourne Stars W'."""
    from tradebot.names import contains_player
    match = "Adelaide Strikers W v Melbourne Stars W"
    assert contains_player("Adelaide Strikers Women", match) and contains_player("Stars W", match)
    assert not contains_player("Hobart Hurricanes W", match)  # 'W' alone says nothing
    assert not contains_player("India W", "India v Australia")  # not the men's match
    assert contains_player("Melbourne Stars", match)


def test_khao_means_lay_is_told_to_the_ai():
    """'COLOMBO Favourite Par Khao, JAFFNA Plus Karna Hai': Kartik's calls were being backed, the opposite."""
    from pathlib import Path
    from tradebot.signal_parser import INSTRUCTIONS
    assert '"Khao"' in INSTRUCTIONS and "LAY that team" in INSTRUCTIONS
    path = Path(__file__).resolve().parent.parent / "channels.example.json"
    kartik = json.loads(path.read_text(encoding="utf-8"))["profiles"]["kartik"]["hint"]
    assert "side lay" in kartik and "= back that team" not in kartik


def test_a_suspended_tennis_market_is_retried_longer(store):
    settings = make_settings()
    pipe = Pipeline(settings, "dry_run", None, RiskManager(settings, store, "dry_run"), store)
    assert pipe._retry_minutes(signal(), "market suspended") == 10
    assert pipe._retry_minutes(signal(), "match not found on site: Tallon v ?") == 5
    assert pipe._retry_minutes(signal(sport="cricket"), "market suspended") == 5
