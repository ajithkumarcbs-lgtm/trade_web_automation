"""Real-time alerts to your own Telegram (Saved Messages by default) through the bot's Telegram login."""
from __future__ import annotations

import logging
from pathlib import Path

from telethon import TelegramClient

log = logging.getLogger(__name__)

ICONS = {"placed": "🟢", "simulated": "🧪", "unconfirmed": "❓", "skipped": "⚠️", "failed": "❌", "pending": "⏳",
         "paper": "📐"}
MESSAGE_LIMIT = 4000  # Telegram's is 4096


def _parts(text: str) -> list[str]:
    """A long message in parts, cut at line ends, so a long report doesn't lose its end."""
    parts = []
    while len(text) > MESSAGE_LIMIT:
        cut = text.rfind("\n", 0, MESSAGE_LIMIT)
        cut = cut if cut > 0 else MESSAGE_LIMIT
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return [*parts, text]


class Notifier:
    def __init__(self, chat: str | int, enabled: bool = True):
        self._chat = chat
        self._enabled = enabled
        self._client: TelegramClient | None = None

    def attach(self, client: TelegramClient) -> None:
        """Called once the Telegram listener has logged in."""
        self._client = client

    async def send(self, text: str, image: Path | None = None) -> None:
        if not (self._enabled and self._client):
            return
        try:
            if image and image.exists():
                await self._client.send_file(self._chat, image, caption=text[:1000], parse_mode="md")
            else:
                for part in _parts(text):
                    await self._client.send_message(self._chat, part, parse_mode="md")
        except Exception:  # an alert must never stop trading
            log.warning("Telegram alert failed", exc_info=True)


def _odds(value) -> str:
    return f"{value:g}" if value else "?"


def trade_summary(pos, closed_how: str, pl: float | None, now: str) -> str:
    """Sent when a trade is booked / loss-cut: the whole trade in one message."""
    exit_line = f"\nLimit exit sent @ {pos.exit_price:g} (stake {pos.exit_stake:g})" if pos.exit_price else ""
    result = f"{pl:+g} locked on both sides" if pl is not None else "see the site's Profit & Loss"
    source = f" [{pos.channel}]" if getattr(pos, "channel", "") else ""
    return (f"📊 *TRADE SUMMARY*{source}\n{pos.event}\n"
            f"Entry: {pos.side.upper()} {pos.selection} @ {_odds(pos.price)}, stake {pos.stake:g} ({pos.opened_at[11:16]})"
            f"{exit_line}\nClosed: {closed_how} at {now}\nResult: {result}")


def confirm_request(number: int, description: str, why: str, timeout_s: int) -> str:
    """A tip the bot won't act on without your OK."""
    return (f"❓ *CONFIRM #{number}*\n{description}\nWhy asked: {why}\n"
            f"Reply in Saved Messages: /yes {number}  or  /no {number}  (dropped after {timeout_s}s)")


def report_text(title: str, rows: list[dict], unit: float, unsettled: list) -> str:
    """Per-channel P/L for /report."""
    if not rows:
        return f"📈 *REPORT* {title}\nNo trades."
    lines = [f"📈 *REPORT* {title}"]
    total = risk = 0.0
    for r in rows:
        pl = r["booked_pl"] + r["settled_pl"]
        total += pl
        risk += r["unsettled_risk"]
        lines.append(f"• {r['channel']}: {r['trades']} trades | booked {r['booked']} ({r['booked_pl']:+.0f})"
                     f" | settled {r['settled']} ({r['settled_pl']:+.0f}) | ran to the end, result unknown "
                     f"{r['unsettled']} (stake {r['unsettled_risk']:.0f}) | open {r['open']}")
    lines.append(f"Known result: {total:+.0f} ({total / unit:+.2f} units)")
    if risk:
        lines.append(f"If the {sum(r['unsettled'] for r in rows)} unknown ones lost: {total - risk:+.0f} "
                     f"({(total - risk) / unit:+.2f} units)")
    if unsettled:
        lines.append("Record their results: " + ", ".join(f"/settle {p.id} win|lose ({p.selection})"
                                                       for p in unsettled[:6]))
    return "\n".join(lines)


def _in_limits(rows: list[dict], field: str = "pl") -> float:
    """The rows' rupees in limits, each at the limit it was opened with: /limit changes only new trades."""
    return sum((r[field] or 0) / r["unit"] for r in rows)


