"""Real-time alerts to your own Telegram (Saved Messages by default) through the bot's Telegram login."""
from __future__ import annotations

import logging
from pathlib import Path

from telethon import TelegramClient

log = logging.getLogger(__name__)

ICONS = {"placed": "🟢", "simulated": "🧪", "unconfirmed": "❓", "skipped": "⚠️", "failed": "❌", "pending": "⏳"}


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
                await self._client.send_message(self._chat, text[:4000], parse_mode="md")
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
