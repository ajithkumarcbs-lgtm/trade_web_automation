"""SQLite log of every message, bet decision and open position (data/trades.db)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .models import BetSignal, ParsedMessage
from .names import NAME_MATCH_MIN, name_score

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
    exit_price  REAL,   -- NULL until a limit exit has been sent
    exit_status TEXT,
    exit_stake  REAL,
    closed      INTEGER DEFAULT 0   -- 1 once cashed out (book set / loss cut)
);
"""
# Columns added after the first release; created on older databases at start-up.
NEW_POSITION_COLUMNS = {"exit_stake": "REAL", "closed": "INTEGER DEFAULT 0"}

# Statuses that count as a bet that exists. "unconfirmed" counts because the bet may be live.
COUNTED = ("placed", "simulated", "unconfirmed")
POSITION_WINDOW = timedelta(hours=12)  # an exit only closes a position opened within this time


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
    exit_stake: float | None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")  # local time, so "today" matches your day


class Store:
    def __init__(self, path: Path):
        self._db = sqlite3.connect(path)
        self._db.executescript(SCHEMA)
        existing = {row[1] for row in self._db.execute("PRAGMA table_info(positions)")}
        for column, kind in NEW_POSITION_COLUMNS.items():
            if column not in existing:
                self._db.execute(f"ALTER TABLE positions ADD COLUMN {column} {kind}")
        self._db.commit()

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
        """Latest not-yet-cashed-out position on this selection opened within POSITION_WINDOW.
        With no selection ('' or '?'), the latest open position of any selection."""
        since = (datetime.now() - POSITION_WINDOW).isoformat(timespec="seconds")
        rows = self._db.execute(
            "SELECT id, opened_at, sport, event, market, selection, side, price, stake, exit_price, exit_stake"
            " FROM positions WHERE mode = ? AND opened_at >= ? AND COALESCE(closed, 0) = 0 ORDER BY id DESC",
            (mode, since),
        ).fetchall()
        if not selection.strip(" ?"):
            return Position(*rows[0]) if rows else None
        for row in rows:
            if name_score(selection, row[5]) >= NAME_MATCH_MIN:
                return Position(*row)
        return None

    def set_exit(self, position_id: int, price: float, stake: float, status: str) -> None:
        self._db.execute(
            "UPDATE positions SET exit_price = ?, exit_stake = ?, exit_status = ? WHERE id = ?",
            (price, stake, status, position_id),
        )
        self._db.commit()

    def set_closed(self, position_id: int) -> None:
        self._db.execute("UPDATE positions SET closed = 1 WHERE id = ?", (position_id,))
        self._db.commit()

    def recent_bets(self, limit: int = 50) -> list[dict]:
        cur = self._db.execute(
            "SELECT created_at, mode, status, description, price, stake, liability, reason FROM bets"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