def _summary(rows: list[dict]) -> str:
    """'6 trades, 3 won 1 lost, 2 unknown, 6.0 limits, +2.86'."""
    results = [r["pl"] for r in rows if r["pl"] is not None]
    unknown = sum(bool(r["closed"]) and r["pl"] is None for r in rows)
    still_open = sum(not r["closed"] for r in rows)
    return ", ".join([f"{len(rows)} trade{'' if len(rows) == 1 else 's'}",
                      f"{sum(pl > 0 for pl in results)} won {sum(pl < 0 for pl in results)} lost",
                      *([f"{unknown} unknown"] if unknown else []), *([f"{still_open} open"] if still_open else []),
                      f"{_in_limits(rows, 'stake'):.1f} limits", f"{_in_limits(rows):+.2f}"])


def _best_first(rows: list[dict], field: str) -> list[tuple[str, list[dict]]]:
    """The rows grouped by a field, the best result first."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r[field], []).append(r)
    return sorted(groups.items(), key=lambda group: -_in_limits(group[1]))


def results_report(title: str, channels: list[str], rows: list[dict], unit: float, unsettled: list) -> str:
    """/report in limits: each channel, then match trades, fancy, toss and tennis league by league. rows:
    Pipeline._results, where a fancy bet counts at the site's line when it was read, else at the tip's; channels: the
    running ones, which are all listed; unit: 1 limit now."""
    mixed = " now, each trade counted at its own" if any(r["unit"] != unit for r in rows) else ""
    lines = [f"📈 *REPORT* {title}, 1 limit = ₹{unit:,.0f}{mixed}", "", "*Channels*"]
    for name, mine in _best_first(rows, "channel"):
        sports = ", ".join(sorted({r["sport"] for r in mine}))
        lines.append(f"• {name or '-'} ({sports}): {_summary(mine)}")
    busy = {r["channel"] for r in rows}
    idle = [name for name in channels if name not in busy]
    if idle:
        lines.append("No trades: " + ", ".join(idle))
    for name, mine in _best_first([r for r in rows if r["sport"] == "Fancy" and r["pl"] is not None], "channel"):
        at_site = [r for r in mine if r["measured"]]
        lines.append(f"📐 {name}: at the tip's line {_in_limits(mine, 'tip_pl'):+.2f}; " + (
            f"the site's line read on {len(at_site)} bets, {_in_limits(at_site):+.2f}"
            if at_site else "the site's line not read"))
    for sport, heading in (("Cricket", "Cricket match"), ("Fancy", "Cricket fancy"), ("Toss", "Cricket toss"),
                           ("Tennis", "Tennis")):
        mine = [r for r in rows if r["sport"] == sport]
        if mine:
            lines += ["", f"*{heading}*: {_summary(mine)}"]
            lines += [f"• {league}: {_summary(games)}" for league, games in _best_first(mine, "league")]
    total = sum(r["pl"] or 0 for r in rows)
    unknown = [r for r in rows if r["closed"] and r["pl"] is None]
    lines += ["", f"Known result: {total:+.0f} ({_in_limits(rows):+.2f} limits)"]
    if unknown:
        worst = total - sum(r["risk"] for r in unknown)
        lines.append(f"If the {len(unknown)} unknown ones lost: {worst:+.0f} "
                     f"({_in_limits(rows) - _in_limits(unknown, 'risk'):+.2f} limits)")
    if unsettled:
        lines.append("Record their results: " + ", ".join(f"/settle {p.id} win|lose ({p.selection})"
                                                       for p in unsettled[:6]))
    return "\n".join(lines)


def _fancy_bet(tip: dict) -> float:
    """A fancy tip's result in units: at the site's line when it was read, else at the tip's."""
    return tip["site_pl"] if tip.get("site_pl") is not None else (tip["pl"] or 0)


