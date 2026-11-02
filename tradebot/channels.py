"""Which Telegram chats the bot follows and how it treats each one (channels.json in the project root).

Chats are followed by their numeric id, which never changes when a channel is renamed. A channel can also
list a `title_regex`: any chat you join later whose title matches it is followed too (e.g. the tipster's
next tournament channel), and you get a Telegram alert when that happens. Without channels.json the bot
follows the single TELEGRAM_CHAT from .env, as before.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from .config import ROOT, Settings

log = logging.getLogger(__name__)

CHANNELS_FILE = ROOT / "channels.json"
DEFAULT = "default"  # name of the single-chat channel used without channels.json
MANUAL = "manual"  # strict commands you type in your Telegram Saved Messages

# .env settings a channel may set differently for its own trades
OVERRIDABLE = {"unit_stake", "max_units_per_bet", "min_odds", "max_odds", "odds_tolerance", "odds_tolerance_abs",
               "min_confidence", "auto_bookset_units", "max_liability_per_bet", "bookset_green_wait_s",
               "book_only_green", "signal_max_age_s", "conditional_entry_min", "both_sides_in_match"}


@dataclass(frozen=True)
class Profile:
    """How the tips of a channel are read and traded."""
    name: str
    trade: bool = True  # False: only watched (e.g. for new group links), never traded
    rules: str = ""  # rule set that double-checks the AI on the usual formats: tipster | ss | tennistv | david | mine |
    # sk | panther_toss | ""
    rules_only: bool = False  # only what the rule set reads; no AI (a channel of one format, which the AI can only misread)
    rules_first: bool = False  # what the rule set reads goes without the AI, whose free limit can run out; the rest goes to it
    strict_only: bool = False  # only the strict command format (BACK Shelton 3.25 1u ...); no AI
    voice: bool = False  # voice notes / video notes are transcribed: context for the next messages
    act_on_voice: bool = False  # with voice: a voice note can also be an instruction by itself (needs good transcripts)
    entry_words: str = ""  # regex: an entry only the AI read, from a text without a screenshot, needs one of these
    act_on_forwards: bool = True  # False: forwarded messages (members' "book set maar diya" testimonials) are context
    ai_filter: bool = False  # True: a text without a picture and without any instruction word skips the AI (saves
    # a free tier's daily tokens: "Good morning", zone lists, promos); it stays in the context
    fancy_paper: bool = False  # True: fancy (session) tips are kept on paper and settled from Cricbuzz's scores
    bare_photos: bool = True  # with ai_filter, False: a photo without a caption (result / promo albums) skips the AI
    hint: str = ""  # extra instructions for the AI about this tipster's style
    aliases: dict[str, str] = field(default_factory=dict)  # tipster's short names -> the site's names
    settings: dict = field(default_factory=dict)  # .env settings that differ for this channel's trades
    small_units: float = 0.5  # stake for "try small amount" tips without a stake; 0 = skip them
    rupees_per_unit: float = 0  # the tipster's own rupees for 1 unit: "Rs500" = 500/this of your units; 0 = your rupees
    green_money: str = "skip"  # "green 20% back 7 odds": skip, or stake that % of the profit just booked
    stake_wait_s: int = 0  # an entry without a stake waits this long for its "Limit 2" / "Green 20%" message
    limit_followup_s: int = 0  # "Limit 2" this many seconds after an entry tops it up to 2 units; 0 = ignore
    enter_before_start_min: int = 0  # a tip before its match is entered this many minutes before the start; 0 = at once
    ladder: bool = False  # a tip that a team wins is traded by laying the other team in steps (tradebot/ladder.py)
    ladder_rules: dict = field(default_factory=dict)  # tradebot/ladder.py LadderRules that differ for this channel
    jackpot_source: bool = False  # the channel's 'Nov 5 Jackpot Alert' posts set the jackpot days
    jackpot_days_only: bool = False  # live: entries only on jackpot days; paper: every day, the days reported apart
    pasted: bool = False  # tips pasted by hand from WhatsApp: stamped lines that aren't new are history, never traded
    paper_only: bool = False  # a live run ignores this channel: DRY_RUN=false must not put every test channel on real money
    confirm: dict = field(default_factory=dict)  # ask you first: always, below_confidence, above_units, disagree
    watch_links: bool = False  # alert you when the channel posts a Telegram group link


@dataclass
class Channel:
    name: str
    profile: Profile
    ids: set[int] = field(default_factory=set)
    title_regex: str = ""
    refs: list[str] = field(default_factory=list)  # @usernames still to resolve to ids at start-up

    def title_matches(self, title: str) -> bool:
        return bool(self.title_regex and title and re.search(self.title_regex, title, re.IGNORECASE))


class Channels:
    def __init__(self, base: Settings, channels: list[Channel], single: bool = False):
        self.base = base
        self.single = single  # no channels.json: every message comes from the one TELEGRAM_CHAT
        self._channels = {c.name: c for c in channels}
        self._settings: dict[str, Settings] = {}
        for channel in channels:
            self._settings[channel.name] = _apply(base, channel.profile)
        manual = Channel(MANUAL, Profile(MANUAL, strict_only=True))
        self._channels.setdefault(MANUAL, manual)
        self._settings.setdefault(MANUAL, base)

    @classmethod
    def load(cls, base: Settings, path: Path = CHANNELS_FILE) -> "Channels":
        if not path.exists():
            return cls.single_chat(base)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise SystemExit(f"{path.name} is not valid JSON: {exc}") from exc
        profiles = {name: _profile(name, values) for name, values in (raw.get("profiles") or {}).items()}
        channels = []
        for item in raw.get("channels") or []:
            profile_name = item.get("profile", "")
            if profile_name not in profiles:
                raise SystemExit(f"{path.name}: channel '{item.get('name')}' uses unknown profile '{profile_name}'")
            ids, refs = set(), []
            for ref in item.get("ids") or []:
                text = str(ref).strip()
                if text.lstrip("-").isdigit():
                    ids.add(int(text))
                elif text:
                    refs.append(text)
            channels.append(Channel(item["name"], profiles[profile_name], ids, item.get("title_regex", ""), refs))
        if not channels:
            raise SystemExit(f"{path.name} lists no channels")
        return cls(base, channels)

    @classmethod
    def single_chat(cls, base: Settings) -> "Channels":
        ref = base.tg_chat
        ids = {ref} if isinstance(ref, int) else set()
        refs = [] if isinstance(ref, int) or not ref else [str(ref)]
        return cls(base, [Channel(DEFAULT, Profile(DEFAULT), ids, refs=refs)], single=True)

    # --- lookups ----------------------------------------------------------------------------------

    def for_chat(self, chat_id: int) -> Channel | None:
        for channel in self._channels.values():
            if chat_id in channel.ids:
                return channel
        if self.single and (chat_id == 0 or not self._channels[DEFAULT].ids):
            return self._channels[DEFAULT]  # tests and a chat given by @username before it is resolved
        return None

    def get(self, name: str) -> Channel | None:
        return self._channels.get(name)

    def by_title(self, title: str) -> Channel | None:
        return next((c for c in self._channels.values() if c.title_matches(title)), None)

    def settings_for(self, name: str) -> Settings:
        return self._settings.get(name, self.base)

    def all(self) -> list[Channel]:
        return [c for c in self._channels.values() if c.name != MANUAL]

    def all_ids(self) -> set[int]:
        return {chat_id for c in self.all() for chat_id in c.ids}

    def any_voice(self) -> bool:
        return any(c.profile.voice for c in self.all())

    def any_auto_bookset(self) -> bool:
        return bool(self.base.auto_bookset_units) or any(self.settings_for(c.name).auto_bookset_units
                                                         for c in self.all())

    # --- changes while running (from /follow, /unfollow or a matching new chat) --------------------

    def follow(self, chat_id: int, name: str) -> Channel:
        channel = self._channels.get(name)
        if channel is None:
            raise ValueError(f"no channel '{name}' in channels.json (have: {', '.join(c.name for c in self.all())})")
        for other in self._channels.values():
            other.ids.discard(chat_id)
        channel.ids.add(chat_id)
        return channel

    def unfollow(self, chat_id: int) -> Channel | None:
        for channel in self._channels.values():
            if chat_id in channel.ids:
                channel.ids.discard(chat_id)
                return channel
        return None


def _profile(name: str, values: dict) -> Profile:
    known = {f.name for f in fields(Profile)} - {"name"}
    unknown = set(values) - known - {"_comment"}
    if unknown:
        raise SystemExit(f"channels.json: profile '{name}' has unknown keys: {', '.join(sorted(unknown))}")
    bad = set(values.get("settings") or {}) - OVERRIDABLE
    if bad:
        raise SystemExit(f"channels.json: profile '{name}' can't override {', '.join(sorted(bad))} "
                         f"(allowed: {', '.join(sorted(OVERRIDABLE))})")
    rules = values.get("rules", "")
    if rules not in ("", "tipster", "ss", "tennistv", "david", "mine", "sk", "panther_toss"):
        raise SystemExit(f"channels.json: profile '{name}' has unknown rules '{rules}' "
                         f"(tipster, ss, tennistv, david, mine or empty)")
    try:
        re.compile(values.get("entry_words", ""))
    except re.error as exc:
        raise SystemExit(f"channels.json: profile '{name}' has an invalid entry_words pattern: {exc}") from exc
    clean = {k: v for k, v in values.items() if k in known}
    clean["aliases"] = {k.lower(): v for k, v in (clean.get("aliases") or {}).items()}
    return Profile(name=name, **clean)


def _apply(base: Settings, profile: Profile) -> Settings:
    """The .env settings with this profile's overrides, converted to the type of the .env setting."""
    if not profile.settings:
        return base
    current = {f.name: getattr(base, f.name) for f in fields(base)}
    changes = {}
    for key, value in profile.settings.items():
        kind = type(current[key])
        changes[key] = value if kind is bool else kind(value)
    return replace(base, **changes)


def apply_aliases(text: str, aliases: dict[str, str]) -> str:
    """'rno' -> 'Rhinos' (whole words, any case): the tipster's short team names as the site lists them."""
    if not aliases or not text:
        return text
    pattern = r"\b(" + "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True)) + r")\b"
    return re.sub(pattern, lambda m: aliases[m.group(1).lower()], text, flags=re.IGNORECASE)
