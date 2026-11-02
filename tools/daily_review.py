"""One day of the bot, channel by channel, to check it the way you would by hand:

    python -m tools.daily_review                   # today
    python -m tools.daily_review 2026-10-21        # that day
    python -m tools.daily_review --days 10         # the last 10 days: one line per day and channel
    python -m tools.daily_review --mode live       # live trades instead of paper (dry_run)

For each channel: the messages read, the instructions the bot found and what it did with each (simulated /
placed / skipped with the reason / failed), the day's P/L in units, the trades still open, and the messages
that look like tips but led to no decision: compare those with the channel.
"""
import argparse
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from tradebot.channels import Channels
from tradebot.config import DATA_DIR, load_settings
from tradebot.store import Store

TIP_WORDS = re.compile(r"\b(back|lay|odds?|book ?set|loss ?cut|cash ?out|limit|win karo|exit|entry|khao)\b", re.I)
CHANNEL_TAG = re.compile(r"^\[(?P<name>[^\]]+)\]")


def _day_bounds(day: date) -> tuple[str, str]:
    start = datetime(day.year, day.month, day.day)
    return start.isoformat(timespec="seconds"), (start + timedelta(days=1)).isoformat(timespec="seconds")


def day_data(db, day: date, mode: str) -> dict:
    """{channel: {"messages": [...], "bets": [...], "opened": [...], "closed": [...]}} for one day."""
    start, end = _day_bounds(day)
    out: dict = defaultdict(lambda: {"messages": [], "bets": [], "opened": [], "closed": []})
    for row in db.rows("SELECT id, received_at, text, summary, COALESCE(channel, ''), deleted_at FROM messages"
                          " WHERE received_at >= ? AND received_at < ? ORDER BY id", (start, end)):
        out[row[4] or "?"]["messages"].append(row)
    for row in db.rows("SELECT b.created_at, b.action, b.status, b.reason, b.description, b.price, b.stake,"
                          " b.message_id, COALESCE(m.channel, '') FROM bets b LEFT JOIN messages m"
                          " ON m.id = b.message_id WHERE b.created_at >= ? AND b.created_at < ? AND b.mode = ?"
                          " ORDER BY b.id", (start, end, mode)):
        tag = CHANNEL_TAG.match(row[4] or "")
        out[row[8] or (tag["name"] if tag else "?")]["bets"].append(row)
    for row in db.rows("SELECT opened_at, selection, side, price, stake, COALESCE(channel, ''), closed"
                          " FROM positions WHERE mode = ? AND opened_at >= ? AND opened_at < ?", (mode, start, end)):
        out[row[5] or "?"]["opened"].append(row)
    for row in db.rows("SELECT closed_at, selection, closed_pl, closed_how, side, price, stake, COALESCE(channel, '')"
                          " FROM positions WHERE mode = ? AND closed = 1 AND closed_at >= ? AND closed_at < ?",
                          (mode, start, end)):
        out[row[7] or "?"]["closed"].append(row)
    return out


def day_pl(closed: list, unit: float) -> tuple[float, int]:
    """(units booked or settled that day, trades that ran to the end unsettled)."""
    total, unsettled = 0.0, 0
    for _, _, pl, how, *_ in closed:
        if pl is None:
            unsettled += how == "finished"
        else:
            total += pl / unit
    return round(total, 2), unsettled


