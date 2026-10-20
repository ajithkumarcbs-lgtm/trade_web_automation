"""Choosing the right match and row, from cases seen live on 10 Oct 2026."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import make_settings
from main import Pipeline
from tradebot import site_selectors as sel
from tradebot.exchange_bot import _best_index, _numbers
from tradebot.models import ParsedMessage
from tradebot.names import contains_player
from tradebot.risk import RiskManager
from tradebot.telegram_listener import IncomingMessage

LISTED = ["Cerundolo v Mensik", "Svrcina v To Etcheverry", "United Arab Emirates v Namibia", "India v West Indies",
          "Fl Broska v Neumayer"]


@pytest.mark.parametrize("selection,listed,expected", [
    ("Roman Safiullin", "Cerundolo v Mensik", False),          # opened the wrong match on 10 Oct
    ("Tomas Martin Etcheverry", "Svrcina v To Etcheverry", True),
    ("Tomas", "Svrcina v To Etcheverry", False),               # first name only: not enough
    ("Florian Broska", "Fl Broska v Neumayer", True),
    ("West Indies", "India v West Indies", True),
    ("Nambia", "United Arab Emirates v Namibia", True),        # typo in a long word
])
def test_contains_player(selection, listed, expected):
    assert contains_player(selection, listed) is expected


def test_match_must_include_the_player_being_backed():
    assert _best_index(LISTED, "Francisco Cerundolo v Roman Safiullin", "Roman Safiullin") is None
    i = _best_index(LISTED, "Tomas Martin Etcheverry v Dalibor Svrcina", "Tomas Martin Etcheverry")
    assert LISTED[i] == "Svrcina v To Etcheverry"
    assert LISTED[_best_index(LISTED, "West Indies v ?", "West Indies")] == "India v West Indies"


def test_full_name_with_typo_finds_surname_only_listing():
    # 10 Oct: "Back Dalibor surcina 2.12" -> the site lists "Svrcina v To Etcheverry"
    i = _best_index(LISTED, "Dalibor surcina v ?".replace(" v ?", ""), "Dalibor surcina")
    assert i is not None and LISTED[i] == "Svrcina v To Etcheverry"


BET_PAGE = Path(__file__).resolve().parent.parent / "data/screenshots/20261010-134450-south-africa-failed.html"


@pytest.mark.skipif(not BET_PAGE.exists(), reason="saved page of the first real bet not present")
def test_book_set_by_hand_from_the_real_page():
    """First real bet (10 Oct): back South Africa Rs10 @ 1.9 in a Test match, which has no Cashout button."""
    bs4 = pytest.importorskip("bs4")
    from tradebot.exchange_bot import _signed_number
    from tradebot.risk import book, book_set_stake

    soup = bs4.BeautifulSoup(BET_PAGE.read_text(encoding="utf-8"), "html.parser")
    assert "ashout" not in soup.get_text(), "this market really has no Cashout button"
    rows = [r for r in soup.select(sel.RUNNER_ROW) if len(r.select(sel.PRICE_CELL)) == 6 and r.select(sel.RUNNER_NAME)]
    market = [r for r in rows if r.parent is rows[0].parent]  # the runners of one market share a parent
    books = {r.select_one(sel.RUNNER_NAME).get_text(strip=True): _signed_number(r.select_one(sel.RUNNER_BOOK).get_text())
             for r in market}
    assert books == {"South Africa": 9.0, "Australia": -10.0, "The Draw": -10.0}
    assert book("back", 10, 1.9) == pytest.approx((9.0, -10.0))  # the site's book matches the bot's maths

    lay_price = _numbers(market[0].select(sel.PRICE_CELL)[3].get_text(" "))[0]  # best lay for South Africa
    stake = book_set_stake(9.0, -10.0, lay_price)
    assert stake == pytest.approx(19 / lay_price, abs=0.01) and stake < 10  # just under the Min Bet of 10
    rounded = 10  # HEDGE_ROUND_UP: placed at the Min Bet
    assert 9.0 - rounded * (lay_price - 1) == pytest.approx(-0.1, abs=0.15) and -10 + rounded == 0


def test_screenshot_description_is_kept_as_context(store):
    settings = make_settings()
    pipe = Pipeline(settings, "signals_only", None, RiskManager(settings, store, "signals_only"), store)

    class Parser:
        async def parse(self, text, image=None, context=None, reply_to=""):
            return ParsedMessage(signals=[], summary="ATP match odds for Svrcina v Etcheverry")

    pipe._parser = Parser()
    asyncio.run(pipe._handle(IncomingMessage(1, datetime.now(timezone.utc), "", b"\xff\xd8 jpeg")))
    assert "Svrcina v Etcheverry" in pipe._history[-1][1]


MATCH_PAGES = [p for p in sorted(Path(__file__).resolve().parent.parent.glob("data/screenshots/*.html"))
               if "grid-cols-12" in p.read_text(encoding="utf-8", errors="ignore")]


@pytest.mark.skipif(not MATCH_PAGES, reason="no saved match page")
def test_real_match_page_layout():
    bs4 = pytest.importorskip("bs4")
    soup = bs4.BeautifulSoup(MATCH_PAGES[-1].read_text(encoding="utf-8"), "html.parser")
    rows = [r for r in soup.select(sel.RUNNER_ROW) if len(r.select(sel.PRICE_CELL)) == 6]
    assert rows
    for row in rows:
        prices = [(_numbers(c.get_text(" ")) or [None])[0] for c in row.select(sel.PRICE_CELL)]
        back, lay = prices[:3], prices[3:]
        assert back == sorted(back) and lay == sorted(lay), "back3 back2 BACK1 | LAY1 lay2 lay3"
    # the Match Odds header is a button that contains 'Cashout'; the Cashout selector must skip it
    header = [b for b in soup.find_all("button") if "Cashout" in b.get_text() and b.find("button")]
    assert ":not(:has(button))" in sel.CASHOUT_BUTTON or not header
