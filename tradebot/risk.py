"""Safety rules every signal must pass before any money moves."""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .models import BetSignal
from .store import Position, Store

SUPPORTED_SPORTS = ("cricket", "tennis")
SUPPORTED_MARKETS = ("match_odds", "bookmaker", "fancy")


@dataclass
class Decision:
    ok: bool
    reason: str
    stake: float = 0.0
    liability: float = 0.0
    position: Position | None = None  # the trade an exit closes


def to_decimal(market: str, price: float) -> float:
    """Bookmaker prices are often shown as a rate (85 = 1.85). Convert so all odds compare as decimal."""
    if market == "bookmaker" and price >= 10:
        return 1 + price / 100
    return price


def liability(side: str, stake: float, price: float | None) -> float:
    """Worst-case loss of a bet."""
    if side == "lay" and price:
        return stake * (price - 1)
    if side == "no" and price:
        return stake * price / 100  # fancy NO at rate 120 risks 1.2x the stake
    return stake


def exit_side(position: Position) -> str:
    return "lay" if position.side == "back" else "back"


class RiskManager:
    def __init__(self, settings: Settings, store: Store, mode: str):
        self._s = settings
        self._store = store
        self._mode = mode

    def precheck(self, sig: BetSignal) -> Decision:
        """Checks that don't need the live price. Run before touching the website."""
        if sig.action == "add":
            return Decision(False, "adds are switched off")
        if sig.sport not in SUPPORTED_SPORTS:
            return Decision(False, f"sport '{sig.sport}' not supported")
        if sig.market not in SUPPORTED_MARKETS:
            return Decision(False, f"market '{sig.market}' not supported")
        if sig.confidence < self._s.min_confidence:
            return Decision(False, f"AI confidence {sig.confidence:.2f} below {self._s.min_confidence}")
        if self._store.has_recent(sig.key(), self._s.duplicate_window_min, self._mode):
            return Decision(False, "duplicate of a recent signal")
        if sig.action == "exit":
            return self._exit_check(sig)

        if sig.market == "fancy" and sig.line is None:
            return Decision(False, "fancy signal without a run line")
        position = self._store.find_position(sig.selection, self._mode)
        if position and position.exit_price is None:
            return Decision(False, f"already in a trade on {position.selection} since {position.opened_at[11:16]}")
        count, _ = self._store.today_totals(self._mode)
        if count >= self._s.max_bets_per_day:
            return Decision(False, f"daily trade limit reached ({count})")
        return Decision(True, "ok")

    def _exit_check(self, sig: BetSignal) -> Decision:
        """An exit lays (or backs) the same stake as the entry at the tipster's price.
        Daily limits don't apply: closing a trade must always be possible."""
        if sig.market != "match_odds":
            return Decision(False, "exits are only supported on Match Odds")
        if not sig.odds:
            return Decision(False, "exit without a price")
        position = self._store.find_position(sig.selection, self._mode)
        if position is None:
            return Decision(False, f"no open trade on {sig.selection} to exit")
        if position.exit_price is not None:
            return Decision(False, f"exit already sent @ {position.exit_price:g}; change it on the site if needed")
        risk = liability(exit_side(position), position.stake, sig.odds)
        if risk > self._s.max_liability_per_bet:
            return Decision(False, f"exit liability {risk:g} above per-bet max {self._s.max_liability_per_bet:g}")
        return Decision(True, "ok", position.stake, risk, position)

    def price_check(self, sig: BetSignal, live_price: float | None) -> Decision:
        """Entry checks against the price actually on offer. live_price is None only when running without the site."""
        stake = self._s.stake
        price = live_price if live_price is not None else sig.odds

        if sig.side in ("back", "lay") and price is not None:
            dec = to_decimal(sig.market, price)
            if not self._s.min_odds <= dec <= self._s.max_odds:
                return Decision(False, f"odds {dec:g} outside {self._s.min_odds:g}-{self._s.max_odds:g}")
            if sig.odds and live_price is not None:
                tip = to_decimal(sig.market, sig.odds)
                tol = self._s.odds_tolerance
                if sig.side == "back" and dec < tip * (1 - tol):
                    return Decision(False, f"back odds dropped to {dec:g} (tip {tip:g})")
                if sig.side == "lay" and dec > tip * (1 + tol):
                    return Decision(False, f"lay odds rose to {dec:g} (tip {tip:g})")
            price = dec

        risk = liability(sig.side, stake, price)
        if risk > self._s.max_liability_per_bet:
            return Decision(False, f"liability {risk:g} above per-bet max {self._s.max_liability_per_bet:g}")
        _, exposure = self._store.today_totals(self._mode)
        if exposure + risk > self._s.max_daily_exposure:
            return Decision(False, f"daily exposure would reach {exposure + risk:g} (max {self._s.max_daily_exposure:g})")
        return Decision(True, "ok", stake, risk)
