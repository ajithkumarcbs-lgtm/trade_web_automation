"""Saves the latest messages from the tips group into samples/ to test the AI on real tips.

    python -m tools.fetch_samples --limit 30

Each message becomes samples/<id>.txt (its text) and samples/<id>.jpg (its image, if any).
"""
import argparse
import asyncio

from tradebot.config import SAMPLES_DIR, TELEGRAM_FIELDS, load_settings
from tradebot.telegram_listener import make_client, resolve_chat, to_incoming


async def main(limit: int) -> None:
    settings = load_settings()
    settings.require(*TELEGRAM_FIELDS, "tg_chat")
    SAMPLES_DIR.mkdir(exist_ok=True)
    client = make_client(settings)
    await client.start(phone=settings.tg_phone)
    chat = await resolve_chat(client, settings.tg_chat)

    saved = 0
    async for msg in client.iter_messages(chat, limit=limit):
        incoming = await to_incoming(msg)
        if not incoming.text and not incoming.image:
            continue
        stem = SAMPLES_DIR / f"{incoming.id}"
        stem.with_suffix(".txt").write_text(
            f"# {incoming.date:%Y-%m-%d %H:%M:%S} UTC\n{incoming.text}", encoding="utf-8"
        )
        if incoming.image:
            stem.with_suffix(".jpg").write_bytes(incoming.image)
        saved += 1
    print(f"Saved {saved} messages from '{getattr(chat, 'title', chat.id)}' to {SAMPLES_DIR}")
    await client.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    asyncio.run(main(ap.parse_args().limit))
