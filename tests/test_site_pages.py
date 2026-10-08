"""Checks the site selectors against pages saved from iob7.com (data/snapshots, data/screenshots/*.html).
Skipped when no saved page or BeautifulSoup is available. Playwright-only selectors (:text-is, :has-text,
visible filters) are checked on the live site by the dry run instead."""
import re
from pathlib import Path

import pytest

from tradebot import site_selectors as sel
from conftest import signal
from tradebot.exchange_bot import _best_index, _match_to_find, _numbers

bs4 = pytest.importorskip("bs4")
DATA = Path(__file__).resolve().parent.parent / "data"


def saved_pages(marker: str) -> list[Path]:
    pages = sorted(DATA.glob("snapshots/*.html")) + sorted(DATA.glob("screenshots/*.html"))
    return [p for p in pages if marker in p.read_text(encoding="utf-8", errors="ignore")]


HOME_PAGES = saved_pages("max-line-1")
MATCH_PAGES = saved_pages("grid-cols-12")


@pytest.mark.skipif(not HOME_PAGES, reason="no saved home page")
@pytest.mark.parametrize("page", HOME_PAGES, ids=lambda p: p.name)
def test_home_list_rows(page):
    soup = bs4.BeautifulSoup(page.read_text(encoding="utf-8"), "html.parser")
    rows = soup.select(sel.EVENT_ROW)
    assert rows, "no match rows found"
    assert {len(r.select(sel.EVENT_NAME)) for r in rows} == {1}
    names = [r.select_one(sel.EVENT_NAME).get_text(strip=True) for r in rows]
    assert all(" v " in n for n in names)
    for name in names[:20]:  # every listed match can be found by its own name
        assert names[_best_index(names, name)] == name


@pytest.mark.skipif(not MATCH_PAGES, reason="no saved match page")
@pytest.mark.parametrize("page", MATCH_PAGES, ids=lambda p: p.name)
def test_match_page_runner_rows(page):
    soup = bs4.BeautifulSoup(page.read_text(encoding="utf-8"), "html.parser")
    rows = [r for r in soup.select(sel.RUNNER_ROW) if len(r.select(sel.PRICE_CELL)) in (2, 6)]
    if not rows:
        pytest.skip("page has no Match Odds rows")
    for row in rows:
        assert row.select(sel.RUNNER_NAME), "team row without a name"
        prices = [_numbers(c.get_text(" ")) for c in row.select(sel.PRICE_CELL)]
        assert any(prices) or "SUSPENDED" in row.get_text()


def test_cricket_short_forms_are_expanded_but_other_names_kept():
    from tradebot.signal_parser import expand_team
    assert expand_team("Wi") == "West Indies" and expand_team(" IND ") == "India"
    assert expand_team("USA") == "USA" and expand_team("Broska") == "Broska"
    assert expand_team("West Indies") == "West Indies"


def test_match_to_find_for_a_book_set_tip():
    assert _match_to_find(signal(event="? v ?", selection="Namibia")) == "Namibia"
    assert _match_to_find(signal(event="Namibia v ?", selection="Namibia")) == "Namibia"
    assert _match_to_find(signal(event="Namibia v USA", selection="Namibia")) == "Namibia v USA"


def test_numbers_parsing():
    assert _numbers("1.85\n12,345") == [1.85, 12345.0]
    assert _numbers("1.12 3.1K") == [1.12, 3.1]
    assert _numbers("- -") == []
    assert re.search(r"-?\d", "Cashout: -123.45")
