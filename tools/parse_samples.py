"""Runs the AI parser over samples/ and prints what it understood from each message, with token cost.

    python -m tools.parse_samples            # all samples
    python -m tools.parse_samples 611 612    # only these message ids (earlier ones still used as context)

Messages are replayed in order with the same chat context the live bot uses.
Compare models by changing OPENAI_MODEL in .env and running this again.
"""
import argparse
import asyncio
from collections import deque
from datetime import datetime, timezone

from tradebot.config import SAMPLES_DIR, load_settings
from tradebot.models import as_bet
from tradebot.signal_parser import CONTEXT_MESSAGES, CONTEXT_WINDOW, context_line, make_parser

# USD per 1M tokens (input, output), from developers.openai.com model pages, Oct 2026.
PRICES = {"gpt-5.4-mini": (0.75, 4.50), "gpt-5.4-nano": (0.20, 1.25)}


def load_sample(stem: str) -> tuple[datetime, str, bytes | None]:
    header, _, text = (SAMPLES_DIR / f"{stem}.txt").read_text(encoding="utf-8").partition("\n")
    date = datetime.strptime(header, "# %Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    jpg = SAMPLES_DIR / f"{stem}.jpg"
    return date, text, jpg.read_bytes() if jpg.exists() else None


async def main(ids: list[str]) -> None:
    settings = load_settings()
    settings.require("groq_api_key" if settings.ai_provider == "groq" else "openai_api_key")
    parser = make_parser(settings)
    price_in, price_out = PRICES.get(settings.openai_model, (0, 0))
    total_in = total_out = count = 0

    stems = sorted((p.stem for p in SAMPLES_DIR.glob("*.txt")), key=lambda s: int(s) if s.isdigit() else 0)
    if not [s for s in stems if not ids or s in ids]:
        print(f"No sample messages in {SAMPLES_DIR}.\n"
              "Download some from the tips group first:  python -m tools.fetch_samples --limit 30")
        return

    history: deque[tuple[datetime, str]] = deque(maxlen=CONTEXT_MESSAGES)
    for stem in stems:
        date, text, image = load_sample(stem)
        context = [line for d, line in history if date - d <= CONTEXT_WINDOW]
        history.append((date, context_line(date, text, image is not None)))
        if ids and stem not in ids:
            continue

        parsed = await parser.parse(text, image, context)
        tokens_in, tokens_out = parser.last_usage
        total_in, total_out, count = total_in + tokens_in, total_out + tokens_out, count + 1

        print(f"\n=== {stem}{' [image]' if image else ''}: {text.strip()[:80]!r}")
        print(f"    {parsed.summary}   [{tokens_in} in / {tokens_out} out tokens]")
        for sig in parsed.signals:
            print(f"    -> {as_bet(sig).describe()}  (sport={sig.sport}, confidence={sig.confidence:.2f})")
        if not parsed.signals:
            print("    -> no bet")

    print(f"\n{settings.openai_model}, image detail {settings.openai_image_detail}: {count} messages, "
          f"{total_in} input + {total_out} output tokens")
    if price_in:
        cost = (total_in * price_in + total_out * price_out) / 1_000_000
        print(f"Cost ${cost:.4f} total, ${cost / count:.5f} per message "
              f"(~${cost / count * 100:.2f} per 100 messages)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*")
    asyncio.run(main(ap.parse_args().ids))
