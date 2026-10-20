"""Turns a Telegram message (text and/or image) into structured bet signals using OpenAI."""
from __future__ import annotations

import base64
import json
import logging
import ssl
from datetime import datetime, timedelta

from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from pydantic import ValidationError

from .models import ParsedMessage

log = logging.getLogger(__name__)

# How much recent chat the AI sees, so "Book set" can be tied to the earlier "Wi back 6 odds".
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

How tips are written (real examples from the channel, Tamil words in English letters):
- Entry: "Back vechrot 1 unit", "Back snigur 4 odds 1 unit", "Back ruplev 2.4 1 unit", \
"Beaz 4 odds la entry ponga 1 unit" ("entry ponga" = take the entry), "Back 5 dds darderi just 0.5 unit" \
("dds" = odds, the number can come before the name), "<team> back <odds>". "<team> lay <odds>" = lay entry. \
"only", "don't go high" = don't stake more than stated; not an instruction on their own. The odds may be missing.
- Player names are often misspelt: write the correct current player's name as shown in the screenshot or as \
you know it (Beaz -> Sebastian Baez, vechrot -> Valentin Vacherot, ruplev -> Andrey Rublev). If unsure of the \
first name, write the surname alone. Nicknames ("Karupan", "🐐", "GOAT") mean the player of the entry the \
message replies to, or else the latest entry.
- Stake size in units: "1 unit", "2 units", "2u", "half unit" (0.5), "1/2 unit" (0.5) -> units. \
"limit" is also used for units when it comes with a back/lay entry: "1 limit back svrcina 2.12", \
"Back svrcina 2.12 odds 1 limit", "half limit" -> units 1, 1, 0.5. \
Stake as money: "Rs 10", "rs10", "₹500", "for 1000", "1000 rs", "1k", "2.5k" -> amount in rupees (10, 500, \
1000, 1000, 2500). A plain number after the odds is money, not units. Leave both null if the message gives \
no stake.
- "limit" always means stake size in units, never a price. On its own right after an entry ("Limit 1.5", \
"Limit 1 only", "Only one limit", "Half limit", "Total 3.5 limit") it is the stake of the entry just posted: \
return one signal with action "stake", units = that number (one = 1, half = 0.5), selection = that entry's \
team, odds null. "Again back 2 limit add" = add.
- An entry for later, once the price gets there: "Berriting 3 odds pona edunga" (= take it if it goes to 3), \
"First entry if 3 odds reached means taken back oman limit 1", "Usa win but high odds 4 or 3.7 odds back if \
come means back" -> entry with when_odds = that price (the lower one when two are given) and odds = the same.
- Green money = a re-entry staked with part of the profit just booked: "Green money 20% back 3.7 odds draw \
again", "Next green money 10% or 20% take. Back 14 odds vic", "8 odds back ind risky green money 20% only" -> \
entry with green_pct = 20 (the larger number when two are given), units and amount null. A message that is \
only "Green 20%" right after an entry -> action "stake" with green_pct 20.
- Exit at a price = the word exit with a price: "If odds up means 4 or 3.9 exit", "exit at 2.9", \
"3.2 odds now exit with profit" -> exit with that odds (take the first price when two are given).
- Close = book the open trade NOW: "bookset", "book set", "boookset", "book pannuga" / "book pannunga" \
("do book"), "bookset pls quick", "bookset done", "book profit", "cashout", "loss cut", "losscut", "cut loss", \
"exit now". This holds even with celebration words, emojis or a profit figure ("Boooom bookset done both side \
1.7 unit profit" = close): that message is often the only signal to book. "both side 1 unit profit" is the \
profit, not a stake: leave units and amount null for close. A price in it ("Bookset pls quick both side \
1 unit 1.55", "Book pannuga 2.20 odds la", "Loss cut 2.5 la") is the booking price: put it in odds.
- For close and exit, selection is never empty: the team or player the message itself names, even when no \
entry on it is in the earlier messages ("Now bookset new south" = New South Wales); when it names none, the \
player of the entry the message replies to, else the latest entry in the earlier messages.
- Not yet a close (return no signals): being ready for or waiting for a bookset ("Ready for bookset", "Wait for \
bookset", "Bookset coming", "Be ready bookset", "Zim fav come wait for bookset").
- NOT instructions (return no signals): the tipster explaining what he did earlier or on another match \
("Guys na already bookset ... deminaur ... again ruplev kuduthurukan"), saying he missed a trade ("missed ... \
entry"), results ("-0.50❌", "+1.7 unit", "won the set" on its own), "Active", "Be active", "wait for next one", \
accumulators / parlays ("accumulater"), greetings and chat.
- "Again back <odds> limit add" or "add" = add: back the same selection again at <odds>.
- "<team> win", "<team> fav or <odds>", "<n> plus expected", "all out expected", "must play game" \
= predictions or hype, not bets.
- Price updates and results are not entries or closes: "Sl 1.2 coming", "1.66 fav ban", "Wi 1.26 fire 🔥", \
"6 odds to 1.5 fav rno", "15 odds to 3.2 now", "3.7 odds reached", "my side non cutting done", "my side exit with \
small profit", "group member profit done", "Loss 1.5 limit only, next match". A stake named in such a message \
or in a plan ("1.2 limit only loss", "2 limit match", "One match 1.5 or 2 limit", "No more entry") is not a \
stake signal either.
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


