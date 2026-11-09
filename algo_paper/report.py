"""Results of the paper algos so far:  python -m algo_paper.report   (day by day, totals, every trade)"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parent / "data" / "algo.db"
ALGOS = ("value-mo", "value-bm", "ladder-mo")


def main() -> None:
    db = sqlite3.connect(DB)
    rows = db.execute("SELECT id, algo, event, runner, opened_at, closed_at, how, pl, note FROM trades ORDER BY id").fetchall()
    matches = db.execute("SELECT COUNT(DISTINCT event) FROM snapshots").fetchone()[0]
    print(f"Paper algos (₹50 a trade, no real money). Matches read: {matches}\n")

    days: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(lambda: [0, 0.0]))
    totals = defaultdict(lambda: {"n": 0, "up": 0, "down": 0, "pl": 0.0, "open": 0})
    for tid, algo, event, runner, opened, closed, how, pl, note in rows:
        t = totals[algo]
        if closed is None:
            t["open"] += 1
            continue
        pl = pl or 0.0
        t["n"] += 1
        t["pl"] += pl
        t["up"] += int(pl > 0)
        t["down"] += int(pl < 0)
        cell = days[closed[:10]][algo]
        cell[0] += 1
        cell[1] += pl

    print("By day (closed trades, P/L ₹):")
    print(f"{'day':10s} " + " ".join(f"{a:>16s}" for a in ALGOS) + f" {'day total':>10s}")
    for day in sorted(days):
        cells = [days[day].get(a, [0, 0.0]) for a in ALGOS]
        print(f"{day:10s} " + " ".join(f"{n:3d} {pl:+10.2f} " for n, pl in cells) + f"{sum(c[1] for c in cells):+10.2f}")

    print(f"\n{'algo':10s} {'trades':>6s} {'up':>4s} {'down':>5s} {'open':>5s} {'P/L ₹':>9s} {'limits':>7s}")
    for algo in ALGOS:
        t = totals[algo]
        print(f"{algo:10s} {t['n']:6d} {t['up']:4d} {t['down']:5d} {t['open']:5d} {t['pl']:9.2f} {t['pl'] / 50:7.2f}")
    all_pl = sum(t["pl"] for t in totals.values())
    print(f"{'all':10s} {sum(t['n'] for t in totals.values()):6d} {'':4s} {'':5s} {'':5s} {all_pl:9.2f} {all_pl / 50:7.2f}")

    print("\nTrades:")
    for tid, algo, event, runner, opened, closed, how, pl, note in rows:
        result = f"{how} {pl:+.2f}" if closed else "OPEN"
        print(f"#{tid:<3d} {algo:9s} {opened[5:16]} {event[:40]:40s} {runner[:22]:22s} {result:18s} {note[:90]}")


if __name__ == "__main__":
    main()
