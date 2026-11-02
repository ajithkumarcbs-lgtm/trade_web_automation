"""Paper record of fancy (session) tips, settled from Cricbuzz's over-by-over scores.

A fancy tip is '06 OVER 56 YES WEST IND' (West Indies' runs after 6 overs: yes = 56 or more) or '1st WICKET
18 under' (the 1st wicket falls before 18: no on '1st Wkt 18'). The betting site shows the session lines but no
score, so the result comes from Cricbuzz: its over-by-over page lists, for the latest overs, each innings' score
and wickets at the end of the over, and its full-commentary feed has every complete over of an innings (so a
match finished, or a restart in the middle, still gives over 10 and 15).
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .names import code_fits, contains_player, same_name
from .signal_parser import expand_team

log = logging.getLogger(__name__)

CRICBUZZ = "https://www.cricbuzz.com"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/141.0 Safari/537.36")
OVER = re.compile(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:over|overs|ov)\b", re.IGNORECASE)
WICKET = re.compile(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:wkt|wicket|wk)\b", re.IGNORECASE)
LAMBI = re.compile(r"\b(lambi|innings? total|full innings)\b", re.IGNORECASE)
NOT_TEAM = re.compile(r"\b(runs?|balls?|session|over|overs|ov|wkt|wicket|wk|partnership|fall|of|yes|no|not|lambi|total|"
                      r"innings?|full|call|pass|line|fancy|player|toss|\d+(?:st|nd|rd|th)?)\b", re.IGNORECASE)
PLAYER_KINDS = ("player_runs", "player_balls")


@dataclass
class Session:
    kind: str  # runs | wicket | player_runs | player_balls | toss
    number: int  # over (runs; 0 = the whole innings) or wicket number
    team: str  # the batting team, as the tip names it; the player's name; the team called for the toss


def read_session(market_name: str, selection: str, event: str = "") -> Session | None:
    """'6 Over Runs West Indies' -> runs after over 6 for West Indies; '1st Wkt Zimbabwe' -> the 1st wicket; market
    'Player Runs' or 'Player Balls' -> that player's runs or balls faced; market 'Toss' -> who wins the toss."""
    text = f"{market_name} {selection}".strip()
    team = ""
    for part in (selection, market_name):  # both usually name the team: the first one that does
        team = " ".join(NOT_TEAM.sub(" ", part).split()).strip(" -:/")
        if team:
            break
    if not team and event:  # the team only in the match name
        team = event.split(" v ")[0].strip(" ?")
    if re.search(r"\btoss\b", market_name, re.IGNORECASE):
        return Session("toss", 0, team)
    if re.search(r"\bplayer\b", market_name, re.IGNORECASE):
        return Session("player_balls" if re.search(r"\bballs?\b", market_name, re.IGNORECASE) else "player_runs", 0, team)
    if m := WICKET.search(text):
        return Session("wicket", int(m.group(1)), team)
    if m := OVER.search(text):
        return Session("runs", int(m.group(1)), team)
    if LAMBI.search(text) or re.search(r"\bruns?\b", text, re.IGNORECASE):  # a team's runs with no over: the innings total
        return Session("runs", 0, team)
    return None


@dataclass
class Innings:
    team: str  # Cricbuzz's short name of the batting side, e.g. 'ZIMW'
    overs: dict[int, tuple[int, int]] = field(default_factory=dict)  # complete over -> (score, wickets) at its end
    total: tuple[int, int, float] | None = None  # scorecard: runs, wickets, overs so far
    powerplay: tuple[int, int] | None = None  # scorecard: (its last over, runs at its end), e.g. (6, 58)
    fow: dict[int, int] = field(default_factory=dict)  # scorecard: wicket number -> the score it fell at

    @property
    def last(self) -> int:
        return max(self.overs) if self.overs else 0


@dataclass
class Batter:
    runs: int
    balls: int
    out: bool
    innings: int


