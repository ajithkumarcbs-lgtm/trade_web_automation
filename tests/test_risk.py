"""Safety rules: stake from units, price tolerance, duplicates, exits and closes."""
import pytest

from conftest import make_settings, signal
from tradebot.risk import RiskManager


def risk(store, **overrides) -> RiskManager:
    return RiskManager(make_settings(**overrides), store, "dry_run")


@pytest.mark.parametrize("units,stake", [(None, 1000), (1, 1000), (2, 2000), (0.5, 500)])
def test_stake_is_units_times_unit_stake(store, units, stake):
    d = risk(store).price_check(signal(units=units), 2.9)
    assert d.ok and d.stake == stake


@pytest.mark.parametrize("amount", [10, 500, 1000, 2500])
def test_rupee_amount_is_used_as_the_stake(store, amount):
    d = risk(store).price_check(signal(amount=amount, units=None), 2.9)
    assert d.ok and d.stake == amount


def test_rupee_amount_wins_over_units(store):
    assert risk(store).price_check(signal(amount=10, units=3), 2.9).stake == 10


def test_units_above_guard_are_skipped(store):
    d = risk(store).precheck(signal(units=10))
    assert not d.ok and "MAX_UNITS_PER_BET" in d.reason


def test_rupee_amount_above_guard_is_skipped(store):
    r = risk(store)  # guard = 5 units x 1000
    assert r.precheck(signal(amount=5000)).ok
    assert "MAX_UNITS_PER_BET" in r.precheck(signal(amount=6000)).reason


def test_units_guard_can_be_switched_off(store):
    assert risk(store, max_units_per_bet=0).precheck(signal(units=10)).ok


def test_back_price_must_not_be_worse_than_tip(store):
    r = risk(store)
    assert r.price_check(signal(odds=2.9), 2.8).ok          # 3.4% worse: within 5%
    assert not r.price_check(signal(odds=2.9), 2.7).ok      # 6.9% worse
    assert r.price_check(signal(odds=2.9), 3.2).ok          # better price is fine


@pytest.mark.parametrize("live,ok", [(3.6, True), (4.2, True), (3.25, True), (3.2, True), (3.15, False), (1.55, False)])
def test_absolute_tolerance_of_0_4_odds(store, live, ok):
    d = risk(store, odds_tolerance_abs=0.4).price_check(signal(odds=3.6), live)
    assert d.ok is ok
    if not ok:
        assert d.reason.startswith("SLIPPAGE_REJECTED") and "lowest allowed 3.20" in d.reason


def test_absolute_tolerance_for_lay(store):
    r = risk(store, odds_tolerance_abs=0.4)
    assert r.price_check(signal(side="lay", odds=2.0), 2.4).ok
    assert "SLIPPAGE_REJECTED" in r.price_check(signal(side="lay", odds=2.0), 2.5).reason


def test_ignore_slippage_works_in_dry_run_only(store):
    tip_far_away = signal(odds=3.6)
    dry = RiskManager(make_settings(ignore_slippage=True), store, "dry_run").price_check(tip_far_away, 1.55)
    assert dry.ok and "IGNORE_SLIPPAGE" in dry.reason and "1.55" in dry.reason
    live = RiskManager(make_settings(ignore_slippage=True, dry_run=False), store, "live").price_check(tip_far_away, 1.55)
    assert not live.ok and live.reason.startswith("SLIPPAGE_REJECTED")


def test_lay_price_must_not_be_worse_than_tip(store):
    r = risk(store)
    assert r.price_check(signal(side="lay", odds=1.5), 1.55).ok
    assert not r.price_check(signal(side="lay", odds=1.5), 1.6).ok


def test_lay_liability(store):
    d = risk(store).price_check(signal(side="lay", odds=3.0, units=1), 3.0)
    assert d.stake == 1000 and d.liability == 2000