def review(store_path: Path, day: date, mode: str) -> None:
    settings = load_settings()
    channels = Channels.load(settings)
    db = Store(store_path)
    data = day_data(db, day, mode)
    print(f"=== {day} ({mode}) ===")
    if not data:
        print("nothing logged that day (bot not running, or no messages)")
    for name, d in sorted(data.items()):
        unit = channels.settings_for(name).unit_stake or settings.unit_stake
        decided = {b[7] for b in d["bets"]}
        actions = Counter(b[1] for b in d["bets"] if not (b[3] or "").startswith("TOP-UP"))
        statuses = Counter(b[2] for b in d["bets"])
        pl, unsettled = day_pl(d["closed"], unit)
        print(f"\n{name}: {len(d['messages'])} messages | read: " +
              (", ".join(f"{n} {a}" for a, n in actions.items()) or "nothing") +
              " | did: " + (", ".join(f"{n} {s}" for s, n in statuses.items()) or "nothing") +
              f" | P/L {pl:+g}u" + (f", {unsettled} ran to the end (settle them)" if unsettled else ""))
        for created, action, status, reason, desc, price, stake, _, _ in d["bets"]:
            numbers = f" @ {price:g}" if price else ""
            numbers += f", stake {stake:g}" if stake else ""
            print(f"  {created[11:16]} {status.upper():<11} {desc}{numbers}")
            if status not in ("simulated", "placed"):
                print(f"        {reason[:160]}")
        check = [m for m in d["messages"] if m[0] not in decided and TIP_WORDS.search(m[2] or "")]
        if check:
            print(f"  check by hand ({len(check)} messages with tip words, no decision):")
            for _, received, text, summary, _, _ in check[:15]:
                print(f"    {received[11:16]} {text[:70]!r:72} -> {summary[:60]}")
        gone = [m for m in d["messages"] if m[5]]
        if gone:
            print(f"  DELETED by the channel ({len(gone)}), the bot's decision on each above:")
            for msg_id, received, text, _, _, deleted in gone:
                acted = " [bot acted on it]" if msg_id in decided else ""
                print(f"    {received[11:16]} (deleted {deleted[11:16]}) {(text or '(media)')[:80]!r}{acted}")
    still_open = db.rows("SELECT selection, price, stake, COALESCE(channel, ''), opened_at FROM positions"
                         " WHERE mode = ? AND COALESCE(closed, 0) = 0 ORDER BY id", (mode,))
    if still_open:
        print("\nopen trades now: " + "; ".join(f"[{c}] {s} @ {p} stake {st:g} since {o[5:16]}"
                                                 for s, p, st, c, o in still_open))


def summary(store_path: Path, days: int, mode: str) -> None:
    settings = load_settings()
    channels = Channels.load(settings)
    db = Store(store_path)
    totals: Counter = Counter()
    print(f"{'day':<11} {'channel':<20} {'msgs':>5} {'entries':>7} {'closes':>6} {'skipped':>7} {'failed':>6} "
          f"{'P/L u':>7}")
    for back in range(days - 1, -1, -1):
        day = date.today() - timedelta(days=back)
        for name, d in sorted(day_data(db, day, mode).items()):
            unit = channels.settings_for(name).unit_stake or settings.unit_stake
            done = [b for b in d["bets"] if b[2] in ("simulated", "placed")]
            entries = sum(b[1] == "entry" for b in done)
            closes = sum(b[1] == "close" for b in done)
            skipped = sum(b[2] == "skipped" for b in d["bets"])
            failed = sum(b[2] == "failed" for b in d["bets"])
            pl, _ = day_pl(d["closed"], unit)
            totals[name] += pl
            print(f"{day!s:<11} {name[:20]:<20} {len(d['messages']):>5} {entries:>7} {closes:>6} {skipped:>7} "
                  f"{failed:>6} {pl:>+7.2f}")
    print("\n" + ", ".join(f"{n}: {v:+.2f}u" for n, v in totals.items()) + f"  ({days} days, booked + settled)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("day", nargs="?", type=date.fromisoformat, default=date.today())
    ap.add_argument("--days", type=int, help="a summary of the last N days instead of one day in detail")
    ap.add_argument("--mode", default="dry_run", choices=["signals_only", "dry_run", "live"])
    args = ap.parse_args()
    path = DATA_DIR / "trades.db"
    if args.days:
        summary(path, args.days, args.mode)
    else:
        review(path, args.day, args.mode)
