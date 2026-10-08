"""Turns a Telegram message (text and/or image) into structured bet signals using OpenAI."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta

from openai import AsyncOpenAI

from .models import ParsedMessage

# How much recent chat the AI sees, so "Limit 1.5" can be tied to the earlier "Wi back 6 odds".
CONTEXT_MESSAGES = 15
CONTEXT_WINDOW = timedelta(hours=6)

# Cricket short forms used by tipsters, expanded to the names the site lists (whole names only).
TEAM_SHORT_FORMS = {
    "ind": "India", "wi": "West Indies", "aus": "Australia", "eng": "England", "pak": "Pakistan",
    "sa": "South Africa", "nz": "New Zealand", "sl": "Sri Lanka", "ban": "Bangladesh", "afg": "Afghanistan",
    "zim": "Zimbabwe", "ire": "Ireland", "ned": "Netherlands", "sco": "Scotland", "nam": "Namibia",
}


def expand_team(name: str) -> str:
    return TEAM_SHORT_FORMS.get(name.strip().lower(), name)


def context_line(date: datetime, text: str, has_image: bool) -> str:
    return f"[{date:%H:%M}] {text.strip()}{' (image)' if has_image else ''}"


INSTRUCTIONS = """\
You read a Telegram group where a tipster trades cricket and tennis on a betting exchange \
(Match Odds, Bookmaker and Fancy/session markets). Turn the NEW message into bet instructions.
Earlier messages are context only (which match is on, which position is open). \
Never extract bets from them; a bet must be instructed by the NEW message itself.

How tips are written:
- "<team> back <odds>" or "<team> back <odds> odds" = entry: back that team at those odds, Match Odds. \
"<team> lay <odds>" = entry: lay that team.
- Stake size in units: "1 unit", "2 units", "2u", "half unit" (0.5), "1/2 unit" (0.5) -> units. \
Stake as money: "Rs 10", "rs10", "₹500", "for 1000", "1000 rs", "1k", "2.5k" -> amount in rupees (10, 500, \
1000, 1000, 2500). A plain number after the odds is money, not units. Leave both null if the message gives \
no stake.
- "Limit <odds>" or "Total <odds> limit" after an entry = exit: take profit on that selection at <odds>.
- "Book set", "bookset", "book", "book profit", "cashout", "cash out", "loss cut", "losscut", "cut loss", \
"exit now", "close" = close: close the open position on that selection now.
- For close and exit, selection is never empty: if the message names no team, it is the team of the latest \
entry in the earlier messages.
- "Again back <odds> limit add" or "add" = add: back the same selection again at <odds>.
- "<team> win", "<team> fav or <odds>", "<n> plus expected", "all out expected", "must play game" \
= predictions or hype, not bets.
- "<a> to <b> over <team> zone" = a momentum forecast for those overs, not a bet.
- "Wait for my entry", results ("run out", "reversed", fire emojis), scorecard screenshots \
and motivation = not bets.
- Abbreviations: ind India, wi West Indies, aus Australia, eng England, pak Pakistan, \
sa South Africa, nz New Zealand, sl Sri Lanka, ban Bangladesh, afg Afghanistan, zim Zimbabwe.

General rules:
- back = bet FOR the selection; lay = bet AGAINST it. Fancy/session lines use yes (over) or no (under).
- sport: cricket or tennis when you can tell from the names or context (national teams, IPL/T20 sides, \
"over", "wicket" = cricket; individual player names = tennis). Use "other" only when it is clearly \
another sport.
- Expand the abbreviations listed above (wi -> West Indies). Keep every other team and player name as written \
in the message or image ("USA" stays "USA", "KKR" stays "KKR"). Write the event as "<A> v <B>", taking the \
opponent from the context: \
two teams mentioned together in recent messages (e.g. "ind zone" ... "wi zone") are the two sides of \
the current match. If the opponent is unknown, write "<A> v ?". Never guess a team that is not in the messages.
- odds: the price quoted ("6 odds" -> 6, "@1.85" -> 1.85). For fancy, line is the runs number and \
odds is the rate ("48 yes @ 100" -> line 48, odds 100).
- If the new message is shorthand you cannot map to a clear instruction, return your best reading \
with confidence below 0.5. Never invent a bet.
"""


class SignalParser:
    def __init__(self, api_key: str, model: str, image_detail: str = "high"):
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model
        self._image_detail = image_detail
        self.last_usage: tuple[int, int] = (0, 0)  # (input, output) tokens of the last call

    async def parse(self, text: str, image: bytes | None = None, context: list[str] | None = None) -> ParsedMessage:
        prompt = f"NEW message:\n{text.strip() or '(no text)'}"
        if context:
            prompt = "Earlier messages, oldest first (context only):\n" + "\n".join(context) + "\n\n" + prompt
        content: list[dict] = [{"type": "input_text", "text": prompt}]
        if image:
            content.append({"type": "input_image", "image_url": _data_url(image), "detail": self._image_detail})

        response = await self._client.responses.parse(
            model=self._model,
            instructions=INSTRUCTIONS,
            input=[{"role": "user", "content": content}],
            text_format=ParsedMessage,
        )
        if response.usage:
            self.last_usage = (response.usage.input_tokens, response.usage.output_tokens)
        parsed = response.output_parsed
        if parsed is None:
            return ParsedMessage(signals=[], summary="(AI returned no result)")
        for sig in parsed.signals:
            sig.confidence = min(max(sig.confidence, 0.0), 1.0)
            # A plain back/lay tip that names no market is Match Odds; the AI sometimes says "other".
            if sig.market == "other" and sig.side in ("back", "lay") and not sig.market_name.strip():
                sig.market = "match_odds"
            sig.selection = expand_team(sig.selection)
            sig.event = " v ".join(expand_team(side) for side in sig.event.split(" v "))
        return parsed


def _data_url(image: bytes) -> str:
    if image.startswith(b"\x89PNG"):
        mime = "image/png"
    elif image[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/jpeg"  # Telegram photos are JPEG
    return f"data:{mime};base64,{base64.b64encode(image).decode()}"
