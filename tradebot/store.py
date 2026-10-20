"""SQLite log of every message, bet decision and open position (data/trades.db)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .models import BetSignal, ParsedMessage
from .names import NAME_MATCH_MIN, contains_player, name_score, same_name

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
    closed      INTEGER DEFAULT 0,  -- 1 once cashed out (book set / loss cut) or the match finished
    closed_at   TEXT,
    close_pending_since TEXT,       -- a bookset was asked for and is being retried until it goes through
    close_loss_cut      INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pending (  -- entries / limit exits the site couldn't take yet, retried until done
    id          INTEGER PRIMARY KEY,
    created_at  TEXT,
    mode        TEXT,
    action      TEXT,
    selection   TEXT,
    signal_json TEXT,
    message_id  INTEGER,
    last_reason TEXT
);
CREATE TABLE IF NOT EXISTS followed (  -- chats followed while running (/follow, or a title matching channels.json)
    chat_id     INTEGER PRIMARY KEY,
    channel     TEXT,
    title       TEXT,
    added_at    TEXT
);
"""
# Columns added after the first release; created on older databases at start-up.
NEW_POSITION_COLUMNS = {"exit_stake": "REAL", "closed": "INTEGER DEFAULT 0", "closed_at": "TEXT",
                        "close_pending_since": "TEXT", "close_loss_cut": "INTEGER DEFAULT 0",
                        "channel": "TEXT DEFAULT ''", "closed_pl": "REAL", "closed_how": "TEXT"}
POSITION_COLUMNS = ("id, opened_at, sport, event, market, selection, side, price, stake, exit_price, exit_stake,"
                    " close_pending_since, close_loss_cut, COALESCE(channel, '')")
RECENTLY_CLOSED = timedelta(hours=3)  # repeated "Bookset done 🥳" messages within this time are ignored

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
    close_pending_since: str | None = None  # a bookset is being retried since then
    close_loss_cut: int = 0
    channel: str = ""  # channels.json channel the tip came from

    @property
    def liability(self) -> float:
        """Worst-case loss of the entry (a lay risks stake x (odds - 1))."""
        if self.side == "lay" and self.price:
            return round(self.stake * (self.price - 1), 2)
        return self.stake


def clock() -> datetime:
    """Local time now, so "today" matches your day. tools/replay_channel replaces it with the time of the
    message being replayed, so windows like "the same tip within 30 min" work on a month of old messages."""
    return datetime.now()


def _now() -> str:
    return clock().isoformat(timespec="seconds")


def _matching(selection: str, names: list[str], loose_ok: list[bool] | None = None) -> int | None:
    """Index of the recorded name this selection means: the first close match ('Rublev' / 'Andrey Rublev'),
    else the only loose one (the AI's 'Gaea' for a trade recorded under the site's 'Arthur Gea'). loose_ok
    limits the loose matches, e.g. to the channel's own trades."""
    for i, name in enumerate(names):
        if name_score(selection, name) >= NAME_MATCH_MIN:
            return i
    loose = [i for i, name in enumerate(names)
             if (loose_ok is None or loose_ok[i]) and same_name(selection, name, short_forms=False)]
    return loose[0] if len(loose) == 1 else None


def period_start(period: str, now: datetime | None = None) -> datetime:
    """Start of today / this week (Monday) / this month, local time."""
    now = now or clock()
    day = datetime(now.year, now.month, now.day)
    if period == "week":
        return day - timedelta(days=day.weekday())
    if period == "month":
        return day.replace(day=1)
    return day


