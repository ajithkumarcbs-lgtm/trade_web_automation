"""Keeping the live bot light on a 1 GB server, and SK-method lays inside their step (11-12 Nov)."""
import asyncio
from datetime import datetime, timedelta

from conftest import make_settings, signal
from tradebot.exchange_bot import ExchangeBot, _above_step
from tradebot.risk import RiskManager


class Tab:
    def __init__(self):
        self.closed, self.urls = False, []

    async def close(self):
        self.closed = True

    async def goto(self, url):
        self.urls.append(url)


class Context:
    def __init__(self):
        self.made = []

    async def new_page(self):
        self.made.append(Tab())
        return self.made[-1]


def test_the_site_tab_is_replaced_every_few_hours(store):
    """10-12 Nov: Playwright's driver grew to 262 MB on one tab in 35 hours, and page reads timed out 100+ times an hour."""
    settings = make_settings()
    bot = ExchangeBot(settings, RiskManager(settings, store, "live"))
    first = Tab()
    bot._ctx, bot._page = Context(), first

    async def action():
        async with bot._using_page():
            pass

    asyncio.run(action())
    assert first.urls == ["about:blank"] and not first.closed  # parked, as before
    bot._page_since -= timedelta(hours=3)
    asyncio.run(action())
    assert first.closed and bot._page is bot._ctx.made[0]
    assert datetime.now() - bot._page_since < timedelta(minutes=1)


def test_an_sk_method_lay_never_goes_above_its_step():
    """11 Nov, MY Tips 'Sydney': the 1.50 step was tried at 1.53 and 1.54, which the 20% tolerance allowed."""
    lay = signal(side="lay", odds=1.5, worst_odds=1.5)
    assert _above_step(lay, 1.54).startswith("price moved: lay 1.54 is above the step's top 1.5")
    assert _above_step(lay, 1.5) == "" and _above_step(lay, 1.43) == ""
    assert _above_step(signal(side="lay", odds=1.5), 1.54) == ""  # a tipster's lay keeps its tolerance
    assert _above_step(signal(odds=2.9), 3.5) == ""  # a back
