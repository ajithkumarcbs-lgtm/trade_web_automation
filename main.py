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
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from typing import Awaitable

from openai import APIConnectionError

from tradebot.channels import MANUAL, Channel, Channels, apply_aliases
from tradebot.config import DATA_DIR, SITE_FIELDS, TELEGRAM_FIELDS, Settings, load_settings
from tradebot.exchange_bot import BetResult, ExchangeBot, _best_index
from tradebot.models import BetSignal, ParsedMessage, as_bet
from tradebot.notifier import Notifier, bet_alert, confirm_request, match_finished, report_text, trade_summary
from tradebot.risk import Decision, RiskManager, booked_profit
from tradebot.names import contains_player, same_name
from tradebot.rules import name_words, not_yet, parse_known, parse_strict, reconcile
from tradebot.skyfair import is_skyfair
from tradebot.signal_parser import CONTEXT_MESSAGES, CONTEXT_WINDOW, context_line, expand_team, make_parser
from tradebot.store import COUNTED, Store, period_start
from tradebot.telegram_listener import IncomingMessage, listen
from tradebot.voice import Transcriber

log = logging.getLogger("tradebot")


BOOKSET_RETRY_SECONDS = 10  # how often a refused bookset / loss cut is tried again
GREEN_MONEY_WITHIN = timedelta(hours=6)  # "green 20%" takes 20% of the channel's last bookset in this time
GROUP_LINK = re.compile(r"(?:https?://)?t\.me/(?:\+|joinchat/)[\w-]+", re.IGNORECASE)
SMALL_STAKE = re.compile(r"\b(?:small|chinna)\b", re.IGNORECASE)  # "try small amount"
HELP = """🤖 *Commands* (send them here, in Saved Messages)
/status: bot state, open trades, today's result
/report [day|week|month]: P/L per channel
/settle <trade id> win|lose|<amount>: result of a trade that ran to the end
/pause, /resume: stop / allow new entries (bookings always work)
/yes <n>, /no <n>: answer a CONFIRM question
/channels: chats followed; /follow <chat id> <channel>; /unfollow <chat id>
Trade by hand: /back Shelton 3.25 1u, /lay Sinner 1.55 0.5u, /back at Berrettini 3 1u,
/exit Shelton 1.6, /bookset Shelton, /losscut Shelton, /cancel Berrettini"""


