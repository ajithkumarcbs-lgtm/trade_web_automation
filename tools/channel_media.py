"""Downloads a channel's photos and voice/video messages, and turns the speech into text with OpenAI.

    python -m tools.channel_media -1003769891853 --days 30
    python -m tools.channel_media -1003769891853 --days 30 --no-transcribe   # download only

Files go to data/history/<chat>/<message id>.<ext>; transcripts to data/history/<chat>/transcripts.json
({message id: {time, kind, seconds, text}}). Anything already downloaded or transcribed is skipped, so the
command can simply be run again after a failure. Stickers and GIFs are ignored.
"""
import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone

import openai

from tools.channel_history import IST, media_kind
from tradebot.config import DATA_DIR, TELEGRAM_FIELDS, load_settings
from tradebot.telegram_listener import make_client, resolve_chat
from tradebot.voice import Transcriber

SAVED_KINDS = ("photo", "voice", "video_note", "audio", "video")
EXTENSIONS = {"photo": ".jpg", "voice": ".ogg", "video_note": ".mp4"}  # Telegram voice = Ogg Opus
MAX_MB = 25  # OpenAI's limit for one transcription file


async def main(chat: str, days: int, transcribe: bool, model: str) -> None:
    settings = load_settings()
    settings.require(*TELEGRAM_FIELDS)
    if transcribe:
        settings.require("openai_api_key")
    client = make_client(settings)
    await client.start(phone=settings.tg_phone)
    entity = await resolve_chat(client, int(chat) if chat.lstrip("-").isdigit() else chat)
    folder = DATA_DIR / "history" / chat
    folder.mkdir(parents=True, exist_ok=True)
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None

    speech = []  # (message id, path, kind, seconds, time)
    photos = skipped = 0
    async for msg in client.iter_messages(entity):  # newest first
        if since and msg.date < since:
            break
        kind = media_kind(msg)
        if kind not in SAVED_KINDS:
            continue
        size_mb = (msg.file.size or 0) / 1_048_576 if msg.file else 0
        if size_mb > MAX_MB:
            print(f"skipped {msg.id} ({kind}, {size_mb:.0f} MB, too big)")
            skipped += 1
            continue
        path = folder / f"{msg.id}{EXTENSIONS.get(kind) or msg.file.ext or '.bin'}"
        if not path.exists():
            await client.download_media(msg, file=str(path))
        if kind == "photo":
            photos += 1
        else:
            seconds = int(getattr(msg.file, "duration", 0) or 0)
            speech.append((msg.id, path, kind, seconds, msg.date.astimezone(IST).strftime("%Y-%m-%d %H:%M")))
    await client.disconnect()
    minutes = sum(s for _, _, _, s, _ in speech) / 60
    print(f"{photos} photos and {len(speech)} voice/video messages ({minutes:.1f} min) in {folder}"
          + (f", {skipped} too big" if skipped else ""))
    if transcribe and speech:
        await transcribe_all(settings.openai_api_key, model, folder, speech)


async def transcribe_all(api_key: str, model: str, folder, speech: list) -> None:
    out = folder / "transcripts.json"
    transcripts = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    transcribe = Transcriber(api_key, model)
    for message_id, path, kind, seconds, time in sorted(speech):
        if str(message_id) in transcripts:
            continue
        try:
            text = await transcribe(path.read_bytes(), path.name)
        except (openai.APIError, ValueError) as exc:
            print(f"FAILED {message_id}: {exc}")
            continue
        transcripts[str(message_id)] = {"time": time, "kind": kind, "seconds": seconds, "text": text}
        out.write_text(json.dumps(transcripts, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{message_id} {time} {kind} {seconds}s: {text[:80]}")
    print(f"transcripts: {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("chat", help="chat id (e.g. -1003769891853) or @username")
    ap.add_argument("--days", type=int, default=30, help="only the last N days (0 = everything)")
    ap.add_argument("--no-transcribe", action="store_true", help="download only, don't call OpenAI")
    ap.add_argument("--model", default="gpt-4o-transcribe", help="OpenAI speech-to-text model")
    args = ap.parse_args()
    asyncio.run(main(args.chat, args.days, not args.no_transcribe, args.model))
