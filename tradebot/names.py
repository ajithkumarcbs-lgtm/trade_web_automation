"""Loose matching of team and player names between tips, the trade log and the site."""
import re

from rapidfuzz import fuzz

NAME_MATCH_MIN = 80  # score (0-100) needed to treat two names as the same team/player
TYPO_MIN_LENGTH = 5  # only words this long may be misspelt ("nambia"); short ones must match exactly
SAME_NAME_MIN = 70  # two readings of one message: 'beaz' / 'Baez' = 75, 'berriting' / 'Berrettini' = 74
IGNORED_WORDS = {"v", "vs"}
WOMEN = {"w", "women", "womens"}  # the site writes 'Melbourne Stars W', tips and the AI 'Stars W' / 'Stars Women'
# Different teams whose names look like typos of each other ('india' / 'indies' = 91): never matched loosely.
CONFUSABLE = {frozenset(pair) for pair in (("india", "indies"), ("austria", "australia"), ("ireland", "iceland"))}


def _confusable(a: str, b: str) -> bool:
    return frozenset((a, b)) in CONFUSABLE


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in IGNORED_WORDS]


def typo_score(a: str, b: str) -> float:
    """Average, over the words of `a`, of how closely each matches a word of `b`.
    'nambia' vs 'United Arab Emirates v Namibia' = 92; 'kng' vs 'King' = 0 (too short to be a typo)."""
    words_a, words_b = _words(a), _words(b)
    if not words_a or not words_b:
        return 0.0
    total = 0.0
    for word in words_a:
        if word in words_b:
            total += 100
        elif len(word) >= TYPO_MIN_LENGTH:
            total += max((fuzz.ratio(word, other) for other in words_b
                          if len(other) >= TYPO_MIN_LENGTH - 1 and not _confusable(word, other)), default=0)
    return total / len(words_a)


def contains_player(selection: str, listed: str) -> bool:
    """True if the listed match name includes the player/team being bet on, judged by the last word of
    the selection (surname, or 'Indies' / 'Africa' / 'USA'), exactly or with a small typo in long words.
    Stops a tip on Safiullin from opening 'Cerundolo v Mensik' just because the opponent matches.
    A women's team ('Stars W', 'Adelaide Strikers Women') is judged by the word before that, in a women's match."""
    words = _words(selection)
    if not words:
        return False
    listed_words = _words(listed)
    if len(words) >= 2 and words[-1] in WOMEN:
        if not WOMEN.intersection(listed_words):
            return False
        words = words[:-1]
    key = words[-1]
    if key in listed_words:
        return True
    return len(key) >= TYPO_MIN_LENGTH and any(fuzz.ratio(key, w) >= NAME_MATCH_MIN and not _confusable(key, w)
                                               for w in listed_words)


def same_name(a: str, b: str, short_forms: bool = True) -> bool:
    """Could two readings of one message (the rules' and the AI's) name the same player/team? Yes for
    'ruplev' / 'Andrey Rublev', 'gea' / 'Gaea', 'beaz' / 'Baez'; no for 'gea' / 'Garin'.
    The surname of each is compared with every word of the other. A blank name fits anything.
    short_forms: also a word that starts the other ('war' / 'Warriors') and a team code ('kg' / 'Knights')."""
    words_a, words_b = _words(a), _words(b)
    if not words_a or not words_b:
        return True
    if short_forms and (_abbreviates(a, b) or _abbreviates(b, a)):
        return True

    def close(x: str, y: str) -> bool:
        if _confusable(x, y):
            return False
        return fuzz.ratio(x, y) >= SAME_NAME_MIN or (short_forms and min(len(x), len(y)) >= 3
                                                     and (x.startswith(y) or y.startswith(x)))

    return any(close(words_a[-1], w) for w in words_b) or any(close(words_b[-1], w) for w in words_a)


def code_fits(code: str, name: str) -> bool:
    """A tipster's team code of 2-4 letters, one word ('af', 'grb'), that fits this team name ('Amsterdam Flames')."""
    return len(_words(code)) == 1 and 2 <= len(code.strip()) <= 4 and _abbreviates(code, name)


def _abbreviates(code: str, name: str) -> bool:
    """'kg' / 'Knights', 'nwd' / 'North West Dragons', 'rno' / 'Rhinos': a code of 2-4 letters made of the
    name's letters in order, starting with its first letter."""
    code, letters = re.sub(r"[^a-z]", "", code.lower()), re.sub(r"[^a-z]", "", name.lower())
    if not 2 <= len(code) <= 4 or len(letters) <= len(code) or code[0] != letters[0]:
        return False
    rest = iter(letters[1:])
    return all(ch in rest for ch in code[1:])  # each letter found after the one before


def name_score(a: str, b: str) -> float:
    """Copes with 'Fl Broska' vs 'Florian Broska', 'Neumayer' vs 'Lukas Neumayer' and typos like 'Nambia'.
    Confusable team names ('India' / 'West Indies') score 0."""
    a, b = a.lower(), b.lower()
    words_a, words_b = _words(a), _words(b)
    if (words_a and words_b and _confusable(words_a[-1], words_b[-1])
            and words_a[-1] not in words_b and words_b[-1] not in words_a):  # 'India v West Indies' has India
        return 0.0
    return max(fuzz.token_set_ratio(a, b), fuzz.WRatio(a, b), typo_score(a, b))
