"""Exact rules for instruction formats, so the bot doesn't depend on the AI alone.

parse_strict: the strict command format you can type (TEST group or Saved Messages), executed exactly:
    BACK Shelton 3.25 1u      LAY Sinner 1.55 0.5u      BACK AT Berrettini 3 1u (waits for 3)
    EXIT Shelton 1.6          BOOKSET Shelton           LOSSCUT Shelton          CANCEL Berrettini

parse_known: the usual short formats of a tipster ("Back Shelton 3.25 odds 1 unit", "5 odds back kg",
"Limit 2", "Bookset now"). The pipeline reads every message with the AI too and compares: when both agree
the trade goes ahead (the AI's spelling of the name, the rule's numbers); when they disagree you're asked.
Anything else (chat, results, narrative) returns None here and is left to the AI.
"""
from __future__ import annotations

import re

from .models import AISignal, BetSignal, as_bet
from .names import same_name

NUM = r"\d{1,3}(?:\.\d{1,2})?"
SIZE = r"(?:(?P<units>\d{1,2}(?:\.\d{1,2})?)\s*(?:u|units?|limits?)|(?:rs\.?|₹)\s*(?P<amount1>\d+)|(?P<amount2>\d+)\s*rs)"

_STRICT_ENTRY = re.compile(rf"^/?(?P<side>back|lay)(?P<at>\s+at)?\s+(?P<name>.+?)\s+(?P<odds>{NUM})(?:\s+{SIZE})?$",
                           re.IGNORECASE)
_STRICT_EXIT = re.compile(rf"^/?exit\s+(?P<name>.+?)\s+(?P<odds>{NUM})$", re.IGNORECASE)
_STRICT_CLOSE = re.compile(r"^/?(?P<cmd>bookset|book\s*set|losscut|loss\s*cut|cashout|cancel)\s+(?P<name>.+)$",
                           re.IGNORECASE)

# Words that can't be a player or team name in a rule match: nicknames and filler go to the AI instead.
NOT_NAMES = {"goat", "karupan", "now", "try", "first", "entry", "the", "my", "again", "only", "high", "odds",
             "back", "lay", "limit", "unit", "units", "green", "money", "small", "amount", "risky", "jackpot"}


def normalise(text: str) -> str:
    """Lower case, emojis and symbols removed (keeps letters, digits, '.', '/', '%'), single spaces."""
    text = text.lower().replace("\n", " ")
    text = re.sub(r"[^\w\s./%₹]", " ", text)
    text = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", text)  # dots that aren't decimal points
    return re.sub(r"\s+", " ", text).strip()


def _signal(action: str, name: str = "", side: str = "back", odds: float | None = None,
            units: float | None = None, amount: float | None = None, **extra) -> BetSignal:
    name = name.strip()
    return BetSignal(action=action, sport="other", event=f"{name} v ?" if name else "? v ?", market="match_odds",
                     market_name="", selection=name, side=side, odds=odds, line=None, units=units, amount=amount,
                     loss_cut=extra.pop("loss_cut", False), confidence=1.0, **extra)


def _size(match: re.Match) -> tuple[float | None, float | None]:
    units = match.groupdict().get("units")
    amount = match.groupdict().get("amount1") or match.groupdict().get("amount2")
    return (float(units) if units else None), (float(amount) if amount else None)


def parse_strict(text: str) -> list[BetSignal] | None:
    """The strict command format, or None if the message isn't one."""
    line = re.sub(r"\s+", " ", text.strip())
    if m := _STRICT_ENTRY.match(line):
        odds = float(m["odds"])
        units, amount = _size(m)
        return [_signal("entry", m["name"], m["side"].lower(), odds, units, amount,
                        when_odds=odds if m["at"] else None)]
    if m := _STRICT_EXIT.match(line):
        return [_signal("exit", m["name"], "back", float(m["odds"]))]
    if m := _STRICT_CLOSE.match(line):
        cmd = re.sub(r"\s+", "", m["cmd"].lower())
        if cmd == "cancel":
            return [_signal("cancel", m["name"])]
        return [_signal("close", m["name"], loss_cut=cmd == "losscut")]
    return None


# --- the tipsters' usual formats -----------------------------------------------------------------

NAME = r"(?P<name>[a-z][a-z' -]{1,28}?)"
ODDS = rf"(?P<odds>{NUM})"
UNITS = r"(?P<units>\d{1,2}(?:\.\d{1,2})?|half|one)"

