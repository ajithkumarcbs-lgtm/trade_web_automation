"""Turns a Telegram message (text and/or image) into structured bet signals using OpenAI."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import ssl
from datetime import datetime, timedelta

import openai
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
    "uae": "United Arab Emirates",
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
- "Khao" (eat, "khaoooo", "khalo") with a team or player named = LAY that team, it is the favourite, never a back: \
"98P - NEWZEALAND Khaooo" = lay New Zealand at 1.98. When the other side is named too ("60P TORONTO KHAO, MONTREAL \
PLUS KRO", "50P Coco Gauff khao, Elena win karo") it is one trade: one signal, the lay of the one to eat. \
"<X> plus karo" alone = back X.
- Player names are often misspelt: write the correct current player's name as shown in the screenshot or as \
you know it (Beaz -> Sebastian Baez, vechrot -> Valentin Vacherot, ruplev -> Andrey Rublev). If unsure of the \
first name, write the surname alone. Nicknames ("Karupan", "🐐", "GOAT") mean the player of the entry the \
message replies to, or else the latest entry.
- Tips often name a player by FIRST name only ("Quentin Win", "Dayana win karo", "Zizou", "Hubert"): the site \
lists players by surname, so write the full name of the player meant, taken from the screenshot, the earlier \
messages or the current players you know (Quentin -> Quentin Halys, Dayana -> Dayana Yastremska, Zizou -> \
Zizou Bergs, Hubert -> Hubert Hurkacz). Only when you can't tell who is meant, keep the first name as written.
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
GROQ_DEFAULTS = {"sport": "other", "event": "? v ?", "market": "other", "market_name": "", "selection": "", "side": "back",
                 "odds": None, "line": None, "units": None, "amount": None, "when_odds": None, "green_pct": None,
                 "loss_cut": False, "confidence": 0.0}


def ai_client(api_key: str, base_url: str | None = None) -> AsyncOpenAI:
    """OpenAI-compatible client that checks TLS certificates with Python's own ssl module (which reads the
    Windows certificate store) instead of the `truststore` package its HTTP library uses by default: on
    Windows that one now and then fails the first connection with "access violation ... CertFreeCertificateChain"."""
    return AsyncOpenAI(api_key=api_key, base_url=base_url,
                       http_client=DefaultAsyncHttpxClient(verify=ssl.create_default_context()))


# Text-only models tried in turn when the main one's free limit (per minute or day) is used up: each has its own
GROQ_FALLBACK_MODELS = ("openai/gpt-oss-120b", "openai/gpt-oss-20b")
# Groq counts the reply budget into its free 8000 tokens a minute: instructions + hint + context (~3.6-4.4k) + a
# screenshot (~1.8k) + this stays under it. Replies are 25-700 tokens (with the model's reasoning).
GROQ_MAX_REPLY = 1000
GROQ_MINUTE_WAIT = 20  # s: a per-minute limit that frees up within this is waited for (the tip is still fresh)
# The reply format for Groq, short (the full JSON schema with its descriptions is ~1,100 tokens every call).
GROQ_FORMAT = """
Reply with one JSON object only: {"summary": "<one short line for the log>", "signals": [<signal>, ...]}
signals = the instructions the NEW message gives (empty for results, ads, chat). Each signal has every key:
action: entry | add (more on an earlier position) | exit (take profit at a target price) | close (book set /
  loss cut / cashout NOW) | stake (the message only gives the stake of the entry just posted)
