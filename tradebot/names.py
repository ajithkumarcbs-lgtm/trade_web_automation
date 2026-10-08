"""Loose matching of team and player names between tips, the trade log and the site."""
from rapidfuzz import fuzz

NAME_MATCH_MIN = 80  # score (0-100) needed to treat two names as the same team/player


def name_score(a: str, b: str) -> float:
    """Copes with 'Fl Broska' vs 'Florian Broska' and 'Neumayer' vs 'Lukas Neumayer'."""
    a, b = a.lower(), b.lower()
    return max(fuzz.token_set_ratio(a, b), fuzz.WRatio(a, b))
