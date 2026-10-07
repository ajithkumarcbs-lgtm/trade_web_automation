"""Drives iob7.com in a Chrome window: logs in, finds the match and market, fills the bet slip."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime

from playwright.async_api import BrowserContext, Locator, Page, Playwright, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout
from rapidfuzz import fuzz

from . import site_selectors as sel
from .config import DATA_DIR, Settings
from .models import BetSignal
from .risk import Decision, RiskManager, exit_side

log = logging.getLogger(__name__)

BROWSER_PROFILE = DATA_DIR / "browser"  # keeps the login between runs
SCREENSHOT_DIR = DATA_DIR / "screenshots"
NAME_MATCH_MIN = 80  # fuzzy score (0-100) needed to accept a match/runner name
OUTCOME_WAIT_S = 20  # in-play exchange bets can take several seconds to be accepted


@dataclass
class BetResult:
    status: str  # placed | simulated | unconfirmed | skipped | failed
    reason: str
    price: float | None = None
    stake: float = 0.0
    liability: float = 0.0


async def launch_browser(pw: Playwright, settings: Settings, headless: bool) -> BrowserContext:
    """Uses the Chrome/Edge already installed on this PC (BROWSER_CHANNEL), with the bot's own profile
    in data/browser, separate from your normal browser profile. "chromium" = Playwright's own download."""
    channel = None if settings.browser_channel == "chromium" else settings.browser_channel
    return await pw.chromium.launch_persistent_context(
        str(BROWSER_PROFILE), channel=channel, headless=headless, viewport={"width": 1400, "height": 900}
    )


class BetAborted(Exception):
    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


