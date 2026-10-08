"""Safety rules every signal must pass, and the book-set maths for exits."""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .models import BetSignal
from .store import Position, Store

# The bot bets on Match Odds (back/lay a team or player). Bookmaker and Fancy need other screens.
SUPPORTED_MARKETS = ("match_odds",)


@dataclass
class Decision:
    ok: bool
    reason: str
    stake: float = 0.0
    liability: float = 0.0
    position: Position | None = None  # the trade an exit/close acts on
    hedge_side: str = ""  # exits: the side of the book-set bet


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


def book(side: str, stake: float, price: float, hedges: list[tuple[str, float, float]] = ()) -> tuple[float, float]:
    """(P/L if the selection wins, P/L if it loses) for a bet plus any hedges (side, stake, price) on it."""
    win, lose = (stake * (price - 1), -stake) if side == "back" else (-stake * (price - 1), stake)
    for h_side, h_stake, h_price in hedges:
        if h_side == "back":
            win, lose = win + h_stake * (h_price - 1), lose - h_stake
        else:
            win, lose = win - h_stake * (h_price - 1), lose + h_stake
    return win, lose


def book_set_stake(win: float, lose: float, price: float) -> float:
    """Stake that makes the P/L equal whichever side wins (the site's Cashout formula)."""
    return round(abs(win - lose) / price, 2)


def exit_plan(position: Position, price: float, filled_stake: float = 0.0,
              filled_price: float | None = None) -> tuple[str, float, float]:
    """(side, stake, liability) of an equal-profit exit at `price`, after an earlier exit bet on this
    position matched `filled_stake` at `filled_price`."""
    side = exit_side(position)
    hedges = [(side, filled_stake, filled_price)] if filled_stake and filled_price else []
    win, lose = book(position.side, position.stake, position.price, hedges)
    stake = book_set_stake(win, lose, price)
    return side, stake, liability(side, stake, price)


class RiskManager:
    def __init__(self, settings: Settings, store: Store, mode: str):
        self._s = settings
        self._store = store
        self._mode = mode

    def precheck(self, sig: BetSignal) -> Decision:
        """Checks that don't need the live price. Run before touching the website."""
        if sig.action == "add":
            return Decision(False, "adds are switched off")
        if sig.confidence < self._s.min_confidence:
            return Decision(False, f"AI confidence {sig.confidence:.2f} below {self._s.min_confidence}")
        if self._store.has_recent(sig.key(), self._s.duplicate_window_min, self._mode):
            return Decision(False, "duplicate of a recent signal")
        if sig.action == "close":
            return self._close_check(sig)
        if sig.action == "exit":
            return self._exit_check(sig)

        if sig.market not in SUPPORTED_MARKETS:
            return Decision(False, f"market '{sig.market}' not supported")
        if sig.side not in ("back", "lay"):
            return Decision(False, f"'{sig.side}' is not a Match Odds bet")
        stake = self.stake(sig)
        if stake <= 0:
            return Decision(False, f"invalid stake {stake:g}")
        max_stake = self._s.max_units_per_bet * self._s.unit_stake
        if max_stake and stake > max_stake:
            return Decision(False, f"stake {stake:g} is above MAX_UNITS_PER_BET x UNIT_STAKE = {max_stake:g}")
        position = self._store.find_position(sig.selection, self._mode)
        if position and position.exit_price is None:
            return Decision(False, f"already in a trade on {position.selection} since {position.opened_at[11:16]}")
        count, _ = self._store.today_totals(self._mode)
        if self._s.max_bets_per_day and count >= self._s.max_bets_per_day:
            return Decision(False, f"daily trade limit reached ({count})")
        return Decision(True, "ok")

    def stake(self, sig: BetSignal) -> float:
        """Rupee amount from the tip if given, else its units (default 1) x UNIT_STAKE."""
        if sig.amount is not None:
            return round(sig.amount, 2)
        return round((1.0 if sig.units is None else sig.units) * self._s.unit_stake, 2)

    def _open_position(self, sig: BetSignal) -> Position | Decision:
        position = self._store.find_position(sig.selection, self._mode)
        if position is None:
            return Decision(False, f"no open trade on {sig.selection}")
        if position.price is None:
            return Decision(False, f"entry price of the {position.selection} trade is unknown")
        return position

    def _exit_check(self, sig: BetSignal) -> Decision:
        """A limit exit books the trade at the tipster's price: equal profit whichever side wins.
        Daily limits don't apply: closing a trade must always be possible."""
        if not sig.odds or sig.odds <= 1:
            return Decision(False, "exit without a valid price")
        position = self._open_position(sig)
        if isinstance(position, Decision):
            return position
        side, stake, risk = exit_plan(position, sig.odds)
        return self.check_exit_size(side, stake, risk, position)

    def check_exit_size(self, side: str, stake: float, risk: float, position: Position) -> Decision:
        if stake <= 0:
            return Decision(False, "nothing left to book", position=position)
        if self._s.max_liability_per_bet and risk > self._s.max_liability_per_bet:
            return Decision(False, f"exit liability {risk:g} above MAX_LIABILITY_PER_BET "
                                   f"{self._s.max_liability_per_bet:g}", position=position)
        return Decision(True, "ok", stake, risk, position, side)

    def _close_check(self, sig: BetSignal) -> Decision:
        """Book set / loss cut now, through the site's Cashout. Also works on a trade placed by hand: the
        site only offers Cashout on a match where you hold a position."""
        position = self._store.find_position(sig.selection, self._mode)
        if position is None and not (sig.selection.strip(" ?") or sig.event.replace("v", "").strip(" ?")):
            return Decision(False, "book set without a team or match to act on")
        return Decision(True, "ok", position=position)

    def price_check(self, sig: BetSignal, live_price: float | None) -> Decision:
        """Entry checks against the price actually on offer. live_price is None only when running without the site."""
        stake = self.stake(sig)
        price = live_price if live_price is not None else sig.odds

        if price is not None:
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
        if self._s.max_liability_per_bet and risk > self._s.max_liability_per_bet:
            return Decision(False, f"liability {risk:g} above MAX_LIABILITY_PER_BET {self._s.max_liability_per_bet:g}")
        _, exposure = self._store.today_totals(self._mode)
        if self._s.max_daily_exposure and exposure + risk > self._s.max_daily_exposure:
            return Decision(False, f"daily exposure would reach {exposure + risk:g} (max {self._s.max_daily_exposure:g})")
        return Decision(True, "ok", stake, risk)
