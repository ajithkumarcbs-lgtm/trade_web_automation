"""Which league or tournament a trade was on, for /report. The betting site's pages name neither, so cricket comes from
Cricbuzz's series names and tennis from ESPN's scoreboards (ATP and WTA: Challengers and ITF aren't on them)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .fancy import CRICBUZZ, Match, _get, parse_matches

log = logging.getLogger(__name__)

ESPN ="https://site.api.espn.com/apis/site/v2/sports/tennis/{tour}/scoreboard?dates={day:%Y%m%d}"
TOURS = ("atp", "wta")
GIVE_UP = timedelta(days=2)  # not found by then, it never will be: 'Other cricket' or 'Tennis other'
OTHERS = {"cricket": "Other cricket", "tennis": "Tennis other"}


@dataclass
class Draw:
    tournament: str  # 'ATP Erste Bank Open'
    players: list[str]  # of one singles match


def series_name(series: str) -> str:
    """Cricbuzz's series without its year. 'Womens Big Bash League 2026' and 'WBBL 2026' are one league."""
    if re.search(r"\bwbbl\b|women'?s big bash", series, re.IGNORECASE):
        return "Big Bash Women"
    return re.sub(r",?\s*\d{4}(?:-\d{2,4})?\s*$", "", series).strip()


def cricket_league(matches: list[Match], event: str, when: datetime) -> str | None:
    """The series of the match a trade at `when` was on: its sides named best, then the nearest in time ('New Zealand v
    India' is the T20I, not India A v New Zealand A the same week)."""
    sides = [re.sub(r"\s+W$", " Women", side.strip()) for side in event.split(" v ") if side.strip(" ?")]
    found = [m for m in matches if m.series and sides and all(m.has_team(side) for side in sides)
             and m.start - timedelta(days=1) <= when <= m.start + timedelta(days=6)]
    best = min(found, key=lambda m: (-sum(m.team_score(side) for side in sides), abs(m.start - when)), default=None)
    return series_name(best.series) if best else None


def parse_draws(page: str, tour: str) -> list[Draw]:
    """Every singles match of the tournaments on an ESPN scoreboard (a doubles side has no athlete of its own)."""
    draws = []
    for event in json.loads(page).get("events", []):
        for group in event.get("groupings", []):
            for game in group.get("competitions", []):
                players = [side.get("athlete", {}).get("displayName", "") for side in game.get("competitors", [])]
                if players and all(players):
                    draws.append(Draw(f"{tour.upper()} {event.get('name', '')}".strip(), players))
    return draws


def _surname(name: str) -> str:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower().split()
    return re.sub(r"[^a-z]", "", plain[-1]) if plain else ""


def _same_player(side: str, player: str) -> bool:
    """'Ziz Bergs' is 'Zizou Bergs'; 'Auger Aliassime' is 'Felix Auger-Aliassime'."""
    a, b = _surname(side), _surname(player)
    return bool(a and b) and (a.endswith(b) or b.endswith(a))


def tennis_league(draws: list[Draw], event: str) -> str | None:
    """The tournament of the match the site calls 'Hurkacz v Ziz Bergs': both surnames in one match of its draw."""
    sides = [side for side in event.split(" v ") if side.strip(" ?")]
    for draw in draws:
        if sides and all(any(_same_player(side, player) for player in draw.players) for side in sides):
            return draw.tournament
    return None


def sport_of(sport: str, league: str | None) -> str:
    """Cricket or Tennis. A trade can be recorded with sport 'other' (the AI's guess): its league tells."""
    return "Tennis" if sport == "tennis" or re.match(r"(?:ATP|WTA|Tennis) ", league or "") else "Cricket"


async def cricket_matches() -> list[Match]:
    """The live page and the recent one: a match leaves the live page soon after it ends."""
    pages = await asyncio.gather(*(asyncio.to_thread(_get, f"{CRICBUZZ}/cricket-match/live-scores{path}")
                                   for path in ("", "/recent-matches")))
    return [match for page in pages for match in parse_matches(page)]


async def tennis_draws(day: date) -> list[Draw]:
    pages = await asyncio.gather(*(asyncio.to_thread(_get, ESPN.format(tour=tour, day=day)) for tour in TOURS))
    return [draw for tour, page in zip(TOURS, pages) for draw in parse_draws(page, tour)]


async def leagues_of(rows: list[tuple], now: datetime, read_matches=cricket_matches,
                     read_draws=tennis_draws) -> dict[int, str]:
    """rows: (trade id, opened_at, sport, event). The name of each one known now; one still unknown after GIVE_UP gets
    the 'other' name, so it isn't looked up for ever."""
    matches = await read_matches() if any(sport != "tennis" for _, _, sport, _ in rows) else []
    draws: dict[date, list[Draw]] = {}
    names = {}
    for trade_id, opened_at, sport, event in rows:
        when = datetime.fromisoformat(opened_at)
        name = cricket_league(matches, event, when) if sport != "tennis" else None
        if name is None and sport != "cricket":
            if when.date() not in draws:
                try:
                    draws[when.date()] = await read_draws(when.date())
                except (OSError, ValueError):  # a public page with no promise behind it: the others still get named
                    log.warning("leagues: ESPN could not be read for %s", when.date())
                    continue
            name = tennis_league(draws[when.date()], event)
        if name is None and now - when > GIVE_UP:
            name = OTHERS.get(sport, "Other")
        if name:
            names[trade_id] = name
    return names
