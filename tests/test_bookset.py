"""Book-set maths: an exit must leave the same P/L whichever side wins, like the site's Cashout."""
import pytest

from tradebot.risk import book, book_set_stake, exit_plan
from tradebot.store import Position


def position(side="back", stake=1000.0, price=2.9, exit_price=None, exit_stake=None):
    return Position(1, "2026-10-07T22:00:00", "tennis", "A v B", "match_odds", "A", side, price, stake,
                    exit_price, exit_stake)


def test_back_book():
    assert book("back", 1000, 2.9) == pytest.approx((1900, -1000))


def test_lay_book():
    assert book("lay", 1000, 2.9) == pytest.approx((-1900, 1000))


def test_site_cashout_formula_example():
    # Site code: stake = (|P/L win| + |P/L lose|) / price for a back in profit/loss on one runner.
    assert book_set_stake(1900, -1000, 1.5) == pytest.approx(1933.33)


@pytest.mark.parametrize("side,stake,price,target", [
    ("back", 1000, 2.9, 1.5),    # price shortened: book profit
    ("back", 1000, 2.9, 4.0),    # price drifted: loss cut
    ("lay", 1000, 2.0, 3.5),     # lay, price drifted: book profit
    ("lay", 500, 1.8, 1.4),      # lay, price shortened: loss cut
    ("back", 2000, 6.0, 1.5),    # the SS sports trade: back 6, limit 1.5
])
def test_exit_equalises_profit(side, stake, price, target):
    pos = position(side, stake, price)
    hedge_side, hedge_stake, _ = exit_plan(pos, target)
    assert hedge_side == ("lay" if side == "back" else "back")
    win, lose = book(side, stake, price, [(hedge_side, hedge_stake, target)])
    assert win == pytest.approx(lose, abs=0.02 * target)  # equal to within the 2-decimal stake rounding


def test_example_numbers_for_the_user():
    side, stake, liability = exit_plan(position("back", 1000, 2.9), 1.5)
    assert (side, stake) == ("lay", pytest.approx(1933.33))
    assert liability == pytest.approx(966.67, abs=0.01)
    win, lose = book("back", 1000, 2.9, [(side, stake, 1.5)])
    assert win == pytest.approx(933.33, abs=0.01) and lose == pytest.approx(933.33, abs=0.01)


def test_replacing_a_partly_matched_exit():
    # Exit 1 at 1.5 (1933.33) matched 600 before being cancelled; exit 2 at 3.5 books the rest.
    pos = position("back", 1000, 2.9, exit_price=1.5, exit_stake=1933.33)
    side, stake, _ = exit_plan(pos, 3.5, filled_stake=600, filled_price=1.5)
    win, lose = book("back", 1000, 2.9, [("lay", 600, 1.5), (side, stake, 3.5)])
    assert win == pytest.approx(lose, abs=0.05)


def test_nothing_left_to_book_when_already_flat():
    pos = position("back", 1000, 2.0)
    side, stake, _ = exit_plan(pos, 2.0, filled_stake=1000, filled_price=2.0)
    assert stake == 0
