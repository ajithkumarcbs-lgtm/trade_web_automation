"""Safety rules every signal must pass, and the book-set maths for exits."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import store as trade_log  # trade_log.clock(): replaced by tools/replay_channel with the message's time
from .config import Settings
from .models import BetSignal
from .store import Position, Store, period_start

# The bot bets on Match Odds (back/lay a team or player). Bookmaker and Fancy need other screens.
SUPPORTED_MARKETS = ("match_odds",)
CONDITIONAL_TOLERANCE = 0.02  # "3 odds pona edunga": goes in once the back price is within 2% of 3
STOP_PERIODS = (("day", "daily_stop_units"), ("week", "weekly_stop_units"), ("month", "monthly_stop_units"))


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


def booked_profit(position: Position, price: float) -> float:
    """P/L locked on both sides if the trade were booked now at `price` (negative = a loss cut)."""
    side, stake, _ = exit_plan(position, price)
    win, lose = book(position.side, position.stake, position.price, [(side, stake, price)])
    return round(min(win, lose), 2)


class RiskManager:
    def __init__(self, settings: Settings, store: Store, mode: str, channels=None):
        """channels: channels.json (tradebot.channels.Channels), whose profiles may change some settings for
        their own trades (odds tolerance, unit stake...). Without it every trade uses the .env settings."""
        self._s = settings
        self._store = store
        self._mode = mode
        self._channels = channels

    def cfg(self, sig_or_channel: BetSignal | Position | str | None = None) -> Settings:
        """Settings for this signal's / trade's channel."""
        name = sig_or_channel if isinstance(sig_or_channel, str) else getattr(sig_or_channel, "channel", "")
        return self._channels.settings_for(name) if self._channels is not None and name else self._s

    def _find(self, sig: BetSignal, channel: str = "") -> Position | None:
        """The recorded trade the instruction is about: by the AI's name, else by the rules' spelling."""
        position = self._store.find_position(sig.selection, self._mode, channel)
        if position is None and sig.aka:
            position = self._store.find_position(sig.aka, self._mode, channel)
        return position

    def precheck(self, sig: BetSignal, retrying: bool = False) -> Decision:
        """Checks that don't need the live price. Run before touching the website.
        retrying: a pending instruction being tried again (its own pending entry is not a duplicate)."""
        s = self.cfg(sig)
        if sig.action == "add":
            return Decision(False, "adds are switched off")
        if sig.action in ("stake", "cancel"):
            return Decision(False, f"'{sig.action}' is handled by the pipeline, not as a bet")
        if sig.confidence < s.min_confidence:
            return Decision(False, f"AI confidence {sig.confidence:.2f} below {s.min_confidence}")
        if self._store.has_recent(sig.key(), s.duplicate_window_min, self._mode):
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
        max_stake = s.max_units_per_bet * s.unit_stake
        if max_stake and stake > max_stake:
            return Decision(False, f"stake {stake:g} is above MAX_UNITS_PER_BET x UNIT_STAKE = {max_stake:g}")
        position = self._find(sig)
        if position and position.exit_price is None:
            return Decision(False, f"already in a trade on {position.selection} since {position.opened_at[11:16]}")
        # The site's Cashout books the whole match: one trade's bookset would close the other too. Needs the site's
        # match names, so not in signals-only mode, where trades carry the AI's guess of the match.
        other = self._store.trade_in_match(sig.selection, self._mode) if self._mode != "signals_only" else None
        if other:
            minutes = (trade_log.clock() - datetime.fromisoformat(other.opened_at)).total_seconds() / 60
            fix = (f". It came {minutes:.0f} min after that entry: if the tipster corrected the team, book "
                   f"{other.selection} by hand" if minutes < 5 else "")
            return Decision(False, f"SAME MATCH: the bot already holds {other.selection} in {other.event} "
                                   f"({other.channel or 'TELEGRAM_CHAT'}); the site books a match as one position, "
                                   f"so a second trade there would be closed by the first one's bookset{fix}")
        if not retrying and self._store.find_pending(sig.selection, self._mode, "entry"):
            return Decision(False, f"an entry on {sig.selection} is already being retried")
        count, _ = self._store.today_totals(self._mode)
        if self._s.max_bets_per_day and count >= self._s.max_bets_per_day:
            return Decision(False, f"daily trade limit reached ({count})")
        stop = self.stop_reason()
        if stop:
            return Decision(False, stop)
        return Decision(True, "ok")

    def stop_reason(self) -> str | None:
        """DAILY / WEEKLY / MONTHLY STOP: no new entries once the period's result reaches -N units (.env).
        Bookings and loss cuts always go through. A trade whose match ended without a booking counts as lost
        until you record its real result with /settle."""
        for period, setting in STOP_PERIODS:
            units = getattr(self._s, setting)
            if not units:
                continue
            result = self._store.period_result(self._mode, period_start(period))
            limit = -units * self._s.unit_stake
            if result <= limit:
                return (f"{period.upper()} STOP: this {period}'s result is {result:+g}, the limit is {limit:g} "
                        f"({units:g} units). No new entries until the next {period}")
        return None

    def stake(self, sig: BetSignal) -> float:
        """Rupee amount from the tip if given, else its units (default 1) x UNIT_STAKE (of its channel)."""
        if sig.amount is not None:
            return round(sig.amount, 2)
        return round((1.0 if sig.units is None else sig.units) * self.cfg(sig).unit_stake, 2)

    def _open_position(self, sig: BetSignal) -> Position | Decision:
        position = self._find(sig, sig.channel)
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
        most = self.cfg(position).max_liability_per_bet
        if most and risk > most:
            return Decision(False, f"exit liability {risk:g} above MAX_LIABILITY_PER_BET {most:g}", position=position)
        return Decision(True, "ok", stake, risk, position, side)

    def _close_check(self, sig: BetSignal) -> Decision:
        """Book set / loss cut now, through the site's Cashout. Also works on a trade placed by hand: the
        site only offers Cashout on a match where you hold a position. A bookset naming no team acts on the
        channel's latest trade."""
        position = self._find(sig, sig.channel)
        if position and position.close_pending_since:  # "Bookset guys" again while it is being booked
            return Decision(False, f"already booking {position.selection} (retrying since "
                                   f"{position.close_pending_since[11:16]})")
        named = bool(sig.selection.strip(" ?"))
        if position is None or not named:
            closed = self._store.recently_closed(sig.selection, self._mode)
            # "Boookset guys 🥳" repeated after the trade was booked: nothing more to do. Without a name it is a
            # repeat when the last thing that happened was a booking, not a newer entry.
            if closed and (position is None or closed[1] > position.opened_at):
                return Decision(False, f"already booked: {closed[0]} trade closed at {closed[1][11:16]}")
        if position is None and not (sig.selection.strip(" ?") or sig.event.replace("v", "").strip(" ?")):
            return Decision(False, "book set without a team or match to act on")
        return Decision(True, "ok", position=position)

    def price_check(self, sig: BetSignal, live_price: float | None) -> Decision:
        """Entry checks against the price actually on offer. live_price is None only when running without the site.
        A waiting entry ("3 odds pona edunga", when_odds) passes only once the price has reached its trigger."""
        s = self.cfg(sig)
        stake = self.stake(sig)
        price = live_price if live_price is not None else (sig.odds or sig.when_odds)
        note = "ok"

        if price is not None:
            dec = to_decimal(sig.market, price)
            if not s.min_odds <= dec <= s.max_odds:
                return Decision(False, f"odds {dec:g} outside {s.min_odds:g}-{s.max_odds:g}")
            if sig.when_odds and live_price is not None:
                waiting = self.waiting(sig, dec)
                if waiting:
                    return Decision(False, waiting)
            elif sig.odds and live_price is not None:
                problem = self.slippage(sig, dec)
                if problem and s.ignore_slippage and self._mode != "live":
                    note = f"IGNORE_SLIPPAGE (test only, never live): {problem}"
                elif problem:
                    return Decision(False, problem)
            price = dec

        risk = liability(sig.side, stake, price)
        if s.max_liability_per_bet and risk > s.max_liability_per_bet:
            return Decision(False, f"liability {risk:g} above MAX_LIABILITY_PER_BET {s.max_liability_per_bet:g}")
        _, exposure = self._store.today_totals(self._mode)
        if self._s.max_daily_exposure and exposure + risk > self._s.max_daily_exposure:
            return Decision(False, f"daily exposure would reach {exposure + risk:g} (max {self._s.max_daily_exposure:g})")
        return Decision(True, note, stake, risk)

    @staticmethod
    def waiting(sig: BetSignal, live: float) -> str | None:
        """Why a waiting entry can't go in yet, or None once the price has reached its trigger."""
        trigger = sig.when_odds
        if sig.side == "back" and live < trigger * (1 - CONDITIONAL_TOLERANCE) - 1e-9:
            return f"WAITING: back price {live:g} has not reached {trigger:g} yet"
        if sig.side == "lay" and live > trigger * (1 + CONDITIONAL_TOLERANCE) + 1e-9:
            return f"WAITING: lay price {live:g} has not come down to {trigger:g} yet"
        return None

    def slippage(self, sig: BetSignal, live: float) -> str | None:
        """Why the live price is too far from the tip, or None if it's acceptable. A better price is always
        fine. ODDS_TOLERANCE_ABS (e.g. 0.4 odds) is used when set, else ODDS_TOLERANCE (e.g. 5%), as set for
        the tip's channel."""
        s = self.cfg(sig)
        tip = to_decimal(sig.market, sig.odds)
        if sig.side == "back":
            lowest = tip - s.odds_tolerance_abs if s.odds_tolerance_abs else tip * (1 - s.odds_tolerance)
            if live < lowest - 1e-9:
                return f"SLIPPAGE_REJECTED: back odds dropped to {live:g} (tip {tip:g}, lowest allowed {lowest:.2f})"
        if sig.side == "lay":
            highest = tip + s.odds_tolerance_abs if s.odds_tolerance_abs else tip * (1 + s.odds_tolerance)
            if live > highest + 1e-9:
                return f"SLIPPAGE_REJECTED: lay odds rose to {live:g} (tip {tip:g}, highest allowed {highest:.2f})"
        return None


def lay_instead_of_back(back_stake: float, lay_odds: float) -> tuple[float, float, float]:
    """A back on one of two runners, placed as a lay of the other one (sites that refuse backs above 10 odds).
    Laying the opponent at `lay_odds` = backing this runner at lay_odds / (lay_odds - 1).
    Returns (equivalent back odds, lay stake, liability): liability = the back stake, the win = the lay stake,
    exactly as the back would have paid."""
    back_odds = lay_odds / (lay_odds - 1)
    lay_stake = round(back_stake * (back_odds - 1), 2)
    return round(back_odds, 4), lay_stake, round(lay_stake * (lay_odds - 1), 2)