@dataclass
class Match:
    match_id: int
    slug: str
    teams: list[tuple[str, str]]  # (name, short name) of both sides
    state: str
    start: datetime
    fmt: str = ""  # T20 | ODI | TEST | ...
    innings: dict[int, Innings] = field(default_factory=dict)
    status: str = ""  # 'England won by 3 runs', 'Match drawn'
    series: str = ""  # 'India tour of New Zealand 2026'
    toss: str = ""  # who won it, from the scorecard page
    batters: dict[str, Batter] = field(default_factory=dict)  # scorecard: those who have batted, by name

    def side_names(self, short: str) -> list[str]:
        """Both names of the side with this short name (just the short name if unknown)."""
        for name, s in self.teams:
            if s == short or name == short:
                return [name, s]
        return [short]

    def team_score(self, team: str) -> int:
        """How well the tip's team names one of the sides: 3 exactly, 2 word by word, 1 loosely, 0 not."""
        return max(fit_score(team, [name, short]) for name, short in self.teams)

    def has_team(self, team: str) -> bool:
        return self.team_score(team) > 0

    def batting_at(self, when: datetime) -> int:
        """The innings most likely being played at that time, from the start and the format (a wicket call names no
        side: 'the 2nd wicket under 42' is about the side batting when it is posted)."""
        first = {"T20": 95, "T20I": 95, "T10": 50, "ODI": 225}.get(self.fmt.upper())
        if not first or self.start == datetime.min:
            return 0
        return 1 if when < self.start + timedelta(minutes=first) else 2

    def ended(self, number: int) -> bool:
        """The innings is over: a later one has started, the match is done, or it is all out / out of overs."""
        inn = self.innings.get(number)
        full = {"T20": 20, "T20I": 20, "ODI": 50, "T10": 10}.get(self.fmt.upper(), 0)
        return (any(n > number for n in self.innings) or self.state == "Complete"
                or bool(inn and inn.total and (inn.total[1] >= 10 or (full and inn.total[2] >= full))))


FILLER = {"men", "mens", "man", "team", "xi", "side"}  # 'AFG MEN' is Afghanistan
OTHER_MARKET = re.compile(r"\b(bhav|boundar\w*|caught|pship|only|surge|ball)\b", re.IGNORECASE)  # not a runs / wicket line


def fit_score(team: str, names: list[str]) -> int:
    """3: 'NZ' / 'New Zealand' is 'New Zealand'; 2: 'WEST IND' / 'ENG CHAMP' / 'AFG MEN' word by word ('West Indies',
    'England Champions', 'Afghanistan'); 1: loosely ('NZ' and 'New Zealand A'); 0: not ('IND' and 'West Indies')."""
    words = [w for w in re.split(r"[^a-z]+", team.lower()) if w and w not in FILLER]
    if len(words) > 1 and words[-1] == "w":  # 'Sydney Thunder W'
        words[-1] = "women"
    wanted = expand_team(" ".join(words)).lower()
    words = [w for w in re.split(r"[^a-z]+", wanted) if w]
    if not words:
        return 0
    best = 0
    for name in names:
        low = name.lower()
        if not low:
            continue
        parts = [p for p in re.split(r"[^a-z]+", low) if p]
        if wanted == low or " ".join(words) == " ".join(parts):
            return 3
        if all(any(p == w or (len(w) >= 3 and p.startswith(w)) for p in parts) for w in words):
            best = max(best, 2 if len(parts) == len(words) else 1)
        # (loosely, not by 'Women': that is the last word of every women's team, which same_name would take for a surname)
        elif contains_player(wanted, name) or same_name(re.sub(r"\s+women$", "", wanted), re.sub(r"\s+women$", "", low)):
            best = max(best, 1)
    return best


def team_fits(team: str, names: list[str]) -> bool:
    """'WEST IND' / 'wi' / 'ZIM' / 'ENG CHAMP' / 'AFG MEN' fits 'West Indies' / 'WI' / 'Zimbabwe Women' /
    'England Champions' / 'Afghanistan'; 'IND' (India) does not fit 'West Indies'."""
    return fit_score(team, names) > 0