class Store:
    def __init__(self, path: Path):
        self._db = sqlite3.connect(path)
        self._db.executescript(SCHEMA)
        existing = {row[1] for row in self._db.execute("PRAGMA table_info(positions)")}
        for column, kind in NEW_POSITION_COLUMNS.items():
            if column not in existing:
                self._db.execute(f"ALTER TABLE positions ADD COLUMN {column} {kind}")
        if "channel" not in {row[1] for row in self._db.execute("PRAGMA table_info(messages)")}:
            self._db.execute("ALTER TABLE messages ADD COLUMN channel TEXT DEFAULT ''")
        self._db.commit()

    def rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        """Read-only query, for the review tools."""
        return self._db.execute(sql, params).fetchall()

    def log_message(self, tg_id: int, text: str, has_image: bool, parsed: ParsedMessage, channel: str = "") -> int:
        cur = self._db.execute(
            "INSERT INTO messages (tg_id, received_at, text, has_image, summary, parsed_json, channel)"
            " VALUES (?,?,?,?,?,?,?)",
            (tg_id, _now(), text, int(has_image), parsed.summary, parsed.model_dump_json(), channel),
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
        today = clock().date().isoformat()
        row = self._db.execute(
            f"SELECT COUNT(*), COALESCE(SUM(liability), 0) FROM bets WHERE mode = ? AND action = 'entry'"
            f" AND created_at >= ? AND status IN ({','.join('?' * len(COUNTED))})",
            (mode, today, *COUNTED),
        ).fetchone()
        return row[0], row[1]

    def has_recent(self, signal_key: str, minutes: int, mode: str) -> bool:
        since = (clock() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        row = self._db.execute(
            f"SELECT 1 FROM bets WHERE signal_key = ? AND mode = ? AND created_at >= ?"
            f" AND status IN ({','.join('?' * len(COUNTED))}) LIMIT 1",
            (signal_key, mode, since, *COUNTED),
        ).fetchone()
        return row is not None

    def open_position(self, signal: BetSignal, mode: str, price: float | None, stake: float) -> None:
        self._db.execute(
            "INSERT INTO positions (opened_at, mode, sport, event, market, selection, side, price, stake, channel)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_now(), mode, signal.sport, signal.event, signal.market, signal.selection, signal.side, price, stake,
             getattr(signal, "channel", "")),
        )
        self._db.commit()

    def add_to_position(self, position_id: int, stake: float, price: float) -> None:
        """A top-up ("Limit 2" after a 1-unit entry): one position with the stake-weighted average odds,
        so booking it later still locks the same profit on both sides."""
        old_price, old_stake = self._db.execute("SELECT price, stake FROM positions WHERE id = ?",
                                                (position_id,)).fetchone()
        total = old_stake + stake
        # backs: same win if the selection wins; lays: same liability. Both give this average.
        average = 1 + (old_stake * (old_price - 1) + stake * (price - 1)) / total
        self._db.execute("UPDATE positions SET stake = ?, price = ? WHERE id = ?",
                         (round(total, 2), round(average, 4), position_id))
        self._db.commit()

    def find_position(self, selection: str, mode: str, channel: str = "") -> Position | None:
        """Latest not-yet-cashed-out position on this selection opened within POSITION_WINDOW.
        With no selection ('' or '?'), the latest open position (of that channel, when one is given)."""
        since = (clock() - POSITION_WINDOW).isoformat(timespec="seconds")
        rows = self._db.execute(
            f"SELECT {POSITION_COLUMNS}"
            " FROM positions WHERE mode = ? AND opened_at >= ? AND COALESCE(closed, 0) = 0 ORDER BY id DESC",
            (mode, since),
        ).fetchall()
        if not selection.strip(" ?"):
            for row in rows:
                if not channel or row[13] == channel:
                    return Position(*row)
            return None
        # loosely only within the channel and for players: team names like India / West Indies must match closely
        index = _matching(selection, [row[5] for row in rows],
                          [(not channel or row[13] == channel) and row[2] != "cricket" for row in rows])
        return Position(*rows[index]) if index is not None else None

    def latest_position(self, mode: str, channel: str, within: timedelta) -> Position | None:
        """The channel's most recent trade opened within `within`, open or not (for "Limit 2" top-ups)."""
        since = (clock() - within).isoformat(timespec="seconds")
        row = self._db.execute(
            f"SELECT {POSITION_COLUMNS} FROM positions WHERE mode = ? AND COALESCE(channel, '') = ?"
            " AND opened_at >= ? AND COALESCE(closed, 0) = 0 ORDER BY id DESC LIMIT 1", (mode, channel, since),
        ).fetchone()
        return Position(*row) if row else None

    def last_booked_profit(self, mode: str, channel: str, within: timedelta) -> float | None:
        """Profit locked by the channel's latest bookset within `within` (for "green money 20%" re-entries)."""
        since = (clock() - within).isoformat(timespec="seconds")
        row = self._db.execute(
            "SELECT closed_pl FROM positions WHERE mode = ? AND COALESCE(channel, '') = ? AND closed = 1"
            " AND closed_pl IS NOT NULL AND closed_at >= ? ORDER BY closed_at DESC LIMIT 1", (mode, channel, since),
        ).fetchone()
        return row[0] if row else None

    def unclosed_positions(self, mode: str) -> list[Position]:
        """Trades within POSITION_WINDOW not cashed out or finished yet (with or without a limit exit)."""
        since = (clock() - POSITION_WINDOW).isoformat(timespec="seconds")
        rows = self._db.execute(
            f"SELECT {POSITION_COLUMNS}"
            " FROM positions WHERE mode = ? AND opened_at >= ? AND COALESCE(closed, 0) = 0 ORDER BY id",
            (mode, since),
        ).fetchall()
        return [Position(*row) for row in rows]

    def trade_in_match(self, selection: str, mode: str) -> Position | None:
        """An unclosed trade on ANOTHER runner of a match this player is in (Alcaraz, while the bot holds Sinner
        in 'Jannik Sinner v Carlos Alcaraz'), from any channel. Trades are recorded under the site's match name,
        which names both sides."""
        if not selection.strip(" ?"):
            return None
        for pos in reversed(self.unclosed_positions(mode)):
            if contains_player(selection, pos.event) and name_score(selection, pos.selection) < NAME_MATCH_MIN:
                return pos
        return None

    def open_positions(self, mode: str) -> list[Position]:
        """Trades within POSITION_WINDOW that have no exit sent and aren't cashed out."""
        since = (clock() - POSITION_WINDOW).isoformat(timespec="seconds")
        rows = self._db.execute(
            f"SELECT {POSITION_COLUMNS}"
            " FROM positions WHERE mode = ? AND opened_at >= ? AND COALESCE(closed, 0) = 0"
            " AND exit_price IS NULL AND price IS NOT NULL ORDER BY id",
            (mode, since),
        ).fetchall()
        return [Position(*row) for row in rows]

    def set_exit(self, position_id: int, price: float, stake: float, status: str) -> None:
        self._db.execute(
            "UPDATE positions SET exit_price = ?, exit_stake = ?, exit_status = ? WHERE id = ?",
            (price, stake, status, position_id),
        )
        self._db.commit()

    def set_closed(self, position_id: int, pl: float | None = None, how: str = "booked") -> None:
        """how: booked (bookset / loss cut / cashout, pl = what it locked) or finished (the match ended
        without a booking: the site settles it, pl unknown until /settle)."""
        self._db.execute("UPDATE positions SET closed = 1, closed_at = ?, close_pending_since = NULL, closed_pl = ?,"
                         " closed_how = ? WHERE id = ?", (_now(), pl, how, position_id))
        self._db.commit()

    def settle(self, position_id: int, pl: float) -> bool:
        """Records the result of a trade that ran to the end of its match."""
        cur = self._db.execute("UPDATE positions SET closed_pl = ?, closed_how = 'settled' WHERE id = ? AND closed = 1",
                               (pl, position_id))
        self._db.commit()
        return cur.rowcount == 1

    def position(self, position_id: int) -> Position | None:
        row = self._db.execute(f"SELECT {POSITION_COLUMNS} FROM positions WHERE id = ?", (position_id,)).fetchone()
        return Position(*row) if row else None

    def period_result(self, mode: str, since: datetime) -> float:
        """Result of the trades opened since `since`: what booked trades locked, plus the full loss of trades
        whose match finished without a booking and haven't been settled (counted as lost until /settle).
        Open trades don't count yet."""
        rows = self._db.execute(
            "SELECT side, price, stake, closed_pl, closed_how FROM positions WHERE mode = ? AND opened_at >= ?"
            " AND closed = 1", (mode, since.isoformat(timespec="seconds")),
        ).fetchall()
        total = 0.0
        for side, price, stake, pl, how in rows:
            if pl is not None:
                total += pl
            elif how == "finished":
                total -= stake * (price - 1) if side == "lay" and price else stake
        return round(total, 2)

    def report(self, mode: str, since: datetime) -> list[dict]:
        """Per channel since `since`: trades, booked (+ their locked P/L), finished without a booking
        (settled P/L, and the stake of the unsettled ones), still open."""
        rows = self._db.execute(
            "SELECT COALESCE(channel, ''), side, price, stake, closed, closed_pl, closed_how FROM positions"
            " WHERE mode = ? AND opened_at >= ?", (mode, since.isoformat(timespec="seconds")),
        ).fetchall()
        out: dict[str, dict] = {}
        for channel, side, price, stake, closed, pl, how in rows:
            r = out.setdefault(channel or "-", {"channel": channel or "-", "trades": 0, "booked": 0, "booked_pl": 0.0,
                                                "settled": 0, "settled_pl": 0.0, "unsettled": 0, "unsettled_risk": 0.0,
                                                "open": 0})
            r["trades"] += 1
            if not closed:
                r["open"] += 1
            elif how in ("booked", None) and pl is not None:
                r["booked"] += 1
                r["booked_pl"] += pl
            elif pl is not None:
                r["settled"] += 1
                r["settled_pl"] += pl
            else:
                r["unsettled"] += 1
                r["unsettled_risk"] += stake * (price - 1) if side == "lay" and price else stake
        return list(out.values())

    def unsettled(self, mode: str, limit: int = 20) -> list[Position]:
        """Trades whose match finished without a booking and whose result isn't recorded yet."""
        rows = self._db.execute(
            f"SELECT {POSITION_COLUMNS} FROM positions WHERE mode = ? AND closed = 1 AND closed_how = 'finished'"
            " AND closed_pl IS NULL ORDER BY id DESC LIMIT ?", (mode, limit),
        ).fetchall()
        return [Position(*row) for row in rows]

    # --- chats followed while running -------------------------------------------------------------

    def follow(self, chat_id: int, channel: str, title: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO followed (chat_id, channel, title, added_at) VALUES (?,?,?,?)",
                         (chat_id, channel, title, _now()))
        self._db.commit()

    def unfollow(self, chat_id: int) -> None:
        self._db.execute("DELETE FROM followed WHERE chat_id = ?", (chat_id,))
        self._db.commit()

    def followed(self) -> list[tuple[int, str, str]]:
        return self._db.execute("SELECT chat_id, channel, title FROM followed ORDER BY added_at").fetchall()

    def mark_close_pending(self, position_id: int, loss_cut: bool) -> None:
        """A bookset / loss cut was asked for but the site hasn't taken it yet: keep retrying."""
        self._db.execute(
            "UPDATE positions SET close_pending_since = COALESCE(close_pending_since, ?), close_loss_cut = ?"
            " WHERE id = ?", (_now(), int(loss_cut), position_id))
        self._db.commit()

    def clear_close_pending(self, position_id: int) -> None:
        self._db.execute("UPDATE positions SET close_pending_since = NULL WHERE id = ?", (position_id,))
        self._db.commit()

    def add_pending(self, signal: BetSignal, mode: str, message_id: int | None, reason: str) -> None:
        self._db.execute(
            "INSERT INTO pending (created_at, mode, action, selection, signal_json, message_id, last_reason)"
            " VALUES (?,?,?,?,?,?,?)",
            (_now(), mode, signal.action, signal.selection, signal.model_dump_json(), message_id, reason),
        )
        self._db.commit()

    def pending_instructions(self, mode: str) -> list[tuple[int, str, BetSignal, int | None, str]]:
        """(id, created_at, signal, message_id, last_reason) of entries / exits still being retried."""
        rows = self._db.execute(
            "SELECT id, created_at, signal_json, message_id, last_reason FROM pending WHERE mode = ? ORDER BY id",
            (mode,),
        ).fetchall()
        return [(row[0], row[1], BetSignal.model_validate_json(row[2]), row[3], row[4]) for row in rows]

    def find_pending(self, selection: str, mode: str, action: str = "entry") -> int | None:
        """Id of a pending instruction of this kind on this selection (any selection when none is named)."""
        pending = [(pending_id, signal) for pending_id, _, signal, _, _ in self.pending_instructions(mode)
                   if signal.action == action]
        if not selection.strip(" ?"):
            return pending[0][0] if pending else None
        index = _matching(selection, [signal.selection for _, signal in pending],
                          [signal.sport != "cricket" for _, signal in pending])
        return pending[index][0] if index is not None else None

    def update_pending(self, pending_id: int, reason: str) -> None:
        self._db.execute("UPDATE pending SET last_reason = ? WHERE id = ?", (reason, pending_id))
        self._db.commit()

    def remove_pending(self, pending_id: int) -> None:
        self._db.execute("DELETE FROM pending WHERE id = ?", (pending_id,))
        self._db.commit()

    def pending_closes(self, mode: str) -> list[Position]:
        """Trades whose bookset / loss cut is still being retried."""
        rows = self._db.execute(
            f"SELECT {POSITION_COLUMNS} FROM positions WHERE mode = ? AND COALESCE(closed, 0) = 0"
            " AND close_pending_since IS NOT NULL ORDER BY id", (mode,),
        ).fetchall()
        return [Position(*row) for row in rows]

    def recently_closed(self, selection: str, mode: str) -> tuple[str, str] | None:
        """(selection, closed time) of a trade on this selection closed within RECENTLY_CLOSED; with no
        selection, of the latest closed trade."""
        since = (clock() - RECENTLY_CLOSED).isoformat(timespec="seconds")
        rows = self._db.execute(
            "SELECT selection, closed_at, sport FROM positions WHERE mode = ? AND closed = 1 AND closed_at >= ?"
            " ORDER BY closed_at DESC", (mode, since),
        ).fetchall()
        if not rows:
            return None
        if not selection.strip(" ?"):
            return rows[0][:2]
        index = _matching(selection, [row[0] for row in rows], [row[2] != "cricket" for row in rows])
        return rows[index][:2] if index is not None else None

    def today_counts(self, mode: str) -> dict[tuple[str, str], int]:
        """{(action, status): count} of today's decisions, for the heartbeat message."""
        today = clock().date().isoformat()
        rows = self._db.execute(
            "SELECT action, status, COUNT(*) FROM bets WHERE mode = ? AND created_at >= ? GROUP BY action, status",
            (mode, today),
        ).fetchall()
        return {(action, status): count for action, status, count in rows}

    def recent_bets(self, limit: int = 50) -> list[dict]:
        cur = self._db.execute(
            "SELECT created_at, mode, status, description, price, stake, liability, reason FROM bets"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