sport: cricket | tennis | other;  event: "<A> v <B>"
market: match_odds (who wins: the default for back/lay tips) | bookmaker | fancy (session / over runs lines) | other
market_name: as written in the message, else "";  selection: team or player (fancy: the session, "6 Over Runs IND")
side: back | lay | yes | no (yes/no only for fancy; for close, the side of the original position)
odds: the price quoted (fancy: the rate, e.g. 100), else null (always null for close)
line: the run line of a fancy tip ("48 yes" -> 48), else null
units: the stake in units, only when unit / u / limit is said ("Limit 2" -> 2, "half unit" -> 0.5), else null
amount: the stake in rupees when money is given ("Rs 500" -> 500, "1k" -> 1000), else null
when_odds: enter only once the price gets there ("3 odds pona edunga" -> 3), else null
green_pct: a re-entry staked with a share of the profit just booked ("green money 20%" -> 20), else null
loss_cut: true only for a close that is a loss cut;  confidence: 0-1, how sure you READ it right
"""
# Added to the instructions for a voice note a channel acts on (profile act_on_voice).
VOICE_HINT = """
The NEW message is a voice note typed out by speech-to-text: Tamil script mixed with English words, badly spelt \
(புக்செட் / புக்க்செட் / புக் செட் / book set / box set = bookset; லாஸ்கட் / லாஸ்ட் கட் / loss cut = loss cut; \
பேக் / பாக்க் = back; லிமிட் = limit; ஆட்ஸ் / ads = odds; பேவர் = favourite). Most voice notes are only talk: give no \
signal unless the listeners are told to act NOW on the match being played:
- close: "இப்போ bookset பண்ணிடுங்க", "loss cut பண்ணிக்கோங்க", "profit இருந்தா bookset பண்ணிக்கோங்க" (book it now if in \
profit). Naming no team is fine: leave selection empty.
- entry: "இப்போ ஒரு limit back பண்ணிக்கோங்க" with the team named (write it in English) or clear from the context.
- stake: only the limit of the entry just posted ("0.5 limit மட்டும்").
No signal for: a bookset still to come ("bookset ரெடியா இருங்க", "bookset வரும்", "நானே சொல்றேன்" = I will tell you); a \
price still to come ("1.8 range வந்தா bookset பண்ணலாம்"); what already happened or another match ("1.7ல bookset பண்ணோம்", \
"லாஸ்கட் பண்ணியிருக்கேன்"); advice for any time ("நல்ல profit வந்தா bookset நீங்களே பண்ணிக்கோங்க", "green money மட்டும் \
use பண்ணுங்க"); predictions, wicket zones, doubts, "wait பண்ணுங்க". Confidence 0.9 only when the command is plainly \
said; when unsure give no signal.
"""
# A tip's name the site's match list doesn't have (28 Oct: 11 of 17 missed entries were tennis first names).
# Asking for the full name first matters: asked "which side?" outright, Qwen answered null for all (28 Oct test).
PICK_SIDE = """\
A betting tip names a player or team the short way: a tennis player by first name ("Novak", "Carlos"), a team \
by a code or short name ("pkr", "Ads"). The betting site lists matches as "A v B" with surnames only, some with \
the start of the first name ("T Griekspoor v Nakashima").
1. full_name: who the tip's name is, from the current players and teams you know (first name + surname, or the \
team's full name), else "".
2. Find that surname / team in the listed matches.
Reply with one JSON object only: {"full_name": "<who it is>", "index": <number of the listed match with that \
player/team, or null if not listed>, "side": "<that side exactly as listed, or empty>"}"""


def make_parser(settings) -> "SignalParser":
    groq = settings.ai_provider == "groq"
    openai_key = settings.openai_api_key if (not groq or settings.groq_openai_fallback) else ""
    return SignalParser(openai_key, settings.openai_model, settings.openai_image_detail,
                        settings.groq_api_key if groq else "", settings.groq_model)


class SignalParser:
    """Reads tips with Groq (free tier) when AI_PROVIDER=groq, screenshots included (its model must take
    images, e.g. Qwen 3.8), else with OpenAI. With Groq and an OpenAI key too (GROQ_OPENAI_FALLBACK), a message
    is read by OpenAI only once every Groq model's free limit is used up; without one, it is not read at all."""

    def __init__(self, api_key: str, model: str, image_detail: str = "high", groq_key: str = "",
                 groq_model: str = "qwen/qwen3.8-27b"):
        self._client = ai_client(api_key) if api_key else None
        self._groq = ai_client(groq_key, GROQ_URL) if groq_key else None
        self._model = model
        self._groq_model = groq_model
        self._image_detail = image_detail
        self.last_usage: tuple[int, int] = (0, 0)  # (input, output) tokens of the last call

    @property
    def name(self) -> str:
        if self._groq:
            return f"groq {self._groq_model}" + (f" (OpenAI {self._model} when its limits are used up)"
                                                 if self._client else "")
        return f"openai {self._model}"

    async def parse(self, text: str, image: bytes | None = None, context: list[str] | None = None,
                    reply_to: str = "", hint: str = "") -> ParsedMessage:
        """hint: what is special about this channel's tipster (channels.json), added to the instructions."""
        prompt = f"NEW message:\n{text.strip() or '(no text)'}"
        if reply_to:
            prompt = f"The NEW message is a reply to this earlier message: {reply_to}\n\n" + prompt
        if context:
            prompt = "Earlier messages, oldest first (context only):\n" + "\n".join(context) + "\n\n" + prompt
        instructions = INSTRUCTIONS + (f"\nAbout this channel's tipster:\n{hint.strip()}\n" if hint.strip() else "")
        if self._groq:
            parsed = await self._parse_groq(prompt, instructions, image)
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

    async def _parse_groq(self, prompt: str, instructions: str = INSTRUCTIONS,
                          image: bytes | None = None) -> ParsedMessage:
        system = instructions + GROQ_FORMAT
        content: str | list[dict] = prompt
        picture = image
        if image:
            try:
                picture = _shrink(image)
            except Exception:  # not a picture Pillow knows: sent as it is
                log.warning("could not scale the screenshot down", exc_info=True)
            content = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": _data_url(picture)}}]
        models = [self._groq_model, *(m for m in GROQ_FALLBACK_MODELS if m != self._groq_model)]
        model = models.pop(0)
        waited: set[str] = set()
        for attempt in range(4 + 2 * len(GROQ_FALLBACK_MODELS)):
            try:
                response = await self._groq.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
                    response_format={"type": "json_object"},
                    temperature=0,
                    max_tokens=GROQ_MAX_REPLY,
                )
            except openai.RateLimitError as exc:
                which = re.search(r"on (tokens|requests) per (minute|day)[^.]*\. Limit \d+, Used \d+, Requested \d+",
                                  str(exc))
                wait = _retry_after(str(exc))
                if which and which.group(2) == "minute" and wait is not None and wait <= GROQ_MINUTE_WAIT \
                        and model not in waited:
                    waited.add(model)  # a burst of messages: this minute's tokens free up in a moment
                    log.info("Groq %s busy this minute: reading again in %.0fs", model, wait)
                    await asyncio.sleep(wait + 0.5)
                    continue
                if not models:
                    if self._client:
                        log.warning("Groq's free limits are used up: reading with OpenAI %s", self._model)
                        return await self._parse_openai(prompt, image, instructions)
                    raise
                log.warning("Groq %s limit reached (%s): reading with %s (text only)", model,
                            which.group(0) if which else str(exc)[:160], models[0])
                model, content = models.pop(0), prompt
                continue
            except openai.BadRequestError as exc:
                if "json_validate_failed" in str(exc):
                    # the model's reply wasn't valid JSON (27 Oct: the message went unread): again, then the next model
                    if attempt and models:
                        model, content = models.pop(0), prompt  # the fallback models read text only
                    log.warning("Groq reply wasn't valid JSON: reading again with %s", model)
                    continue
                if not image or isinstance(content, str):
                    raise
                log.warning("Groq %s refused the screenshot (%s): reading the text only", model, exc)
                content = prompt
                continue
            if response.usage:
                self.last_usage = (response.usage.prompt_tokens, response.usage.completion_tokens)
            try:
                data = json.loads(response.choices[0].message.content or "{}")
                for sig in data.get("signals") or []:
                    for key, value in GROQ_DEFAULTS.items():
                        if sig.get(key) is None:  # left out, or null (a close naming no team: side and selection)
                            sig[key] = value
                data.setdefault("summary", "")
                return ParsedMessage.model_validate(data)
            except (ValueError, ValidationError) as exc:
                log.warning("Groq reply not in the expected format (attempt %d): %s", attempt + 1, exc)
        return ParsedMessage(signals=[], summary="(AI reply unreadable)")

    async def resolve_name(self, name: str, event: str, listed: list[str]) -> str | None:
        """The side of the site's list ('T Griekspoor') that a tip's short name ('Tallon') means, as listed, or
        None when no model can tell. Each model knows different players (28 Oct: Qwen knew Zizou Bergs and Jacob
        Fearnley, gpt-oss Tallon Griekspoor and Frances Tiafoe), so the next one is asked when one can't say."""
        matches = "\n".join(f"{i}. {n[:60]}" for i, n in enumerate(listed))
        prompt = f"The tip names: {name} (its match: {event})\nListed matches:\n{matches}"
        async for model, reply in self._json_replies(PICK_SIDE, prompt):
            side = _listed_side(reply, listed)
            if side:
                log.info("AI (%s): '%s' is %s, listed as %s", model, name, reply.get("full_name") or "?", side)
                return side
        return None

    async def _json_replies(self, system: str, prompt: str):
        """A small text question answered as JSON by each Groq model in turn (one at its limit is skipped, not
        waited for), then OpenAI: yields (model, reply) until the caller has its answer."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        if self._groq:
            for model in [self._groq_model, *(m for m in GROQ_FALLBACK_MODELS if m != self._groq_model)]:
                try:
                    response = await self._groq.chat.completions.create(
                        model=model, messages=messages, response_format={"type": "json_object"}, temperature=0,
                        max_tokens=GROQ_MAX_REPLY)
                except (openai.RateLimitError, openai.BadRequestError) as exc:
                    log.info("Groq %s could not answer (%s): asking the next model", model, str(exc)[:120])
                    continue
                yield model, _json_object(response.choices[0].message.content)
        if self._client:
            response = await self._client.chat.completions.create(
                model=self._model, messages=messages, response_format={"type": "json_object"})
            yield self._model, _json_object(response.choices[0].message.content)


def _side_key(side: str) -> str:
    """The word that tells a side apart: 'Griekspoor' of 'T Griekspoor', 'Stars' of 'Melbourne Stars W' (both sides of
    a women's match end in W: 31 Oct, 'Melbourne Stars W' was taken for the 'Adelaide Strikers W' listed first)."""
    words = [w for w in re.findall(r"[a-z0-9]+", side.lower()) if w not in ("w", "women", "men", "xi")]
    return words[-1] if words else ""


def _listed_side(reply: dict, listed: list[str]) -> str | None:
    """The side of the listed match the AI named, spelt as listed ('Griekspoor' -> 'T Griekspoor'), or None."""
    index, side = reply.get("index"), str(reply.get("side") or "").strip().lower()
    if isinstance(index, str) and index.isdigit():
        index = int(index)
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(listed) or not side:
        return None
    sides = [s.strip() for s in re.split(r"\s+(?:v|vs)\s+", listed[index], flags=re.IGNORECASE)]
    fits = [s for s in sides if s.lower() == side] or [s for s in sides if _side_key(s) and _side_key(s) == _side_key(side)]
    return fits[0] if len(fits) == 1 else None


def _json_object(text: str | None) -> dict:
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _retry_after(error: str) -> float | None:
    """Seconds Groq says to wait ('Please try again in 1m2.5s' / 'in 830ms'), None if it doesn't say."""
    m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)s)?(?:([\d.]+)ms)?", error)
    if not m or not any(m.groups()):
        return None
    hours, minutes, seconds, millis = (float(g) if g else 0.0 for g in m.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


GROQ_IMAGE_SIDE = 768  # px: a full-size screenshot is more tokens than Groq's free tier takes in one minute


def _shrink(image: bytes, side: int = GROQ_IMAGE_SIDE) -> bytes:
    """The screenshot scaled down to `side` px on its longer edge, as JPEG (odds and names stay readable)."""
    from PIL import Image  # only needed with Groq

    with Image.open(io.BytesIO(image)) as picture:
        if max(picture.size) <= side:
            return image
        picture.thumbnail((side, side))
        out = io.BytesIO()
        picture.convert("RGB").save(out, "JPEG", quality=85)
        return out.getvalue()


def _data_url(image: bytes) -> str:
    if image.startswith(b"\x89PNG"):
        mime = "image/png"
    elif image[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/jpeg"  # Telegram photos are JPEG
    return f"data:{mime};base64,{base64.b64encode(image).decode()}"