def plain_team(team: str) -> str:
    """The tip's team as the site's match list names it: 'AFG MEN' -> 'Afghanistan', 'NZ' -> 'New Zealand'."""
    return expand_team(" ".join(w for w in team.split() if w.lower() not in FILLER))


def _row_is_team(listed: str, team: str) -> bool:
    """The site's name of a side in a fancy row ('MLSW', 'NZ') is the tip's team ('Melbourne Stars W', 'NZ', 'INDIA')."""
    return fit_score(team, [listed]) > 0 or code_fits(listed, plain_team(team))


def same_batter(tip: str, name: str) -> bool:
    """'G Readmayne' is 'Georgia Redmayne', 'D singh Aree' 'Dipendra Singh Airee', 'Gill' 'Shubman Gill': a surname close
    enough (tipsters misspell them) and, when the tip gives more of the name, the same first letter."""
    words = [w for w in re.split(r"[^a-z]+", tip.lower()) if w]
    parts = [p for p in re.split(r"[^a-z]+", name.lower()) if p]
    if not words or not parts:
        return False
    close = [w for w in words if len(w) >= 3 and difflib.SequenceMatcher(None, w, parts[-1]).ratio() >= 0.8]
    if not close:
        return False
    others = [w for w in words if w != close[-1]]
    return not others or others[0][0] == parts[0][0]


def site_session_row(names: list[str], session: Session) -> int | None:
    """Which of the site's fancy rows is this session: '15 Over MLSW' (not its '15 Over MLSW 1' at other rates, nor
    '16th Over Runs Only MLSW'), for the innings total the longest over listed, '2nd Wkt AFG', 'A Sutherland Runs'."""
    if session.kind in PLAYER_KINDS:
        word = "run" if session.kind == "player_runs" else "ball"
        for i, name in enumerate(names):
            found = re.fullmatch(rf"(.+?)\s+{word}s?", name.strip(), re.IGNORECASE)
            if found and same_batter(session.team, found.group(1)):
                return i
        return None
    overs = {}
    for i, name in enumerate(names):
        if OTHER_MARKET.search(name):
            continue
        if session.kind == "wicket":
            m = re.fullmatch(r"(\d+)(?:st|nd|rd|th)\s+Wkt\s+([A-Za-z][A-Za-z ]*)", name.strip(), re.IGNORECASE)
            if m and int(m.group(1)) == session.number and _row_is_team(m.group(2).strip(), session.team):
                return i
        elif m := re.fullmatch(r"(\d+)\s+Overs?(?:\s+Runs?)?\s+([A-Za-z][A-Za-z ]*)", name.strip(), re.IGNORECASE):
            if _row_is_team(m.group(2).strip(), session.team):
                overs[int(m.group(1))] = i
    if session.kind == "wicket":
        return None
    return overs.get(session.number) if session.number else overs.get(max(overs, default=0))


def coming_match(matches: list[Match], team: str, event: str, now: datetime) -> Match | None:
    """The team's next match within a day that hasn't started, against the opponent the tip names if it names one: the
    side named best, then the soonest. None when it is playing now or Cricbuzz doesn't list it (SK Astro 31 Oct: 'Sri
    Lanka' 11 min into its match with Pakistan is no tip on its Hong Kong match the next morning)."""
    others = [side for side in event.split(" v ") if side.strip(" ?") and not team_fits(side, [team])]
    found = [m for m in matches if now < m.start <= now + timedelta(hours=24) and m.has_team(team)
             and all(m.has_team(o) for o in others)]
    return max(found, key=lambda m: (m.team_score(team), -m.start.timestamp()), default=None)


