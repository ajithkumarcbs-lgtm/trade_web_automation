"""The user's method, for SK Astro's tips and their own. A tip that a team wins is traded by laying the other team, in steps
as that team's price falls: one unit the first time its lay price is in a step (1.50 to 1.20, then 1.20 to 1.10, then 1.10
to 1.01). A lay gains when the laid team's price rises and loses when it falls, so the lower steps are a capped averaging
into a loss: each risks little (1.15 risks 0.15 of a unit) and all three lose at most about 0.8 of a unit.

Exits, by exit_call. Bookset needs both: P&L at the target (1 unit) and the tipped team favourite. Below the target, the two
teams at about equal prices is an exit, always in profit for a lay taken at 1.50 or less: a quarter of a unit with one entry,
about two thirds with two. All three are over the target there, so they wait the tick it takes to be favourite, and are out
if the profit falls back instead. A price back up at STOP_ABOVE ends the entries, never the trade. A jump past equal below the
target waits: for the target, or for the favourite to be lost, which is an exit too.
SK's jackpot days (its posts, or /jackpot) are kept here too: SS sports goes live only on those days.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

STEPS = ((1.20, 1.50), (1.10, 1.20), (1.01, 1.10))  # the other team's lay price: above the low end, up to the high end
STOP_ABOVE = 1.70
BOOKSET, EQUAL_EXIT, FELL_BACK, GIVEBACK = "bookset", "equal exit", "fell back", "giveback"  # exit_call: out now
WAIT_FAVOURITE, WAIT_PROFIT = "wait favourite", "wait profit"  # exit_call: one of the bookset's two conditions is met
MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
DATED_POST = re.compile(r"\b([a-z]{3})[a-z]*\s+(\d{1,2})\s+jackpot\s+(?:alert|day)\b", re.IGNORECASE)  # Nov 5 Jackpot Alert
SOON_POST = re.compile(r"\b(today|tomorrow)\s+jackpot\s+day\b", re.IGNORECASE)  # Tomorrow Jackpot Day
TIP_WORDS = re.compile(r"\s+(?:toss|match|win|even|equal|or)\b.*$", re.IGNORECASE)  # 'UAE Toss or Match Win'


def step_at(price: float) -> int | None:
    """The step this price of the other team is in, None above or below them all. A price on the line between two steps
    is in the lower one."""
    for step, (low, high) in enumerate(STEPS):
        if low < price <= high or (step == len(STEPS) - 1 and price == low):
            return step
    return None


@dataclass(frozen=True)
class LadderRules:
    """The user's limits; a channel changes them with 'ladder_rules' in channels.json. Amounts are in units (limits)."""
    profit_target: float = 1.0  # a bookset needs this P&L, and the tipped team favourite
    equal_band: float = 0.10  # the two teams' back prices this close are 'equal' (1.95 / 1.95, 1.92 / 2.00)
    giveback: float = 0.2  # after reaching the target, this much off the best P&L: out
    daily_loss: float = 3.0  # no new match after this loss in a day
    max_open: int = 3  # matches with entries in at once
    max_drawdown: float = 10.0  # no new match after this fall from the best result
    cooldown_min: int = 30  # no new match for this long after a losing trade
    frozen_reads: int = 20  # this many reads in a row at one price (about 10 min): stale, no entry


def exit_call(pl: float, team_back: float, other_back: float, target: float, giveback: float, peak: float,
              was_favourite: bool, equal_band: float = LadderRules.equal_band) -> str:
    """What an open lay should do now. pl: locked if booked now, at the other team's back price. "" = hold."""
    favourite = team_back < other_back
    if pl >= target:
        return BOOKSET if favourite else WAIT_FAVOURITE
    if abs(team_back - other_back) <= equal_band:
        return EQUAL_EXIT
    if was_favourite and not favourite:
        return FELL_BACK
    if peak >= target and peak - pl >= giveback:
        return GIVEBACK
    return WAIT_PROFIT if favourite else ""


