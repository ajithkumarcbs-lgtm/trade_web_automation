"""SQLite log of every message, bet decision and open position (data/trades.db)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from rapidfuzz import fuzz

from .models import BetSignal, ParsedMessage

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    tg_id       INTEGER,
    received_at TEXT,
    text        TEXT,
    has_image   INTEGER,
    summary     TEXT,
    parsed_json TEXT
);
CREATE TABLE IF NOT EXISTS bets (
    id          INTEGER PRIMARY KEY,
    message_id  INTEGER REFERENCES messages(id),
    created_at  TEXT,
    mode        TEXT,   -- signals_only | dry_run | live
    action      TEXT,   -- entry | add | exit
    status      TEXT,   -- placed | simulated | unconfirmed | skipped | failed
    reason      TEXT,
    signal_key  TEXT,
    description TEXT,
    price       REAL,
    stake       REAL,
    liability   REAL
);
CREATE TABLE IF NOT EXISTS positions (
    id          INTEGER PRIMARY KEY,
    opened_at   TEXT,
    mode        TEXT,
    sport       TEXT,
    event       TEXT,
    market      TEXT,
    selection   TEXT,
    side        TEXT,
    price       REAL,
    stake       REAL,
    exit_price  REAL,   -- NULL until an exit bet has been sent
    exit_status TEXT
);
"""

# Statuses that count as a bet that exists. "unconfirmed" counts because the bet may be live.
COUNTED = ("placed", "simulated", "unconfirmed")
POSITION_WINDOW = timedelta(hours=12)  # an exit only closes a position opened within this time
NAME_MATCH_MIN = 80


@dataclass
class Position:
    id: int
    opened_at: str
    sport: str
    event: str
    market: str
    selection: str
    side: str
    price: float | None
    stake: float
    exit_price: float | None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")  # local time, so "today" matches your day


class Store:
    def __init__(self, path: Path):
        self._db = sqlite3.connect(path)
        self._db.executescript(SCHEMA)

    def log_message(self, tg_id: int, text: str, has_image: bool, parsed: ParsedMessage) -> int:
        cur = self._db.execute(
            "INSERT INTO messages (tg_id, received_at, text, has_image, summary, parsed_json) VALUES (?,?,?,?,?,?)",
            (tg_id, _now(), text, int(has_image), parsed.summary, parsed.model_dump_json()),
        )
        self._db.commit()
        return cur.lastrowid

    def log_bet(
        self,
        message_id: int,
        signal: BetSignal,
        mode: str,
        status: str,
        reason: str,
        price: float | None = None,
        stake: float = 0.0,
        liability: float = 0.0,
    ) -> None:
        self._db.execute(
            "INSERT INTO bets (message_id, created_at, mode, action, status, reason, signal_key, description, price,"
            " stake, liability) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (message_id, _now(), mode, signal.action, status, reason, signal.key(), signal.describe(), price, stake,
             liability),
        )
        self._db.commit()

    def today_totals(self, mode: str) -> tuple[int, float]:
        """(number of trades opened, their total worst-case loss) today for this mode. Exits don't count."""
        today = datetime.now().date().isoformat()
        row = self._db.execute(
            f"SELECT COUNT(*), COALESCE(SUM(liability), 0) FROM bets WHERE mode = ? AND action = 'entry'"
            f" AND created_at >= ? AND status IN ({','.join('?' * len(COUNTED))})",
            (mode, today, *COUNTED),
        ).fetchone()
        return row[0], row[1]

    def has_recent(self, signal_key: str, minutes: int, mode: str) -> bool:
        since = (datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        row = self._db.execute(
            f"SELECT 1 FROM bets WHERE signal_key = ? AND mode = ? AND created_at >= ?"
            f" AND status IN ({','.join('?' * len(COUNTED))}) LIMIT 1",
            (signal_key, mode, since, *COUNTED),
        ).fetchone()
        return row is not None

    def open_position(self, signal: BetSignal, mode: str, price: float | None, stake: float) -> None:
        self._db.execute(
            "INSERT INTO positions (opened_at, mode, sport, event, market, selection, side, price, stake)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (_now(), mode, signal.sport, signal.event, signal.market, signal.selection, signal.side, price, stake),
        )
        self._db.commit()

    def find_position(self, selection: str, mode: str) -> Position | None:
        """Latest position on this selection opened within POSITION_WINDOW (names matched loosely)."""
        since = (datetime.now() - POSITION_WINDOW).isoformat(timespec="seconds")
        rows = self._db.execute(
            "SELECT id, opened_at, sport, event, market, selection, side, price, stake, exit_price FROM positions"
            " WHERE mode = ? AND opened_at >= ? ORDER BY id DESC",
            (mode, since),
        ).fetchall()
        for row in rows:
            if fuzz.token_set_ratio(selection.lower(), row[5].lower()) >= NAME_MATCH_MIN:
                return Position(*row)
        return None

    def set_exit(self, position_id: int, price: float | None, status: str) -> None:
        self._db.execute(
            "UPDATE positions SET exit_price = ?, exit_status = ? WHERE id = ?", (price, status, position_id)
        )
        self._db.commit()

    def recent_bets(self, limit: int = 50) -> list[dict]:
        cur = self._db.execute(
            "SELECT created_at, mode, status, description, price, stake, liability, reason FROM bets"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
