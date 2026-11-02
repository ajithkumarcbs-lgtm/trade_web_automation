"""SQLite log of every message, bet decision and open position (data/trades.db)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .ladder import Ladder
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
CREATE TABLE IF NOT EXISTS price_log (  -- each open trade's closing price as checked (every ~20 s): its path, so exit
    position_id INTEGER,                -- rules ("book once it reaches even", hedges) can be tested on real moves
    at          TEXT,
    price       REAL
);
CREATE TABLE IF NOT EXISTS fancy (  -- fancy (session) tips, kept on paper and settled from Cricbuzz's scores
    id          INTEGER PRIMARY KEY,
    created_at  TEXT,
    mode        TEXT,
    channel     TEXT,
    message_id  INTEGER,
    event       TEXT,
    team        TEXT,
    kind        TEXT,     -- runs | wicket
    number      INTEGER,  -- over (runs; 0 = innings total) or wicket number
    line        REAL,
    side        TEXT,     -- yes | no
    rate        REAL,
    units       REAL,
    match_id    INTEGER,  -- Cricbuzz match
    outcome     TEXT,     -- won | lost | unknown; NULL while open
    pl          REAL,     -- units
    note        TEXT,
    settled_at  TEXT,
    site_name   TEXT,     -- the site's row when the tip came ('15 Over MLSW'), its line and rate for this side,
    site_line   REAL,     -- so the tip's result can be compared with what the site really offered then
    site_rate   REAL,
    site_pl     REAL      -- units, at the site's line and rate
);
CREATE TABLE IF NOT EXISTS ladders (  -- tips traded by laying the other team in steps (tradebot/ladder.py)
    id          INTEGER PRIMARY KEY,
    created_at  TEXT,
    mode        TEXT,
    channel     TEXT,
    message_id  INTEGER,
    team        TEXT,
    event       TEXT,
    sport       TEXT DEFAULT 'cricket',
    start_at    TEXT,
    other       TEXT DEFAULT '',
    first_price REAL,
    last_step   INTEGER DEFAULT -1,
    stopped     INTEGER DEFAULT 0,
    ended_at    TEXT,
    end_reason  TEXT
);
CREATE TABLE IF NOT EXISTS jackpot_days (  -- days SK Astro named: from its posts, or /jackpot
    day         TEXT PRIMARY KEY,
    source      TEXT,
    added_at    TEXT
);
"""
# Columns added after the first release; created on older databases at start-up.
NEW_POSITION_COLUMNS = {"exit_stake": "REAL", "closed": "INTEGER DEFAULT 0", "closed_at": "TEXT",
                        "close_pending_since": "TEXT", "close_loss_cut": "INTEGER DEFAULT 0",
                        "channel": "TEXT DEFAULT ''", "closed_pl": "REAL", "closed_how": "TEXT", "league": "TEXT"}
POSITION_COLUMNS = ("id, opened_at, sport, event, market, selection, side, price, stake, exit_price, exit_stake,"
                    " close_pending_since, close_loss_cut, COALESCE(channel, '')")
RECENTLY_CLOSED = timedelta(hours=3)  # repeated "Bookset done 🥳" messages within this time are ignored
LADDER_COLUMNS = ("id, created_at, channel, team, event, start_at, message_id, COALESCE(other, ''), first_price,"
                  " last_step, stopped, ended_at, COALESCE(sport, 'cricket'), COALESCE(entries, 0), COALESCE(confirmed_by, ''),"
                  " COALESCE(conflict, 0), COALESCE(peak, 0), COALESCE(was_favourite, 0), last_lay, last_back, team_back, last_pl,"
                  " COALESCE(entry_prices, ''), COALESCE(end_reason, ''), COALESCE(waiting, 0)")
NEW_LADDER_COLUMNS = {"sport": "TEXT DEFAULT 'cricket'", "entries": "INTEGER DEFAULT 0", "confirmed_by": "TEXT DEFAULT ''",
                      "conflict": "INTEGER DEFAULT 0", "peak": "REAL DEFAULT 0", "was_favourite": "INTEGER DEFAULT 0",
                      "last_lay": "REAL", "last_back": "REAL", "team_back": "REAL", "last_pl": "REAL",
                      "entry_prices": "TEXT DEFAULT ''", "waiting": "INTEGER DEFAULT 0"}
