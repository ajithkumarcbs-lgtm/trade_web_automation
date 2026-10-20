"""Tries one action on the site directly, without Telegram or the AI, to check the clicks step by step.

    python -m tools.try_bet back Svrcina 10             back Svrcina, Rs 10, at the best price now
    python -m tools.try_bet back Svrcina 10 --odds 2.12 the same, but skip if the price is >5% worse
    python -m tools.try_bet lay Svrcina 10              lay Svrcina, Rs 10
    python -m tools.try_bet limit Svrcina 1.5           book-set exit at 1.5 on the open Svrcina trade
    python -m tools.try_bet cashout Svrcina             bookset now (site's Cashout, waits for green)
    python -m tools.try_bet losscut Svrcina             loss cut now (books red or green)
    python -m tools.try_bet back Svrcina 10 --dry-run   fill and check the slip, never place

It runs the same steps and safety checks as a Telegram tip, and records the trade like one, so a
'back' followed by 'limit' or 'cashout' works on that trade. DRY_RUN in .env decides whether Placebet is
clicked (--dry-run forces a dry run). Every step is logged; screenshots go to data/screenshots/.
"""
import argparse
import asyncio
import logging
from dataclasses import replace

from main import BOOKSET_RETRY_SECONDS, Pipeline
from tradebot.config import DATA_DIR, SITE_FIELDS, load_settings
from tradebot.exchange_bot import ExchangeBot
from tradebot.models import BetSignal
from tradebot.risk import RiskManager
from tradebot.store import Store

log = logging.getLogger("tradebot")


def make_signal(action: str, player: str, value: float | None, odds: float | None) -> BetSignal:
    base = dict(sport="tennis", event=f"{player} v ?", market="match_odds", market_name="", selection=player,
                line=None, units=None, amount=None, loss_cut=False, confidence=1.0)
    if action in ("back", "lay"):
        return BetSignal(action="entry", side=action, odds=odds, **{**base, "amount": value})
    if action == "limit":
        return BetSignal(action="exit", side="lay", odds=value, **base)
    return BetSignal(action="close", side="back", odds=None, **{**base, "loss_cut": action == "losscut"})


async def main(args) -> None:
    settings = load_settings()
    settings.require(*SITE_FIELDS)
    if args.dry_run:
        settings = replace(settings, dry_run=True)
    mode = "dry_run" if settings.dry_run else "live"
    log.info("try_bet: %s %s %s | mode %s%s", args.action, args.player, args.value or "", mode,
             "" if settings.dry_run else " (REAL BET)")

    store = Store(DATA_DIR / "trades.db")
    risk = RiskManager(settings, store, mode)
    bot = ExchangeBot(settings, risk)
    pipeline = Pipeline(settings, mode, bot, risk, store)
    try:
        await bot.start()
        sig = make_signal(args.action, args.player, args.value, args.odds)
        result = await pipeline.execute(sig)
        print(f"\nRESULT: {result.status.upper()} | {result.reason}")
        while store.pending_closes(mode) or store.pending_instructions(mode):  # keep retrying, as the bot does
            print(f"booking still pending, retrying in {BOOKSET_RETRY_SECONDS}s (Ctrl+C to stop)")
            await asyncio.sleep(BOOKSET_RETRY_SECONDS)
            await pipeline.retry_pending_once()
        if result.status == "pending":
            print("Booking finished: see the last log lines above.")
        print("Check: the log lines above, data/screenshots/, and on the site: Open Bets and BAL/EXP.")
    finally:
        await bot.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["back", "lay", "limit", "cashout", "losscut"])
    ap.add_argument("player", help="team or player as the site shows it, e.g. Svrcina or 'South Africa'")
    ap.add_argument("value", nargs="?", type=float, help="stake in rupees (back/lay) or target odds (limit)")
    ap.add_argument("--odds", type=float, help="tip price for back/lay; without it the best price now is taken")
    ap.add_argument("--dry-run", action="store_true", help="never click Placebet / CASHOUT")
    arguments = ap.parse_args()
    if arguments.action in ("back", "lay", "limit") and not arguments.value:
        ap.error(f"{arguments.action} needs a value (stake for back/lay, odds for limit)")
    DATA_DIR.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(DATA_DIR / "bot.log", encoding="utf-8")])
    asyncio.run(main(arguments))