def finished_match(matches: list[Match], team: str, event: str, when: datetime) -> Match | None:
    """The finished match a trade or order placed at `when` was on: its teams, and `when` from a day before its start
    to six days after it (the same teams' next Test isn't this one). team may be 'The Draw': the event names the teams."""
    sides = [side for side in event.split(" v ") if side.strip(" ?")]
    if not re.search(r"\bdraw\b", team, re.IGNORECASE):
        sides.append(team)
    found = [m for m in matches if m.state == "Complete" and sides and all(m.has_team(side) for side in sides)
             and m.start - timedelta(days=1) <= when <= m.start + timedelta(days=6)]
    return max(found, key=lambda m: m.start, default=None)


def parse_matches(page: str) -> list[Match]:
    """The matches on Cricbuzz's live-scores page (any state), from the JSON inside it."""
    raw = page.replace('\\"', '"')
    slugs = dict(re.findall(r"/live-cricket-scores/(\d+)/([a-z0-9-]+)", raw))
    decoder = json.JSONDecoder()
    matches: dict[int, Match] = {}
    for found in re.finditer(r'"matchInfo":\{"matchId"', raw):
        try:
            info, _ = decoder.raw_decode(raw, found.start() + len('"matchInfo":'))
        except ValueError:
            continue
        match_id = int(info.get("matchId") or 0)
        if not match_id or match_id in matches:
            continue
        teams = [(info.get(t, {}).get("teamName", ""), info.get(t, {}).get("teamSName", "")) for t in ("team1", "team2")]
        start = datetime.fromtimestamp(int(info.get("startDate") or 0) / 1000)
        matches[match_id] = Match(match_id, slugs.get(str(match_id), "x"), teams, info.get("state", ""), start,
                                  info.get("matchFormat", ""), status=info.get("status", ""),
                                  series=info.get("seriesName", ""))
    return list(matches.values())


def legal_balls(summary: str) -> int:
    """Deliveries that count in an over summary ('1 Wd 4 N1 6 W 0 2': 6); wides and no-balls are bowled again."""
    return sum(1 for ball in summary.split() if not ball.startswith(("Wd", "N")))


def parse_overs(page: str, match: Match) -> int:
    """Adds the over-by-over page's complete overs (the latest ones listed) to the match; returns how many it
    listed. The over being bowled is listed too, with its score so far: it counts once it has 6 legal balls or
    the next over is listed."""
    raw = page.replace('\\"', '"')
    at = raw.find('"overSummaryList":')
    if at < 0:
        return 0
    try:
        items, _ = json.JSONDecoder().raw_decode(raw, at + len('"overSummaryList":'))
    except ValueError:
        return 0
    listed = {(int(i.get("inningsId") or 0), int(i.get("overs") or 0)) for i in items}
    for item in items:
        number, over = int(item.get("inningsId") or 0), int(item.get("overs") or 0)
        if legal_balls(item.get("ovrSummary") or "") < 6 and (number, over + 1) not in listed:
            continue  # still being bowled (or the innings' last, short over: the scorecard has its total)
        innings = match.innings.setdefault(number, Innings(item.get("batTeamName", "")))
        innings.overs[over] = (int(item.get("score") or 0), int(item.get("wickets") or 0))
    return len(items)


def parse_commentary(page: str, match: Match) -> int:
    """Adds every complete over from Cricbuzz's full-commentary feed (JSON): each over ends with a separator giving
    the score and wickets then ('overNum' 9.6 = the end of over 10; the over being bowled has none yet). Returns
    the overs read."""
    try:
        data = json.loads(page)
    except ValueError:
        return 0
    found = 0
    for sep in _over_separators(data):
        try:
            number, over = int(sep["inningsId"]), int(float(sep["overNum"])) + 1
            score, wickets = int(sep["score"]), int(sep["wickets"])
        except (KeyError, TypeError, ValueError):
            continue
        innings = match.innings.setdefault(number, Innings(sep.get("batTeamName", "")))
        innings.overs[over] = (score, wickets)
        found += 1
    return found