RECENT_LADDERS = timedelta(hours=24)  # a tipster's bookset can name a team whose steps ended this long ago

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
    """Start of today / this week (Monday) / this month / this year, local time."""
    now = now or clock()
    day = datetime(now.year, now.month, now.day)
    if period == "week":
        return day - timedelta(days=day.weekday())
    if period == "month":
        return day.replace(day=1)
    if period == "year":
        return day.replace(month=1, day=1)
    return day


class Store:
    def __init__(self, path: Path):
        self._db = sqlite3.connect(path)
        self._db.executescript(SCHEMA)
        existing = {row[1] for row in self._db.execute("PRAGMA table_info(positions)")}
        for column, kind in NEW_POSITION_COLUMNS.items():
            if column not in existing:
                self._db.execute(f"ALTER TABLE positions ADD COLUMN {column} {kind}")
        ladder_columns = {row[1] for row in self._db.execute("PRAGMA table_info(ladders)")}
        for column, kind in NEW_LADDER_COLUMNS.items():
            if column not in ladder_columns:
                self._db.execute(f"ALTER TABLE ladders ADD COLUMN {column} {kind}")
        message_columns = {row[1] for row in self._db.execute("PRAGMA table_info(messages)")}
        if "channel" not in message_columns:
            self._db.execute("ALTER TABLE messages ADD COLUMN channel TEXT DEFAULT ''")
        for column in ("deleted_at", "edited_at", "edited_text"):
            if column not in message_columns:
                self._db.execute(f"ALTER TABLE messages ADD COLUMN {column} TEXT")
        fancy_columns = {row[1] for row in self._db.execute("PRAGMA table_info(fancy)")}
        for column, kind in (("site_name", "TEXT"), ("site_line", "REAL"), ("site_rate", "REAL"), ("site_pl", "REAL"),
                             ("league", "TEXT")):
            if column not in fancy_columns:
                self._db.execute(f"ALTER TABLE fancy ADD COLUMN {column} {kind}")
        self._db.commit()

    def mark_deleted(self, channel: str, tg_ids: list[int]) -> list[tuple[int, str, str]]:
        """Records that the channel deleted these messages; returns (tg id, received at, text) of those the bot
        had received (the latest copy of each id: ids are per chat, the channel name tells them apart)."""
        found = []
        for tg_id in tg_ids:
            row = self._db.execute("SELECT id, received_at, text FROM messages WHERE channel = ? AND tg_id = ?"
                                   " AND deleted_at IS NULL ORDER BY id DESC LIMIT 1", (channel, tg_id)).fetchone()
            if row:
                self._db.execute("UPDATE messages SET deleted_at = ? WHERE id = ?", (_now(), row[0]))
                found.append((tg_id, row[1], row[2] or ""))
        self._db.commit()
        return found

    def mark_edited(self, channel: str, tg_id: int, text: str) -> str | None:
        """The channel changed a message: the new words are kept beside the first ones, which are returned. None when
        the words are the same (a button or a reaction count) or the bot never got the message."""
        row = self._db.execute("SELECT id, text, edited_text FROM messages WHERE channel = ? AND tg_id = ?"
                               " ORDER BY id DESC LIMIT 1", (channel, tg_id)).fetchone()
        if row is None or text.strip() == (row[2] if row[2] is not None else row[1] or "").strip():
            return None
        self._db.execute("UPDATE messages SET edited_at = ?, edited_text = ? WHERE id = ?", (_now(), text, row[0]))
        self._db.commit()
        return row[1] or ""

    def decisions_on(self, channel: str, tg_id: int) -> list[str]:
        """What the bot did on that message: 'ENTRY simulated', ... (for the deletion alert)."""
        rows = self._db.execute(
            "SELECT b.action, b.status FROM bets b JOIN messages m ON b.message_id = m.id"
            " WHERE m.channel = ? AND m.tg_id = ?", (channel, tg_id)).fetchall()
        return [f"{action.upper()} {status}" for action, status in rows]

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

    def find_position(self, selection: str, mode: str, channel: str = "", strict: bool = False) -> Position | None:
        """Latest not-yet-cashed-out position on this selection opened within POSITION_WINDOW.
        With no selection ('' or '?'), the latest open position (of that channel, when one is given).
        strict: only that channel's positions (paper trading keeps each channel's trades apart)."""
        since = (clock() - POSITION_WINDOW).isoformat(timespec="seconds")
        rows = self._db.execute(
            f"SELECT {POSITION_COLUMNS}"
            " FROM positions WHERE mode = ? AND opened_at >= ? AND COALESCE(closed, 0) = 0 ORDER BY id DESC",
            (mode, since),
        ).fetchall()
        if strict and channel:
            rows = [row for row in rows if row[13] == channel]
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

    def old_open_positions(self, mode: str) -> list[Position]:
        """Trades still open after POSITION_WINDOW (a Test match runs for days): nothing watches them any more."""
        since = (clock() - POSITION_WINDOW).isoformat(timespec="seconds")
        rows = self._db.execute(
            f"SELECT {POSITION_COLUMNS}"
            " FROM positions WHERE mode = ? AND opened_at < ? AND COALESCE(closed, 0) = 0 ORDER BY id",
            (mode, since),
        ).fetchall()
        return [Position(*row) for row in rows]

    def trade_in_match(self, selection: str, mode: str, channel: str = "") -> Position | None:
        """An unclosed trade on ANOTHER runner of a match this player is in (Alcaraz, while the bot holds Sinner
        in 'Jannik Sinner v Carlos Alcaraz'), from any channel (only that channel's, when one is given). Trades
        are recorded under the site's match name, which names both sides."""
        if not selection.strip(" ?"):
            return None
        for pos in reversed(self.unclosed_positions(mode)):
            if channel and pos.channel != channel:
                continue
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

    def results(self, mode: str, since: datetime, until: datetime = datetime.max) -> list[dict]:
        """The trades opened in [since, until), for /report. A trade booked at exactly 0 is a void one (the ledger's hand
        corrections), not a result. risk: what an unknown result can lose."""
        rows = self._db.execute(
            "SELECT COALESCE(channel, ''), sport, league, side, price, stake, closed, closed_pl, closed_how FROM positions"
            " WHERE mode = ? AND opened_at >= ? AND opened_at < ? ORDER BY id",
            (mode, since.isoformat(timespec="seconds"), until.isoformat(timespec="seconds"))).fetchall()
        return [{"channel": channel, "sport": sport, "league": league, "stake": stake, "closed": closed, "pl": pl,
                 "risk": stake * (price - 1) if side == "lay" and price else stake}
                for channel, sport, league, side, price, stake, closed, pl, how in rows
                if not (how == "booked" and pl == 0)]

    def trades_without_league(self, mode: str, since: datetime) -> list[tuple]:
        """(id, opened_at, sport, event) of the trades whose league isn't named yet."""
        return self._db.execute("SELECT id, opened_at, sport, event FROM positions WHERE mode = ? AND opened_at >= ?"
                                " AND league IS NULL ORDER BY id", (mode, since.isoformat(timespec="seconds"))).fetchall()

    def set_league(self, position_id: int, league: str) -> None:
        self._db.execute("UPDATE positions SET league = ? WHERE id = ?", (league, position_id))
        self._db.commit()

    def fancy_results(self, mode: str, since: datetime, until: datetime = datetime.max) -> list[dict]:
        """The fancy tips given in [since, until), for /report: pl at the tip's line, site_pl at the site's (units)."""
        rows = self._db.execute(
            "SELECT COALESCE(channel, ''), kind, league, units, outcome, pl, site_pl FROM fancy WHERE mode = ? AND"
            " created_at >= ? AND created_at < ? ORDER BY id",
            (mode, since.isoformat(timespec="seconds"), until.isoformat(timespec="seconds"))).fetchall()
        keys = ("channel", "kind", "league", "units", "outcome", "pl", "site_pl")
        return [dict(zip(keys, row)) for row in rows]

    def fancy_without_league(self, mode: str, since: datetime) -> list[tuple]:
        """(id, created_at, 'cricket', event) of the fancy tips whose league isn't named yet."""
        return self._db.execute("SELECT id, created_at, 'cricket', event FROM fancy WHERE mode = ? AND created_at >= ?"
                                " AND league IS NULL ORDER BY id", (mode, since.isoformat(timespec="seconds"))).fetchall()

    def set_fancy_league(self, fancy_id: int, league: str) -> None:
        self._db.execute("UPDATE fancy SET league = ? WHERE id = ?", (league, fancy_id))
        self._db.commit()

    def day_trades(self, mode: str, start: datetime, end: datetime) -> list[tuple]:
        """(channel, selection, price, stake, closed, closed_pl, closed_how) of the trades opened in [start, end)."""
        return self._db.execute(
            "SELECT COALESCE(channel, ''), selection, price, stake, closed, closed_pl, closed_how FROM positions"
            " WHERE mode = ? AND opened_at >= ? AND opened_at < ? ORDER BY id",
            (mode, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))).fetchall()

    def day_misses(self, mode: str, start: datetime, end: datetime) -> list[tuple]:
        """(channel, description, reason) of the entries not followed in [start, end): skipped or failed."""
        return self._db.execute(
            "SELECT COALESCE(m.channel, ''), b.description, b.reason FROM bets b LEFT JOIN messages m"
            " ON m.id = b.message_id WHERE b.mode = ? AND b.created_at >= ? AND b.created_at < ?"
            " AND b.action = 'entry' AND b.status IN ('skipped', 'failed') ORDER BY b.id",
            (mode, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))).fetchall()

    def day_deleted(self, start: datetime, end: datetime) -> dict[str, tuple[int, int]]:
        """channel -> (messages it deleted in [start, end), how many of them the bot had traded on)."""
        rows = self._db.execute(
            "SELECT m.channel, COUNT(DISTINCT m.id), COUNT(DISTINCT b.message_id) FROM messages m"
            " LEFT JOIN bets b ON b.message_id = m.id AND b.status IN ('placed', 'simulated')"
            " WHERE m.deleted_at >= ? AND m.deleted_at < ? GROUP BY m.channel",
            (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))).fetchall()
        return {channel: (n, traded) for channel, n, traded in rows}

    def log_price(self, position_id: int, price: float) -> None:
        self._db.execute("INSERT INTO price_log (position_id, at, price) VALUES (?,?,?)", (position_id, _now(), price))
        self._db.commit()

    def last_prices(self, position_id: int, count: int) -> list[float]:
        return [row[0] for row in self._db.execute("SELECT price FROM price_log WHERE position_id = ? ORDER BY rowid DESC"
                                                   " LIMIT ?", (position_id, count)).fetchall()]

    def closed_results(self, mode: str, channels: list[str]) -> list[tuple[str, float]]:
        """(closed at, result) of these channels' finished trades with a known result, oldest first."""
        marks = ",".join("?" for _ in channels)
        return self._db.execute(f"SELECT closed_at, closed_pl FROM positions WHERE mode = ? AND channel IN ({marks}) AND closed = 1"
                                " AND closed_pl IS NOT NULL ORDER BY closed_at", (mode, *channels)).fetchall()

    # --- SK Astro's method (tradebot/ladder.py) ------------------------------------------------------

    def add_ladder(self, mode: str, channel: str, team: str, event: str, start_at: datetime, message_id: int | None,
                   sport: str = "cricket") -> int:
        cur = self._db.execute(
            "INSERT INTO ladders (created_at, mode, channel, message_id, team, event, sport, start_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (_now(), mode, channel, message_id, team, event, sport, start_at.isoformat(timespec="seconds")))
        self._db.commit()
        return cur.lastrowid

    def active_ladders(self, mode: str) -> list[Ladder]:
        rows = self._db.execute(f"SELECT {LADDER_COLUMNS} FROM ladders WHERE mode = ? AND ended_at IS NULL ORDER BY id",
                                (mode,)).fetchall()
        return [Ladder(*row) for row in rows]

    def recent_ladders(self, mode: str) -> list[Ladder]:
        """Latest first, ended ones too."""
        since = (clock() - RECENT_LADDERS).isoformat(timespec="seconds")
        rows = self._db.execute(f"SELECT {LADDER_COLUMNS} FROM ladders WHERE mode = ? AND created_at >= ? ORDER BY id DESC",
                                (mode, since)).fetchall()
        return [Ladder(*row) for row in rows]

    def update_ladder(self, ladder_id: int, **values) -> None:
        names = ", ".join(f"{name} = ?" for name in values)
        self._db.execute(f"UPDATE ladders SET {names} WHERE id = ?", (*values.values(), ladder_id))
        self._db.commit()

    def end_ladder(self, ladder_id: int, reason: str) -> None:
        self.update_ladder(ladder_id, ended_at=_now(), end_reason=reason)

    def add_jackpot_day(self, day: date, source: str) -> bool:
        """False when the day was already known."""
        cur = self._db.execute("INSERT OR IGNORE INTO jackpot_days (day, source, added_at) VALUES (?,?,?)",
                               (day.isoformat(), source, _now()))
        self._db.commit()
        return cur.rowcount == 1

    def remove_jackpot_day(self, day: date) -> bool:
        cur = self._db.execute("DELETE FROM jackpot_days WHERE day = ?", (day.isoformat(),))
        self._db.commit()
        return cur.rowcount == 1

    def jackpot_days(self) -> list[str]:
        return [row[0] for row in self._db.execute("SELECT day FROM jackpot_days ORDER BY day")]

    def is_jackpot_day(self, day: date) -> bool:
        return self._db.execute("SELECT 1 FROM jackpot_days WHERE day = ?", (day.isoformat(),)).fetchone() is not None

    def channel_days(self, mode: str, channel: str) -> dict[str, tuple[int, float]]:
        """Per day a trade was opened: how many, and what the closed ones made."""
        rows = self._db.execute(
            "SELECT substr(opened_at, 1, 10), COUNT(*), COALESCE(SUM(closed_pl), 0) FROM positions"
            " WHERE mode = ? AND COALESCE(channel, '') = ? GROUP BY 1", (mode, channel)).fetchall()
        return {day: (count, pl) for day, count, pl in rows}

    # --- fancy tips (paper) ------------------------------------------------------------------------

    def add_fancy(self, mode: str, channel: str, message_id: int | None, event: str, team: str, kind: str,
                  number: int, line: float, side: str, rate: float | None, units: float, site_name: str = "",
                  site_line: float | None = None, site_rate: float | None = None) -> int:
        cur = self._db.execute(
            "INSERT INTO fancy (created_at, mode, channel, message_id, event, team, kind, number, line, side, rate,"
            " units, site_name, site_line, site_rate) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_now(), mode, channel, message_id, event, team, kind, number, line, side, rate, units, site_name,
             site_line, site_rate))
        self._db.commit()
        return cur.lastrowid

    def open_fancy(self, mode: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT id, created_at, channel, event, team, kind, number, line, side, rate, units, match_id, site_line,"
            " site_rate FROM fancy WHERE mode = ? AND outcome IS NULL ORDER BY id", (mode,)).fetchall()
        keys = ("id", "created_at", "channel", "event", "team", "kind", "number", "line", "side", "rate", "units",
                "match_id", "site_line", "site_rate")
        return [dict(zip(keys, row)) for row in rows]

    def set_fancy_match(self, fancy_id: int, match_id: int, event: str = "") -> None:
        """The tip's Cricbuzz match, and that match's name when given (a tip often knows only one side of it)."""
        self._db.execute("UPDATE fancy SET match_id = ?, event = COALESCE(NULLIF(?, ''), event) WHERE id = ?",
                         (match_id, event, fancy_id))
        self._db.commit()

    def settle_fancy(self, fancy_id: int, outcome: str, pl: float, note: str, site_pl: float | None = None) -> None:
        self._db.execute("UPDATE fancy SET outcome = ?, pl = ?, note = ?, settled_at = ?, site_pl = ? WHERE id = ?",
                         (outcome, pl, note, _now(), site_pl, fancy_id))
        self._db.commit()

    def day_fancy(self, mode: str, start: datetime, end: datetime) -> list[tuple]:
        """(channel, team, kind, number, line, side, outcome, pl, note, site_pl) of the fancy tips given in [start, end)."""
        return self._db.execute(
            "SELECT COALESCE(channel, ''), team, kind, number, line, side, outcome, pl, note, site_pl FROM fancy"
            " WHERE mode = ? AND created_at >= ? AND created_at < ? ORDER BY id",
            (mode, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))).fetchall()

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

    def find_pending(self, selection: str, mode: str, action: str = "entry", channel: str = "") -> int | None:
        """Id of a pending instruction of this kind on this selection (any selection when none is named), of any
        channel (only that channel's, when one is given)."""
        pending = [(pending_id, signal) for pending_id, _, signal, _, _ in self.pending_instructions(mode)
                   if signal.action == action and (not channel or signal.channel == channel)]
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
