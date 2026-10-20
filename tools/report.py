"""Prints the most recent bet decisions, or the P/L per channel, from data/trades.db.

    python -m tools.report --limit 30                # last 30 decisions
    python -m tools.report --pl week --mode dry_run  # P/L per channel this week (paper trading)

The P/L report is the same as /report in Telegram: booked trades (what the bookset locked), trades that ran
to the end of the match (record their real result with /settle), and open trades.
"""
import argparse

from tradebot.config import DATA_DIR, load_settings
from tradebot.notifier import report_text
from tradebot.store import Store, period_start


def main(limit: int, pl: str | None, mode: str) -> None:
    store = Store(DATA_DIR / "trades.db")
    if pl:
        unit = load_settings().unit_stake
        print(report_text(f"({pl}, {mode})", store.report(mode, period_start(pl)), unit, store.unsettled(mode))
              .replace("*", ""))
        return
    rows = store.recent_bets(limit)
    if not rows:
        print("No bets logged yet.")
    for r in reversed(rows):
        price = f"{r['price']:g}" if r["price"] is not None else "-"
        print(f"{r['created_at']}  {r['mode']:<12} {r['status']:<11} {r['description']}  "
              f"price {price}  stake {r['stake']:g}  risk {r['liability']:g}  | {r['reason']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--pl", choices=["day", "week", "month"], help="P/L per channel instead of the decisions")
    ap.add_argument("--mode", default="dry_run", choices=["signals_only", "dry_run", "live"])
    args = ap.parse_args()
    main(args.limit, args.pl, args.mode)
