"""The structured format the AI fills in from each Telegram message."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field


class BetSignal(BaseModel):
    action: Literal["entry", "add", "exit", "close"] = Field(
        description="entry = open a new position; add = add to a position opened earlier in the chat; "
        "exit = take profit at a target price on an earlier position (e.g. 'Limit 1.5'); "
        "close = close the earlier position NOW at the current price ('book set', 'loss cut', 'cashout')."
    )
    sport: Literal["cricket", "tennis", "other"]
    event: str = Field(
        description="The match, as '<A> v <B>', e.g. 'Jannik Sinner v Carlos Alcaraz' or 'India v Australia'."
    )
    market: Literal["match_odds", "bookmaker", "fancy", "other"] = Field(
        description="match_odds = who wins the match (the default for back/lay tips on a team or player); "
        "bookmaker = cricket Bookmaker market; fancy = session/over runs lines; other = set winner, totals, etc."
    )
    market_name: str = Field(
        description="Market name exactly as written in the message, e.g. '6 Over Runs IND'. Empty string if not stated."
    )
    selection: str = Field(
        description="Team or player the instruction is about. For fancy, the session name (e.g. '6 Over Runs IND')."
    )
    side: Literal["back", "lay", "yes", "no"] = Field(
        description="back = bet FOR the selection, lay = bet AGAINST it; yes/no only for fancy lines. "
        "For close, the side of the original position."
    )
    odds: float | None = Field(
        description="Price quoted by the tipster, e.g. '@1.85' -> 1.85; for exit, the target price. "
        "For fancy, the rate (e.g. 100). null if none (always null for close)."
    )
    line: float | None = Field(description="Run line for fancy bets, e.g. '48 yes' -> 48. null otherwise.")
    units: float | None = Field(
        description="Stake in UNITS, only when the message says unit/units/u ('2 units' -> 2, 'half unit' -> 0.5, "
        "'1u' -> 1). null otherwise. Never put a rupee amount here."
    )
    amount: float | None = Field(
        description="Stake in RUPEES when the message gives money instead of units ('Rs 10' -> 10, '₹500' -> 500, "
        "'for 1000' -> 1000, '1k' -> 1000, '2.5k' -> 2500). null otherwise."
    )
    confidence: float = Field(
        description="0 to 1: how sure you are that you READ the signal correctly (not whether it will win)."
    )

    def key(self) -> str:
        """Normalised identity used to spot the same tip posted twice."""
        parts = [self.action, self.sport, self.event, self.market, self.selection, self.side,
                 str(self.line or ""), str(self.odds or "")]
        return "|".join(re.sub(r"[^a-z0-9.]+", " ", p.lower()).strip() for p in parts)

    def describe(self) -> str:
        if self.action == "close":
            return f"CLOSE (cashout) {self.selection} [{self.market}] {self.event}"
        action = "" if self.action == "entry" else f"{self.action.upper()} "
        price = f" @ {self.odds:g}" if self.odds else ""
        line = f" {self.line:g}" if self.line is not None else ""
        size = f" Rs{self.amount:g}" if self.amount else (f" {self.units:g}u" if self.units else "")
        return f"{action}{self.side.upper()} {self.selection}{line}{price}{size} [{self.market}] {self.event}"


class ParsedMessage(BaseModel):
    signals: list[BetSignal] = Field(
        description="Instructions the message gives the reader to act on now. Empty for results, ads, chat, "
        "or anything that is not a new instruction."
    )
    summary: str = Field(description="One short line describing the message, for the log.")