GROQ_URL = "https://api.groq.com/openai/v1"
# Values used when Groq's JSON leaves a field out. A missing confidence counts as unreadable (skipped).
GROQ_DEFAULTS = {"sport": "other", "event": "? v ?", "market": "other", "market_name": "", "side": "back",
                 "odds": None, "line": None, "units": None, "amount": None, "when_odds": None, "green_pct": None,
                 "loss_cut": False, "confidence": 0.0}


def ai_client(api_key: str, base_url: str | None = None) -> AsyncOpenAI:
    """OpenAI-compatible client that checks TLS certificates with Python's own ssl module (which reads the
    Windows certificate store) instead of the `truststore` package its HTTP library uses by default: on
    Windows that one now and then fails the first connection with "access violation ... CertFreeCertificateChain"."""
    return AsyncOpenAI(api_key=api_key, base_url=base_url,
                       http_client=DefaultAsyncHttpxClient(verify=ssl.create_default_context()))


def make_parser(settings) -> "SignalParser":
    return SignalParser(settings.openai_api_key, settings.openai_model, settings.openai_image_detail,
                        settings.groq_api_key if settings.ai_provider == "groq" else "", settings.groq_model)


class SignalParser:
    """Reads tips with Groq when a Groq key is given, else OpenAI. Images always go to OpenAI
    (Groq has no production vision model); without an OpenAI key only the message text is read."""

    def __init__(self, api_key: str, model: str, image_detail: str = "high", groq_key: str = "",
                 groq_model: str = "llama-3.3-70b-versatile"):
        self._client = ai_client(api_key) if api_key else None
        self._groq = ai_client(groq_key, GROQ_URL) if groq_key else None
        self._model = model
        self._groq_model = groq_model
        self._image_detail = image_detail
        self.last_usage: tuple[int, int] = (0, 0)  # (input, output) tokens of the last call

    @property
    def name(self) -> str:
        return f"groq {self._groq_model}" if self._groq else f"openai {self._model}"

    async def parse(self, text: str, image: bytes | None = None, context: list[str] | None = None,
                    reply_to: str = "", hint: str = "") -> ParsedMessage:
        """hint: what is special about this channel's tipster (channels.json), added to the instructions."""
        prompt = f"NEW message:\n{text.strip() or '(no text)'}"
        if reply_to:
            prompt = f"The NEW message is a reply to this earlier message: {reply_to}\n\n" + prompt
        if context:
            prompt = "Earlier messages, oldest first (context only):\n" + "\n".join(context) + "\n\n" + prompt
        instructions = INSTRUCTIONS + (f"\nAbout this channel's tipster:\n{hint.strip()}\n" if hint.strip() else "")
        if self._groq and not (image and self._client):
            if image:
                log.warning("image tip read as text only: Groq has no vision model and no OPENAI_API_KEY is set")
            parsed = await self._parse_groq(prompt, instructions)
        else:
            parsed = await self._parse_openai(prompt, image, instructions)
        for sig in parsed.signals:
            sig.confidence = min(max(sig.confidence, 0.0), 1.0)
            # A plain back/lay tip that names no market is Match Odds; the AI sometimes says "other".
            if sig.market == "other" and sig.side in ("back", "lay") and not sig.market_name.strip():
                sig.market = "match_odds"
            sig.selection = expand_team(sig.selection)
            sig.event = " v ".join(expand_team(side) for side in sig.event.split(" v "))
        return parsed

    async def _parse_openai(self, prompt: str, image: bytes | None, instructions: str = INSTRUCTIONS) -> ParsedMessage:
        content: list[dict] = [{"type": "input_text", "text": prompt}]
        if image:
            content.append({"type": "input_image", "image_url": _data_url(image), "detail": self._image_detail})
        response = await self._client.responses.parse(
            model=self._model,
            instructions=instructions,
            input=[{"role": "user", "content": content}],
            text_format=ParsedMessage,
        )
        if response.usage:
            self.last_usage = (response.usage.input_tokens, response.usage.output_tokens)
        return response.output_parsed or ParsedMessage(signals=[], summary="(AI returned no result)")

    async def _parse_groq(self, prompt: str, instructions: str = INSTRUCTIONS) -> ParsedMessage:
        system = (instructions + "\nReply with one JSON object only, matching this JSON schema:\n"
                  + json.dumps(ParsedMessage.model_json_schema()))
        for attempt in range(2):
            response = await self._groq.chat.completions.create(
                model=self._groq_model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0,
            )
            if response.usage:
                self.last_usage = (response.usage.prompt_tokens, response.usage.completion_tokens)
            try:
                data = json.loads(response.choices[0].message.content or "{}")
                for sig in data.get("signals") or []:
                    for key, value in GROQ_DEFAULTS.items():
                        sig.setdefault(key, value)
                data.setdefault("summary", "")
                return ParsedMessage.model_validate(data)
            except (ValueError, ValidationError) as exc:
                log.warning("Groq reply not in the expected format (attempt %d): %s", attempt + 1, exc)
        return ParsedMessage(signals=[], summary="(AI reply unreadable)")


def _data_url(image: bytes) -> str:
    if image.startswith(b"\x89PNG"):
        mime = "image/png"
    elif image[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/jpeg"  # Telegram photos are JPEG
    return f"data:{mime};base64,{base64.b64encode(image).decode()}"
