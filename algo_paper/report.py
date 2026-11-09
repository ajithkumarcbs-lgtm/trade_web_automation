"""Results of the paper algos so far:  .venv\\Scripts\\python.exe -m algo_paper.report"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parent / "data" / "algo.db"


def main() -> None:
    db = sqlite3.connect(DB)
    rows = db.execute("SELECT id, algo, event, runner, opened_at, closed_at, how, pl, note FROM trades ORDER BY id").fetchall()
    matches = db.execute("SELECT COUNT(DISTINCT event) FROM snapshots").fetchone()[0]
    print(f"Paper algos (₹50 a trade, no real money). Matches read: {matches}\n")
    totals = defaultdict(lambda: {"n": 0, "up": 0, "down": 0, "pl": 0.0, "open": 0})
    for tid, algo, event, runner, opened, closed, how, pl, note in rows:
        t = totals[algo]
        if closed is None:
            t["open"] += 1
            continue
        t["n"] += 1
        t["pl"] += pl or 0
        if (pl or 0) > 0:
            t["up"] += 1
        elif (pl or 0) < 0:
            t["down"] += 1
    print(f"{'algo':10s} {'trades':>6s} {'up':>4s} {'down':>5s} {'open':>5s} {'P/L ₹':>9s} {'limits':>7s}")
    for algo, t in sorted(totals.items()):
        print(f"{algo:10s} {t['n']:6d} {t['up']:4d} {t['down']:5d} {t['open']:5d} {t['pl']:9.2f} {t['pl'] / 50:7.2f}")
    print("\nTrades:")
    for tid, algo, event, runner, opened, closed, how, pl, note in rows:
        result = f"{how} {pl:+.2f}" if closed else "OPEN"
        print(f"#{tid:<3d} {algo:9s} {opened[5:16]} {event[:40]:40s} {runner[:22]:22s} {result:18s} {note[:90]}")


if __name__ == "__main__":
    main()