def _over_separators(node) -> list[dict]:
    if isinstance(node, list):
        return [sep for item in node for sep in _over_separators(item)]
    if not isinstance(node, dict):
        return []
    own = [node["overSeparator"]] if isinstance(node.get("overSeparator"), dict) else []
    return own + [sep for key, value in node.items() if key != "overSeparator" for sep in _over_separators(value)]


def parse_scorecard(page: str, match: Match) -> int:
    """Adds who won the toss and each innings' total, powerplay runs, fall of wickets and batters from the scorecard
    page; returns innings read."""
    raw = page.replace('\\"', '"')
    if toss := re.search(r'"tossWinnerName":"([^"]+)"', raw):
        match.toss = toss.group(1)
    at = raw.find('"scoreCard":[')
    if at < 0:
        return 0
    try:
        cards, _ = json.JSONDecoder().raw_decode(raw, at + len('"scoreCard":'))
    except ValueError:
        return 0
    for card in cards:
        team = card.get("batTeamDetails") or {}
        short, name = team.get("batTeamShortName", ""), team.get("batTeamName", "")
        if name and short and (name, short) not in match.teams:
            match.teams.append((name, short))
        number = int(card.get("inningsId") or 0)
        inn = match.innings.setdefault(number, Innings(short or name))
        for batter in (team.get("batsmenData") or {}).values():
            how_out = batter.get("outDesc") or ""
            if batter.get("batName") and (how_out or batter.get("balls")):
                match.batters[batter["batName"]] = Batter(int(batter.get("runs") or 0), int(batter.get("balls") or 0),
                                                          how_out not in ("", "batting", "not out"), number)
        score = card.get("scoreDetails") or {}
        inn.total = (int(score.get("runs") or 0), int(score.get("wickets") or 0), float(score.get("overs") or 0))
        for pp in (card.get("ppData") or {}).values():
            if pp.get("ppType") == "Mandatory" and pp.get("ppOversTo"):
                inn.powerplay = (int(float(pp["ppOversTo"])), int(pp.get("runsScored") or 0))
        for wicket in (card.get("wicketsData") or {}).values():
            if wicket.get("wktNbr"):
                inn.fow[int(wicket["wktNbr"])] = int(wicket.get("wktRuns") or 0)
    return len(cards)


def _won(side: str, line: float, result: float) -> bool:
    return result >= line if side == "yes" else result < line


def _settle_batter(session: Session, side: str, line: float, match: Match) -> tuple[bool | None, str]:
    """Past the line is known at once; below it only when the batter is out or the innings over. A batter not on the
    scorecard hasn't batted: not known yet."""
    name = next((n for n in match.batters if same_batter(session.team, n)), None)
    if name is None:
        return None, ""
    batter = match.batters[name]
    value, unit = (batter.runs, "runs") if session.kind == "player_runs" else (batter.balls, "balls")
    if value >= line or batter.out or match.ended(batter.innings):
        return _won(side, line, value), f"{name} {value} {unit}"
    return None, ""