def test_any_sport_is_accepted_when_match_odds(store):
    assert risk(store).precheck(signal(sport="other", selection="USA", event="Namibia v USA")).ok


def test_adds_low_confidence_and_other_markets_are_skipped(store):
    r = risk(store)
    assert "adds" in r.precheck(signal(action="add")).reason
    assert "confidence" in r.precheck(signal(confidence=0.4)).reason
    assert "market" in r.precheck(signal(market="fancy", side="yes", line=48)).reason


def test_exit_needs_a_trade_the_bot_opened(store):
    assert "no open trade" in risk(store).precheck(signal(action="exit", side="lay", odds=1.5)).reason


def test_close_works_on_a_trade_placed_by_hand(store):
    d = risk(store).precheck(signal(action="close", odds=None, selection="Namibia", event="? v ?"))
    assert d.ok and d.position is None


def test_close_without_a_name_uses_the_latest_open_trade(store):
    store.open_position(signal(), "dry_run", 2.9, 1000)
    store.open_position(signal(selection="Namibia", event="Namibia v USA"), "dry_run", 3.0, 100)
    d = risk(store).precheck(signal(action="close", odds=None, selection="", event="? v ?"))
    assert d.ok and d.position.selection == "Namibia"
    d = risk(store).precheck(signal(action="exit", side="lay", odds=1.5, selection="", event="? v ?"))
    assert d.ok and d.position.selection == "Namibia"


def test_repeated_bookset_after_booking_is_skipped(store):
    store.open_position(signal(selection="Djokovic", event="Djokovic v ?"), "dry_run", 2.56, 10)
    store.set_closed(store.find_position("Djokovic", "dry_run").id)
    for name in ("Djokovic", "Novak Djokovic", ""):  # "Bookset done guys ✅🐐🥳" with or without a name
        d = risk(store).precheck(signal(action="close", odds=None, selection=name, event="? v ?"))
        assert not d.ok and d.reason.startswith("already booked"), name


def test_bare_bookset_after_a_booking_does_not_close_an_older_open_trade(store):
    import time
    store.open_position(signal(selection="Darderi", event="Darderi v ?"), "dry_run", 5.0, 5)  # never booked
    time.sleep(1.1)  # timestamps have 1 s resolution
    store.open_position(signal(selection="Gea", event="Gea v ?"), "dry_run", 5.0, 10)
    time.sleep(1.1)
    store.set_closed(store.find_position("Gea", "dry_run").id)  # "Loss cut 2.5 la"
    d = risk(store).precheck(signal(action="close", odds=None, selection="", event="? v ?"))
    assert not d.ok and "already booked: Gea" in d.reason  # not a cashout of Darderi


def test_close_needs_a_team_or_match(store):
    d = risk(store).precheck(signal(action="close", odds=None, selection="?", event="? v ?"))
    assert not d.ok


def test_exit_is_sized_as_book_set(store):
    store.open_position(signal(), "dry_run", 2.9, 1000)
    d = risk(store).precheck(signal(action="exit", side="lay", odds=1.5))
    assert d.ok and d.hedge_side == "lay" and d.stake == pytest.approx(1933.33)


def test_second_entry_on_same_player_waits_for_exit(store):
    store.open_position(signal(), "dry_run", 2.9, 1000)
    assert "already in a trade" in risk(store).precheck(signal(odds=3.0)).reason


def test_closed_trade_allows_new_entry(store):
    store.open_position(signal(), "dry_run", 2.9, 1000)
    pos = store.find_position("Florian Broska", "dry_run")
    store.set_closed(pos.id)
    assert risk(store).precheck(signal(odds=3.0)).ok
    assert store.find_position("Broska", "dry_run") is None


def test_optional_daily_caps(store):
    store.open_position(signal(), "dry_run", 2.9, 1000)
    r = risk(store, max_liability_per_bet=1500)
    assert not r.price_check(signal(side="lay", odds=3.0), 3.0).ok  # liability 2000
