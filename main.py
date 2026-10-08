"""Telegram tips -> AI reads them -> safety checks -> bet on the exchange.

    python main.py --signals-only   # no browser: read, parse and log what it WOULD bet
    python main.py                  # full pipeline; DRY_RUN in .env decides whether Place Bet is clicked
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from collections import deque
from datetime import datetime, timezone

from openai import APIConnectionError

from tradebot.config import DATA_DIR, SITE_FIELDS, TELEGRAM_FIELDS, Settings, load_settings
from tradebot.exchange_bot import BetResult, ExchangeBot
from tradebot.models import BetSignal, ParsedMessage
from tradebot.risk import Decision, RiskManager
from tradebot.signal_parser import CONTEXT_MESSAGES, CONTEXT_WINDOW, SignalParser, context_line
from tradebot.store import COUNTED, Store
from tradebot.telegram_listener import IncomingMessage, listen

log = logging.getLogger("tradebot")


class Pipeline:
    def __init__(self, settings: Settings, mode: str, bot: ExchangeBot | None, risk: RiskManager, store: Store):
        self._s = settings
        self._mode = mode
        self._bot = bot
        self._risk = risk
        self._store = store
        self._parser = SignalParser(settings.openai_api_key, settings.openai_model, settings.openai_image_detail)
        self._queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        self._history: deque[tuple[datetime, str]] = deque(maxlen=CONTEXT_MESSAGES)

    def preload(self, messages: list[IncomingMessage]) -> None:
        for msg in messages:
            self._history.append((msg.date, context_line(msg.date, msg.text, msg.image is not None)))
        log.info("Loaded %d earlier messages as context", len(messages))

    async def enqueue(self, msg: IncomingMessage) -> None:
        await self._queue.put(msg)

    async def worker(self) -> None:
        # One message at a time, so the browser never handles two bets at once.
        while True:
            msg = await self._queue.get()
            try:
                await self._handle(msg)
            except Exception:
                log.exception("Failed to handle message %s", msg.id)

    async def _handle(self, msg: IncomingMessage) -> None:
        context = [line for date, line in self._history if msg.date - date <= CONTEXT_WINDOW]
        self._history.append((msg.date, context_line(msg.date, msg.text, msg.image is not None)))

        age = (datetime.now(timezone.utc) - msg.date).total_seconds()
        if age > self._s.signal_max_age_s:
            log.info("msg %s ignored: %.0fs old", msg.id, age)
            return

        parsed = await self._parse_while_fresh(msg, context)
        if parsed is None:
            return
        message_id = self._store.log_message(msg.id, msg.text, msg.image is not None, parsed)
        log.info("msg %s: %s (%d signal(s))", msg.id, parsed.summary, len(parsed.signals))

        for sig in parsed.signals:
            check = self._risk.precheck(sig)
            if not check.ok:
                result = BetResult("skipped", check.reason)
            elif self._bot is None:
                result = self._simulate(sig, check)
            else:
                result = await self._bot.place(sig, check.position)
            self._record(message_id, sig, result, check)

    async def _parse_while_fresh(self, msg: IncomingMessage, context: list[str]) -> ParsedMessage | None:
        """Retries the AI through network drops for as long as the tip is still fresh."""
        while True:
            try:
                return await self._parser.parse(msg.text, msg.image, context)
            except APIConnectionError:
                age = (datetime.now(timezone.utc) - msg.date).total_seconds()
                if age > self._s.signal_max_age_s:
                    log.error("msg %s MISSED: could not reach OpenAI for %.0fs (network down?): %r",
                              msg.id, age, msg.text[:80])
                    return None
                log.warning("msg %s: OpenAI unreachable, retrying (%.0fs old)", msg.id, age)
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
        return BetResult("simulated", f"signals only, {what}", sig.odds, check.stake, check.liability)

    def _record(self, message_id: int, sig: BetSignal, result: BetResult, check: Decision) -> None:
        self._store.log_bet(
            message_id, sig, self._mode, result.status, result.reason, result.price, result.stake, result.liability
        )
        if sig.action == "entry" and result.status in COUNTED:
            self._store.open_position(sig, self._mode, result.price, result.stake)
        elif sig.action == "exit" and result.status in COUNTED:
            self._store.set_exit(check.position.id, sig.odds, result.stake, result.status)
        elif sig.action == "close" and result.status in ("placed", "simulated") and check.position:
            self._store.set_closed(check.position.id)
        elif sig.action == "close" and result.status == "unconfirmed":
            log.warning("  cashout not confirmed: check the %s trade on the site", sig.selection)
        price = f"{result.price:g}" if result.price else "-"
        log.info("  %s %s | price %s stake %g | %s", result.status.upper(), sig.describe(), price, result.stake,
                 result.reason)


async def run(signals_only: bool) -> None:
    settings = load_settings()
    settings.require(*TELEGRAM_FIELDS, "tg_chat", "openai_api_key")
    if not signals_only:
        settings.require(*SITE_FIELDS)

    mode = "signals_only" if signals_only else ("dry_run" if settings.dry_run else "live")
    store = Store(DATA_DIR / "trades.db")
    risk = RiskManager(settings, store, mode)
    bot = None if signals_only else ExchangeBot(settings, risk)
    log.info("Mode: %s | 1 unit = %g | max units/bet %s | odds tolerance %g%%", mode, settings.unit_stake,
             settings.max_units_per_bet or "off", settings.odds_tolerance * 100)
    if mode == "live":
        log.warning("LIVE MODE: real bets will be placed")

    pipeline = Pipeline(settings, mode, bot, risk, store)
    try:
        if bot:
            await bot.start()
        await asyncio.gather(
            listen(settings, pipeline.enqueue, pipeline.preload, CONTEXT_MESSAGES), pipeline.worker()
        )
    finally:
        if bot:
            await bot.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--signals-only", action="store_true", help="don't open the betting site; only parse and log")
    args = ap.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(DATA_DIR / "bot.log", encoding="utf-8")],
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args.signals_only))
    except KeyboardInterrupt:
        log.info("Stopped")


if __name__ == "__main__":
    main()