@dataclass
class Ladder:
    """One tipped team and how far its steps have gone."""
    id: int
    created_at: str
    channel: str
    team: str
    event: str
    start_at: str  # nothing is read before: the match's start, less the channel's lead
    message_id: int | None
    other: str = ""  # the site's name of the other team, once read
    first_price: float | None = None  # its lay price when first read
    last_step: int = -1
    stopped: bool = False
    ended_at: str | None = None
    sport: str = "cricket"
    entries: int = 0  # how many went in (a late tip can start at a lower step)
    confirmed_by: str = ""  # the other channel that tipped the same team
    conflict: bool = False  # the other channel tipped the other team: no entries until /sk decides
    peak: float = 0.0  # the best P&L seen
    was_favourite: bool = False
    last_lay: float | None = None  # the latest read of the other team's lay and back price, and of the tipped team's back
    last_back: float | None = None
    team_back: float | None = None
    last_pl: float | None = None
    entry_prices: str = ""  # '1.5,1.15'
    end_reason: str = ""
    waiting: bool = False  # the profit is there, the favourite isn't

    def step_to_take(self, lay: float) -> int | None:
        """Each step once and only going down: a price back up in a higher step is no new entry."""
        step = step_at(lay)
        return step if step is not None and step > self.last_step and not self.stopped and not self.conflict else None

    @property
    def state(self) -> str:
        if self.ended_at:
            if self.end_reason.startswith("BOOKSET"):
                return "BOOKSET_TRIGGERED"
            return "VIRTUAL_EXITED" if self.entries else "MATCH_SETTLED"
        if self.conflict:
            return "SIGNAL_CONFLICT"
        if self.waiting:
            return "BOOKSET_WAITING"
        if self.stopped:
            return "STOP_LOSS_TRIGGERED"
        return f"ENTRY_{self.entries}_ACTIVE" if self.entries else "WAITING_FOR_ENTRY"

    def stops_at(self, lay: float) -> bool:
        return self.last_step >= 0 and not self.stopped and lay >= STOP_ABOVE

    @property
    def entries_over(self) -> bool:
        return self.stopped or self.last_step == len(STEPS) - 1


def _date_of(month: str, day: str, today: date) -> date | None:
    """A month and day as written, in the year that makes it recent or coming: 'Jan 2' in December is next January."""
    number = MONTHS.get(month[:3].lower())
    if number is None:
        return None
    for year in (today.year, today.year + 1):
        try:
            found = date(year, number, int(day))
        except ValueError:
            return None
        if found >= today - timedelta(days=180):
            return found
    return None


def jackpot_days_in(text: str, today: date) -> list[date]:
    """The days a post names: 'Nov 5 Jackpot Alert ⚠️', 'Tomorrow Jackpot Day 👍🏻'."""
    found = [_date_of(month, day, today) for month, day in DATED_POST.findall(text)]
    found += [today + timedelta(days=word.lower() == "tomorrow") for word in SOON_POST.findall(text)]
    return [day for day in found if day]


def parse_day(words: list[str], today: date) -> date:
    """A day as typed in /jackpot: '5 Nov', 'Nov 5', 'today', 'tomorrow', '2026-11-05'."""
    text = " ".join(words).strip().lower()
    if text in ("today", "tomorrow"):
        return today + timedelta(days=text == "tomorrow")
    if m := re.fullmatch(r"(\d{4}-\d{2}-\d{2})", text):
        return date.fromisoformat(m[1])
    m = re.fullmatch(r"(\d{1,2})\s+([a-z]+)", text)
    found = _date_of(m[2], m[1], today) if m else None
    if not found and (m := re.fullmatch(r"([a-z]+)\s+(\d{1,2})", text)):
        found = _date_of(m[1], m[2], today)
    if not found:
        raise ValueError("give a day like 5 Nov, today or tomorrow")
    return found


def read_sk_tip(text: str) -> tuple[str, str]:
    """What /sk was given ('UAE Toss or Match Win', 'Sri Lanka Even or Win', 'UAE v Netherlands'): the team and the
    opponent ('?' if not named)."""
    names = re.split(r"\s+(?:v|vs)\s+", TIP_WORDS.sub("", text.strip()), maxsplit=1, flags=re.IGNORECASE)
    return names[0].strip(), names[1].strip() if len(names) > 1 else "?"
