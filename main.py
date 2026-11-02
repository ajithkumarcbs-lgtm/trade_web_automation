"""Telegram tips -> rules + AI read them -> safety checks -> bet on the exchange.

    python main.py --signals-only   # no browser: read, parse and log what it WOULD bet
    python main.py                  # full pipeline; DRY_RUN in .env decides whether Place Bet is clicked

Which chats are followed and how each is traded: channels.json (see channels.example.json). Without it, the
single TELEGRAM_CHAT from .env is followed as before. Commands go to your Telegram Saved Messages: /help.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import heapq
import itertools
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from typing import Awaitable

from openai import APIConnectionError, RateLimitError

from tradebot.channels import MANUAL, Channel, Channels, apply_aliases
from tradebot.config import DATA_DIR, SITE_FIELDS, TELEGRAM_FIELDS, Settings, load_settings
from tradebot.exchange_bot import BetResult, ExchangeBot, TeamQuote, _best_index
from tradebot.fancy import (PLAYER_KINDS, Cricbuzz, Match, Session, coming_match, finished_match, read_session, same_batter,
                            team_fits)
from tradebot.fancy import settle as settle_session
from tradebot.ladder import (BOOKSET, EQUAL_EXIT, FELL_BACK, GIVEBACK, STEPS, WAIT_FAVOURITE, Ladder, LadderRules, exit_call,
                             jackpot_days_in, parse_day, read_sk_tip)
from tradebot.leagues import leagues_of, sport_of
from tradebot.models import BetSignal, ParsedMessage, as_bet
from tradebot.notifier import (Notifier, bet_alert, confirm_request, day_report, match_finished, results_report,
                               trade_summary)
from tradebot.risk import Decision, RiskManager, booked_profit, other_runners_price
from tradebot.names import contains_player, same_name
from tradebot.rules import fresh_part_of_paste, name_words, not_yet, parse_known, parse_strict, reconcile
from tradebot.skyfair import is_skyfair
from tradebot.signal_parser import CONTEXT_MESSAGES, CONTEXT_WINDOW, VOICE_HINT, context_line, expand_team, make_parser
from tradebot import store as trade_log
from tradebot.store import COUNTED, Position, Store, period_start
from tradebot.telegram_listener import IncomingMessage, listen
from tradebot.voice import Transcriber

log = logging.getLogger("tradebot")


MIN_LIMIT = 10  # /limit: the site's Min Bet, on every market seen so far
MAX_LIMIT = 10_000  # a slip of the finger must not make 1 limit a lakh
BOOKSET_RETRY_SECONDS = 10  # how often a refused bookset / loss cut is tried again
TENNIS_SUSPENDED_MINUTES = 10  # a tennis entry whose market is suspended is retried this long (not ENTRY_RETRY_MINUTES)
HELD_ENTRY_MINUTES = 40  # an entry held for its match is tried this long from its time (10 min before to 30 after the start)
# A waiting entry allowed longer than this can wait for days (CricStatz's Test orders): its price is read SLOW_WAIT_EVERY,
# not every 10 s, which would keep the browser busy for all that time.
SLOW_WAIT_ABOVE_MIN = 180
SLOW_WAIT_EVERY = timedelta(seconds=60)
LONG_HOLD_POLL = timedelta(minutes=10)  # how often the results of trades held for days are looked for
GREEN_MONEY_WITHIN = timedelta(hours=6)  # "green 20%" takes 20% of the channel's last bookset in this time
GROUP_LINK = re.compile(r"(?:https?://)?t\.me/(?:\+|joinchat/)[\w-]+", re.IGNORECASE)
SMALL_STAKE = re.compile(r"\b(?:small|chinna)\b", re.IGNORECASE)  # "try small amount"
NOT_ENTERED = re.compile(r"\bentry\s+(?:nahi|nhi|nahin)\b", re.IGNORECASE)  # "50p h jiska entry nahi hua hai kar lo"
AGAIN = re.compile(r"\bagain\b", re.IGNORECASE)  # "Again bookset": the trade entered again, not the newest one
# Words (and prices) without which a text can't be an instruction of any channel followed: entries, stakes, closes
AI_WORDS = re.compile(
    r"\b(back|lay|khao+|lagao|lagaa|win|winner|entry|entries|entr|limit|limits|unit|units|odds|paise|\d+\s*p|"
    r"book\W*set|bookset|cash\W*out|cashout|loss\W*cut|losscut|exit|skip|even|fav|favourite|favorite|take|taken|try|"
    r"add|extra|half|double|single|green|small|stake|rs|cancel|plus|khelo|karo|kro|now)\b|\d\.\d|₹",
    re.IGNORECASE)
# A voice note a channel acts on goes to the AI only with one of these (about 2 in 5 of SS's), in English or Tamil letters
VOICE_WORDS = re.compile(r"book\W*set|box\W*set|loss\W*cut|last\W*cut|cash\W*out|\bback\b|\bentry\b|\blimit\b|"
                         r"புக்|லாஸ்|பேக்|பாக்க்|லிமிட்|என்ட்ரி", re.IGNORECASE)
VOICE_MIN_CONFIDENCE = 0.85  # talk is looser than a typed tip
# Only "book it now" acts from a voice note. On 16 of SS's old notes the AI alone gave 5 false closes, all of these:
# a price or over still to come ("1.8 range-la bookset", "35-th over-la bookset") or "should / could" ("பண்ணனும்", "பண்ணலாம்").
VOICE_CLOSE_NOW = re.compile(r"பண்ணிடுங்க|பணிடுங்க|பண்ணிக்கோங்க|பண்ணிகோங்க|பணிக்கோங்க|பணிகோங்க|பண்ணுங்க|"
                             r"pann?unga|pann?ik+on?ga|pann?idunga", re.IGNORECASE)
VOICE_NOT_NOW = re.compile(r"\d\.\d|ரேஞ்ச்|\brange\b|ஓவர்|\bovers?\b|வந்தா|அப்படின்னா|பண்ணனும்|பண்ணியனும்|பண்ணிக்கலாம்|"
                           r"பண்ணலாம்|வரும்|ரெடி|\bready\b|\bwait\b|வெய்ட்", re.IGNORECASE)
HELP = """🤖 *Commands* (send them here, in Saved Messages)
/status: bot state, open trades, today's result
/today, /yesterday: each channel's trades of the day, won / lost, missed tips, deleted messages
/report [day|week|month|year]: limits per channel, cricket and tennis, league by league;
/report 2026-10, /report 2026-10-29: the same for a past month or day
/settle <trade id> win|lose|<amount>: result of a trade that ran to the end
/limit: what 1 limit is; /limit 100, /limit 1000: its rupees from now on (open trades keep theirs)
/stop SK, /start SK: stop / allow one channel's new entries (its open trades and booksets go on); /stop: which are stopped
/pause, /resume: stop / allow new entries of all channels (bookings always work); /stop all, /start all: the same
/yes <n>, /no <n>: answer a CONFIRM question
/channels: chats followed; /follow <chat id> <channel>; /unfollow <chat id>
Trade by hand: /back Shelton 3.25 1u, /lay Sinner 1.55 0.5u, /back at Berrettini 3 1u,
/exit Shelton 1.6, /bookset Shelton, /losscut Shelton, /cancel Berrettini
SK method: /sk shows each tip's state, odds, entries, P&L, bookset and stop; /sk UAE Toss or Match Win, /sk Sri Lanka Even
or Win: a tip you got elsewhere, treated as SK's post, and it decides when SK and your channel disagree; /bookset UAE books
it, /cancel UAE stops its entries
SK jackpot days: /jackpot shows them and SS's result on them; /jackpot 5 Nov, /jackpot today, /jackpot off 5 Nov"""


def _clock() -> str:
    return datetime.now().strftime("%d %b %H:%M:%S")


def _date_range(text: str) -> tuple[datetime, datetime] | None:
    """[start, end) of '2026', '2026-10' or '2026-10-29'. None if it isn't a date like these."""
    found = REPORT_DATE.fullmatch(text)
    if not found:
        return None
    year, month, day = found.groups()
    start = datetime(int(year), int(month or 1), int(day or 1))
    if day:
        return start, start + timedelta(days=1)
    return start, (start + timedelta(days=32)).replace(day=1) if month else start.replace(year=start.year + 1)


def _fancy_result(tip: dict, match) -> tuple[bool | None, str]:
    """(won? None = not known yet, what happened) of an open fancy tip on its Cricbuzz match."""
    session = Session(tip["kind"], tip["number"], tip["team"])
    return settle_session(session, tip["side"], tip["line"], match, datetime.fromisoformat(tip["created_at"]))


def _site_pl(tip: dict, match) -> float | None:
    """The tip's result in units had it been taken at the line and rate the site showed when it came."""
    if tip.get("site_line") is None:
        return None
    won, _ = _fancy_result({**tip, "line": tip["site_line"]}, match)
    if won is None:
        return None
    return round(tip["units"] * tip["site_rate"] / 100, 4) if won else -tip["units"]


def _long_hold_pl(pos: Position, status: str) -> float | None:
    """A held trade's result from its match's final status ('England won by 3 runs', 'Match drawn'), None if unreadable."""
    if re.search(r"abandon|no result", status, re.IGNORECASE):
        return 0.0
    if re.search(r"\bdraw", status, re.IGNORECASE):
        won = bool(re.search(r"\bdraw\b", pos.selection, re.IGNORECASE))
    elif m := re.match(r"\s*(.+?)\s+won\b", status, re.IGNORECASE):
        won = team_fits(pos.selection, [m.group(1)])
    else:
        return None
    if pos.side == "lay":
        return round(-pos.liability if won else pos.stake, 2)
    return round(pos.stake * (pos.price - 1) if won else -pos.stake, 2)


def _session_name(kind: str, number: int) -> str:
    if kind == "wicket":
        return f"wicket {number}"
    if kind in PLAYER_KINDS:
        return "runs" if kind == "player_runs" else "balls"
    return f"{number} over runs" if number else "innings runs"


def _fancy_text(team: str, kind: str, number: int, line: float, side: str) -> str:
    return f"{team} toss" if kind == "toss" else f"{team} {_session_name(kind, number)} {line:g} {side.upper()}"


