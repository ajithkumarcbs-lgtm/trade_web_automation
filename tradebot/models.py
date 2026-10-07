"""The structured format the AI fills in from each Telegram message."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field


class BetSignal(BaseModel):
    action: Literal["entry", "add", "exit"] = Field(
        description="entry = open a new position; add = add to a position opened earlier in the chat; "
        "exit = close / take profit on an earlier position (e.g. 'Limit 1.5' after a back = lay at 1.5)."
    )
    sport: Literal["cricket", "tennis", "other"]
    event: str = Field(
        description="The match, as '<A> v <B>', e.g. 'Jannik Sinner v Carlos Alcaraz' or 'India v Australia'."
    )
    market: Literal["match_odds", "bookmaker", "fancy", "other"] = Field(
        description="match_odds = who wins the match (default for tennis); bookmaker = cricket Bookmaker market; "
        "fancy = session/over runs lines; other = anything else (set winner, totals, etc.)."
    )
    market_name: str = Field(
        description="Market name exactly as written in the message, e.g. '6 Over Runs IND'. Empty string if not stated."
    )
    selection: str = Field(
        description="Team or player to bet on. For fancy, the session name (e.g. '6 Over Runs IND')."
    )
    side: Literal["back", "lay", "yes", "no"] = Field(
        description="back = bet FOR the selection, lay = bet AGAINST it; yes/no only for fancy lines."
    )
    odds: float | None = Field(
        description="Price quoted by the tipster, e.g. '@1.85' -> 1.85. For fancy, the rate (e.g. 100). null if none."
    )
    line: float | None = Field(description="Run line for fancy bets, e.g. '48 yes' -> 48. null otherwise.")
    confidence: float = Field(
        description="0 to 1: how sure you are that you READ the signal correctly (not whether it will win)."
    )

    def key(self) -> str:
        """Normalised identity used to spot the same tip posted twice."""
        parts = [self.action, self.sport, self.event, self.market, self.selection, self.side,
                 str(self.line or ""), str(self.odds or "")]
        return "|".join(re.sub(r"[^a-z0-9.]+", " ", p.lower()).strip() for p in parts)

    def describe(self) -> str:
        action = "" if self.action == "entry" else f"{self.action.upper()} "
        price = f" @ {self.odds:g}" if self.odds else ""
        line = f" {self.line:g}" if self.line is not None else ""
        return f"{action}{self.side.upper()} {self.selection}{line}{price} [{self.market}] {self.event}"


class ParsedMessage(BaseModel):
    signals: list[BetSignal] = Field(
        description="Bets the message tells the reader to place now. Empty for results, ads, chat, "
        "or anything that is not a new bet instruction."
    )
    summary: str = Field(description="One short line describing the message, for the log.")
