"""The structured format the AI fills in from each Telegram message, and the instruction the bot acts on."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field


class AISignal(BaseModel):
    """What the AI reads from one message. Every field is required, so the AI always answers each one."""
    action: Literal["entry", "add", "exit", "close", "stake"] = Field(
        description="entry = open a new position; add = add to a position opened earlier in the chat; "
        "exit = take profit at a target price on an earlier position (e.g. 'exit at 2.9'); "
        "close = close the earlier position NOW at the current price ('book set', 'loss cut', 'cashout'); "
        "stake = the message only gives the stake of the entry just posted ('Limit 2', 'One limit', "
        "'Half limit only')."
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
        description="Stake in UNITS, only when the message says unit/units/u/limit ('2 units' -> 2, 'half unit' "
        "-> 0.5, '1u' -> 1, 'Limit 2' -> 2). null otherwise. Never put a rupee amount here."
    )
    amount: float | None = Field(
        description="Stake in RUPEES when the message gives money instead of units ('Rs 10' -> 10, '₹500' -> 500, "
        "'for 1000' -> 1000, '1k' -> 1000, '2.5k' -> 2500). null otherwise."
    )
    when_odds: float | None = Field(
        description="Entry only once the price gets there, not now: '3 odds pona edunga', 'if 3 odds reached "
        "means taken back' -> 3; '4 or 3.7 odds back if come means back' -> 3.7. null for an entry to take now "
        "and for every other action."
    )
    green_pct: float | None = Field(
        description="Re-entry staked with a share of the profit just booked: 'green money 20%', 'green 20% "
        "back', 'try green amount 20%' -> 20. null otherwise."
    )
    loss_cut: bool = Field(
        description="true only for a close that is a loss cut ('loss cut', 'losscut', 'cut loss'); false for "
        "bookset / book profit / cashout and for every other action."
    )
    confidence: float = Field(
        description="0 to 1: how sure you are that you READ the signal correctly (not whether it will win)."
    )


class BetSignal(AISignal):
    """An instruction the bot acts on: the AI's reading (or a rule's), plus where it came from."""
    action: Literal["entry", "add", "exit", "close", "stake", "cancel"] = Field(
        description="As AISignal, plus cancel = drop a waiting entry / waiting exit (strict commands only).")
    when_odds: float | None = None
    green_pct: float | None = None
    channel: str = ""  # name of the channel in channels.json the tip came from ("" = the single TELEGRAM_CHAT)
    # The rules' spelling of the selection when the AI wrote it differently ('gea' for the AI's 'Gaea'):
    # the site is searched with both names, and nothing is placed when they point to different matches.
    aka: str = ""

    def key(self) -> str:
        """Normalised identity used to spot the same tip posted twice."""
        parts = [self.action, self.sport, self.event, self.market, self.selection, self.side,
                 str(self.line or ""), str(self.odds or "")]
        return "|".join(re.sub(r"[^a-z0-9.]+", " ", p.lower()).strip() for p in parts)

    def describe(self) -> str:
        source = f"[{self.channel}] " if self.channel else ""
        if self.action == "close":
            kind = "LOSS CUT" if self.loss_cut else "BOOKSET"
            return f"{source}CLOSE ({kind}) {self.selection} [{self.market}] {self.event}"
        if self.action == "cancel":
            return f"{source}CANCEL waiting bets on {self.selection}"
        if self.action == "stake":
            return f"{source}STAKE {self.units:g}u for the last entry" if self.units else f"{source}STAKE"
        action = "" if self.action == "entry" else f"{self.action.upper()} "
        price = f" @ {self.odds:g}" if self.odds else ""
        when = f" WHEN {self.side} price reaches {self.when_odds:g}" if self.when_odds else ""
        line = f" {self.line:g}" if self.line is not None else ""
        size = f" Rs{self.amount:g}" if self.amount else (f" {self.units:g}u" if self.units else "")
        green = f" (green money {self.green_pct:g}%)" if self.green_pct else ""
        return (f"{source}{action}{self.side.upper()} {self.selection}{line}{price}{size}{green}{when} "
                f"[{self.market}] {self.event}")


def as_bet(signal: AISignal, channel: str = "", **changes) -> BetSignal:
    """The AI's reading as an instruction from `channel` (BetSignal fields can be changed on the way)."""
    values = signal.model_dump()
    values.update(channel=channel or values.get("channel", ""), **changes)
    return BetSignal(**values)


class ParsedMessage(BaseModel):
    signals: list[AISignal] = Field(
        description="Instructions the message gives the reader to act on now. Empty for results, ads, chat, "
        "or anything that is not a new instruction."
    )
    summary: str = Field(description="One short line describing the message, for the log.")