def day_report(title: str, channels: list[dict], week: dict[str, float], summary: str = "") -> str:
    """The day's trades of each channel in one message. channels: {"name", "trades": [{"selection", "unit",
    "units", "price", "state" (won|lost|booked|open|ended), "pl" (units or None)}], "fancy": [{..., "unit"}], "missed": [str],
    "deleted": int, "deleted_traded": int}, unit: the trade's own rupees of 1 limit; week: channel -> units this week;
    summary: goes first, as a long message is cut at its end."""
    lines = [f"📊 *DAILY REPORT* {title}"]
    if summary:
        lines.append(f"\n{summary}")
    if not channels:
        lines.append("No tips today.")
    for c in channels:
        trades = c["trades"]
        fancy = c.get("fancy", [])
        total = sum(t["pl"] for t in trades if t["pl"] is not None) + sum(_fancy_bet(f) for f in fancy)
        won = sum(t["pl"] is not None and t["pl"] > 0 for t in trades)
        lost = sum(t["pl"] is not None and t["pl"] <= 0 for t in trades)
        still = sum(t["pl"] is None for t in trades)
        rupees = sum(t["pl"] * t["unit"] for t in trades if t["pl"] is not None) + sum(_fancy_bet(f) * f["unit"] for f in fancy)
        lines.append(f"\n*{c['name']}*: {total:+.2f}u (₹{rupees:+,.0f})")
        if trades or not fancy:
            lines.append(f"  ✅ {won} up · ❌ {lost} down · ⏳ {still} open/unknown")
        if fancy:
            f_won = sum(f["outcome"] == "won" for f in fancy)
            f_lost = sum(f["outcome"] == "lost" for f in fancy)
            f_open = sum(f["outcome"] is None for f in fancy)
            f_pl = sum(_fancy_bet(f) for f in fancy)
            lines.append(f"  📐 fancy (paper): ✅ {f_won} · ❌ {f_lost} · ⏳ {f_open} open"
                         + (f" · ❔ {len(fancy) - f_won - f_lost - f_open} no result" if
                            len(fancy) - f_won - f_lost - f_open else "") + f" → {f_pl:+.2f}u")
            for f in fancy[:8]:
                icon = {"won": "✅", "lost": "❌", None: "⏳"}.get(f["outcome"], "❔")
                lines.append(f"    {icon} {f['text']}" + (f" → {f['note']}" if f["note"] else "")
                             + (f" (at the site's line {f['site_pl']:+g}u)" if f.get("site_pl") is not None else ""))
        for t in trades:
            icon = {"open": "⏳", "ended": "❔"}.get(t["state"], "✅" if (t["pl"] or 0) > 0 else "❌")
            result = f"{t['pl']:+.2f}u" if t["pl"] is not None else t["state"]
            how = {"booked": "booked", "won": "won at the end", "lost": "lost at the end"}.get(t["state"], "")
            lines.append(f"  {icon} {t['selection']} {t['units']:g}u @ {_odds(t['price'])} → {result}"
                         + (f" ({how})" if how else ""))
        for miss in c["missed"][:5]:
            lines.append(f"  ⚠️ not followed: {miss}")
        if c["deleted"]:
            lines.append(f"  🗑 the channel deleted {c['deleted']} message(s)"
                         + (f", {c['deleted_traded']} of them tips the bot traded" if c["deleted_traded"] else ""))
    if week:
        lines.append("\nThis week: " + " · ".join(f"{name} {units:+.2f}u" for name, units in week.items()))
    return "\n".join(lines)


def match_finished(pos, now: str) -> str:
    """Sent when a trade's match is no longer listed on the site and the trade was never booked."""
    exit_line = f"\nA limit exit @ {pos.exit_price:g} was waiting." if pos.exit_price else ""
    return (f"🏁 *MATCH FINISHED* (trade not booked)\n{pos.event}\n"
            f"Entry: {pos.side.upper()} {pos.selection} @ {_odds(pos.price)}, stake {pos.stake:g} ({pos.opened_at[11:16]})"
            f"{exit_line}\nSettled by the site at the result: check Profit & Loss / Account Statement. ({now})")


def bet_alert(mode: str, action: str, status: str, description: str, price: float | None, stake: float,
              reason: str, when: str) -> str:
    title = {"entry": "ENTRY", "exit": "LIMIT EXIT", "close": "BOOKSET / CASHOUT", "add": "ADD"}.get(action, action)
    tag = "" if mode == "live" else f" [{mode.replace('_', ' ')}]"
    lines = [f"{ICONS.get(status, '•')} *{title} {status.upper()}*{tag}", description]
    if price or stake:
        lines.append(f"Odds {price:g} | Stake {stake:g}" if price else f"Stake {stake:g}")
    lines += [reason, when]
    return "\n".join(line for line in lines if line)