def settle(session: Session, side: str, line: float, match: Match,
           given: datetime | None = None) -> tuple[bool | None, str]:
    """(won? None = not known yet, what happened) for a fancy tip on this match, given at `given`. Yes wins at the
    line or above, no below it; a wicket session is the score the wicket fell at ('1st Wkt 18' no = it fell under
    18) of the side batting when the call was given (wicket calls name no side; the AI guesses one)."""
    if session.kind == "toss":
        if not match.toss:
            return None, ""
        return team_fits(session.team, [match.toss]) == (side == "yes"), f"{match.toss} won the toss"
    if session.kind in PLAYER_KINDS:
        return _settle_batter(session, side, line, match)
    found = [(n, inn) for n, inn in sorted(match.innings.items())
             if team_fits(session.team, match.side_names(inn.team))]
    if session.kind == "wicket" and given is not None and match.batting_at(given) in match.innings:
        found = [(match.batting_at(given), match.innings[match.batting_at(given)])]
    if not found:
        return None, ""
    number, inn = found[0]
    ended = match.ended(number)
    n = session.number
    if session.kind == "runs":
        if n and n in inn.overs:
            return _won(side, line, inn.overs[n][0]), f"{inn.overs[n][0]} after over {n}"
        # A powerplay still being bowled shows its runs so far (1 Nov: '2 after over 6', 3 min into the match).
        if n and inn.powerplay and inn.powerplay[0] == n and (ended or (inn.total and inn.total[2] >= n)):
            return _won(side, line, inn.powerplay[1]), f"{inn.powerplay[1]} after over {n} (powerplay)"
        if ended and inn.total and (n == 0 or n >= inn.total[2]):
            return _won(side, line, inn.total[0]), f"{inn.total[0]} (innings total, {inn.total[2]:g} overs)"
        if ended and inn.overs and (n == 0 or n > inn.last):
            runs = inn.overs[inn.last][0]
            return _won(side, line, runs), f"{runs} (innings ended at over {inn.last})"
        return None, ""
    if n in inn.fow:
        return _won(side, line, inn.fow[n]), f"wicket {n} fell at {inn.fow[n]}"
    # without the scorecard: it fell between the score at the end of the over before and at the end of its over
    low = 0
    for over in sorted(inn.overs):
        score, wickets = inn.overs[over]
        if wickets >= n:
            if over - 1 not in inn.overs and over > 1:
                return None, ""  # the over before is missing: the range isn't known
            if score < line:
                return side == "no", f"wicket {n} fell at {low}-{score}"
            if low >= line:
                return side == "yes", f"wicket {n} fell at {low}-{score}"
            return None, ""  # the range spans the line: wait for the scorecard
        low = score
    if ended and inn.total:
        return _won(side, line, inn.total[0]), f"wicket {n} never fell ({inn.total[0]})"
    return None, ""


def _get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:  # raises on an HTTP error
        return response.read().decode("utf-8", errors="replace")


class Cricbuzz:
    """Reads Cricbuzz's public pages (no key). One instance keeps every over it has seen of each match."""

    def __init__(self) -> None:
        self.matches: dict[int, Match] = {}

    async def refresh_list(self) -> list[Match]:
        page = await asyncio.to_thread(_get, f"{CRICBUZZ}/cricket-match/live-scores")
        for match in parse_matches(page):
            known = self.matches.get(match.match_id)
            if known:
                known.state, known.fmt, known.status = match.state, match.fmt or known.fmt, match.status or known.status
                if match.slug != "x":
                    known.slug = match.slug
            else:
                self.matches[match.match_id] = match
        return list(self.matches.values())

    async def refresh_overs(self, match: Match) -> int:
        page = await asyncio.to_thread(_get, f"{CRICBUZZ}/live-cricket-over-by-over/{match.match_id}/{match.slug}")
        return parse_overs(page, match)

    async def refresh_scorecard(self, match: Match) -> int:
        page = await asyncio.to_thread(_get, f"{CRICBUZZ}/live-cricket-scorecard/{match.match_id}/{match.slug}")
        return parse_scorecard(page, match)

    async def refresh_commentary(self, match: Match) -> int:
        """Every complete over of each innings started (from the scorecard; the 1st if none is known yet)."""
        found = 0
        for number in sorted(match.innings) or [1]:
            page = await asyncio.to_thread(_get, f"{CRICBUZZ}/api/mcenter/{match.match_id}/full-commentary/{number}")
            found += parse_commentary(page, match)
        return found

    async def match_by_id(self, match_id: int) -> Match | None:
        """A match no longer on the live-scores page (finished a while ago), rebuilt from its scorecard."""
        if match_id in self.matches:
            return self.matches[match_id]
        match = Match(match_id, "x", [], "Complete", datetime.min)
        if not await self.refresh_scorecard(match):
            return None
        self.matches[match_id] = match
        return match