def _clock() -> str:
    return datetime.now().strftime("%d %b %H:%M:%S")


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
        # One queue and reader per channel: channels are read side by side, each in the order its messages came.
        self._queues: dict[str, asyncio.Queue[asyncio.Future]] = {}
        self._readers: set[asyncio.Task] = set()
        self._history: dict[int, deque[tuple[datetime, str]]] = {}
        # Tips, retries and the auto-bookset check take turns (bookings first), so one trade can't be booked twice
        # and the browser does one thing at a time.
        self._trade_lock = TradeLock()
        self._missing: dict[int, int] = {}  # trade id -> times in a row its match was missing from the site
        self._paused = False
        self._asked: dict[int, Asked] = {}
        self._ask_number = 0
        self._held: dict[str, Held] = {}  # channel -> entry waiting for its stake message
        self._last_entry: dict[str, tuple[BetSignal, str]] = {}  # channel -> its latest entry and what became of it

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
        if not channel.profile.trade:
            return

        age = (datetime.now(timezone.utc) - msg.date).total_seconds()
        if age > self._risk.cfg(channel.name).signal_max_age_s:
            log.info("msg %s ignored: %.0fs old", msg.id, age)
            return

        signals, how, parsed = await self._read(msg, channel, context)
        if parsed is None:
            return
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
        if msg.voice and not profile.act_on_voice:
            # The transcript stays in the chat context, but a voice note alone is no instruction: transcribed
            # Tamil is too loose ("bookset-ku ready-aa irunga" = be ready for a bookset, read as "bookset").
            msg = replace(msg, text=msg.caption)
            if not msg.text and msg.image is None:
                return [], "voice", ParsedMessage(signals=[], summary="voice note (context only)")
        rule = parse_known(msg.text, profile.rules)
        if rule:  # the channel's short names ("kg" -> Knights), so the rule's name compares with the AI's
            rule = [s.model_copy(update={"selection": expand_team(apply_aliases(s.selection, profile.aliases))})
                    for s in rule]
        parsed = await self._parse_while_fresh(msg, context, profile.hint)
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
          the context) acts on the channel's latest entry, and on nothing when that entry was never placed."""
        profile = channel.profile
        kept = []
        for sig in signals:
            if (sig.action == "entry" and how == "ai-only" and profile.entry_words and msg.image is None
                    and not re.search(profile.entry_words, msg.text, re.IGNORECASE)):
                log.info("  not an entry: %r has none of the entry words of %s", msg.text[:60], channel.name)
                continue
            if (sig.action == "entry" and how == "ai-only" and profile.entry_words and msg.image is None
                    and not _mentions(msg.text, sig.selection, profile.aliases)):
                # "Now 8.4 odds back" (team in the next message), "Esl 6 odds back" read as England
                log.info("  not an entry: %r doesn't name %s (the AI took it from the context)", msg.text[:60],
                         sig.selection)
                continue
            if sig.action == "close" and not_yet(msg.text):
                log.info("  not a close yet: %r", msg.text[:60])
                continue
            if (sig.action == "close" and not _mentions(msg.text, sig.selection, profile.aliases)
                    and not _mentions(msg.reply_to, sig.selection, profile.aliases)):
                latest = self._last_entry.get(channel.name)
                if latest is not None:
                    entry, status = latest
                    if status not in (*COUNTED, "pending"):
                        log.info("  close: the latest entry of %s (%s) was not placed, nothing to book",
                                 channel.name, entry.selection)
                        continue
                    sig = sig.model_copy(update={"selection": entry.selection, "event": entry.event, "aka": ""})
            kept.append(sig)
        return kept

    async def _act(self, sig: BetSignal, text: str, channel: Channel, how: str, message_id: int | None) -> None:
        sig, problem = self._prepare(sig, text, channel)
        if problem:
            await self._record(message_id, sig, BetResult("skipped", problem), Decision(False, problem))
            return
        if sig.action == "stake":
            await self._stake_message(sig, channel, message_id)
            return
        if (sig.action == "entry" and how != "strict" and channel.profile.stake_wait_s and sig.units is None
                and sig.amount is None and not sig.green_pct and not sig.when_odds):
            await self._hold(sig, channel, how, message_id)
            return
        await self._go(sig, channel, how, message_id)

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
        names["event"] = " v ".join(expand_team(apply_aliases(side, profile.aliases)) for side in sig.event.split(" v "))
        sig = sig.model_copy(update=names)
        if sig.action != "entry":
            return sig, ""
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
        s = self._risk.cfg(channel.name)
        target = round(sig.amount if sig.amount else sig.units * s.unit_stake, 2)  # "Try 200 or 300": rupees
        extra = round(target - pos.stake, 2)
        if extra <= 0:
            return await skip(f"{pos.selection} already has stake {pos.stake:g} (>= {target:g})")
        if s.max_units_per_bet and target > s.max_units_per_bet * s.unit_stake:
            return await skip(f"top-up to {target:g} is above MAX_UNITS_PER_BET")
        add = BetSignal(action="entry", sport=pos.sport, event=pos.event, market=pos.market, market_name="",
                        selection=pos.selection, side=pos.side, odds=pos.price, line=None, units=None, amount=extra,
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
        if sig.action == "cancel":
            return await self._cancel(sig, message_id)
        async with self._trade_lock(_priority(sig)):
            if sig.action == "close" and self._cancel_pending_entry(sig):
                result = BetResult("skipped", "the tipster closed this trade before our entry could be placed: "
                                              "pending entry cancelled, nothing to book")
                await self._record(message_id, sig, result, Decision(False, result.reason))
                return result
            if sig.action == "entry" and self._paused:
                result = BetResult("skipped", "PAUSED: no new entries until /resume")
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
                    minutes = s.conditional_entry_min if sig.when_odds else self._s.entry_retry_min
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
        if self._store.find_position(sig.selection, self._mode, sig.channel):
            return False  # we are in the trade: book it
        pending_id = self._store.find_pending(sig.selection, self._mode, "entry")
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
            async with self._trade_lock(_priority(sig)):
                if pending_id not in {p[0] for p in self._store.pending_instructions(self._mode)}:
                    continue  # cancelled meanwhile (e.g. the tipster booked first)
                waited = (datetime.now() - datetime.fromisoformat(created_at)).total_seconds()
                minutes = self._risk.cfg(sig).conditional_entry_min if sig.when_odds else self._s.entry_retry_min
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
                await self._record(message_id, sig, result, check)

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
        """Books a trade automatically once booking it now would lock AUTO_BOOKSET_UNITS of profit
        (the setting of the trade's channel)."""
        if self._bot is None or not self._channels.any_auto_bookset():
            return
        log.info("Auto-bookset on, checked every %ds", self._s.monitor_interval_s)
        while True:
            await asyncio.sleep(self._s.monitor_interval_s)
            for pos in self._store.open_positions(self._mode):
                s = self._risk.cfg(pos)
                if not s.auto_bookset_units:
                    continue
                target = s.auto_bookset_units * s.unit_stake
                async with self._trade_lock(TradeLock.OTHER):  # a price check: tipsters' instructions go first
                    if pos.id not in {p.id for p in self._store.open_positions(self._mode)}:
                        continue  # booked by a tip meanwhile
                    price = await self._bot.exit_price(pos)
                    if price is None:
                        continue
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
        return (f"✅ *Bot {state}* ({self._mode.replace('_', ' ')}), {_clock()}\n"
                f"Today: {entries} entries, {booked} booked, {skipped} skipped, {failed} failed, "
                f"result {today:+g}\n" + (f"⛔ {stop}\n" if stop else "") + f"Open trades:\n{trades}")

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
                self._store.set_closed(pos.id, how="finished")
                self._missing.pop(pos.id, None)
                log.info("MATCH FINISHED: %s (%s trade was not booked)", pos.event, pos.selection)
                if self._notifier:
                    await self._notifier.send(match_finished(pos, _clock()) + f"\nRecord its result: /settle {pos.id} "
                                                                              f"win|lose")

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
            if name in ("help", "start"):
                return HELP
            if name == "status":
                return self.status_text()
            if name == "pause":
                self._paused = True
                return "⏸ Paused: no new entries. Bookings, loss cuts and waiting retries still work. /resume to go on."
            if name == "resume":
                self._paused = False
                return "▶️ Resumed: new entries allowed."
            if name in ("yes", "no"):
                return await self._answer(name == "yes", int(args[0]))
            if name == "report":
                return self._report(args[0] if args else "day")
            if name == "settle":
                return self._settle(int(args[0]), args[1])
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
        if period not in ("day", "week", "month"):
            raise ValueError("period must be day, week or month")
        rows = self._store.report(self._mode, period_start(period))
        title = f"({ {'day': 'today', 'week': 'this week', 'month': 'this month'}[period] }, {self._mode})"
        return report_text(title, rows, self._s.unit_stake, self._store.unsettled(self._mode))

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
        lines = ["📡 *Channels*"]
        for channel in self._channels.all():
            ids = ", ".join(str(i) for i in sorted(channel.ids)) or "none yet"
            extra = f", title /{channel.title_regex}/" if channel.title_regex else ""
            lines.append(f"• {channel.name} ({channel.profile.name}{'' if channel.profile.trade else ', watch only'})"
                         f": {ids}{extra}")
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
        if sig.action == "entry" and not result.reason.startswith("TOP-UP"):
            # under the site's own names when it gave them, so booksets and price checks find the trade later
            names = {k: v for k, v in (("selection", result.name), ("event", result.event)) if v}
            entry = sig.model_copy(update=names)
            self._last_entry[sig.channel] = (entry, result.status)
            if result.status in COUNTED:
                self._store.open_position(entry, self._mode, result.price, result.stake)
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
    log.info("Mode: %s | 1 unit = %g | max units/bet %s | odds tolerance %g%% | channels: %s", mode,
             settings.unit_stake, settings.max_units_per_bet or "off", settings.odds_tolerance * 100,
             ", ".join(c.name for c in channels.all()) + (" (TELEGRAM_CHAT, no channels.json)" if channels.single else ""))
    if mode == "live":
        log.warning("LIVE MODE: real bets will be placed")

    transcriber = None
    if channels.any_voice():
        if settings.openai_api_key:
            transcriber = Transcriber(settings.openai_api_key, settings.transcribe_model)
        else:
            log.warning("voice notes need OPENAI_API_KEY: they will be logged without their words")

    notifier = Notifier(settings.alert_chat, settings.alerts)
    pipeline = Pipeline(settings, mode, bot, risk, store, notifier, channels)
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
                   on_command, pipeline.new_chat, transcriber),
            pipeline.monitor(), pipeline.keep_alive(), pipeline.retry_pending_closes(),
            pipeline.heartbeat(),
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