def _seconds_until(hhmm: str) -> float:
    """Seconds from now to the next HH:MM (local time)."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


SCREENSHOT_KEEP_DAYS = 14


def _remove_old_screenshots() -> None:
    """24/7: screenshots and saved pages older than SCREENSHOT_KEEP_DAYS are deleted at start-up."""
    cutoff = time.time() - SCREENSHOT_KEEP_DAYS * 86400
    for path in (DATA_DIR / "screenshots").glob("*.*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def _mentions(text: str, name: str, aliases: dict[str, str]) -> bool:
    """Does the message itself name this team / player: as written, misspelt ('beaz' = Baez) or by a short
    name ('kg', 'wi', 'war')?"""
    if not name.strip(" ?"):
        return True
    expanded = " ".join(expand_team(word) for word in apply_aliases(text, aliases).split())
    return contains_player(name, expanded) or any(same_name(word, name) for word in name_words(text))


SURE_WINNER, SURE_LOSER = 1.10, 11.0  # a runner's last price before its market closed: it had (nearly) won / lost
RE_ENTRY_PRICE_MOVE = 0.15  # an entry on a team already traded, at a price this far from the trade's: an add
RE_ENTRY_MIN_AGE = timedelta(minutes=10)  # ... if the trade is at least this old
TIPPED_LATELY = timedelta(minutes=60)  # an entry naming no team takes the team of an entry tip this recent
LIMIT_ALERT_EVERY = timedelta(minutes=15)  # messages missed through the AI's limit: one summary alert at most this often
FANCY_POLL = timedelta(minutes=2)  # Cricbuzz lists a match's latest ~9 overs: read often enough to see every over
FANCY_GIVE_UP = timedelta(hours=18)  # a fancy tip with no result by then is closed as 'unknown'
TOSS_AHEAD = timedelta(hours=16)  # a toss call can come this long before its match (Panther: 05:00 for a 10:10 start)
TOSS_RATE = 98  # the toss pays 98p for 1 (fancy rates are per 100)
BEFORE_THE_TOSS = timedelta(minutes=45)  # a match starting later than this has nothing on Cricbuzz to read yet
PLAYER_AHEAD = timedelta(hours=14)  # a player's line can come this long before the match ('pre match pick')
NO_PRICE_FINISHED = 15  # price checks in a row with no price (~5+ min) after a clear last price: the match is over
SK_CALL = re.compile(r"^\s*(?P<team>[A-Za-z][A-Za-z .]*?)\s+(?:toss\s+or\s+)?match\s+win\b", re.IGNORECASE | re.MULTILINE)
SK_CALL_WITHIN = timedelta(hours=3)  # 'Sydney Match Win ✅' this long before a Sydney match is a call; after one, a result
PASTE_FRESH = timedelta(minutes=3)  # a stamped line pasted from WhatsApp older than this is history, not a tip
LADDER_POLL = timedelta(seconds=20)  # how often an SK-method match's price is read, once it is on
LADDER_LONGEST = timedelta(hours=12)  # a match still on after this isn't watched: nor are its trades (POSITION_WINDOW)
LADDER_NOT_LISTED = timedelta(minutes=40)  # a tipped match the site still doesn't list this long after its start: missed
LADDER_GONE_READS = 15  # reads in a row (~5 min) without the match on the site, after it was there: it is over
SAME_MATCH_WITHIN = timedelta(hours=12)  # two channels' tips with watches this close, on these teams, are one match
LEAGUE_POLL = timedelta(minutes=30)  # how often trades with no league yet are looked up on Cricbuzz and ESPN
LEDGER_START = datetime(2026, 10, 26, 19)  # the ₹1000-a-limit test began here: older trades had other stakes and channels
PERIODS = {"day": "today", "week": "this week", "month": "this month", "year": "this year"}
REPORT_DATE = re.compile(r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?")  # /report 2026, /report 2026-10, /report 2026-10-29


def result_from_price(pos: Position, price: float | None) -> str:
    """'win' / 'lose' for a trade whose match ended without a booking, from the last price seen for its runner
    ('' when that price was not clear either way)."""
    if not price:
        return ""
    if price <= SURE_WINNER:
        runner_won = True
    elif price >= SURE_LOSER:
        runner_won = False
    else:
        return ""
    return "win" if runner_won == (pos.side == "back") else "lose"


def _close_signal(pos, loss_cut: bool) -> BetSignal:
    """A bookset / loss cut instruction for a recorded trade (auto-bookset and retries)."""
    return BetSignal(action="close", sport=pos.sport, event=pos.event, market=pos.market, market_name="",
                     selection=pos.selection, side=pos.side, odds=None, line=None, units=None, amount=None,
                     loss_cut=loss_cut, confidence=1.0, channel=getattr(pos, "channel", ""))


class TradeLock:
    """One trade action at a time (there is one browser), first come first served, except that bookings go
    first: a bookset, loss cut, exit or cancel waiting for its turn is served before the entries and price
    checks waiting with it, so it is not held up while another channel's tips go in. An action already
    running is never interrupted."""
    BOOKING, OTHER = 0, 1

    def __init__(self) -> None:
        self._busy = False
        self._waiting: list[tuple[int, int, asyncio.Future]] = []  # heap of (priority, arrival, waiter)
        self._arrivals = itertools.count()

    @contextlib.asynccontextmanager
    async def __call__(self, priority: int = OTHER):
        await self._acquire(priority)
        try:
            yield
        finally:
            self._release()

    async def _acquire(self, priority: int) -> None:
        if not self._busy:
            self._busy = True
            return
        waiter = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiting, (priority, next(self._arrivals), waiter))
        try:
            await waiter
        except asyncio.CancelledError:
            if waiter.done() and not waiter.cancelled():  # handed the lock just as this task was cancelled
                self._release()
            raise

    def _release(self) -> None:
        while self._waiting:
            *_, waiter = heapq.heappop(self._waiting)
            if not waiter.done():  # cancelled waiters are skipped
                waiter.set_result(None)  # the lock passes straight to it
                return
        self._busy = False


def _priority(sig: BetSignal) -> int:
    return TradeLock.BOOKING if sig.action in ("close", "exit", "cancel") else TradeLock.OTHER


@dataclass
class Asked:
    """A tip waiting for your /yes."""
    sig: BetSignal
    message_id: int | None
    asked_at: datetime
    why: str


@dataclass
class Held:
    """An entry without a stake, waiting a few seconds for its "Limit 2" / "Green 20%" message."""
    sig: BetSignal
    message_id: int | None
    how: str
    task: asyncio.Task | None = None


class Pipeline:
    def __init__(self, settings: Settings, mode: str, bot: ExchangeBot | None, risk: RiskManager, store: Store,
                 notifier: Notifier | None = None, channels: Channels | None = None):
        self._s = settings
        self._mode = mode
        self._bot = bot
        self._risk = risk
        self._store = store
        self._notifier = notifier
        self._channels = channels or Channels.single_chat(settings)
        self._parser = make_parser(settings)
        self._cricbuzz = Cricbuzz()
        self._last_look: dict[int, datetime] = {}  # pending entry -> when its price was last read (slow waits only)
        # One queue and reader per channel: channels are read side by side, each in the order its messages came.
        self._queues: dict[str, asyncio.Queue[asyncio.Future]] = {}
        self._readers: set[asyncio.Task] = set()
        self._history: dict[int, deque[tuple[datetime, str]]] = {}
        # Tips, retries and the auto-bookset check take turns (bookings first), so one trade can't be booked twice
        # and the browser does one thing at a time.
        self._trade_lock = TradeLock()
        self._missing: dict[int, int] = {}  # trade id -> times in a row its match was missing from the site
        self._last_price: dict[int, float] = {}  # trade id -> its closing price when last checked
        self._no_price: dict[int, int] = {}  # trade id -> price checks in a row that found no price
        self._ladder_gone: dict[int, int] = {}  # SK-method tip -> reads in a row that didn't find its match on the site
        self._ladder_blocked: dict[int, str] = {}  # SK-method tip -> why its entry is blocked, told once
        self._missed_by_limit = 0  # messages not read since the last "AI limit" alert
        self._last_limit_alert = datetime.min
        # What your commands set stays over a restart.
        self._paused = store.setting("paused") == "1"
        self._stopped = {name for name in json.loads(store.setting("stopped") or "[]") if self._channels.get(name)}
        self._channels.limit = float(store.setting("limit") or 0) or None
        self._asked: dict[int, Asked] = {}
        self._ask_number = 0
        self._held: dict[str, Held] = {}  # channel -> entry waiting for its stake message
        self._last_entry: dict[str, tuple[BetSignal, str]] = {}  # channel -> its latest entry and what became of it
        self._entries: dict[str, deque[tuple[BetSignal, str]]] = {}  # channel -> its recent entries, the same way

    # --- messages -------------------------------------------------------------------------------

    def _history_for(self, chat_id: int) -> deque[tuple[datetime, str]]:
        return self._history.setdefault(chat_id, deque(maxlen=CONTEXT_MESSAGES))

    def preload(self, messages: list[IncomingMessage]) -> None:
        for msg in messages:
            self._history_for(msg.chat_id).append((msg.date, context_line(msg.date, msg.text, msg.image is not None)))
        log.info("Loaded %d earlier messages as context", len(messages))

    async def enqueue(self, chat_id: int, message: Awaitable[IncomingMessage]) -> None:
        """A new message, handed over the moment it arrives while its photo / voice note is still being read.
        Each channel has its own reader, so a tip in one channel is read by the AI while the other channel's
        trade is on the site, and a channel's messages are handled in the order they came, even when a voice
        note takes longer to transcribe than the "Limit 2" after it. Trades still go one at a time."""
        channel = self._channels.for_chat(chat_id)
        key = channel.name if channel else str(chat_id)
        queue = self._queues.get(key)
        if queue is None:
            queue = self._queues[key] = asyncio.Queue()
            reader = asyncio.get_running_loop().create_task(self._reader(key, queue))
            self._readers.add(reader)
            reader.add_done_callback(self._readers.discard)
        queue.put_nowait(asyncio.ensure_future(message))  # starts downloading now, in parallel

    async def _reader(self, key: str, queue: asyncio.Queue[asyncio.Future]) -> None:
        while True:
            pending = await queue.get()
            try:
                await self._handle(await pending)
            except Exception:
                log.exception("Failed to handle a message of %s", key)

    async def _handle(self, msg: IncomingMessage) -> None:
        channel = self._channels.for_chat(msg.chat_id)
        if channel is None:
            return
        history = self._history_for(msg.chat_id)
        context = [line for date, line in history if msg.date - date <= CONTEXT_WINDOW]
        history.append((msg.date, context_line(msg.date, msg.text, msg.image is not None)))
        if channel.profile.watch_links:
            await self._report_links(msg, channel)
        if not channel.profile.trade or (self._mode == "live" and channel.profile.paper_only):
            return

        age = (datetime.now(timezone.utc) - msg.date).total_seconds()
        if age > self._risk.cfg(channel.name).signal_max_age_s:
            log.info("msg %s ignored: %.0fs old", msg.id, age)
            return
        if channel.profile.jackpot_source:
            await self._note_jackpot_days(msg.text, channel)
        if channel.profile.pasted:
            text = fresh_part_of_paste(msg.text, datetime.now(), PASTE_FRESH)
            if not text and msg.image is None:  # kept for the audit: a block of old posts must not trade at today's prices
                summary = "pasted history: nothing new in it, not traded"
                self._store.log_message(msg.id, msg.text, False, ParsedMessage(signals=[], summary=summary), channel.name)
                log.info("msg %s [%s]: %s", msg.id, channel.name, summary)
                return
            msg = replace(msg, text=text)
        if channel.name in self._stopped and not any(self._trade_is_for(p, channel) for p in self._store.unclosed_positions(self._mode)):
            # /stop, and no trade its posts could book: the AI's free limit isn't spent on it
            self._store.log_message(msg.id, msg.text, msg.image is not None,
                                    ParsedMessage(signals=[], summary="channel stopped, not read"), channel.name)
            log.info("msg %s [%s]: channel stopped, not read", msg.id, channel.name)
            return

        signals, how, parsed = await self._read(msg, channel, context)
        if parsed is None:
            if not channel.profile.ladder:
                return
            parsed = ParsedMessage(signals=[], summary="not read by the AI")  # its free limit: the SK check below needs no AI
        if channel.profile.ladder and not signals:
            signals = await self._call_before_match(msg.text, channel)
        if msg.image and parsed.summary:  # later tips can then refer to what the screenshot showed
            history[-1] = (msg.date, context_line(msg.date, f"{msg.text} [screenshot: {parsed.summary}]", False))
        message_id = self._store.log_message(msg.id, msg.text, msg.image is not None, parsed, channel.name)
        log.info("msg %s [%s]: %s (%d signal(s), %s)", msg.id, channel.name, parsed.summary, len(signals), how)

        for sig in signals:
            await self._act(sig, msg.text, channel, how, message_id)

    async def _read(self, msg: IncomingMessage, channel: Channel,
                    context: list[str]) -> tuple[list[BetSignal], str, ParsedMessage | None]:
        """The message's instructions and how they were checked: strict, agree, disagree, rule-only, ai-only.
        A channel with a rule set has its usual formats read by the rules too, and compared with the AI."""
        profile = channel.profile
        if profile.strict_only:
            found = parse_strict(msg.text) or []
            summary = f"strict command: {msg.text.strip()[:80]}" if found else "not a strict command"
            return [as_bet(s, channel.name) for s in found], "strict" if found else "none", \
                ParsedMessage(signals=found, summary=summary)
        if msg.forwarded and not profile.act_on_forwards:  # e.g. a member's "book set maar diya" testimonial
            return [], "forward", ParsedMessage(signals=[], summary="forwarded message (context only)")
        if profile.rules_only:
            found = parse_known(msg.text, profile.rules) or []
            summary = ", ".join(s.selection for s in found) or "not one of its formats"
            return [as_bet(s, channel.name) for s in found], "rule-only" if found else "none", \
                ParsedMessage(signals=found, summary=summary)
        if msg.voice and not profile.act_on_voice:
            # The transcript stays in the chat context, but a voice note alone is no instruction: transcribed
            # Tamil is too loose ("bookset-ku ready-aa irunga" = be ready for a bookset, read as "bookset").
            msg = replace(msg, text=msg.caption, voice=False)
            if not msg.text and msg.image is None:
                return [], "voice", ParsedMessage(signals=[], summary="voice note (context only)")
        elif msg.voice and not VOICE_WORDS.search(msg.text):
            return [], "voice", ParsedMessage(signals=[], summary="voice note without instruction words (not sent to "
                                                                  "the AI)")
        rule = None if msg.voice else parse_known(msg.text, profile.rules)
        if profile.ai_filter and not rule and not msg.voice and not (
                AI_WORDS.search(msg.text) or (profile.entry_words and re.search(profile.entry_words, msg.text, re.I))):
            # a text, or a photo's caption, with no instruction word; a bare photo where tips always come in words
            if msg.image is None:
                return [], "none", ParsedMessage(signals=[], summary="no instruction words (not sent to the AI)")
            if msg.text.strip() or not profile.bare_photos:
                return [], "none", ParsedMessage(signals=[], summary="photo without instruction words (not sent "
                                                                         "to the AI)")
        if rule:  # the channel's short names ("kg" -> Knights), so the rule's name compares with the AI's
            rule = [s.model_copy(update={"selection": expand_team(apply_aliases(s.selection, profile.aliases))})
                    for s in rule]
            if profile.rules_first:
                summary = ", ".join(f"{s.action} {s.selection}".strip() for s in rule)
                return self._screen([as_bet(s, channel.name) for s in rule], msg, channel, "rule-only"), "rule-only", \
                    ParsedMessage(signals=rule, summary=summary)
        parsed = await self._parse_while_fresh(msg, context, profile.hint + (VOICE_HINT if msg.voice else ""))
        if parsed is None:
            return [], "missed", None
        signals, how = reconcile(rule, parsed.signals)
        return self._screen([as_bet(s, channel.name) for s in signals], msg, channel, how), how, parsed

    def _screen(self, signals: list[BetSignal], msg: IncomingMessage, channel: Channel, how: str) -> list[BetSignal]:
        """The channel's checks on what was read, so a loose reading by the AI doesn't trade:
        - an entry only the AI found, in a text without a screenshot that has none of the channel's entry words
          ("Sl 1.2 coming", "1.66 fav ban" are price updates) or doesn't name the team it was read for;
        - a close in a message about a bookset still to come ("Ready for bookset", "wait for bookset");
        - a close naming a team that neither the message nor the one it replies to mentions (the AI took it from
          the context) acts on the channel's latest entry; when that entry was never placed, on the channel's open
          trade in its match, else on nothing. When the latest entry was placed and the AI's team is another open trade
          of the channel ("Again bookset" for the earlier trade), the AI's team is kept.
        A voice note acts only as a plain, confident "book it now" (VOICE_CLOSE_NOW); what else it was read as stays in
        the log, to judge later whether voice entries can be followed too."""
        profile = channel.profile
        kept = []
        for index, sig in enumerate(signals):
            if msg.voice and not (sig.action == "close" and sig.confidence >= VOICE_MIN_CONFIDENCE
                                  and VOICE_CLOSE_NOW.search(msg.text) and not VOICE_NOT_NOW.search(msg.text)):
                log.info("  voice note: no action on %s (%.2f): %r", sig.describe(), sig.confidence, msg.text[:100])
                continue
            if (sig.action == "entry" and how == "ai-only" and profile.entry_words and msg.image is None
                    and not re.search(profile.entry_words, msg.text, re.IGNORECASE)):
                log.info("  not an entry: %r has none of the entry words of %s", msg.text[:60], channel.name)
                continue
            if (sig.action == "entry" and how == "ai-only" and profile.entry_words and msg.image is None
                    and sig.market != "fancy"  # a session ('2nd wicket call') takes its batting team from the context
                    and not _mentions(msg.text, sig.selection, profile.aliases)
                    and not self._same_team_as_last_entry(sig, channel)
                    and not self._tipped_lately(sig, msg, channel)):
                # "Now 8.4 odds back" (team in the next message), "Esl 6 odds back" read as England; but
                # "10 odds green 5% back" right after a green-money India entry is India again, and
                # "18p se entry kro guys" after "Navone WIN KRO" is Navone
                log.info("  not an entry: %r doesn't name %s (the AI took it from the context)", msg.text[:60],
                         sig.selection)
                continue
            if sig.action == "close" and any(earlier.action == "entry" and same_name(earlier.selection, sig.selection)
                                             for earlier in signals[:index]):
                # 'Leylah WIN KRO / BOOK SET COMPALSARY': advice for later, in the entry itself
                log.info("  not a close: %r is the entry on %s", msg.text[:60], sig.selection)
                continue
            if sig.action == "close" and not msg.voice and not_yet(msg.text):
                log.info("  not a close yet: %r", msg.text[:60])
                continue
            if (sig.action == "close" and not _mentions(msg.text, sig.selection, profile.aliases)
                    and not _mentions(msg.reply_to, sig.selection, profile.aliases)):
                again = self._again_entry(channel) if AGAIN.search(msg.text) else None
                latest = again or self._last_entry.get(channel.name)
                if again is not None:  # "Again bookset": the team entered again after its first bookset
                    entry, status = again
                    if status not in (*COUNTED, "pending") and \
                            self._store.find_position(entry.selection, self._mode, channel.name, strict=True) is None:
                        log.info("  close: the re-entry of %s on %s was not placed, nothing to book",
                                 channel.name, entry.selection)
                        continue
                    sig = sig.model_copy(update={"selection": entry.selection, "event": entry.event, "aka": ""})
                elif latest is not None:
                    entry, status = latest
                    placed = status in (*COUNTED, "pending")
                    if placed and sig.selection and self._open_trade(sig, channel):
                        pass  # "Again bookset" for an earlier trade still open: the AI's team, not the newest entry
                    elif not placed and self._store.find_position(entry.selection, self._mode, channel.name,
                                                                  strict=True) is None:
                        # (a re-entry skipped while the first trade on its team is open leaves the close for that)
                        pos = self._trade_in_match_of(entry, channel)
                        if pos is None:
                            log.info("  close: the latest entry of %s (%s) was not placed, nothing to book",
                                     channel.name, entry.selection)
                            continue
                        # SS 25 Oct: 'Pakistan 1.5u' (a fancy bet, skipped) after England back, then 'Losscut must'
                        sig = sig.model_copy(update={"selection": pos.selection, "event": pos.event, "aka": ""})
                    else:
                        sig = sig.model_copy(update={"selection": entry.selection, "event": entry.event, "aka": ""})
            kept.append(sig)
        return kept

    async def _act(self, sig: BetSignal, text: str, channel: Channel, how: str, message_id: int | None) -> None:
        sig, problem = self._prepare(sig, text, channel)
        if problem:
            await self._record(message_id, sig, BetResult("skipped", problem), Decision(False, problem))
            return
        if sig.action in ("entry", "add", "stake") and (off := self._entries_off(channel.name)):
            await self._record(message_id, sig, BetResult("skipped", off), Decision(False, off))
            return
        if sig.action == "close" and not self._open_trade(sig, channel):
            held = self._trade_in_match_of(sig, channel)
            if held:  # 'BACKED INDIA 3.05', then 'Loss cut @1.74 NZ': the team named is where the price is
                sig = sig.model_copy(update={"selection": held.selection, "event": held.event, "aka": ""})
        if sig.action == "stake":
            await self._stake_message(sig, channel, message_id)
            return
        if sig.action == "entry" and sig.market == "fancy" and channel.profile.fancy_paper:
            await self._fancy_entry(sig, channel, message_id)
            return
        if (sig.action in ("entry", "add") and channel.profile.ladder and self._bot is not None
                and sig.market == "match_odds" and sig.side == "back"):
            await self._start_ladder(sig.model_copy(update={"action": "entry"}), text, channel, message_id)
            return
        if (sig.action in ("entry", "add") and channel.profile.jackpot_days_only and self._mode == "live"
                and not self._store.is_jackpot_day(datetime.now().date())):
            reason = f"not an SK jackpot day: {channel.name} goes live only on those days (/jackpot)"
            await self._record(message_id, sig, BetResult("skipped", reason), Decision(False, reason))
            return
        if sig.action in ("entry", "add") and NOT_ENTERED.search(text) and self._open_trade(sig, channel):
            reason = f"a call for those not in yet: {channel.name} already holds {sig.selection}"
            await self._record(message_id, sig, BetResult("skipped", reason), Decision(False, reason))
            return
        if sig.action == "add" or self._is_re_entry(sig, channel):
            await self._add(sig, channel, how, message_id)
            return
        if (sig.action == "entry" and channel.profile.enter_before_start_min and self._bot is not None
                and not sig.when_odds and await self._wait_for_start(sig, channel, message_id)):
            return
        if (sig.action == "entry" and how != "strict" and channel.profile.stake_wait_s and sig.units is None
                and sig.amount is None and not sig.green_pct and not sig.when_odds):
            await self._hold(sig, channel, how, message_id)
            return
        await self._go(sig, channel, how, message_id)

    async def _wait_for_start(self, sig: BetSignal, channel: Channel, message_id: int | None) -> bool:
        """A tip posted hours before its match (SK Astro, 31 Oct: 2-3 h early, tried for 5 min, missed) is entered just
        before the start, at the price then, and the price when tipped is noted to compare. False when there is
        nothing to wait for (the match is on, or Cricbuzz doesn't list it): the entry goes ahead as usual."""
        try:
            matches = await self._cricbuzz.refresh_list()
        except Exception:
            log.warning("  no match time for %s: Cricbuzz could not be read", sig.selection, exc_info=True)
            return False
        now = datetime.now()
        match = coming_match(matches, sig.selection, sig.event, now)
        at = match.start - timedelta(minutes=channel.profile.enter_before_start_min) if match else now
        if at <= now:
            return False
        async with self._trade_lock(_priority(sig)):
            check = self._risk.precheck(sig)
            if not check.ok:
                await self._record(message_id, sig, BetResult("skipped", check.reason), check)
                return True
            price = await self._bot.quote(sig)
            held = sig.model_copy(update={"not_before": at.isoformat(timespec="seconds"), "tip_price": price})
            reason = (f"waiting for {' v '.join(name for name, _ in match.teams)}: starts {match.start:%d %b %H:%M}, "
                      f"entry at {at:%H:%M}, price now {f'{price:g}' if price else 'not listed yet'}")
            self._store.add_pending(held, self._mode, message_id, reason)
            await self._record(message_id, held, BetResult("pending", reason, price), check)
        return True

    async def _go(self, sig: BetSignal, channel: Channel, how: str, message_id: int | None) -> BetResult | None:
        """Acts on the instruction, or asks you first when the channel's confirm settings say so. Paper trading
        runs unattended (unless CONFIRM_IN_DRY_RUN): it goes ahead and notes that live would have asked."""
        why = self._confirm_reason(sig, channel, how)
        if why and (self._mode == "live" or self._s.confirm_in_dry_run):
            await self._ask(sig, message_id, why)
            return None
        return await self.execute(sig, message_id, note=f"[live would ask: {why}]" if why else "")

    def _prepare(self, sig: BetSignal, text: str, channel: Channel) -> tuple[BetSignal, str]:
        """Channel conventions applied to an instruction: short names -> site names, 'try small amount',
        'green money 20%'. Returns the instruction and why it must be skipped ('' = go ahead)."""
        profile = channel.profile
        names = {"selection": expand_team(apply_aliases(sig.selection, profile.aliases))}
        if sig.sport == "tennis":  # 'NAKASHIMA B' (surname, initial): the site finds players by surname
            names["selection"] = re.sub(r"^(\S{3,})\s+[A-Za-z]\.?$", r"\1", names["selection"])
        names["event"] = " v ".join(expand_team(apply_aliases(side, profile.aliases)) for side in sig.event.split(" v "))
        sig = sig.model_copy(update=names)
        if profile.rupees_per_unit and sig.amount and not sig.green_pct:  # the tipster's rupees, not ours
            sig = sig.model_copy(update={"units": round(sig.amount / profile.rupees_per_unit, 2), "amount": None})
        if sig.action != "entry":
            return sig, ""
        most = self._risk.cfg(channel.name).max_units_per_bet
        if sig.units and most and sig.units > most:  # a big tip is still followed, at the most you allow
            log.info("  %s: %g units capped at MAX_UNITS_PER_BET %g", sig.selection, sig.units, most)
            sig = sig.model_copy(update={"units": most})
        if sig.green_pct:
            if profile.green_money != "stake":
                return sig, f"green-money re-entry ({sig.green_pct:g}% of the profit) is off for {channel.name}"
            profit = self._store.last_booked_profit(self._mode, channel.name, GREEN_MONEY_WITHIN)
            if not profit or profit <= 0:
                return sig, f"green-money re-entry: no booked profit in the last {GREEN_MONEY_WITHIN} to take " \
                            f"{sig.green_pct:g}% of"
            return sig.model_copy(update={"amount": round(profit * sig.green_pct / 100, 2), "units": None}), ""
        if sig.units is None and sig.amount is None and SMALL_STAKE.search(text):
            if profile.small_units <= 0:
                return sig, f"'try small amount' tips are off for {channel.name}"
            return sig.model_copy(update={"units": profile.small_units}), ""
        return sig, ""

    def _confirm_reason(self, sig: BetSignal, channel: Channel, how: str) -> str:
        """Why you are asked before this instruction ('' = act at once). Bookings are never held up."""
        rules = channel.profile.confirm
        if not rules or how == "strict" or sig.action not in ("entry", "exit"):
            return ""
        if rules.get("always"):
            return f"{channel.name} asks before every trade"
        if how in ("disagree", "rule-only") and rules.get("disagree", True):
            return "the rules and the AI read this message differently"
        if how == "ai-only" and sig.confidence < rules.get("below_confidence", 0):
            return f"the AI is only {sig.confidence:.0%} sure it read it right"
        if rules.get("above_units") and (sig.units or 1) > rules["above_units"]:
            return f"{sig.units:g} units is more than {rules['above_units']:g}"
        if rules.get("no_odds") and sig.action == "entry" and not (sig.odds or sig.when_odds):
            return "the tip gives no price: the bot would take whatever is on offer"
        return ""

    async def _ask(self, sig: BetSignal, message_id: int | None, why: str) -> None:
        self._ask_number += 1
        self._asked[self._ask_number] = Asked(sig, message_id, datetime.now(), why)
        log.info("  CONFIRM #%d asked: %s (%s)", self._ask_number, sig.describe(), why)
        if self._notifier:
            await self._notifier.send(confirm_request(self._ask_number, sig.describe(), why,
                                                      self._s.confirm_timeout_s))

    async def _expire_asked(self) -> None:
        for number, asked in list(self._asked.items()):
            if (datetime.now() - asked.asked_at).total_seconds() > self._s.confirm_timeout_s:
                del self._asked[number]
                reason = f"CONFIRM #{number} not answered within {self._s.confirm_timeout_s}s: skipped"
                await self._record(asked.message_id, asked.sig, BetResult("skipped", reason), Decision(False, reason))

    # --- stakes given in a second message ("Limit 2", "Green 20%") -------------------------------

    async def _hold(self, sig: BetSignal, channel: Channel, how: str, message_id: int | None) -> None:
        earlier = self._held.pop(channel.name, None)
        if earlier:  # a newer entry: the earlier one goes now with its default stake
            if earlier.task:
                earlier.task.cancel()
            await self._go(earlier.sig, channel, earlier.how, earlier.message_id)
        held = Held(sig, message_id, how)
        held.task = asyncio.get_running_loop().create_task(self._release(channel, held))
        self._held[channel.name] = held
        log.info("  holding %s up to %ds for its stake message", sig.describe(), channel.profile.stake_wait_s)

    async def _release(self, channel: Channel, held: Held) -> None:
        await asyncio.sleep(channel.profile.stake_wait_s)
        if self._held.get(channel.name) is held:
            del self._held[channel.name]
            await self._go(held.sig, channel, held.how, held.message_id)

    async def _stake_message(self, sig: BetSignal, channel: Channel, message_id: int | None) -> None:
        held = self._held.pop(channel.name, None)
        if held:
            if held.task:
                held.task.cancel()
            stake = {"amount": sig.amount, "units": None} if sig.amount else ({"units": sig.units} if sig.units else {})
            entry = held.sig.model_copy(update={**stake, **({"green_pct": sig.green_pct} if sig.green_pct else {})})
            entry, problem = self._prepare(entry, "", channel)
            if problem:
                await self._record(held.message_id, entry, BetResult("skipped", problem), Decision(False, problem))
                return
            await self._go(entry, channel, held.how, held.message_id)
            return
        await self._top_up(sig, channel, message_id)

    async def _top_up(self, sig: BetSignal, channel: Channel, message_id: int | None) -> None:
        """'Limit 2' soon after an entry placed with 1 unit: adds the difference at the price on offer now
        (if still close to the entry price), so the trade ends up with the tipster's stake."""
        def skip(reason: str):
            return self._record(message_id, sig, BetResult("skipped", reason), Decision(False, reason))

        window = channel.profile.limit_followup_s
        if not window:  # e.g. "One limit" in a channel whose entries already give their units: nothing to do
            log.info("  stake message ignored: %s has no top-ups (limit_followup_s 0)", channel.name)
            return None
        if sig.green_pct or not (sig.units or sig.amount):
            return await skip("a stake message after the entry can only raise its stake")
        pos = self._store.latest_position(self._mode, channel.name, timedelta(seconds=window))
        if pos is None:
            return await skip(f"no entry of {channel.name} in the last {window}s to top up")
        s = self._risk.cfg(pos)
        target = round(sig.amount if sig.amount else sig.units * s.unit_stake, 2)  # "Try 200 or 300": rupees
        await self._raise_stake(pos, target, pos.price, sig, channel, message_id)

    def _again_entry(self, channel: Channel) -> tuple[BetSignal, str] | None:
        """The channel's latest entry on a team it had already booked a trade on (and what became of it)."""
        for entry, status in reversed(self._entries.get(channel.name, ())):
            if self._store.recently_closed(entry.selection, self._mode):
                return entry, status
        return None

    def _same_team_as_last_entry(self, sig: BetSignal, channel: Channel) -> bool:
        """The channel's latest entry (placed or not) was on this team: a follow-up entry may leave it unnamed."""
        latest = self._last_entry.get(channel.name)
        return bool(latest and sig.selection and same_name(latest[0].selection, sig.selection))

    def _tipped_lately(self, sig: BetSignal, msg: IncomingMessage, channel: Channel) -> bool:
        """An earlier message of the chat in the last TIPPED_LATELY was an entry on this team ('Navone WIN KRO'),
        also when it came before the bot (re)started and so was only read as context."""
        profile = channel.profile
        earlier = list(self._history_for(msg.chat_id))[:-1]  # the last line is this message
        return bool(sig.selection) and any(
            msg.date - when <= TIPPED_LATELY and re.search(profile.entry_words, line, re.IGNORECASE)
            and _mentions(line, sig.selection, profile.aliases) for when, line in earlier)

    def _trade_in_match_of(self, entry: BetSignal, channel: Channel) -> Position | None:
        """The channel's open trade in the match of this (unplaced) entry, on whichever side."""
        sides = [side for side in entry.event.split(" v ") if side.strip(" ?")]
        for pos in reversed(self._store.unclosed_positions(self._mode)):
            if self._trade_is_for(pos, channel) and any(contains_player(side, pos.event) for side in sides):
                return pos
        return None

    def _trade_is_for(self, pos: Position, channel: Channel) -> bool:
        """The channel's own trade. SK's and your own channel's tips on a match share the first one's trade (one match, one
        ₹3,000 at most, on paper too), and either can book it."""
        if pos.channel == channel.name:
            return True
        other = self._channels.get(pos.channel)
        return channel.profile.ladder and other is not None and other.profile.ladder

    def _open_trade(self, sig: BetSignal, channel: Channel) -> Position | None:
        """The channel's open trade on this instruction's team (by the AI's name, else the rules' spelling)."""
        pos = self._store.find_position(sig.selection, self._mode, channel.name, strict=True)
        if pos is None and sig.aka:
            pos = self._store.find_position(sig.aka, self._mode, channel.name, strict=True)
        return pos

    async def _price_on_trade(self, pos: Position, sig: BetSignal) -> float | None:
        """The site's price now on the trade's runner, on this instruction's side. None when it can't be read."""
        if self._bot is None:
            return None
        async with self._trade_lock(TradeLock.OTHER):
            return await self._bot.quote(sig.model_copy(update={"selection": pos.selection, "event": pos.event,
                                                                "aka": ""}))

    def _is_re_entry(self, sig: BetSignal, channel: Channel) -> bool:
        """A new entry on a team the channel's trade is already on, posted after the duplicate window or at a
        clearly different price ('35P entry kr lena sabhi log' an hour after 'Uchijima win karo', '30P rate hai
        entry kr lo' 29 min after Chidekh at 2.7): more stake on that trade."""
        if sig.action != "entry" or not sig.selection:
            return False
        pos = self._open_trade(sig, channel)
        if pos is None:
            return False
        if sig.side in ("back", "lay") and sig.side != pos.side:
            return True  # 'LAY KARO Rublev' on the channel's Rublev back: trading out (_add books it)
        window = timedelta(minutes=self._risk.cfg(channel.name).duplicate_window_min)
        opened = datetime.fromisoformat(pos.opened_at)
        price = sig.when_odds or sig.odds
        # a different price only counts once the trade is a while old: 'AB KRLO ENTRY' 2 min after the tip is
        # the same entry, with '1.70 limit' misread as its price
        moved = bool(price and pos.price and abs(price - pos.price) / pos.price > RE_ENTRY_PRICE_MOVE
                     and opened <= trade_log.clock() - RE_ENTRY_MIN_AGE)
        return moved or opened <= trade_log.clock() - window

    async def _add(self, sig: BetSignal, channel: Channel, how: str, message_id: int | None) -> None:
        """'Add one more 1 limit', 'HALF LIMIT EXTRA' or a re-entry: the open trade on that team gets that many
        units more (1 if not given), at the tip's price (else the trade's), up to MAX_UNITS_PER_BET.
        With no open trade on the team it is an entry."""
        def skip(reason: str):
            return self._record(message_id, sig, BetResult("skipped", reason), Decision(False, reason))

        pos = self._open_trade(sig, channel)
        if pos is None:
            await self._go(sig.model_copy(update={"action": "entry"}), channel, how, message_id)
            return
        if off := self._entries_off(channel.name):
            return await skip(off)
        s = self._risk.cfg(pos)
        if sig.confidence < s.min_confidence:  # SS 1 Nov: '3.7' alone, read as one more add at 0.40
            return await skip(f"AI confidence {sig.confidence:.2f} below {s.min_confidence}")
        odds = sig.odds or pos.price
        extra = sig.amount if sig.amount else (sig.units or 1) * s.unit_stake
        if sig.side and sig.side != pos.side:
            price = await self._price_on_trade(pos, sig) if sig.side == "lay" and sig.odds else None
            if not price or not other_runners_price(sig.odds, price):
                # 'LAY KARO Rublev' while the channel's Rublev back is open: trading out of it, i.e. book it now
                log.info("  %s on %s while the channel's %s is open: booking it", sig.side, pos.selection, pos.side)
                close = sig.model_copy(update={"action": "close", "loss_cut": False, "odds": None, "units": None,
                                               "amount": None, "selection": pos.selection, "event": pos.event,
                                               "aka": ""})
                await self.execute(close, message_id)
                return
            # '45P LAY KARO, Adolfo PLUS KARO' with Adolfo at 3.35: the favourite is laid at 1.45, which is more of the
            # Adolfo back, not a way out of it. Sized as that lay: it wins about the stake if Adolfo wins.
            log.info("  %g is the other runner's price (%s is %g): adding to the trade", sig.odds, pos.selection, price)
            odds, extra = price, extra * (sig.odds - 1)
        if pos.side == "back" and odds and self._s.max_back_odds and odds >= self._s.max_back_odds:
            return await skip(f"can't add to the {pos.selection} back at {odds:g}: backs from "
                              f"{self._s.max_back_odds:g} are placed as a lay of the opponent")
        why = self._confirm_reason(sig.model_copy(update={"action": "entry"}), channel, how)
        if why and (self._mode == "live" or self._s.confirm_in_dry_run):
            return await skip(f"add not made: {why}")
        await self._raise_stake(pos, round(pos.stake + extra, 2), odds, sig, channel, message_id)

    async def _raise_stake(self, pos: Position, target: float, odds: float | None, sig: BetSignal,
                           channel: Channel, message_id: int | None) -> None:
        """Brings the trade's stake up to target (at most MAX_UNITS_PER_BET), at the price on offer now if it is
        still close to odds."""
        def skip(reason: str):
            return self._record(message_id, sig, BetResult("skipped", reason), Decision(False, reason))

        s = self._risk.cfg(pos)
        if s.max_units_per_bet:  # topped up to the most you allow, as a big entry is
            target = min(target, s.max_units_per_bet * s.unit_stake)
        extra = round(target - pos.stake, 2)
        if extra <= 0:
            return await skip(f"{pos.selection} already has stake {pos.stake:g} (>= {target:g}, "
                              f"MAX_UNITS_PER_BET counted)")
        add = BetSignal(action="entry", sport=pos.sport, event=pos.event, market=pos.market, market_name="",
                        selection=pos.selection, side=pos.side, odds=odds, line=None, units=None, amount=extra,
                        loss_cut=False, confidence=1.0, channel=channel.name)
        async with self._trade_lock(TradeLock.OTHER):
            if self._bot is None:
                result = self._simulate(add, Decision(True, "ok"))
            else:
                result = await self._bot.place(add, None)
            if result.status in COUNTED and result.price:
                self._store.add_to_position(pos.id, result.stake, result.price)
                result.reason = f"TOP-UP of {pos.selection} to stake {target:g}: {result.reason}"
            elif result.status == "retry":
                result = BetResult("skipped", f"top-up not placed: {result.reason}")
            await self._record(message_id, add, result, Decision(False, "top-up"))

    # --- instructions ----------------------------------------------------------------------------

    async def execute(self, sig: BetSignal, message_id: int | None = None, note: str = "") -> BetResult:
        """Carries out one instruction: safety checks, the site, the trade log and the alert. The tipster
        posts each instruction once, so anything the site can't take yet is retried until it goes through.
        note goes in front of the decision's reason (e.g. that live would have asked first)."""
        if sig.action in ("close", "cancel") and (ladder := self._ladder_of(sig)):
            sig, done = await self._ladder_instruction(sig, ladder, message_id)
            if done:
                return done
        if sig.action == "cancel":
            return await self._cancel(sig, message_id)
        async with self._trade_lock(_priority(sig)):
            if sig.action == "close" and self._cancel_pending_entry(sig):
                result = BetResult("skipped", "the tipster closed this trade before our entry could be placed: "
                                              "pending entry cancelled, nothing to book")
                await self._record(message_id, sig, result, Decision(False, result.reason))
                return result
            if sig.action == "entry" and (off := self._entries_off(sig.channel)):
                result = BetResult("skipped", off)
                await self._record(message_id, sig, result, Decision(False, result.reason))
                return result
            check = self._risk.precheck(sig)
            s = self._risk.cfg(sig)
            if not check.ok:
                result = BetResult("skipped", check.reason)
            elif self._bot is None:
                result = self._simulate(sig, check)
            else:
                green = sig.action == "close" and (s.book_only_green or (not sig.loss_cut and s.bookset_green_wait_s > 0))
                result = await self._bot.place(sig, check.position, require_green=green)
                if result.status == "retry" and sig.action == "close":
                    result = self._keep_retrying(sig, check, result)
                elif result.status == "retry":
                    self._store.add_pending(sig, self._mode, message_id, result.reason)
                    minutes = self._retry_minutes(sig, result.reason)
                    what = (f"waiting for the {sig.side} price to reach {sig.when_odds:g}" if sig.when_odds
                            else "will retry")
                    result = BetResult("pending", f"{what}, every {BOOKSET_RETRY_SECONDS}s for up to {minutes} "
                                                  f"min: {result.reason}", result.price)
            if note:
                result.reason = f"{note} {result.reason}"
            await self._record(message_id, sig, result, check)
            return result

    async def _cancel(self, sig: BetSignal, message_id: int | None) -> BetResult:
        """CANCEL <name>: drops a waiting entry and the waiting (unmatched) exit bets on that trade."""
        async with self._trade_lock(TradeLock.BOOKING):
            done = []
            pending_id = self._store.find_pending(sig.selection, self._mode, "entry")
            if pending_id is not None:
                self._store.remove_pending(pending_id)
                done.append("waiting entry dropped")
            pos = self._store.find_position(sig.selection, self._mode)
            if pos and pos.exit_price is not None and self._bot is not None:
                cancelled = await self._bot.cancel_waiting(pos)
                done.append(f"waiting exit bets cancelled on the site (stake {cancelled:g})")
            result = BetResult("placed" if done else "skipped",
                               "; ".join(done) or f"nothing waiting on {sig.selection}")
            await self._record(message_id, sig, result, Decision(bool(done), result.reason))
            return result

    def _cancel_pending_entry(self, sig: BetSignal) -> bool:
        """A bookset / loss cut for a trade whose entry is still being retried: drop the entry instead."""
        own = self._mode != "live"  # on paper, each channel's trades are its own
        if self._store.find_position(sig.selection, self._mode, sig.channel, own):
            return False  # we are in the trade: book it
        pending_id = self._store.find_pending(sig.selection, self._mode, "entry", sig.channel if own else "")
        if pending_id is None:
            return False
        self._store.remove_pending(pending_id)
        log.info("  pending entry on %s cancelled by the close instruction", sig.selection or "the latest tip")
        return True

    def _keep_retrying(self, sig: BetSignal, check: Decision, result: BetResult) -> BetResult:
        """The tipster says bookset once: when the site can't take it yet, keep trying in the background."""
        if check.position is None:
            return BetResult("failed", f"{result.reason} (no recorded trade to keep retrying: book it by hand)")
        self._store.mark_close_pending(check.position.id, sig.loss_cut)
        return BetResult("pending", f"will retry every {BOOKSET_RETRY_SECONDS}s for up to "
                                    f"{self._s.bookset_retry_min} min: {result.reason}")

    async def retry_pending_closes(self) -> None:
        """Retries every asked-for bookset / loss cut until the site takes it. A bookset waits up to
        BOOKSET_GREEN_WAIT_SECONDS for a green (profit) booking, then books anyway; a loss cut books at once.
        Also drops CONFIRM questions nobody answered."""
        while True:
            await asyncio.sleep(BOOKSET_RETRY_SECONDS)
            await self._expire_asked()
            if self._bot is not None:
                await self.retry_pending_once()

    async def retry_pending_once(self) -> None:
        """One retry of every pending entry, limit exit, bookset and loss cut."""
        await self._retry_pending_instructions()
        await self._retry_pending_closes()

    async def _retry_pending_instructions(self) -> None:
        """Entries / limit exits the site couldn't take yet. Each try checks the live price against the tip
        again, so a stale price is never taken; after ENTRY_RETRY_MINUTES (CONDITIONAL_ENTRY_MINUTES for a
        waiting entry) the tip is reported as missed."""
        for pending_id, created_at, sig, message_id, last_reason in self._store.pending_instructions(self._mode):
            if sig.action == "entry" and sig.channel in self._stopped:
                self._store.remove_pending(pending_id)
                reason = self._entries_off(sig.channel)
                await self._record(message_id, sig, BetResult("skipped", reason), Decision(False, reason))
                continue
            if sig.not_before and datetime.now() < datetime.fromisoformat(sig.not_before):
                continue  # held for its match: the site isn't opened until then
            if self._looked_lately(pending_id, sig):
                continue
            async with self._trade_lock(_priority(sig)):
                if pending_id not in {p[0] for p in self._store.pending_instructions(self._mode)}:
                    continue  # cancelled meanwhile (e.g. the tipster booked first)
                if sig.not_before and self._paused:
                    self._store.remove_pending(pending_id)
                    reason = "PAUSED: no new entries until /resume"
                    await self._record(message_id, sig, BetResult("skipped", reason), Decision(False, reason))
                    continue
                waited = (datetime.now() - datetime.fromisoformat(sig.not_before or created_at)).total_seconds()
                minutes = self._retry_minutes(sig, last_reason)
                if waited > minutes * 60:
                    self._store.remove_pending(pending_id)
                    result = BetResult("failed", f"{sig.action.upper()} MISSED after {minutes} min "
                                                 f"of {'waiting' if sig.when_odds else 'retrying'}; "
                                                 f"last problem: {last_reason}")
                    await self._record(message_id, sig, result, Decision(False, result.reason))
                    continue
                check = self._risk.precheck(sig, retrying=True)
                if not check.ok:
                    self._store.remove_pending(pending_id)
                    await self._record(message_id, sig, BetResult("skipped", check.reason), check)
                    continue
                result = await self._bot.place(sig, check.position)
                if result.status == "retry":
                    self._store.update_pending(pending_id, result.reason)
                    log.info("  still trying %s %s (%.0fs): %s", sig.action, sig.selection, waited, result.reason)
                    continue
                self._store.remove_pending(pending_id)
                result.reason = f"after {waited:.0f}s of {'waiting' if sig.when_odds else 'retrying'}: {result.reason}"
                if sig.tip_price:
                    result.reason += f" [price when tipped {sig.tip_price:g}]"
                await self._record(message_id, sig, result, check)

    def _looked_lately(self, pending_id: int, sig: BetSignal) -> bool:
        """A waiting entry that can wait for days had its price read within the last SLOW_WAIT_EVERY."""
        if not sig.when_odds or self._risk.cfg(sig).conditional_entry_min <= SLOW_WAIT_ABOVE_MIN:
            return False
        now = datetime.now()
        if now - self._last_look.get(pending_id, datetime.min) < SLOW_WAIT_EVERY:
            return True
        self._last_look[pending_id] = now
        return False

    def _retry_minutes(self, sig: BetSignal, reason: str) -> int:
        """How long an entry the site can't take yet is retried: a waiting entry until its price comes; a tennis
        market suspended between points / at a medical break longer (27 Oct: a 5-minute suspension missed one)."""
        if sig.when_odds:
            return self._risk.cfg(sig).conditional_entry_min
        if sig.not_before:
            return HELD_ENTRY_MINUTES
        if sig.sport == "tennis" and "suspended" in reason.lower():
            return max(self._s.entry_retry_min, TENNIS_SUSPENDED_MINUTES)
        return self._s.entry_retry_min

    async def _retry_pending_closes(self) -> None:
        """Booksets / loss cuts the site refused so far."""
        for pos in self._store.pending_closes(self._mode):
            async with self._trade_lock(TradeLock.BOOKING):
                current = next((p for p in self._store.pending_closes(self._mode) if p.id == pos.id), None)
                if current is None:
                    continue  # booked meanwhile
                s = self._risk.cfg(current)
                waited = (datetime.now() - datetime.fromisoformat(current.close_pending_since)).total_seconds()
                sig = _close_signal(current, bool(current.close_loss_cut))
                if waited > self._s.bookset_retry_min * 60:
                    self._store.clear_close_pending(current.id)
                    if s.book_only_green:
                        reason = (f"NOT BOOKED: never green in {self._s.bookset_retry_min} min (BOOK_ONLY_GREEN). "
                                  f"The trade stays open and settles at the result; book it by hand if you want")
                    else:
                        reason = f"NOT BOOKED after {self._s.bookset_retry_min} min of retrying: BOOK IT BY HAND"
                    await self._record(None, sig, BetResult("failed", reason),
                                       Decision(True, "retry", position=current))
                    continue
                green = s.book_only_green or (not current.close_loss_cut and waited < s.bookset_green_wait_s)
                result = await self._bot.place(sig, current, require_green=green)
                if result.status == "retry":
                    log.info("  still booking %s (%.0fs): %s", current.selection, waited, result.reason)
                    continue
                if result.status not in ("placed", "unconfirmed"):
                    self._store.clear_close_pending(current.id)
                result.reason = f"after {waited:.0f}s of retrying: {result.reason}"
                await self._record(None, sig, result, Decision(True, "retry", position=current))

    async def monitor(self) -> None:
        """Watches the price of every open trade: the last one seen settles a trade whose match ends without a
        booking (check_finished_matches). With AUTO_BOOKSET_UNITS (the setting of the trade's channel) it also
        books a trade once booking it now would lock that much profit."""
        if self._bot is None:
            return
        log.info("Open trades' prices checked every %ds%s", self._s.monitor_interval_s,
                 " (auto-bookset on)" if self._channels.any_auto_bookset() else "")
        while True:
            await asyncio.sleep(self._s.monitor_interval_s)
            for pos in self._store.open_positions(self._mode):
                s = self._risk.cfg(pos)
                async with self._trade_lock(TradeLock.OTHER):  # a price check: tipsters' instructions go first
                    if pos.id not in {p.id for p in self._store.open_positions(self._mode)}:
                        continue  # booked by a tip meanwhile
                    price = await self._bot.exit_price(pos)
                    if price is None:
                        # a match still listed after its market closed: settled once its last price was clear
                        self._no_price[pos.id] = self._no_price.get(pos.id, 0) + 1
                        if (self._no_price[pos.id] >= NO_PRICE_FINISHED
                                and result_from_price(pos, self._last_price.get(pos.id))):
                            await self._finish(pos)
                        continue
                    self._no_price.pop(pos.id, None)
                    self._last_price[pos.id] = price
                    self._store.log_price(pos.id, price)
                    if not s.auto_bookset_units:
                        continue
                    target = s.auto_bookset_units * s.unit_stake
                    profit = booked_profit(pos, price)
                    if profit < target:
                        continue
                    log.info("AUTO BOOKSET %s: booking at %g locks %+g (target %g)", pos.selection, price,
                             profit, target)
                    sig = _close_signal(pos, loss_cut=False)
                    result = await self._bot.place(sig, pos)
                    if result.status == "retry":
                        continue  # the next check tries again while the profit is still there
                    result.reason = f"auto-bookset at +{profit:g}: {result.reason}"
                    await self._record(None, sig, result, Decision(True, "auto-bookset", position=pos))

    async def keep_alive(self) -> None:
        """Every KEEPALIVE_MINUTES: reloads the site so the login doesn't time out, and reports trades whose
        match has finished without being booked."""
        if self._bot is None or not self._s.keepalive_min:
            return
        while True:
            await asyncio.sleep(self._s.keepalive_min * 60)
            if not self._bot.busy:
                await self._bot.keep_alive()
                await self.check_finished_matches()

    async def heartbeat(self) -> None:
        """Every HEARTBEAT_HOURS: 'still running' + today's numbers, so a silent bot is noticed."""
        if not (self._notifier and self._s.heartbeat_hours):
            return
        while True:
            await asyncio.sleep(self._s.heartbeat_hours * 3600)
            await self._notifier.send(self.status_text())

    # --- SK Astro's method: the other team laid in steps (tradebot/ladder.py) --------------------------

    async def _start_ladder(self, sig: BetSignal, text: str, channel: Channel, message_id: int | None) -> None:
        """A tip that a team wins: its match is watched from the start, or at once if it is on."""
        def skip(reason: str):
            return self._record(message_id, sig, BetResult("skipped", reason), Decision(False, reason))

        if off := self._entries_off(channel.name):
            return await skip(off)
        if any(ladder.channel == channel.name and team_fits(sig.selection, [ladder.team])
               for ladder in self._store.active_ladders(self._mode)):
            return await skip(f"{sig.selection} is being watched already")
        try:
            matches = await self._cricbuzz.refresh_list()
        except Exception:
            log.warning("  no match time for %s: Cricbuzz could not be read", sig.selection, exc_info=True)
            matches = []
        now = datetime.now()
        match = coming_match(matches, sig.selection, sig.event, now)
        at = max(now, match.start - timedelta(minutes=channel.profile.enter_before_start_min)) if match else now
        event = " v ".join(name for name, _ in match.teams) if match and "?" in sig.event else sig.event
        twin, rival = self._tips_on_match(sig.selection, event, at, channel.name)
        if twin:
            self._store.update_ladder(twin.id, confirmed_by=channel.name)
            return await skip(f"already watched: SIGNAL_CONFIRMED, {twin.channel} and {channel.name} both say {sig.selection}")
        if rival:
            self._store.update_ladder(rival.id, conflict=1)
            return await skip(f"SIGNAL_CONFLICT: {rival.channel} says {rival.team}, {channel.name} says {sig.selection}: "
                              f"no more entries in this match until /sk <team> decides")
        async with self._trade_lock(TradeLock.OTHER):
            price = await self._bot.quote(sig.model_copy(update={"event": event}))
        self._store.add_ladder(self._mode, channel.name, sig.selection, event, at, message_id,
                               "cricket" if match else sig.sport)
        reason = (f"SK method: the other team laid in steps from {at:%d %b %H:%M}, booked at even; {sig.selection} now "
                  f"{f'{price:g}' if price else 'not listed yet'}")
        await self._record(message_id, sig, BetResult("pending", reason, price), Decision(True, reason))

    def _tips_on_match(self, team: str, event: str, at: datetime, channel: str) -> tuple[Ladder | None, Ladder | None]:
        """Another channel's SK-method tip on this match: (on the same team, on the other team)."""
        sides = [side for side in event.split(" v ") if side.strip(" ?")]
        for ladder in self._store.active_ladders(self._mode):
            if ladder.channel == channel or abs(datetime.fromisoformat(ladder.start_at) - at) > SAME_MATCH_WITHIN:
                continue
            if team_fits(team, [ladder.team]):
                return ladder, None
            if (ladder.other and team_fits(team, [ladder.other])) or any(team_fits(ladder.team, [side]) for side in sides):
                return None, ladder
        return None, None

    async def _call_before_match(self, text: str, channel: Channel) -> list[BetSignal]:
        """SK Astro 1 Nov 13:05: 'Sydney Toss Loss / Sydney Match Win ✅', read as a result, 35 min before Sixers v
        Hurricanes. A team's 'Match Win' shortly before its match is a call, whatever the ✅ looks like."""
        found = SK_CALL.search(text)
        if not found or len(text.splitlines()) > 3 or self._bot is None:  # a longer list is the day's results
            return []
        team = expand_team(found["team"].strip())
        try:
            matches = await self._cricbuzz.refresh_list()
        except Exception:
            log.warning("  no match time for %s: Cricbuzz could not be read", team, exc_info=True)
            return []
        now = datetime.now()
        match = coming_match(matches, team, f"{team} v ?", now)
        if match is None or match.start - now > SK_CALL_WITHIN:
            return []
        log.info("  '%s' comes %s before its match: a call", found[0].strip(), match.start - now)
        return [BetSignal(action="entry", sport="cricket", event=f"{team} v ?", market="match_odds", market_name="",
                          selection=team, side="back", odds=None, line=None, units=None, amount=None, loss_cut=False,
                          confidence=1.0, channel=channel.name)]

    async def ladder_watcher(self) -> None:
        """Every LADDER_POLL, one look at each SK-method match that is on."""
        if self._bot is None or not any(c.profile.ladder for c in self._channels.all()):
            return
        while True:
            await asyncio.sleep(LADDER_POLL.total_seconds())
            for ladder in self._store.active_ladders(self._mode):
                try:
                    await self._ladder_step(ladder)
                except Exception:
                    log.exception("SK method: could not look at %s", ladder.team)

    async def _ladder_step(self, ladder: Ladder) -> None:
        """Takes the step the price is in, stops the steps after a swing, books or exits by exit_call, ends when nothing
        is left."""
        now = datetime.now()
        start = datetime.fromisoformat(ladder.start_at)
        if now < start:
            return  # a tip before its match: the site isn't opened until then
        if now - start > LADDER_LONGEST:
            return await self._end_ladder(ladder, "no longer watched, 12 hours after its start")
        async with self._trade_lock(TradeLock.OTHER):  # a price check: tipsters' instructions go first
            active = {now_on.id: now_on for now_on in self._store.active_ladders(self._mode)}
            if ladder.id not in active:
                return  # the tipster closed it meanwhile
            ladder = active[ladder.id]  # as it is now: another tip's look may have blocked it
            quote = await self._bot.other_team_quote(ladder.event, ladder.team)
        if quote.gone:
            misses = self._ladder_gone[ladder.id] = self._ladder_gone.get(ladder.id, 0) + 1
            if ladder.other and misses >= LADDER_GONE_READS:
                await self._end_ladder(ladder, "its match is over")
            elif not ladder.other and now - start > LADDER_NOT_LISTED:
                await self._end_ladder(ladder, f"ENTRY MISSED: {quote.problem}")
            return
        self._ladder_gone.pop(ladder.id, None)
        if quote.lay is None:
            log.info("  SK method, %s: no price: %s", ladder.team, quote.problem or "none on offer")
            return
        if not ladder.other:
            ladder = replace(ladder, other=quote.other, event=quote.event or ladder.event, first_price=quote.lay)
            self._store.update_ladder(ladder.id, other=ladder.other, event=ladder.event, first_price=ladder.first_price)
            ladder = await self._note_rival(ladder)
        self._store.log_price(-ladder.id, quote.lay)  # its path, to test other steps on later
        team_back = quote.team_back or (round(quote.back / (quote.back - 1), 2) if quote.back and quote.back > 1 else None)
        self._store.update_ladder(ladder.id, last_lay=quote.lay, last_back=quote.back, team_back=team_back)
        trade = self._store.find_position(ladder.other, self._mode, ladder.channel, strict=True)
        if ladder.last_step >= 0 and trade is None:
            return await self._end_ladder(ladder, "its trade is closed")
        if trade and quote.back and team_back and await self._ladder_exit(ladder, trade, quote.back, team_back):
            return
        if ladder.stops_at(quote.lay):
            ladder = replace(ladder, stopped=True)
            self._store.update_ladder(ladder.id, stopped=1)
            await self._ladder_note(ladder, f"{ladder.other} back up at {quote.lay:g}: no more entries in this match, and "
                                            f"none if it comes down again")
        step = ladder.step_to_take(quote.lay)
        if step is not None and not self._ladder_entries_off(ladder):
            blocked = self._ladder_block(ladder)
            if not blocked:
                await self._take_step(ladder, step, quote, trade)
            elif self._ladder_blocked.get(ladder.id) != blocked:
                self._ladder_blocked[ladder.id] = blocked
                await self._ladder_note(ladder, f"entry {ladder.entries + 1} BLOCKED: {blocked}")

    async def _ladder_note(self, ladder: Ladder, text: str) -> None:
        log.info("  SK method, %s: %s", ladder.team, text)
        if self._notifier:
            await self._notifier.send(f"🪜 *SK method*, {ladder.team}: {text}")

    async def _note_rival(self, ladder: Ladder) -> Ladder:
        """A tip whose other team only now is known: it may be what another channel tipped."""
        for rival in self._store.active_ladders(self._mode):
            if rival.id != ladder.id and rival.channel != ladder.channel and team_fits(ladder.other, [rival.team]) and \
                    abs(datetime.fromisoformat(rival.start_at) - datetime.fromisoformat(ladder.start_at)) <= SAME_MATCH_WITHIN:
                self._store.update_ladder(rival.id, conflict=1)
                self._store.update_ladder(ladder.id, conflict=1)
                await self._ladder_note(ladder, f"SIGNAL_CONFLICT: {ladder.channel} says {ladder.team}, {rival.channel} says "
                                                f"{rival.team}: no more entries until /sk <team> decides")
                return replace(ladder, conflict=True)
        return ladder

    def _ladder_rules(self, channel: str) -> LadderRules:
        found = self._channels.get(channel)
        return LadderRules(**found.profile.ladder_rules) if found else LadderRules()

    def _ladder_block(self, ladder: Ladder) -> str:
        """Why no entry can go in now ('' = it can): a stale market, always; the limits on opening a new match."""
        rules = self._ladder_rules(ladder.channel)
        prices = self._store.last_prices(-ladder.id, rules.frozen_reads)
        if len(prices) == rules.frozen_reads and len(set(prices)) == 1:
            return f"market PAUSED, {ladder.other} at {prices[0]:g} for {rules.frozen_reads} reads: the data is stale"
        if ladder.entries:
            return ""
        if sum(1 for other in self._store.active_ladders(self._mode) if other.entries) >= rules.max_open:
            return f"{rules.max_open} matches already open"
        closed = self._store.closed_results(self._mode, [c.name for c in self._channels.all() if c.profile.ladder],
                                            self._s.unit_stake)
        now = datetime.now()
        lost_today = sum(pl for at, pl in closed if at and at[:10] == now.date().isoformat())
        if lost_today <= -rules.daily_loss:
            return f"the day's loss limit of {rules.daily_loss:g} limits is reached ({lost_today:+.2f})"
        total = best = 0.0
        for _, pl in closed:
            total += pl
            best = max(best, total)
        if best - total >= rules.max_drawdown:
            return f"the drawdown limit of {rules.max_drawdown:g} limits is reached ({total - best:+.2f})"
        losses = [at for at, pl in closed if pl < 0 and at]
        if losses and now - datetime.fromisoformat(losses[-1]) < timedelta(minutes=rules.cooldown_min):
            return f"cooling down for {rules.cooldown_min} min after a losing trade"
        return ""

    async def _ladder_exit(self, ladder: Ladder, trade: Position, other_back: float, team_back: float) -> bool:
        """Books or exits when the user's rules say so (tradebot/ladder.py, exit_call). True when it did."""
        rules = self._ladder_rules(ladder.channel)
        unit = self._risk.cfg(trade).unit_stake
        target = rules.profit_target * unit
        pl = booked_profit(trade, other_back)
        peak = max(ladder.peak, pl)
        call = exit_call(pl, team_back, other_back, target, rules.giveback * unit, peak, ladder.was_favourite, rules.equal_band)
        self._store.update_ladder(ladder.id, peak=peak, last_pl=pl, waiting=int(call == WAIT_FAVOURITE),
                                  was_favourite=int(ladder.was_favourite or team_back < other_back))
        prices = f"{ladder.team} {team_back:g}, {ladder.other} {other_back:g}"
        if call == WAIT_FAVOURITE and not ladder.waiting:
            await self._ladder_note(ladder, f"PROFIT CONDITION SATISFIED, WAITING FOR FAVOURITE CONFIRMATION: P&L {pl:+,.0f}, {prices}")
        reason = {BOOKSET: f"BOOKSET TRIGGERED, both conditions: P&L {pl:+,.0f} and {ladder.team} favourite ({prices})",
                  EQUAL_EXIT: f"EQUAL-ODDS EXIT: {prices}, P&L {pl:+,.0f}, under the {target:,.0f} target",
                  FELL_BACK: f"EXIT: {ladder.team} was favourite and lost it ({prices}), P&L {pl:+,.0f}",
                  GIVEBACK: f"EXIT, profit giving back: best {peak:+,.0f}, now {pl:+,.0f} ({prices})"}.get(call)
        if reason is None:
            return False
        await self._end_ladder(ladder, reason)
        await self.execute(_close_signal(trade, loss_cut=False), ladder.message_id)
        return True

    async def _take_step(self, ladder: Ladder, step: int, quote: TeamQuote, trade: Position | None) -> None:
        """One unit more on the lay of the other team, at the limit of its trade once there is one. The next look tries
        again if the site can't take it now."""
        s = self._risk.cfg(trade or ladder.channel)
        unit = s.unit_stake
        if trade and s.max_units_per_bet and trade.stake + unit > s.max_units_per_bet * unit:
            self._store.update_ladder(ladder.id, stopped=1)  # or each look would try, and alert, again
            log.info("  SK method, %s: no entry %d, MAX_UNITS_PER_BET %g reached", ladder.team, step + 1, s.max_units_per_bet)
            return
        sig = BetSignal(action="entry", sport=ladder.sport, event=quote.event or ladder.event, market="match_odds",
                        market_name="", selection=quote.other, side="lay", odds=quote.lay, line=None, units=None,
                        amount=unit, loss_cut=False, confidence=1.0, channel=ladder.channel)
        if trade is None:
            async with self._trade_lock(TradeLock.OTHER):
                check = self._risk.precheck(sig)
                if not check.ok:
                    await self._record(ladder.message_id, sig, BetResult("skipped", check.reason), check)
                    await self._end_ladder(ladder, f"not entered: {check.reason}")
                    return
                result = await self._bot.place(sig, None)
                if result.status in COUNTED:
                    result.reason = f"SK method, entry {step + 1}: {result.reason}"
                    await self._record(ladder.message_id, sig, result, check)
            taken = result.status in COUNTED
        else:
            await self._raise_stake(trade, round(trade.stake + unit, 2), quote.lay, sig,
                                    self._channels.get(ladder.channel), ladder.message_id)
            taken = self._store.position(trade.id).stake > trade.stake
        if not taken:
            log.info("  SK method, %s: entry %d not taken yet", ladder.team, step + 1)
            return
        prices = ",".join(filter(None, (ladder.entry_prices, f"{quote.lay:g}")))
        self._store.update_ladder(ladder.id, last_step=step, entries=ladder.entries + 1, entry_prices=prices)

    async def _end_ladder(self, ladder: Ladder, reason: str) -> None:
        self._store.end_ladder(ladder.id, reason)
        self._ladder_gone.pop(ladder.id, None)
        first = f", {ladder.other} first read at {ladder.first_price:g}" if ladder.first_price else ""
        text = f"{ladder.team}: {reason} ({ladder.entries} of {len(STEPS)} entries{first})"
        log.info("SK method, %s", text)
        if self._notifier:
            await self._notifier.send(f"🪜 *SK method*, {text}")

    def _ladder_of(self, sig: BetSignal) -> Ladder | None:
        """The latest SK-method tip a bookset or cancel is about, by its team or the team it lays (any, when it names
        none). Your own commands reach any channel's, and a channel that tipped the same team reaches the one trade."""
        name = sig.selection.strip(" ?")
        for ladder in self._store.recent_ladders(self._mode):
            if sig.channel in (ladder.channel, ladder.confirmed_by, MANUAL) and (
                    not name or team_fits(name, [ladder.team]) or (ladder.other and team_fits(name, [ladder.other]))):
                return ladder
        return None

    async def _ladder_instruction(self, sig: BetSignal, ladder: Ladder,
                                  message_id: int | None) -> tuple[BetSignal, BetResult | None]:
        """A bookset or cancel on an SK-method tip means no more entries. A bookset then acts on the lay of the other
        team, whichever team it names; the result is given when there is nothing more to do."""
        if ladder.ended_at is None:
            await self._end_ladder(ladder, "cancelled" if sig.action == "cancel" else "bookset asked for")
        trade = self._store.find_position(ladder.other, self._mode, ladder.channel, strict=True) if ladder.other else None
        if sig.action == "close" and trade:  # as the trade's own channel: the other one may have asked
            return sig.model_copy(update={"selection": trade.selection, "event": trade.event, "aka": "",
                                          "channel": trade.channel}), None
        if sig.action == "cancel":
            result = BetResult("placed", f"{ladder.team}: no more entries" + (f"; the {trade.selection} lay stays open"
                                                                              if trade else ""))
        else:
            result = BetResult("skipped", f"{ladder.team}: no entry had gone in, nothing to book")
        await self._record(message_id, sig, result, Decision(False, result.reason))
        return sig, result

    def _ladder_status(self, ladder: Ladder) -> str:
        """All of a tip at a glance, in the order the user asked for."""
        rules = self._ladder_rules(ladder.channel)
        trade = self._store.find_position(ladder.other, self._mode, ladder.channel, strict=True) if ladder.other else None
        unit = self._risk.cfg(trade or ladder.channel).unit_stake
        signal = {True: f"SIGNAL_CONFLICT, {ladder.channel} alone says {ladder.team}"}.get(ladder.conflict) or (
            f"SIGNAL_CONFIRMED, {ladder.channel} + {ladder.confirmed_by}" if ladder.confirmed_by else ladder.channel)
        lines = [f"• *{ladder.event}*: {ladder.team} (lays {ladder.other or 'the other team'}) · {ladder.state}",
                 f"  Signal: {signal}. Watched from {ladder.start_at[8:10]}/{ladder.start_at[5:7]} {ladder.start_at[11:16]}"]
        if ladder.last_back and ladder.team_back:
            favourite = ladder.team if ladder.team_back < ladder.last_back else ladder.other
            lines.append(f"  Odds: {ladder.team} {ladder.team_back:g}, {ladder.other} {ladder.last_back:g} (lay {ladder.last_lay:g}). "
                         f"Favourite: {favourite}")
        taken = ladder.entry_prices.split(",") if ladder.entry_prices else []
        later = "blocked" if ladder.stopped or ladder.conflict else "waiting"
        lines.append("  Entries: " + " · ".join(f"{n + 1} {'@' + taken[n] if n < len(taken) else later}" for n in range(len(STEPS))))
        if trade:
            target = rules.profit_target * unit
            pl = ladder.last_pl if ladder.last_pl is not None else booked_profit(trade, ladder.last_back or trade.price)
            lines.append(f"  Stake {trade.stake:,.0f}, liability {trade.liability:,.0f}, P&L now {pl:+,.0f}")
            lines.append(f"  Target {target:,.0f}: {'yes' if pl >= target else 'no'}. Favourite: "
                         f"{'yes' if ladder.team_back and ladder.last_back and ladder.team_back < ladder.last_back else 'no'}. "
                         f"Bookset: {'waiting' if ladder.waiting else 'no'}. Entries stopped: {'yes' if ladder.stopped else 'no'}")
        return "\n".join(lines)

    def _ladders_text(self) -> str:
        lines = [self._ladder_status(ladder) for ladder in self._store.active_ladders(self._mode)]
        return "🪜 *SK method*\n" + "\n".join(lines) if lines else ""

    def _decide_conflict(self, team: str) -> str:
        """/sk <team> on a match SK and your channel disagree on. The text to answer with, '' to go on and start its watch."""
        for ladder in self._store.active_ladders(self._mode):
            if not ladder.conflict:
                continue
            if team_fits(team, [ladder.team]):
                self._store.update_ladder(ladder.id, conflict=0)
                return f"✅ {ladder.team}: decided, its entries go on."
            if ladder.other and team_fits(team, [ladder.other]):
                if ladder.entries:
                    return f"⚠️ {ladder.team} already has {ladder.entries} entries: it stays, with no more. /bookset {ladder.team} closes it."
                self._store.end_ladder(ladder.id, f"the conflict was decided for {team}")
        return ""

    async def _sk_command(self, text: str) -> str:
        """/sk: the tips being watched. /sk <tip>: a tip you got elsewhere (SK's private posts), as if SK had posted it."""
        channel = next((c for c in self._channels.all() if c.profile.ladder), None)
        if channel is None:
            return "⚠️ No channel in channels.json uses the SK method."
        if not text:
            return self._ladders_text() or "🪜 No SK tip is being watched."
        team, other = read_sk_tip(text)
        if not team:
            raise ValueError("give a team: /sk UAE Toss or Match Win")
        team, other = expand_team(team), expand_team(other)
        decided = self._decide_conflict(team)
        if decided:
            return decided
        sig = BetSignal(action="entry", sport="cricket", event=f"{team} v {other}", market="match_odds", market_name="",
                        selection=team, side="back", odds=None, line=None, units=None, amount=None, loss_cut=False,
                        confidence=1.0, channel=channel.name)
        await self._start_ladder(sig, text, channel, None)
        return ""  # the decision's own alert says what happened

    async def _note_jackpot_days(self, text: str, channel: Channel) -> None:
        for day in jackpot_days_in(text, datetime.now().date()):
            if self._store.add_jackpot_day(day, channel.name):
                log.info("%s: jackpot day %s", channel.name, day)
                if self._notifier:
                    await self._notifier.send(f"📅 *{channel.name}*: jackpot day {day:%d %b}")

    def _jackpot_command(self, args: list[str]) -> str:
        today = datetime.now().date()
        if not args:
            return self._jackpot_text()
        if args[0].lower() == "off":
            day = parse_day(args[1:], today)
            return f"✅ {day:%d %b} removed." if self._store.remove_jackpot_day(day) else f"⚠️ {day:%d %b} wasn't listed."
        day = parse_day(args, today)
        self._store.add_jackpot_day(day, MANUAL)
        return f"✅ Jackpot day: {day:%d %b}.\n{self._jackpot_text()}"

    def _jackpot_text(self) -> str:
        """The days, and how each jackpot-days channel did on them and on the other days since the first."""
        days = self._store.jackpot_days()
        shown = ", ".join(f"{date.fromisoformat(day):%d %b}" for day in days[-15:]) or "none yet"
        lines = [f"📅 *SK jackpot days*: {shown}"]
        for channel in self._channels.all():
            if not (channel.profile.jackpot_days_only and days):
                continue
            by_day = self._store.channel_days(self._mode, channel.name)
            on = [by_day[day] for day in days if day in by_day]
            off = [result for day, result in by_day.items() if day not in days and day >= days[0]]
            lines.append(f"{channel.name} on those days: {sum(n for n, _ in on)} trades, {sum(pl for _, pl in on):+.0f}")
            lines.append(f"{channel.name} on other days: {sum(n for n, _ in off)} trades, {sum(pl for _, pl in off):+.0f}")
        return "\n".join(lines)

    # --- fancy (session) tips, on paper -------------------------------------------------------------

    async def _fancy_entry(self, sig: BetSignal, channel: Channel, message_id: int | None) -> None:
        """A session tip ('06 OVER 56 YES WEST IND', 'Meg Lanning 87 Runs Yes') or a toss call: kept on paper, settled
        later from Cricbuzz. There is no code that places these bets."""
        session = read_session(sig.market_name, sig.selection, sig.event)
        side = {"back": "yes", "lay": "no"}.get(sig.side, sig.side)
        toss = session is not None and session.kind == "toss"
        line = 0.0 if toss else sig.line

        def skip(reason: str):
            return self._record(message_id, sig, BetResult("skipped", reason), Decision(False, reason))

        if session is None or line is None or side not in ("yes", "no") or not session.team:
            return await skip("fancy tip not understood: it needs the over or wicket, the team, the line and yes/no")
        if any((t["team"], t["kind"], t["number"], t["line"], t["side"]) ==
               (session.team, session.kind, session.number, line, side) and t["channel"] == channel.name
               for t in self._store.open_fancy(self._mode)):
            return await skip("duplicate of an open fancy tip")
        unit = self._risk.cfg(channel.name).unit_stake
        units = round(sig.units or (sig.amount / unit if sig.amount and unit else 1.0), 2)
        quote = await self._fancy_quote(sig, session, side)
        self._store.add_fancy(self._mode, channel.name, message_id, sig.event, session.team, session.kind,
                              session.number, line, side, TOSS_RATE if toss else sig.odds, units,
                              quote.name if quote else "", quote.line if quote else None, quote.rate if quote else None,
                              unit)
        reason = (f"FANCY on paper: {_fancy_text(session.team, session.kind, session.number, line, side)}, {units:g}u; "
                  f"settled from Cricbuzz")
        if quote:
            reason += (f"; site {quote.name}: {side.upper()} {quote.line:g} @{quote.rate:g}, other side "
                       f"{quote.other_line:g}{', ball running' if quote.closed else ''}"
                       f"{'; ' + quote.slip if quote.slip else ''}")
        elif self._bot is not None and not toss:
            reason += "; site line not found"
        await self._record(message_id, sig, BetResult("paper", reason), Decision(True, reason))

    async def _fancy_quote(self, sig: BetSignal, session: Session, side: str):
        """What the site offers for this tip now. A tip stays on paper whatever happens on the site."""
        if self._bot is None:
            return None
        try:
            async with self._trade_lock(_priority(sig)):
                return await self._bot.fancy_quote(sig.event, session, side)
        except Exception:
            log.warning("  fancy: could not read the site's line", exc_info=True)
            return None

    async def long_hold_settler(self) -> None:
        """Every LONG_HOLD_POLL, for channels whose trades can run for days (see _settle_long_holds)."""
        if not any(self._risk.cfg(c.name).both_sides_in_match for c in self._channels.all()):
            return
        while True:
            await asyncio.sleep(LONG_HOLD_POLL.total_seconds())
            try:
                await self._settle_long_holds()
            except Exception:
                log.warning("long holds: Cricbuzz could not be read this time", exc_info=True)

    async def _settle_long_holds(self) -> None:
        """Nothing watches a trade after POSITION_WINDOW, and a Test match runs for days: its result comes from Cricbuzz
        once the match is over. An order still waiting by then was never matched: no bet, nothing to count."""
        trades = [p for p in (*self._store.old_open_positions(self._mode), *self._store.unsettled(self._mode))
                  if self._risk.cfg(p).both_sides_in_match]
        orders = [p for p in self._store.pending_instructions(self._mode)
                  if p[2].when_odds and self._risk.cfg(p[2]).both_sides_in_match]
        if not trades and not orders:
            return
        matches = await self._cricbuzz.refresh_list()
        for pos in trades:
            match = finished_match(matches, pos.selection, pos.event, datetime.fromisoformat(pos.opened_at))
            pl = _long_hold_pl(pos, match.status) if match else None
            if pl is None:
                continue
            self._store.set_closed(pos.id, pl=pl, how="settled")
            log.info("HELD TRADE settled: %s %s @ %g -> %s (%+g)", pos.channel, pos.selection, pos.price, match.status, pl)
            if self._notifier:
                await self._notifier.send(f"📘 *HELD TRADE {'WON ✅' if pl > 0 else 'LOST ❌' if pl < 0 else 'VOID'}* (paper)\n"
                                          f"[{pos.channel}] {pos.selection} @ {pos.price:g}, stake {pos.stake:g}\n"
                                          f"{match.status}\n{pl:+g}")
        for pending_id, created_at, sig, message_id, _ in orders:
            match = finished_match(matches, sig.selection, sig.event, datetime.fromisoformat(created_at))
            if match:
                self._store.remove_pending(pending_id)
                reason = f"order @ {sig.when_odds:g} never matched, no bet: {match.status}"
                await self._record(message_id, sig, BetResult("skipped", reason), Decision(False, reason))

    async def fancy_settler(self) -> None:
        """Every FANCY_POLL while fancy tips are open: finds each tip's match on Cricbuzz, reads its latest overs and
        settles what is known. A tip still open after FANCY_GIVE_UP is closed as 'unknown' (no result found)."""
        if not any(c.profile.fancy_paper for c in self._channels.all()):
            return
        while True:
            await asyncio.sleep(FANCY_POLL.total_seconds())
            try:
                await self._settle_fancy(self._cricbuzz)
            except Exception:
                log.warning("fancy tips: Cricbuzz could not be read this time", exc_info=True)

    async def _settle_fancy(self, cricbuzz: Cricbuzz) -> None:
        tips = self._store.open_fancy(self._mode)
        if not tips:
            return
        matches = {m.match_id: m for m in await cricbuzz.refresh_list()}
        for tip in tips:
            if tip["match_id"]:
                if tip["match_id"] not in matches:  # finished and off the live page: from its scorecard
                    match = await cricbuzz.match_by_id(tip["match_id"])
                    if match:
                        matches[match.match_id] = match
                continue
            found = await self._match_of_tip(tip, matches, cricbuzz)
            if found:
                tip["match_id"] = found.match_id
                self._store.set_fancy_match(tip["id"], found.match_id, " v ".join(name for name, _ in found.teams))
        now = datetime.now()
        for match_id in {t["match_id"] for t in tips if t["match_id"] in matches}:
            match = matches[match_id]
            if match.start - BEFORE_THE_TOSS > now:
                continue
            sessions = [t for t in tips if t["match_id"] == match_id and t["kind"] in ("runs", "wicket")]
            try:
                if sessions:
                    await cricbuzz.refresh_overs(match)
                await cricbuzz.refresh_scorecard(match)
                if any(_fancy_result(t, match)[0] is None for t in sessions):
                    await cricbuzz.refresh_commentary(match)  # overs bowled before the bot read the match (or a restart)
            except Exception:  # one match's pages must not keep the others from being settled
                log.warning("fancy tips: match %s could not be read this time", match_id, exc_info=True)
        for tip in tips:
            match = matches.get(tip["match_id"])
            won, note = _fancy_result(tip, match) if match else (None, "")
            what = f"[{tip['channel']}] " + _fancy_text(tip["team"], tip["kind"], tip["number"], tip["line"], tip["side"])
            if won is None:
                void = match is not None and re.search(r"abandon|no result", f"{match.state} {match.status}", re.IGNORECASE)
                if void or now - datetime.fromisoformat(tip["created_at"]) > FANCY_GIVE_UP:
                    outcome = "void" if void else "unknown"
                    note = note or ("match abandoned" if void else
                                    "match not found on Cricbuzz" if match is None else "result not readable")
                    self._store.settle_fancy(tip["id"], outcome, 0.0, note)
                    log.info("FANCY %s: %s (%s)", outcome.upper(), what, note)
                continue
            rate = tip["rate"] or 100
            pl = round(tip["units"] * rate / 100, 4) if won else -tip["units"]
            site_pl = _site_pl(tip, match)
            self._store.settle_fancy(tip["id"], "won" if won else "lost", pl, note, site_pl)
            log.info("FANCY %s: %s -> %s (%+g units, at the site's line %s)", "WON" if won else "LOST", what, note, pl,
                     "?" if site_pl is None else f"{site_pl:+g}")
            if self._notifier:
                at_site = "" if site_pl is None else \
                    f"\nAt the site's line {tip['site_line']:g} @{tip['site_rate']:g}: {site_pl:+g} units"
                await self._notifier.send(f"📐 *FANCY {'WON ✅' if won else 'LOST ❌'}* (paper)\n{what}\n"
                                          f"Result: {note}\n{pl:+g} units{at_site}")

    async def _match_of_tip(self, tip: dict, matches: dict[int, Match], cricbuzz: Cricbuzz) -> Match | None:
        """The Cricbuzz match of a fancy tip. A toss call: the team's next match. A player's line: the match the tip
        names, else the limited-overs match on now whose scorecard has the player. A session: by its batting side."""
        created = datetime.fromisoformat(tip["created_at"])
        team = tip["team"]
        if tip["kind"] == "toss":
            found = [m for m in matches.values() if m.has_team(team) and created - timedelta(hours=1) <= m.start <= created + TOSS_AHEAD]
            return min(found, key=lambda m: (-m.team_score(team), m.start), default=None)
        if tip["kind"] in PLAYER_KINDS:
            near = [m for m in matches.values() if m.start - PLAYER_AHEAD <= created <= m.start + timedelta(hours=14)]
            sides = [side for side in tip["event"].split(" v ") if side.strip(" ?")]
            named = [m for m in near if sides and all(m.has_team(side) for side in sides)]
            if named:
                return min(named, key=lambda m: abs(m.start - created))
            for match in near:
                if match.fmt.upper() != "TEST" and match.state != "Preview" and created <= match.start + timedelta(hours=6):
                    await cricbuzz.refresh_scorecard(match)
                    if any(same_batter(team, name) for name in match.batters):
                        return match
            return None
        found = [m for m in matches.values() if m.has_team(team)
                 and m.start - timedelta(hours=1) <= created <= m.start + timedelta(hours=14)]
        # the side named best ('NZ' is New Zealand, not New Zealand A), a limited-overs match (sessions are
        # T20 / ODI calls), one being played
        found.sort(key=lambda m: (m.team_score(team), m.fmt.upper() not in ("TEST", ""),
                                  m.state not in ("Complete", "Preview")), reverse=True)
        return found[0] if found else None

    async def league_namer(self) -> None:
        """At the start and every LEAGUE_POLL: names the league or tournament of the trades and fancy tips that have
        none yet."""
        while True:
            try:
                rows = self._store.trades_without_league(self._mode, LEDGER_START)
                if rows:
                    for trade_id, league in (await leagues_of(rows, datetime.now())).items():
                        self._store.set_league(trade_id, league)
                tips = self._store.fancy_without_league(self._mode, LEDGER_START)
                if tips:
                    for tip_id, league in (await leagues_of(tips, datetime.now())).items():
                        self._store.set_fancy_league(tip_id, league)
            except Exception:
                log.warning("leagues: Cricbuzz or ESPN could not be read this time", exc_info=True)
            await asyncio.sleep(LEAGUE_POLL.total_seconds())

    def _is_running(self, channel: str) -> bool:
        """A removed channel's trades stay in the ledger, but the reports leave them out."""
        return channel in (MANUAL, "", "-") or self._channels.get(channel) is not None

    def _results(self, since: datetime, until: datetime = datetime.max) -> list[dict]:
        """The running channels' trades and fancy bets in [since, until), with their sport and league named, and unit, the
        rupees of 1 limit each was opened with. A fancy bet counts at the site's line when it was read then, else at the
        tip's; a void one not at all."""
        since = max(since, LEDGER_START)
        rows = [{**r, "sport": sport_of(r["sport"], r["league"]), "league": r["league"] or "Not named yet",
                 "unit": r["unit"] or self._s.unit_stake} for r in self._store.results(self._mode, since, until)]
        for tip in self._store.fancy_results(self._mode, since, until):
            if tip["outcome"] == "void":
                continue
            known = tip["outcome"] in ("won", "lost")
            unit = tip["unit"] or self._s.unit_stake
            stake = tip["units"] * unit
            rows.append({"channel": tip["channel"], "sport": "Toss" if tip["kind"] == "toss" else "Fancy", "unit": unit,
                         "league": tip["league"] or "Not named yet", "stake": stake, "risk": stake,
                         "closed": tip["outcome"] is not None, "measured": tip["site_pl"] is not None,
                         "tip_pl": tip["pl"] * unit if known else None,
                         "pl": (tip["site_pl"] if tip["site_pl"] is not None else tip["pl"]) * unit if known else None})
        return [r for r in rows if self._is_running(r["channel"])]

    def _limits_text(self) -> str:
        """Match trades, fancy, toss, tennis and all in limits, over today, this week, this month and this year."""
        periods = {period: self._results(period_start(period)) for period in PERIODS}

        def line(title: str, sport: str = "") -> str:
            return title + ": " + " · ".join(
                f"{period} {sum((r['pl'] or 0) / r['unit'] for r in rows if sport in ('', r['sport'])):+.2f}"
                for period, rows in periods.items())

        return "\n".join(["📈 *Limits* (/report week: channels and leagues)", line("🏏 Match", "Cricket"),
                          line("📐 Fancy", "Fancy"), line("🪙 Toss", "Toss"), line("🎾 Tennis", "Tennis"), line("All")])

    async def daily_report(self) -> None:
        """At DAILY_REPORT_AT every day: the day's trades of each channel in one message."""
        if not (self._notifier and self._s.daily_report_at):
            return
        while True:
            await asyncio.sleep(_seconds_until(self._s.daily_report_at))
            await self._notifier.send(self.day_report_text())
            await asyncio.sleep(61)  # past the minute, so it is sent once

    def day_report_text(self, day: date | None = None) -> str:
        day = day or datetime.now().date()
        start = datetime.combine(day, datetime.min.time())
        end = start + timedelta(days=1)
        channels: dict[str, dict] = {}

        def entry(name: str) -> dict:
            return channels.setdefault(name, {"name": name or "-", "trades": [], "missed": [],
                                              "deleted": 0, "deleted_traded": 0, "fancy": []})

        for name, selection, price, stake, closed, pl, how, unit in self._store.day_trades(self._mode, start, end):
            c = entry(name)
            unit = unit or self._s.unit_stake
            state = "open" if not closed else ("ended" if pl is None else
                                               ("booked" if how in ("booked", None) else ("won" if pl > 0 else "lost")))
            c["trades"].append({"selection": selection, "unit": unit, "units": round(stake / unit, 2),
                                "price": round(price, 2) if price else price,
                                "state": state, "pl": None if pl is None else pl / unit})
        for name, description, reason in self._store.day_misses(self._mode, start, end):
            tag = re.match(r"\[([^\]]+)\]", description or "")
            name = name or (tag[1] if tag else "")
            if (reason or "").startswith(("duplicate", "already")):
                continue  # the same tip again, not a missed one
            what = re.sub(r"^\[[^\]]+\]\s*", "", description or "").split(" [")[0]
            entry(name or "-")["missed"].append(f"{what}: {(reason or '').replace('[live would ask: ', '[')[:90]}")
        for name, (n, traded) in self._store.day_deleted(start, end).items():
            c = entry(name)
            c["deleted"], c["deleted_traded"] = n, traded
        for name, team, kind, number, line, side, outcome, pl, note, site_pl, unit in self._store.day_fancy(self._mode, start, end):
            entry(name)["fancy"].append({"text": _fancy_text(team, kind, number, line, side), "outcome": outcome, "pl": pl,
                                         "note": note or "", "site_pl": site_pl, "unit": unit or self._s.unit_stake})
        week: dict[str, float] = {}
        for r in self._results(period_start("week")):
            week[r["channel"] or "-"] = round(week.get(r["channel"] or "-", 0) + (r["pl"] or 0) / r["unit"], 2)
        mode = "paper" if self._mode != "live" else "LIVE"
        title = f"{day:%a %d %b} ({mode})"
        shown = [c for c in channels.values() if self._is_running(c["name"]) and (c["name"] != MANUAL or c["trades"])]
        report = day_report(title, shown, week, self._limits_text())
        return f"{report}\n\n{self._jackpot_text()}" if self._store.is_jackpot_day(day) else report

    def status_text(self) -> str:
        counts = self._store.today_counts(self._mode)
        entries = counts.get(("entry", "placed"), 0) + counts.get(("entry", "simulated"), 0)
        booked = counts.get(("close", "placed"), 0) + counts.get(("close", "simulated"), 0)
        skipped = sum(n for (_, status), n in counts.items() if status == "skipped")
        failed = sum(n for (_, status), n in counts.items() if status == "failed")
        open_trades = self._store.unclosed_positions(self._mode)
        trades = "\n".join(f"• {p.side.upper()} {p.selection} @ {p.price or '?'}, stake {p.stake:g}"
                           + (f" [{p.channel}]" if p.channel else "")
                           + (" (booking…)" if p.close_pending_since else "") for p in open_trades) or "none"
        today = self._store.period_result(self._mode, period_start("day"))
        state = "⏸ PAUSED (no new entries)" if self._paused else "running"
        stop = self._risk.stop_reason()
        ladders = self._ladders_text()
        stopped = f"⏹ Stopped: {', '.join(sorted(self._stopped))}\n" if self._stopped else ""
        return (f"✅ *Bot {state}* ({self._mode.replace('_', ' ')}), {_clock()}\n"
                f"1 limit = ₹{self._limit_now():,.0f}\n{stopped}"
                f"Today: {entries} entries, {booked} booked, {skipped} skipped, {failed} failed, "
                f"result {today:+g}\n" + (f"⛔ {stop}\n" if stop else "") + f"Open trades:\n{trades}"
                + (f"\n{ladders}" if ladders else ""))

    async def check_finished_matches(self) -> None:
        """A trade's match missing from the site's list twice in a row has finished: the site settles it."""
        trades = self._store.unclosed_positions(self._mode)
        if not trades:
            return
        names = await self._bot.listed_matches()
        if names is None:
            return
        async with self._trade_lock(TradeLock.OTHER):
            for pos in trades:
                if _best_index(names, pos.event.replace(" v ?", ""), pos.selection) is not None:
                    self._missing.pop(pos.id, None)
                    continue
                self._missing[pos.id] = self._missing.get(pos.id, 0) + 1
                if self._missing[pos.id] < 2:
                    continue
                await self._finish(pos)

    async def _finish(self, pos: Position) -> None:
        """The trade's match is over without a booking: closed, and settled from the last price seen."""
        self._store.set_closed(pos.id, how="finished")
        self._missing.pop(pos.id, None)
        self._no_price.pop(pos.id, None)
        log.info("MATCH FINISHED: %s (%s trade was not booked)", pos.event, pos.selection)
        last = self._last_price.pop(pos.id, None)
        outcome = result_from_price(pos, last)
        if outcome:
            note = f"\nSettled as a {outcome.upper()} from its last price {last:g}: {self._settle(pos.id, outcome)}" \
                   f" Wrong? /settle {pos.id} win|lose"
        else:
            note = f"\nLast price {last:g}: result unclear." if last else ""
            note += f"\nRecord its result: /settle {pos.id} win|lose"
        log.info("  %s", note.strip().replace("\n", " | "))
        if self._notifier:
            await self._notifier.send(match_finished(pos, _clock()) + note)

    async def edited(self, chat_id: int, tg_id: int, text: str) -> None:
        """The channel changed a message's words: kept and logged, not acted on. A tip's first words are what it said
        when it could still be taken; a later edit can only tell what was changed."""
        channel = self._channels.for_chat(chat_id)
        if channel is None:
            return
        first = self._store.mark_edited(channel.name, tg_id, text)
        if first is not None:
            log.info("EDITED by %s: msg %s %r -> %r", channel.name, tg_id, first[:150], text[:150])

    async def deleted(self, chat_id: int, tg_ids: list[int]) -> None:
        """The channel deleted messages: recorded, and you are told which of them the bot had acted on."""
        channel = self._channels.for_chat(chat_id)
        if channel is None:
            return
        found = self._store.mark_deleted(channel.name, tg_ids)
        if not found:
            return
        lines = []
        for tg_id, received, text in found:
            did = self._store.decisions_on(channel.name, tg_id)
            lines.append(f"• {received[11:16]} {text[:80]!r}" + (f" → bot: {', '.join(did)}" if did else ""))
            log.info("DELETED by %s: msg %s [%s] %r%s", channel.name, tg_id, received[11:16], text[:80],
                     f" (bot: {', '.join(did)})" if did else "")
        if self._notifier:
            await self._notifier.send(f"🗑 *{channel.name} deleted {len(found)} message(s)*\n" + "\n".join(lines))

    # --- Telegram ---------------------------------------------------------------------------------

    async def _report_links(self, msg: IncomingMessage, channel: Channel) -> None:
        links = GROUP_LINK.findall(msg.text)
        if not (links and self._notifier):
            return
        follows = [f"{c.name} (title /{c.title_regex}/)" for c in self._channels.all() if c.title_regex]
        await self._notifier.send(
            f"🔗 *{channel.name}* posted a group link: {' '.join(links)}\nJoin it in Telegram. "
            + (f"It is followed by itself if its title matches: {', '.join(follows)}. " if follows else "")
            + "Otherwise send /follow <chat id> <channel> (ids: python -m tools.telegram_setup).")

    async def new_chat(self, channel: Channel, chat_id: int, title: str) -> None:
        if self._notifier:
            await self._notifier.send(f"🆕 Now following *{title}* ({chat_id}) as {channel.name}: its title matches "
                                      f"channels.json. Not wanted? /unfollow {chat_id}")

    async def command(self, text: str) -> str:
        """A command you sent to your Saved Messages; returns the reply."""
        words = text.strip().split()
        name, args = words[0].lower().lstrip("/"), words[1:]
        try:
            if name == "help" or (name == "start" and not args):
                return HELP
            if name == "status":
                return self.status_text()
            if name == "limit":
                return self._limit_command(args)
            if name == "stop":
                return self._stop_command(" ".join(args))
            if name == "start":
                return self._start_command(" ".join(args))
            if name == "pause":
                return self._set_paused(True)
            if name == "resume":
                return self._set_paused(False)
            if name in ("yes", "no"):
                return await self._answer(name == "yes", int(args[0]))
            if name == "report":
                return self._report(args[0] if args else "day")
            if name == "today":
                return self.day_report_text()
            if name == "yesterday":
                return self.day_report_text(datetime.now().date() - timedelta(days=1))
            if name == "settle":
                return self._settle(int(args[0]), args[1])
            if name == "sk":
                return await self._sk_command(" ".join(args))
            if name == "jackpot":
                return self._jackpot_command(args)
            if name == "channels":
                return self._channels_text()
            if name == "follow":
                chat_id, channel = int(args[0]), " ".join(args[1:])
                self._channels.follow(chat_id, channel)
                self._store.follow(chat_id, channel, "")
                return f"✅ Following {chat_id} as {channel} (restart not needed)."
            if name == "unfollow":
                channel = self._channels.unfollow(int(args[0]))
                self._store.unfollow(int(args[0]))
                return f"✅ Not following {args[0]} any more" + (f" (was {channel.name})." if channel else ".")
        except (IndexError, ValueError) as exc:
            return f"⚠️ {exc or 'missing value'}. /help shows the commands."
        signals = parse_strict(text)
        if not signals:
            return "⚠️ Unknown command. /help shows the commands."
        replies = []
        for sig in signals:
            sig = as_bet(sig, MANUAL)
            result = await self.execute(sig)
            replies.append(f"{result.status.upper()}: {result.reason}")
        return "\n".join(replies)

    def _limit_now(self) -> float:
        return self._channels.settings_for(MANUAL).unit_stake

    def _limit_text(self) -> str:
        unit = self._limit_now()
        worst = sum(high - 1 for _, high in STEPS) * unit
        return (f"A tip's 1 limit = ₹{unit:,.0f}, its 2 limits = ₹{2 * unit:,.0f}. SK method: {len(STEPS)} entries of "
                f"₹{unit:,.0f} in a match, at most about -₹{worst:,.0f}.\nNew trades only: a trade already open keeps its own. "
                f"({self._mode.replace('_', ' ')})")

    def _limit_command(self, args: list[str]) -> str:
        """/limit: what 1 limit is. /limit 100: that many rupees from now on, also after a restart."""
        before = self._limit_now()
        if not args:
            return f"💰 *1 limit = ₹{before:,.0f}*\n{self._limit_text()}"
        amount = "".join(args).lstrip("₹").replace(",", "")
        if not amount.isdigit() or not MIN_LIMIT <= int(amount) <= MAX_LIMIT:
            raise ValueError(f"a limit is a whole amount from ₹{MIN_LIMIT} to ₹{MAX_LIMIT:,}, like /limit 100; it stays ₹{before:,.0f}")
        self._channels.limit = float(amount)
        self._store.set_setting("limit", str(int(amount)))
        log.info("LIMIT: 1 limit = %s (was %g)", amount, before)
        return f"💰 *1 limit = ₹{int(amount):,}* (was ₹{before:,.0f})\n{self._limit_text()}"

    def _entries_off(self, channel: str) -> str:
        """Why this channel can't open anything new ('' = it can). /pause stands over /start: one channel can't be let in
        while all are stopped."""
        if self._paused:
            return "PAUSED: no new entries until /resume"
        if channel in self._stopped:
            return f"STOPPED: no new entries from {channel} until /start {channel}"
        return ""

    def _ladder_entries_off(self, ladder: Ladder) -> str:
        """A tip two channels gave goes on while either of them isn't stopped."""
        return "" if ladder.confirmed_by and not self._entries_off(ladder.confirmed_by) else self._entries_off(ladder.channel)

    def _set_paused(self, paused: bool) -> str:
        self._paused = paused
        self._store.set_setting("paused", "1" if paused else "0")
        if paused:
            return "⏸ Paused: no new entries. Bookings, loss cuts and waiting retries still work. /resume to go on."
        return "▶️ Resumed: new entries allowed." + (f" Still stopped: {', '.join(sorted(self._stopped))}." if self._stopped else "")

    def _channel_named(self, text: str) -> Channel:
        found = self._channels.named(text)
        if len(found) == 1:
            return found[0]
        names = ", ".join(c.name for c in found or self._channels.all())
        raise ValueError(f"'{text}' could be {names}" if found else f"no channel '{text}'; the channels are {names}")

    def _stop_command(self, text: str) -> str:
        """/stop <channel>: no new entries from it, also after a restart. Its open trades stay and its bookset posts still
        book them; its tips waiting for a first entry are dropped, unless another channel gave them too."""
        if not text:
            return (f"⏹ Stopped: {', '.join(sorted(self._stopped))}. /start <channel> to go on." if self._stopped else
                    f"Nothing is stopped. /stop <channel>: {', '.join(c.name for c in self._channels.all())}")
        if text.lower() == "all":
            return self._set_paused(True)
        channel = self._channel_named(text)
        if channel.name in self._stopped:
            return f"⏹ {channel.name} is already stopped. /start {channel.name} to go on."
        self._stopped.add(channel.name)
        self._store.set_setting("stopped", json.dumps(sorted(self._stopped)))
        waiting = [ladder for ladder in self._store.active_ladders(self._mode) if ladder.channel == channel.name
                   and not ladder.entries and (not ladder.confirmed_by or ladder.confirmed_by in self._stopped)]
        for ladder in waiting:
            self._store.end_ladder(ladder.id, f"{channel.name} was stopped")
        still = [p.selection for p in self._store.unclosed_positions(self._mode) if p.channel == channel.name]
        log.info("STOPPED %s: %d waiting tips dropped, %d trades stay open", channel.name, len(waiting), len(still))
        return "\n".join([f"⏹ *{channel.name} stopped*: no new entries from it. /start {channel.name} to go on.",
                          *([f"{len(waiting)} tip(s) waiting for an entry dropped: {', '.join(ladder.team for ladder in waiting)}."] if waiting else []),
                          *([f"Open trades stay, its booksets still work: {', '.join(still)}."] if still else [])])

    def _start_command(self, text: str) -> str:
        if text.lower() == "all":
            return self._set_paused(False)
        channel = self._channel_named(text)
        self._stopped.discard(channel.name)
        self._store.set_setting("stopped", json.dumps(sorted(self._stopped)))
        log.info("STARTED %s", channel.name)
        return f"▶️ *{channel.name} started*: its new tips are followed." + ("\n⏸ But all entries are paused: /resume." if self._paused else "")

    async def _answer(self, yes: bool, number: int) -> str:
        asked = self._asked.pop(number, None)
        if asked is None:
            return f"⚠️ No open question #{number} (answered, or dropped after {self._s.confirm_timeout_s}s)."
        if not yes:
            reason = f"CONFIRM #{number}: you said no"
            await self._record(asked.message_id, asked.sig, BetResult("skipped", reason), Decision(False, reason))
            return f"❌ #{number} dropped."
        result = await self.execute(asked.sig, asked.message_id)
        return f"#{number} {result.status.upper()}: {result.reason}"

    def _report(self, period: str) -> str:
        if period in PERIODS:
            start, until, name = period_start(period), datetime.max, PERIODS[period]
        elif dates := _date_range(period):
            start, until, name = *dates, period
        else:
            raise ValueError("period must be day, week, month, year, or a date like 2026-10 or 2026-10-29")
        since = f" from {LEDGER_START:%d %b %H:%M}" if start < LEDGER_START else ""
        unsettled = [p for p in self._store.unsettled(self._mode)
                     if self._is_running(p.channel) and p.opened_at >= LEDGER_START.isoformat()]
        return results_report(f"({name}{since}, {self._mode})", [c.name for c in self._channels.all()],
                              self._results(start, until), self._limit_now(), unsettled)

    def _settle(self, position_id: int, outcome: str) -> str:
        pos = self._store.position(position_id)
        if pos is None:
            return f"⚠️ No trade {position_id}."
        if outcome.lower() == "win":
            pl = pos.stake * (pos.price - 1) if pos.side == "back" else pos.stake
        elif outcome.lower() == "lose":
            pl = -pos.liability
        else:
            pl = float(outcome)
        if not self._store.settle(position_id, round(pl, 2)):
            return f"⚠️ Trade {position_id} is still open: book it or wait for the match to end."
        return f"✅ Trade {position_id} ({pos.selection}) settled at {pl:+.2f}."

    def _channels_text(self) -> str:
        lines = [f"📡 *Channels*, 1 limit = ₹{self._limit_now():,.0f}"]
        for channel in self._channels.all():
            ids = ", ".join(str(i) for i in sorted(channel.ids)) or "none yet"
            extra = f", title /{channel.title_regex}/" if channel.title_regex else ""
            stopped = " ⏹ STOPPED" if channel.name in self._stopped else ""
            lines.append(f"• {channel.name} ({channel.profile.name}{'' if channel.profile.trade else ', watch only'})"
                         f": {ids}{extra}{stopped}")
        return "\n".join(lines)

    # --- helpers ---------------------------------------------------------------------------------

    async def _parse_while_fresh(self, msg: IncomingMessage, context: list[str], hint: str = "") -> ParsedMessage | None:
        """Retries the AI through network drops (and TLS errors, an OSError) for as long as the tip is fresh."""
        extra = {"hint": hint} if hint else {}
        while True:
            try:
                return await self._parser.parse(msg.text, msg.image, context, msg.reply_to, **extra)
            except (APIConnectionError, OSError):
                age = (datetime.now(timezone.utc) - msg.date).total_seconds()
                if age > self._s.signal_max_age_s:
                    log.error("msg %s MISSED: could not reach the AI for %.0fs (network down?): %r",
                              msg.id, age, msg.text[:80])
                    if self._notifier:
                        await self._notifier.send(f"❌ *TIP MISSED* (AI unreachable for {age:.0f}s)\n{msg.text[:200]}")
                    return None
                log.warning("msg %s: AI unreachable, retrying (%.0fs old)", msg.id, age)
                await asyncio.sleep(3)
            except RateLimitError as exc:  # a free tier's daily limit used up
                log.error("msg %s MISSED: AI limit reached (%s): %r", msg.id, str(exc)[:200], msg.text[:80])
                self._missed_by_limit += 1
                now = datetime.now()
                if self._notifier and now - self._last_limit_alert >= LIMIT_ALERT_EVERY:
                    await self._notifier.send(
                        f"❌ *{self._missed_by_limit} message(s) not read*: the AI's free limit is used up "
                        f"(latest: {msg.text[:120]!r}). Reading goes on as the limit frees up.")
                    self._last_limit_alert, self._missed_by_limit = now, 0
                return None

    def _simulate(self, sig: BetSignal, check: Decision) -> BetResult:
        """Signals-only mode has no site, so entries are checked against the tipster's own price."""
        if sig.action == "entry":
            check = self._risk.price_check(sig, None)
        if not check.ok:
            return BetResult("skipped", check.reason)
        if sig.action == "close":
            return BetResult("simulated", "signals only: would cash out (book set) on the site")
        what = f"would {check.hedge_side} {check.stake:g} (book set)" if sig.action == "exit" else "nothing sent"
        return BetResult("simulated", f"signals only, {what}", sig.odds or sig.when_odds, check.stake, check.liability)

    async def _record(self, message_id: int | None, sig: BetSignal, result: BetResult, check: Decision) -> None:
        self._store.log_bet(
            message_id, sig, self._mode, result.status, result.reason, result.price, result.stake, result.liability
        )
        if sig.action == "entry" and sig.market != "fancy" and not result.reason.startswith("TOP-UP"):
            # under the site's own names when it gave them, so booksets and price checks find the trade later
            names = {k: v for k, v in (("selection", result.name), ("event", result.event)) if v}
            entry = sig.model_copy(update=names)
            self._last_entry[sig.channel] = (entry, result.status)
            self._entries.setdefault(sig.channel, deque(maxlen=10)).append((entry, result.status))
            if result.status in COUNTED:
                self._store.open_position(entry, self._mode, result.price, result.stake, self._risk.cfg(sig).unit_stake)
        elif sig.action == "exit" and result.status in COUNTED:
            self._store.set_exit(check.position.id, sig.odds, result.stake, result.status)
        elif sig.action == "close" and result.status in ("placed", "simulated") and check.position:
            self._store.set_closed(check.position.id, pl=result.pl, how="booked")
            if self._notifier:
                how = "auto-bookset" if "auto-bookset" in result.reason else "bookset / cashout"
                await self._notifier.send(trade_summary(check.position, how, result.pl, _clock()))
        elif sig.action == "close" and result.status == "unconfirmed":
            log.warning("  cashout not confirmed: check the %s trade on the site", sig.selection)
        price = f"{result.price:g}" if result.price else "-"
        log.info("  %s %s | price %s stake %g | %s", result.status.upper(), sig.describe(), price, result.stake,
                 result.reason)
        if self._notifier:
            await self._notifier.send(
                bet_alert(self._mode, sig.action, result.status, sig.describe(), result.price, result.stake,
                          result.reason, _clock()),
                result.screenshot,
            )


async def run(signals_only: bool) -> None:
    settings = load_settings()
    channels = Channels.load(settings)
    needed = [*TELEGRAM_FIELDS, "groq_api_key" if settings.ai_provider == "groq" else "openai_api_key"]
    settings.require(*needed, *(["tg_chat"] if channels.single else []))
    if not signals_only:
        settings.require(*SITE_FIELDS)
        if is_skyfair(settings.site_url):
            raise SystemExit(
                "sky-fair's betting steps (match list, match page, bet slip) are not built yet: they need its\n"
                "logged-in pages. Save them with:  python -m tools.snapshot_page\n"
                "Meanwhile `python main.py --signals-only` reads the tips and logs what it would bet.")

    mode = "signals_only" if signals_only else ("dry_run" if settings.dry_run else "live")
    store = Store(DATA_DIR / "trades.db")
    risk = RiskManager(settings, store, mode, channels)
    bot = None if signals_only else ExchangeBot(settings, risk)

    transcriber = None
    if channels.any_voice():
        if (settings.voice_provider or settings.ai_provider) == "groq" and settings.groq_api_key:
            transcriber = Transcriber(settings.groq_api_key, groq=True)
        elif settings.openai_api_key:
            transcriber = Transcriber(settings.openai_api_key, settings.transcribe_model)
        else:
            log.warning("voice notes need OPENAI_API_KEY (or GROQ_API_KEY with AI_PROVIDER=groq): they will be "
                        "logged without their words")

    notifier = Notifier(settings.alert_chat, settings.alerts)
    pipeline = Pipeline(settings, mode, bot, risk, store, notifier, channels)
    log.info("Mode: %s | 1 unit = %g | max units/bet %s | odds tolerance %g%% | channels: %s", mode,
             pipeline._limit_now(), settings.max_units_per_bet or "off", settings.odds_tolerance * 100,
             ", ".join(c.name for c in channels.all()) + (" (TELEGRAM_CHAT, no channels.json)" if channels.single else ""))
    if mode == "live":
        log.warning("LIVE MODE: real bets will be placed")
    if bot:
        bot.resolve_name = pipeline._parser.resolve_name  # 'Tallon' -> the site's 'T Griekspoor'
    log.info("AI: %s | alerts to %s: %s", pipeline._parser.name, settings.alert_chat, "on" if settings.alerts else "off")

    def connected(client) -> None:
        notifier.attach(client)
        asyncio.get_running_loop().create_task(
            notifier.send(f"🤖 *Bot started* ({mode.replace('_', ' ')}), site {settings.site_url}. /help for commands"))

    async def on_command(text: str) -> None:
        reply = await pipeline.command(text)
        if reply:
            await notifier.send(reply)

    try:
        if bot:
            await bot.start()
        await asyncio.gather(
            listen(settings, channels, store, pipeline.enqueue, pipeline.preload, CONTEXT_MESSAGES, connected,
                   on_command, pipeline.new_chat, transcriber, pipeline.deleted, pipeline.edited),
            pipeline.monitor(), pipeline.keep_alive(), pipeline.retry_pending_closes(),
            pipeline.heartbeat(), pipeline.daily_report(), pipeline.fancy_settler(), pipeline.long_hold_settler(),
            pipeline.ladder_watcher(), pipeline.league_namer(),
        )
    finally:
        if bot:
            await bot.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--signals-only", action="store_true", help="don't open the betting site; only parse and log")
    args = ap.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    _remove_old_screenshots()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        # 24/7: the log rolls over at 5 MB and keeps the last 10 files (~50 MB)
        handlers=[logging.StreamHandler(),
                  RotatingFileHandler(DATA_DIR / "bot.log", maxBytes=5_000_000, backupCount=10, encoding="utf-8")],
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args.signals_only))
    except KeyboardInterrupt:
        log.info("Stopped")


if __name__ == "__main__":
    main()
