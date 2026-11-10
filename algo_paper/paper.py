"""Paper ledger and the two algos. Stakes are rupees on paper; nothing here can reach the betting site.

value  : the model (with the market's team view blended in) against the price. Back a side the market gives too
         little chance, lay one it gives too much, when the gap is EDGE or more on two reads running, filled at the
         next read's price (the site's bet delay and our read interval). Book at +TAKE, cut at -CUT, also book once
         the gap has closed and the trade is not red. Two trades a match at most. Run on Match Odds and, separately,
         on the Bookmaker rates, to compare the two markets.
ladder : the SK / MY Tips method with no tip: lay the favourite in three steps as its lay price falls (1.50-1.20,
         1.20-1.10, 1.10-1.01), no new step once its back is 1.70 again; book at equal odds, at +50 once the other
         side leads, or when 20% of a +50 peak is given back; else the result settles it.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

STAKE = 50.0
EDGE = 0.07
TAKE = 15.0
CUT = 30.0
MIN_PRICE, MAX_PRICE = 1.15, 8.0
MAX_SPREAD = 0.06  # lay / back - 1
MAX_TRADES = 2
COOLDOWN_S = 300
JUMP = 0.05  # a move this big in a minute: a wicket or six the score feed may not show yet, so no new trade
STEPS = ((1.20, 1.50), (1.10, 1.20), (1.01, 1.10))
STOP_ABOVE = 1.70
LADDER_TARGET = 50.0


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Price:
    back: float | None
    lay: float | None
    suspended: bool = False

    def ok(self) -> bool:
        return (not self.suspended and self.back is not None and self.lay is not None and 1.0 < self.back <= self.lay
                and self.lay / self.back - 1 <= MAX_SPREAD)


def book(legs: list[tuple[str, float, float]]) -> tuple[float, float]:
    """P/L if the runner wins, and if it loses, for legs (side, stake, price) on one runner."""
    win = sum(s * (p - 1) if side == "back" else -s * (p - 1) for side, s, p in legs)
    lose = sum(-s if side == "back" else s for side, s, p in legs)
    return round(win, 2), round(lose, 2)


def locked(legs: list[tuple[str, float, float]], price: Price) -> float | None:
    """What booking now would lock on every result (lay the runner at its lay, or back it at its back)."""
    win, lose = book(legs)
    if abs(win - lose) < 0.01:
        return lose
    if win > lose:
        if not price.lay:
            return None
        return round(lose + (win - lose) / price.lay, 2)
    if not price.back:
        return None
    return round(lose - (lose - win) / price.back, 2)


class Ledger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots (ts TEXT, event TEXT, runner TEXT, mo_back REAL, mo_lay REAL,
                mo_susp INT, bm_back REAL, bm_lay REAL, bm_susp INT);
            CREATE TABLE IF NOT EXISTS states (ts TEXT, event TEXT, cb_id INT, innings INT, runs INT, wkts INT,
                balls INT, target INT, bat_first TEXT, p_model REAL, p_blend REAL, p_market REAL);
            CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, algo TEXT, event TEXT, runner TEXT, legs TEXT,
                opened_at TEXT, closed_at TEXT, how TEXT, pl REAL, note TEXT);
        """)
        self.db.commit()

    def snapshot(self, event: str, mo: dict[str, Price], bm: dict[str, Price]) -> None:
        ts = now()
        for runner in sorted(set(mo) | set(bm)):
            m, b = mo.get(runner, Price(None, None)), bm.get(runner, Price(None, None))
            self.db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)",
                            (ts, event, runner, m.back, m.lay, int(m.suspended), b.back, b.lay, int(b.suspended)))
        self.db.commit()

    def state(self, event: str, cb_id: int, st: tuple, bat_first: str, p_model: float, p_blend: float,
              p_market: float | None) -> None:
        self.db.execute("INSERT INTO states VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (now(), event, cb_id, *st, bat_first, p_model, p_blend, p_market))
        self.db.commit()

    def open(self, algo: str, event: str, runner: str, legs: list, note: str) -> int:
        cur = self.db.execute("INSERT INTO trades (algo, event, runner, legs, opened_at, note) VALUES (?,?,?,?,?,?)",
                              (algo, event, runner, json.dumps(legs), now(), note))
        self.db.commit()
        return cur.lastrowid

    def update_legs(self, trade_id: int, legs: list, note: str) -> None:
        self.db.execute("UPDATE trades SET legs = ?, note = note || ' | ' || ? WHERE id = ?",
                        (json.dumps(legs), note, trade_id))
        self.db.commit()

    def close(self, trade_id: int, how: str, pl: float, note: str = "") -> None:
        self.db.execute("UPDATE trades SET closed_at = ?, how = ?, pl = ?, note = note || ' | ' || ? WHERE id = ?",
                        (now(), how, round(pl, 2), note, trade_id))
        self.db.commit()

    def trades_of(self, algo: str) -> list[tuple]:
        """(id, event, runner, legs, opened_at, closed_at) of every trade of an algo, oldest first."""
        return [(tid, ev, runner, json.loads(legs), opened, closed) for tid, ev, runner, legs, opened, closed in
                self.db.execute("SELECT id, event, runner, legs, opened_at, closed_at FROM trades WHERE algo = ? "
                                "ORDER BY id", (algo,))]

    def totals_since(self, since: str) -> dict[str, tuple[int, float]]:
        """algo -> (closed trades, P/L) since a time."""
        return {algo: (n, pl or 0.0) for algo, n, pl in self.db.execute(
            "SELECT algo, COUNT(*), SUM(pl) FROM trades WHERE closed_at >= ? GROUP BY algo", (since,))}

    def day_stats(self, day: str) -> dict[str, list]:
        """algo -> [trades, up, down, P/L] of the trades closed on a day ('2026-11-10')."""
        out: dict[str, list] = {}
        for algo, pl in self.db.execute("SELECT algo, pl FROM trades WHERE substr(closed_at, 1, 10) = ?", (day,)):
            s = out.setdefault(algo, [0, 0, 0, 0.0])
            s[0] += 1
            s[1] += int((pl or 0) > 0)
            s[2] += int((pl or 0) < 0)
            s[3] += pl or 0
        return out

    def matches_on(self, day: str) -> int:
        return self.db.execute("SELECT COUNT(DISTINCT event) FROM snapshots WHERE substr(ts, 1, 10) = ?",
                               (day,)).fetchone()[0]

    def open_trades(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM trades WHERE closed_at IS NULL").fetchone()[0]

    def cb_id_of(self, event: str) -> int | None:
        """The Cricbuzz match id last read for this match."""
        row = self.db.execute("SELECT cb_id FROM states WHERE event = ? ORDER BY ts DESC LIMIT 1", (event,)).fetchone()
        return row[0] if row else None


@dataclass
class Open:
    trade_id: int
    runner: str
    legs: list
    opened: datetime
    peak: float = 0.0


@dataclass
class MatchView:
    """One read of a match: prices of both sides and the blended chance of each."""
    event: str
    a: str  # the side batting first
    b: str
    p: dict[str, float]  # runner -> chance
    prices: dict[str, Price]
    jumped: bool  # the market moved JUMP or more in the last minute
    context: str = ""  # the score, for alerts


class ValueAlgo:
    def __init__(self, name: str, ledger: Ledger, log, notify=None) -> None:
        self.name, self.ledger, self.log = name, ledger, log
        self.notify = notify or (lambda text: None)
        self.open: dict[str, Open] = {}
        self.count: dict[str, int] = {}
        self.last_exit: dict[str, datetime] = {}
        self.seen: dict[str, tuple] = {}  # event -> the candidate read last time
        self.order: dict[str, tuple] = {}  # event -> (runner, side) to fill at this read
        self.restore()

    def restore(self) -> None:
        """After a restart: open trades go on being managed, and trades already made count for their match."""
        for tid, ev, runner, legs, opened, closed in self.ledger.trades_of(self.name):
            self.count[ev] = self.count.get(ev, 0) + 1
            if closed is None:
                self.open[ev] = Open(tid, runner, [tuple(leg) for leg in legs], datetime.fromisoformat(opened))
                self.log.info("[%s] carried on after a restart: %s %s", self.name, ev, legs)

    def _candidate(self, view: MatchView) -> tuple | None:
        best = None
        for runner in (view.a, view.b):
            price, p = view.prices.get(runner), view.p.get(runner)
            if not price or not price.ok() or p is None:
                continue
            for side, edge, odds in (("back", p - 1 / price.back, price.back), ("lay", 1 / price.lay - p, price.lay)):
                if edge >= EDGE and MIN_PRICE <= odds <= MAX_PRICE and (best is None or edge > best[2]):
                    best = (runner, side, round(edge, 3), odds)
        return best

    def on_read(self, view: MatchView) -> None:
        ev = view.event
        held = self.open.get(ev)
        if held:
            self._manage(view, held)
            return
        if ev in self.order:  # decided last read: fill now, if the edge is still there at this price
            runner, side = self.order.pop(ev)
            price, p = view.prices.get(runner), view.p.get(runner)
            if price and price.ok() and p is not None:
                odds = price.back if side == "back" else price.lay
                edge = p - 1 / odds if side == "back" else 1 / odds - p
                if edge >= EDGE / 2:
                    legs = [(side, STAKE, odds)]
                    note = f"{side} {runner} @{odds:g}: chance {p:.2f}, market {1 / odds:.2f}"
                    tid = self.ledger.open(self.name, ev, runner, legs, note)
                    self.open[ev] = Open(tid, runner, legs, datetime.now())
                    self.count[ev] = self.count.get(ev, 0) + 1
                    self.log.info("[%s] PAPER %s", self.name, note)
                    self.notify(f"📐 ALGO PAPER ENTRY ({self.name})\n{side.upper()} {runner} @ {odds:g}, ₹50\n{ev}\n"
                                f"{view.context}\nModel {p:.0%} · Market {1 / odds:.0%}\n(no real money)")
                    return
            self.log.info("[%s] order on %s dropped: the price moved", self.name, runner)
            return
        if view.jumped or self.count.get(ev, 0) >= MAX_TRADES:
            self.seen.pop(ev, None)
            return
        if (datetime.now() - self.last_exit.get(ev, datetime.min)).total_seconds() < COOLDOWN_S:
            return
        cand = self._candidate(view)
        before, self.seen[ev] = self.seen.get(ev), cand
        if cand and before and cand[:2] == before[:2]:
            self.order[ev] = cand[:2]  # two reads running: fill at the next read
            self.seen.pop(ev, None)

    def _manage(self, view: MatchView, held: Open) -> None:
        price, p = view.prices.get(held.runner), view.p.get(held.runner)
        if not price or not price.ok():
            return
        lock = locked(held.legs, price)
        if lock is None:
            return
        side = held.legs[0][0]
        gap = None if p is None else (p - 1 / price.lay if side == "back" else 1 / price.back - p)
        how = ("bookset" if lock >= TAKE else "losscut" if lock <= -CUT
               else "gap closed" if gap is not None and gap <= 0.01 and lock >= 0 else "")
        if how:
            self._close(view.event, held, how, lock, f"booked at {price.back:g}/{price.lay:g}")

    def _close(self, ev: str, held: Open, how: str, pl: float, note: str) -> None:
        self.ledger.close(held.trade_id, how, pl, note)
        self.log.info("[%s] PAPER %s %s %+.2f (%s)", self.name, how.upper(), held.runner, pl, note)
        icon = "🟢" if pl > 0 else "🔴" if pl < 0 else "⚪"
        self.notify(f"{icon} ALGO PAPER {how.upper()} ({self.name})\n{held.runner} {pl:+.2f}\n{ev}\n{note}\n(no real money)")
        self.open.pop(ev, None)
        self.last_exit[ev] = datetime.now()

    def settle(self, ev: str, winner: str | None) -> None:
        held = self.open.get(ev)
        if not held:
            return
        win, lose = book(held.legs)
        if winner is None:
            self._close(ev, held, "void", 0.0, "tie / no result")
        else:
            self._close(ev, held, "result", win if winner == held.runner else lose, f"winner {winner}")
        self.order.pop(ev, None)


class LadderAlgo(ValueAlgo):
    def __init__(self, name: str, ledger: Ledger, log, notify=None) -> None:
        self.steps: dict[str, set] = {}
        self.stopped: set[str] = set()
        super().__init__(name, ledger, log, notify)

    def restore(self) -> None:
        super().restore()
        for ev, held in self.open.items():
            self.steps[ev] = set(range(len(held.legs)))
        self.stopped |= {ev for ev in self.count if ev not in self.open}  # booked once already: the method is done

    def on_read(self, view: MatchView) -> None:
        ev = view.event
        a, b = view.prices.get(view.a), view.prices.get(view.b)
        if not a or not b or not a.ok() or not b.ok():
            return
        held = self.open.get(ev)
        fav = held.runner if held else (view.a if a.back < b.back else view.b)
        other = view.b if fav == view.a else view.a
        fp, op = view.prices[fav], view.prices[other]
        if held:
            lock = locked(held.legs, fp)
            if lock is None:
                return
            held.peak = max(held.peak, lock)
            if abs(fp.back - op.back) <= 0.10:
                self._close(ev, held, "equal odds", lock, f"{fav} {fp.back:g} / {other} {op.back:g}")
                self.stopped.add(ev)
                return
            if lock >= LADDER_TARGET and op.back < fp.back:
                self._close(ev, held, "target", lock, f"{other} now leads at {op.back:g}")
                self.stopped.add(ev)
                return
            if held.peak >= LADDER_TARGET and lock <= 0.8 * held.peak:
                self._close(ev, held, "giveback", lock, f"peak {held.peak:+g}")
                self.stopped.add(ev)
                return
            if fp.back >= STOP_ABOVE:
                self.stopped.add(ev)  # no more steps; the trade stays
        if ev in self.stopped or view.jumped:
            return
        done = self.steps.setdefault(ev, set())
        for number, (low, high) in enumerate(STEPS):
            if number in done or not (low < fp.lay <= high or (number == 2 and fp.lay == low)):
                continue
            leg = ("lay", STAKE, fp.lay)
            done.add(number)
            if held:
                held.legs.append(leg)
                self.ledger.update_legs(held.trade_id, held.legs, f"step {number + 1} lay {fav} @{fp.lay:g}")
            else:
                tid = self.ledger.open(self.name, ev, fav, [leg], f"step 1 lay {fav} @{fp.lay:g} ({other} {op.back:g})")
                self.open[ev] = Open(tid, fav, [leg], datetime.now())
            self.log.info("[%s] PAPER step %d: lay %s @%g", self.name, number + 1, fav, fp.lay)
            self.notify(f"📐 ALGO PAPER STEP {number + 1} ({self.name})\nLAY {fav} @ {fp.lay:g}, ₹50\n{ev}\n"
                        f"{view.context}\n(no real money)")
            break
