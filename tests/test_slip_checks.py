"""Bet-slip checks: quick-stake buttons, Min/Max Bet, typo-tolerant names, BAL/EXP reading."""
import re
from pathlib import Path

import pytest

from tradebot import site_selectors as sel
from tradebot.exchange_bot import _best_index, _parse_limits, _stake_label
from tradebot.names import NAME_MATCH_MIN, name_score

DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.mark.parametrize("label,value", [("100", 100), ("500", 500), ("1K", 1000), ("5K", 5000), ("10K", 10000),
                                         ("100K", 100000), ("200K", 200000), (" 1k ", 1000), ("1,000", 1000)])
def test_quick_stake_labels(label, value):
    assert _stake_label(label) == value


@pytest.mark.parametrize("label", ["MIN STAKE", "MAX STAKE", "ALL IN", "CLEAR", "Cancel", "Placebet 6s", ""])
def test_buttons_that_are_not_amounts_are_never_clicked_as_stakes(label):
    assert _stake_label(label) is None


@pytest.mark.parametrize("text,limits", [
    ("Min Bet: 1 Max Bet: 0", (1, 0)),             # South Africa v Australia (Test): no betting allowed
    ("Min Bet: 10 Max Bet: 25000", (10, 25000)),   # tennis Match Odds
    ("Min Bet: 10 Max Bet: 1000", (10, 1000)),     # Namibia v USA
    ("Min Bet: 10 Max Bet: 1K", (10, 1000)),
    ("nothing here", (None, None)),
])
def test_slip_limits(text, limits):
    assert _parse_limits(text) == limits


@pytest.mark.parametrize("tip,site,same", [
    ("nambia", "United Arab Emirates v Namibia", True),
    ("vechrot", "Valentin Vacherot", True),
    ("Fl Broska", "Florian Broska", True),
    ("kng", "King Kong FC", False),       # too short to be treated as a typo
    ("usa", "UAE v Oman", False),
    ("Florian Broska", "Lukas Neumayer", False),
])
def test_names(tip, site, same):
    assert (name_score(tip, site) >= NAME_MATCH_MIN) is same


def test_match_choice_prefers_exact_team_over_legends():
    names = ["South Africa Legends v Australia Legends", "South Africa v Australia", "United Arab Emirates v Namibia"]
    assert names[_best_index(names, "South Africa")] == "South Africa v Australia"
    assert names[_best_index(names, "nambia")] == "United Arab Emirates v Namibia"
    assert _best_index(names, "Sinner") is None


LOGGED_IN = sorted(p for p in DATA.glob("screenshots/*.html")
                   if ">BAL<" in p.read_text(encoding="utf-8", errors="ignore"))


@pytest.mark.skipif(not LOGGED_IN, reason="no saved logged-in page")
def test_balance_and_exposure_on_saved_page():
    bs4 = pytest.importorskip("bs4")
    soup = bs4.BeautifulSoup(LOGGED_IN[-1].read_text(encoding="utf-8"), "html.parser")
    values = {}
    for span in soup.find_all("span"):  # same rule as the BALANCE / EXPOSURE xpaths: <span>BAL<b>amount</b></span>
        first = next(iter(span.strings), "").strip()
        if first in ("BAL", "EXP") and span.find("b"):
            values[first] = float(span.find("b").get_text(strip=True))
    assert set(values) == {"BAL", "EXP"}
    assert "normalize-space(text())='BAL'" in sel.BALANCE and re.search("EXP", sel.EXPOSURE)
