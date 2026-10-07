"""Prints the most recent bet decisions from data/trades.db.

    python -m tools.report --limit 30
"""
import argparse

from tradebot.config import DATA_DIR
from tradebot.store import Store


def main(limit: int) -> None:
    rows = Store(DATA_DIR / "trades.db").recent_bets(limit)
    if not rows:
        print("No bets logged yet.")
    for r in reversed(rows):
        price = f"{r['price']:g}" if r["price"] is not None else "-"
        print(f"{r['created_at']}  {r['mode']:<12} {r['status']:<11} {r['description']}  "
              f"price {price}  stake {r['stake']:g}  risk {r['liability']:g}  | {r['reason']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    main(ap.parse_args().limit)