TIPSTER_ENTRIES = [
    rf"^back {NAME} (?:on )?{ODDS} ?(?:odds?|dds)? ?/? ?{UNITS} ?units?(?: only)?$",  # Back Shelton 3.25 odds / 1 unit
    rf"^back {NAME} {UNITS} ?units? {ODDS} ?(?:odds?)?$",  # Back rublev 1 unit 2.30 odds
    rf"^{NAME} back {ODDS} ?(?:odds?)? {UNITS} ?units?$",  # Tormo back 3 odds 1 unit
    rf"^back {NAME} {ODDS} ?odds?$",  # Back daniel 8 odds
    rf"^back {NAME} {UNITS} ?units?$",  # Back preston 1 unit
    rf"^{NAME} back {UNITS} ?units?$",  # Bondar back 1 unit
]
SS_ENTRIES = [
    rf"^(?:ready )?(?:now )?{ODDS} ?odds? back {NAME}(?: limit {UNITS})?(?: only)?$",  # 6 odds back war limit 1
    rf"^{ODDS} ?odds? {NAME} back(?: limit {UNITS})?(?: only)?$",  # 3.15 odds pak back
    rf"^{NAME} back {ODDS} ?odds?(?: limit {UNITS})?$",  # Sl back 5.7 odds limit 2
    rf"^{NAME} {ODDS} ?odds? back(?: limit {UNITS})?(?: only)?$",  # Wi 3.85 odds back, Lim 5 odds back limit 1
    rf"^{ODDS} ?odds? {NAME} limit {UNITS}(?: only)?$",  # 3.6 odds hob limit 1 only
]
SS_STAKES = [
    rf"^(?:limit|limi|limt) {UNITS}(?: (?:only|use|enough))*$",  # Limit 2, Limit 1.5 use
    r"^(?:try )?(?:only )?(?P<units>one|half) limit(?: (?:only|enough))*$",  # Only one limit, Try only one limit
    r"^limit (?P<units>one|half)(?: only)?$",  # Limit one
    r"^(?:try )?(?:only )?(?:very )*(?:small|chinna)(?: amounts?)?(?: only)?$",  # Try small amount (0.5 unit)
]
SS_MONEY_STAKE = re.compile(r"^(?:try |rs ?|₹ ?)+(?P<a>\d{2,5})(?: or (?:rs ?|₹ ?)?(?P<b>\d{2,5}))?(?: rs)?"
                            r"(?: only)?$")  # Try 200 or 300: the smaller amount, in rupees (a bare "22" is odds)
# Tennis Tv (Shubh Joshi): "SHEVCHENKO Win Karo...🏆 ... 1.70 limit use kro 4 limit rhega SHEVCHENKO pr✅"
# = back Shevchenko with 1.7 units to win 4: odds 1 + 4 / 1.7.
TV_NAME = re.compile(r"^\s*(?P<name>[A-Za-z][A-Za-z .'-]{1,30}?)\s+win karo", re.IGNORECASE | re.MULTILINE)
TV_STAKE = re.compile(r"(?P<a>\d+(?:\.\d+)?)\s*limit use kro\s*(?P<b>\d+(?:\.\d+)?)\s*limit rhega", re.IGNORECASE)
CLOSE_WORDS = r"(?P<word>bookset|book set|boookset|bookser|losscut|loss cut|cashout|book pannuga|book pannunga)"
# "Bookset coming", "Ready for bookset", "Japan trade possible ... boookset": not yet
# "Cashout khudse kr lena" (cash out yourself): the tipster gives no exit, so no close either
NOT_YET = re.compile(r"\b(coming|ready|be active|wait|soon|if|zone|possible|your choice|chance|anybody|khudse)\b")
FILLER = re.compile(r"^(?:now|good|perfect|great|super|bo+m|fire|done|guys)\b")


def _units(value: str | None) -> float | None:
    if not value:
        return None
    return {"one": 1.0, "half": 0.5}.get(value, None) if not value[0].isdigit() else float(value)


def parse_known(text: str, rules: str) -> list[BetSignal] | None:
    """A signal when the message is exactly one of the tipster's usual formats, else None."""
    if not rules:
        return None
    if rules == "tennistv" and (name := TV_NAME.search(text)) and (stake := TV_STAKE.search(text)):
        units, wins = float(stake["a"]), float(stake["b"])
        if units > 0:
            player = re.sub(r"[^A-Za-z .'-]", "", name["name"]).strip().title()
            return [_signal("entry", player, "back", round(1 + wins / units, 2), units)]
    line = normalise(text)
    if not line or len(line.split()) > 10:
        return None
    entries = {"tipster": TIPSTER_ENTRIES, "ss": SS_ENTRIES}.get(rules, [])
    for pattern in entries:
        if m := re.match(pattern, line):
            name = m["name"].strip()
            if name in NOT_NAMES or any(w in NOT_NAMES for w in name.split()):
                return None  # a nickname or filler word: the AI knows who is meant
            odds = float(m["odds"]) if m.groupdict().get("odds") else None
            return [_signal("entry", name, "back", odds, _units(m.groupdict().get("units")))]
    if rules == "ss":
        for pattern in SS_STAKES:
            if m := re.match(pattern, line):
                return [_signal("stake", units=_units(m.groupdict().get("units")) or 0.5)]  # "small" = half
        if m := SS_MONEY_STAKE.match(line):
            amounts = [float(a) for a in (m["a"], m["b"]) if a]
            return [_signal("stake", amount=min(amounts))]
    if m := re.search(CLOSE_WORDS, line):
        if NOT_YET.search(line):
            return None  # "Bookset coming be active", "Ready for bookset": not yet
        before = line[:m.start()].strip()
        # Only "<team> bookset [now]" names the trade ("Eng bookset"); otherwise the latest trade of the channel.
        name = before if before and len(line.split()) <= 3 and not FILLER.match(before) else ""
        if name in NOT_NAMES:
            name = ""
        return [_signal("close", name, loss_cut="loss" in m["word"])]
    return None


