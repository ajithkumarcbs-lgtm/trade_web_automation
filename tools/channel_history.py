"""Saves Telegram channels' message history (text, time, reply links, media kind) for analysis.

    python -m tools.channel_history -1003769891853              # whole history
    python -m tools.channel_history -1003769891853 --days 90    # last 90 days only
    python -m tools.channel_history -1001 -1002 -1003 --days 90 # several channels, one login

Writes data/history/<chat id>.json per channel, oldest message first. Times are India time (IST).
"""
import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone

from tradebot.config import DATA_DIR, TELEGRAM_FIELDS, load_settings
from tradebot.telegram_listener import make_client, resolve_chat

IST = timezone(timedelta(hours=5, minutes=30))


def media_kind(msg) -> str:
    """What a message carries: photo, voice, video_note, audio, video, gif, sticker, file, ... or ""."""
    for kind in ("photo", "voice", "video_note", "audio", "gif", "sticker", "video", "poll"):
        if getattr(msg, kind, None):
            return kind
    if msg.action:
        return "service"  # joins, pins, title changes
    if msg.document:
        return "file"
    return type(msg.media).__name__.replace("MessageMedia", "").lower() if msg.media else ""


async def save_history(client, chat: str, since: datetime | None) -> None:
    entity = await resolve_chat(client, int(chat) if chat.lstrip("-").isdigit() else chat)
    messages = []
    async for msg in client.iter_messages(entity):  # newest first
        if since and msg.date < since:
            break
        messages.append({
            "id": msg.id,
            "time": msg.date.astimezone(IST).strftime("%Y-%m-%d %H:%M"),
            "text": msg.raw_text or "",
            "reply_to": msg.reply_to_msg_id,
            "photo": bool(msg.photo),
            "media": type(msg.media).__name__.replace("MessageMedia", "") if msg.media else "",
            "kind": media_kind(msg),
            "duration": int(getattr(msg.file, "duration", 0) or 0) if msg.file else 0,  # seconds
            "forwarded": msg.fwd_from is not None,
            "edited": msg.edit_date.astimezone(IST).strftime("%Y-%m-%d %H:%M") if msg.edit_date else "",
        })
    messages.reverse()

    out = DATA_DIR / "history" / f"{chat}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(messages, ensure_ascii=False, indent=1), encoding="utf-8")
    first, last = (messages[0]["time"], messages[-1]["time"]) if messages else ("-", "-")
    print(f"{len(messages)} messages from '{getattr(entity, 'title', chat)}' ({first} to {last}) -> {out}")


async def main(chats: list[str], days: int) -> None:
    settings = load_settings()
    settings.require(*TELEGRAM_FIELDS)
    client = make_client(settings)
    await client.start(phone=settings.tg_phone)
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    for chat in chats:
        try:
            await save_history(client, chat, since)
        except Exception as exc:  # one unreadable channel must not stop the rest
            print(f"FAILED {chat}: {exc}")
    await client.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("chats", nargs="+", help="chat ids (e.g. -1003769891853) or @usernames")
    ap.add_argument("--days", type=int, default=0, help="only the last N days (0 = everything)")
    args = ap.parse_args()
    asyncio.run(main(args.chats, args.days))