class ExchangeBot:
    def __init__(self, settings: Settings, risk: RiskManager):
        self._s = settings
        self._risk = risk
        self._pw = None
        self._ctx = None
        self._page: Page | None = None

    async def start(self) -> None:
        sel.check_configured()
        self._pw = await async_playwright().start()
        self._ctx = await launch_browser(self._pw, self._s, self._s.headless)
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        await self._login()

    async def close(self) -> None:
        if self._ctx:
            await self._ctx.close()
        if self._pw:
            await self._pw.stop()

    async def place(self, sig: BetSignal, check: Decision) -> BetResult:
        """check is the passed precheck; for an exit it carries the position being closed."""
        try:
            if sig.action == "exit":
                return await self._place_exit(sig, check)
            return await self._place_entry(sig)
        except BetAborted as exc:
            return BetResult(exc.status, exc.reason)
        except PlaywrightTimeout as exc:
            await self._screenshot(sig, "timeout")
            return BetResult("failed", f"timed out: {str(exc).splitlines()[0]}")

    async def _login(self) -> None:
        page = self._page
        await page.goto(self._s.site_url + sel.LOGIN_PATH)
        username = page.locator(sel.LOGIN_USERNAME)
        try:
            await username.wait_for(timeout=8000)
        except PlaywrightTimeout:
            log.info("Already logged in")
            return
        await username.fill(self._s.site_username)
        await page.locator(sel.LOGIN_PASSWORD).fill(self._s.site_password)
        await page.locator(sel.LOGIN_SUBMIT).first.click()
        await username.wait_for(state="detached", timeout=20000)
        log.info("Logged in to %s", self._s.site_url)

    async def _place_entry(self, sig: BetSignal) -> BetResult:
        row_name = (sig.market_name or sig.selection) if sig.market == "fancy" else sig.selection
        row = await self._find_runner(sig.sport, sig.event, sig.market, row_name)
        cell, live_price = await self._price_cell(row, sig.market, sig.side, sig.line)
        decision = self._risk.price_check(sig, live_price)
        if not decision.ok:
            raise BetAborted("skipped", decision.reason)
        await cell.click()
        return await self._submit(sig, decision, live_price)

    async def _place_exit(self, sig: BetSignal, check: Decision) -> BetResult:
        """Opposite bet on the position's runner, at the tipster's price, left unmatched until it fills."""
        pos = check.position
        row = await self._find_runner(pos.sport, pos.event, pos.market, pos.selection)
        cell, _ = await self._price_cell(row, pos.market, exit_side(pos), None, need_price=False)
        await cell.click()
        await self._page.locator(sel.ODDS_INPUT).first.fill(f"{sig.odds:g}")
        unmatch = self._page.locator(sel.UNMATCH_OPTION).first
        if await unmatch.count() and not await unmatch.is_checked():
            await unmatch.check()
        return await self._submit(sig, check, sig.odds)

    async def _find_runner(self, sport: str, event_name: str, market: str, row_name: str) -> Locator:
        page = self._page
        await self._open(sel.SPORT_PATHS[sport])
        event = await _best_match(page.locator(sel.EVENT_ITEM), event_name)
        if event is None:
            raise BetAborted("failed", f"match not found on site: {event_name}")
        await event.click()

        title = re.compile(re.escape(sel.MARKET_TITLES[market]), re.I)
        block = page.locator(sel.MARKET_BLOCK).filter(has_text=title).first
        await block.wait_for(timeout=15000)
        row = await _best_match(block.locator(sel.RUNNER_ROW), row_name)
        if row is None:
            raise BetAborted("failed", f"selection not found in {market}: {row_name}")
        return row

    async def _submit(self, sig: BetSignal, decision: Decision, price: float | None) -> BetResult:
        page = self._page
        await page.locator(sel.STAKE_INPUT).first.fill(f"{decision.stake:g}")
        await self._screenshot(sig, "slip")

        if self._s.dry_run:
            cancel = page.locator(sel.CANCEL_BET).first
            if await cancel.count():
                await cancel.click()
            return BetResult("simulated", "dry run: slip filled, not placed", price, decision.stake, decision.liability)

        await page.locator(sel.PLACE_BET).first.click()
        status, reason = await _wait_for_outcome(page)
        await self._screenshot(sig, status)
        return BetResult(status, reason, price, decision.stake, decision.liability)

    async def _price_cell(
        self, row: Locator, market: str, side: str, line: float | None, need_price: bool = True
    ) -> tuple[Locator, float | None]:
        cells = row.locator(sel.PRICE_CELL)
        count = await cells.count()

        if market == "fancy":
            index = sel.FANCY_YES_INDEX if side == "yes" else sel.FANCY_NO_INDEX
            if side not in ("yes", "no") or count < 2:
                raise BetAborted("failed", f"cannot bet {side} on this fancy row ({count} odds boxes)")
        else:
            if side not in ("back", "lay"):
                raise BetAborted("failed", f"'{side}' is not valid for {market}")
            if count >= 6:
                index = 2 if side == "back" else 3
            elif count == 2:
                index = 0 if side == "back" else 1
            else:
                raise BetAborted("failed", f"unexpected odds layout ({count} boxes)")

        cell = cells.nth(index)
        numbers = _numbers(await cell.inner_text())
        if not numbers:
            if need_price:
                raise BetAborted("skipped", "market suspended or no price on offer")
            return cell, None

        if market != "fancy":
            return cell, numbers[0]

        # Fancy box shows "<line>\n<rate>". A lower line is better for YES, a higher one for NO.
        live_line, rate = numbers[0], (numbers[1] if len(numbers) > 1 else None)
        worse = live_line > line if side == "yes" else live_line < line
        if worse:
            raise BetAborted("skipped", f"line moved to {live_line:g} (tip {line:g})")
        return cell, rate

    async def _open(self, path: str) -> None:
        await self._page.goto(self._s.site_url + path)
        if await self._page.locator(sel.LOGIN_USERNAME).count():
            await self._login()
            await self._page.goto(self._s.site_url + path)

    async def _screenshot(self, sig: BetSignal, tag: str) -> None:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", sig.selection.lower()).strip("-")[:40]
        path = SCREENSHOT_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}-{tag}.png"
        try:
            await self._page.screenshot(path=str(path))
        except Exception:  # a failed screenshot must never stop a bet decision
            log.warning("Screenshot failed: %s", path, exc_info=True)


async def _best_match(items: Locator, name: str) -> Locator | None:
    texts = await items.all_inner_texts()
    if not texts:
        return None
    target = name.lower()
    scores = [(fuzz.token_set_ratio(target, t.lower()), fuzz.ratio(target, t.lower()), i) for i, t in enumerate(texts)]
    best, _, index = max(scores)
    log.debug("Best match for %r: %r (score %d)", name, texts[index].splitlines()[0], best)
    return items.nth(index) if best >= NAME_MATCH_MIN else None


async def _wait_for_outcome(page: Page) -> tuple[str, str]:
    success = page.locator(sel.BET_SUCCESS).first
    error = page.locator(sel.BET_ERROR).first
    for _ in range(OUTCOME_WAIT_S * 2):
        if await success.is_visible():
            return "placed", (await success.inner_text()).strip()
        if await error.is_visible():
            return "failed", (await error.inner_text()).strip()
        await page.wait_for_timeout(500)
    return "unconfirmed", f"no confirmation within {OUTCOME_WAIT_S}s, check My Bets on the site"


def _numbers(text: str) -> list[float]:
    return [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]