# Words of a tip that are never the team or player it is about
NAME_STOPWORDS = {
    "the", "and", "for", "all", "now", "one", "two", "odds", "odd", "back", "lay", "limit", "limits", "unit", "units",
    "try", "win", "won", "fav", "set", "book", "bookset", "done", "only", "small", "amount", "amounts", "match",
    "entry", "entries", "take", "taken", "high", "low", "good", "big", "biggest", "mini", "fire", "jackpot", "profit",
    "loss", "cut", "losscut", "exit", "with", "this", "that", "our", "your", "side", "both", "just", "not", "yes",
    "pls", "guys", "from", "after", "before", "over", "zone", "ready", "active", "wait", "next", "today", "rate",
    "pona", "ponga", "edunga", "podunga", "vandha", "kro", "karo", "means", "come", "coming", "very", "risky",
    "open", "start", "again", "first", "half", "double", "group", "member", "members", "enjoy", "time", "perfect",
    "already", "reached", "skip", "safely", "looking", "same", "his", "her", "you", "who", "but", "are", "was",
    "use", "enough", "chance", "possible", "play", "playing", "player", "team", "vip", "today", "tomorrow",
}
NAME_SHORT_STOPWORDS = {"in", "to", "of", "on", "or", "la", "is", "it", "me", "my", "we", "do", "an", "at", "by",
                        "be", "if", "so", "no", "up", "go", "rs", "ki", "ka", "ke", "se", "hi"}


def name_words(text: str) -> list[str]:
    """The words of a message that could be a team or player ('beaz', 'kg', 'esl', 'wi'), for checking that a
    team the AI named really is in the message."""
    words = re.findall(r"[a-z][a-z'-]*", normalise(text))
    return [w for w in words if w not in NAME_STOPWORDS and w not in NAME_SHORT_STOPWORDS and len(w) >= 2]


def not_yet(text: str) -> bool:
    """A message about a bookset that is not an instruction yet ("Ready for bookset", "wait for bookset")."""
    line = normalise(text)
    return bool(re.search(CLOSE_WORDS, line) and NOT_YET.search(line))


def reconcile(rule: list[BetSignal] | None, ai: list[AISignal]) -> tuple[list[BetSignal | AISignal], str]:
    """Combines the rule's reading with the AI's. Returns the signals to act on and how they were checked:
    'agree' (both read the same instruction), 'rule-only' / 'ai-only' (one of them found nothing),
    'disagree' (different instructions, numbers, or names that can't be the same player), or 'none'.
    The rule's names should already have the channel's short names expanded ('kg' -> 'Knights')."""
    if rule is None:
        return list(ai), ("ai-only" if ai else "none")
    if not ai:
        return list(rule), "rule-only"
    r = rule[0]
    same = [a for a in ai if a.action == r.action]
    if not same:
        return list(rule), "disagree"
    a = same[0]
    # The AI missing a number the rule read ("... limit 2" read without its units) is no disagreement:
    # the rule's numbers are used. Only a different number is.
    numbers_match = ((r.odds is None or a.odds is None or abs(r.odds - a.odds) < 0.011)
                     and (r.units is None or a.units is None or a.units == r.units))
    if r.action == "entry" and not numbers_match:
        return list(rule), "disagree"
    if not same_name(r.selection, a.selection):  # e.g. the AI wrote another player: the rule read the text as is
        return list(rule), "disagree"
    # Agreed: the AI's name (it knows "ruplev" is Rublev) with the rule's spelling kept for the site
    # (the AI turned "gea" into "Gaea"), and the rule's numbers.
    aka = r.selection if r.selection.strip().lower() != a.selection.strip().lower() else ""
    merged = as_bet(a, odds=r.odds if r.odds is not None else a.odds,
                    units=r.units if r.units is not None else a.units,
                    loss_cut=r.loss_cut if r.action == "close" else a.loss_cut,
                    confidence=max(a.confidence, 0.95), aka=aka)
    return [merged], "agree"
