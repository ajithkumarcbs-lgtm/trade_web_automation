"""Drives iob7.com in Chrome the way a person would: finds the match, bets from the slip, books trades with
the site's Cashout, and cancels waiting bets from Open Bets."""
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
from .names import NAME_MATCH_MIN, name_score
from .risk import RiskManager, exit_plan
from .store import Position

log = logging.getLogger(__name__)

BROWSER_PROFILE = DATA_DIR / "browser"  # keeps the login between runs
SCREENSHOT_DIR = DATA_DIR / "screenshots"
OUTCOME_WAIT_S = 20  # the site holds Match Odds bets for 6 s before accepting them


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
        str(BROWSER_PROFILE),
        channel=channel,
        headless=headless,
        viewport={"width": 1400, "height": 900},
        chromium_sandbox=True,  # Playwright turns Chrome's sandbox off by default
    )


class BetAborted(Exception):
    def __init__(self, status: str, reason: str, price: float | None = None):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.price = price


class ExchangeBot:
    def __init__(self, settings: Settings, risk: RiskManager):
        self._s = settings
        self._risk = risk
        self._pw = None
        self._ctx = None
        self._page: Page | None = None

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._ctx = await launch_browser(self._pw, self._s, self._s.headless)
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        try:
            await self._open_list()
        except (BetAborted, PlaywrightTimeout) as exc:
            shot = await self._screenshot_named("start-failed")
            raise SystemExit(f"Could not log in to {self._s.site_url}: {str(exc).splitlines()[0]}\n"
                             f"Screenshot: {shot}\nIf the site asks for an OTP, log in once by hand with "
                             f"`python -m tools.snapshot_page`, then start the bot again.")
        log.info("Logged in to %s", self._s.site_url)

    async def close(self) -> None:
        if self._ctx:
            await self._ctx.close()
        if self._pw:
            await self._pw.stop()

    async def place(self, sig: BetSignal, position: Position | None) -> BetResult:
        """position is the trade an exit or close acts on (from the precheck)."""
        try:
            if sig.action == "close":
                return await self._cash_out(sig, position)
            if sig.action == "exit":
                return await self._place_exit(sig, position)
            return await self._place_entry(sig)
        except BetAborted as exc:
            if exc.status == "failed":
                await self._save_page(sig, "failed")
            return BetResult(exc.status, exc.reason, exc.price)
        except PlaywrightTimeout as exc:
            await self._save_page(sig, "timeout")
            return BetResult("failed", f"timed out: {str(exc).splitlines()[0]}")

    # --- trades ---------------------------------------------------------------------------------

    async def _place_entry(self, sig: BetSignal) -> BetResult:
        """Back or lay at the best price on offer now, if it is still close to the tip's price."""
        await self._open_match(sig.event)
        for attempt in range(2):
            cell, price = await self._odds_box(sig.selection, sig.side)
            decision = self._risk.price_check(sig, price)
            if not decision.ok:
                raise BetAborted("skipped", decision.reason, price)
            slip = await self._open_slip(cell)
            await self._set_unmatch(slip, False)  # take the price now; never leave an entry waiting
            result = await self._submit(sig, slip, decision.stake, decision.liability, price)
            moved = result.status == "failed" and sel.PRICE_MOVED_ERROR in result.reason.lower()
            if not (moved and attempt == 0):
                return result
            log.info("  price moved before the bet was accepted; checking the new price once more")
        return result

    async def _place_exit(self, sig: BetSignal, pos: Position) -> BetResult:
        """Book the trade at the tipster's price: an equal-profit opposite bet that waits (unmatched) until
        the price is reached. A newer exit replaces the earlier waiting one."""
        await self._open_match(pos.event)
        filled = 0.0
        if pos.exit_price is not None:
            remaining = await self._cancel_unmatched()
            previous = pos.exit_stake or 0.0
            if self._s.dry_run:
                remaining = previous  # nothing was really placed in a dry run
            filled = max(previous - remaining, 0.0)
            if previous and filled >= previous:
                raise BetAborted("skipped", f"earlier exit @ {pos.exit_price:g} already fully matched; trade is booked")
        side, stake, risk = exit_plan(pos, sig.odds, filled, pos.exit_price)
        check = self._risk.check_exit_size(side, stake, risk, pos)
        if not check.ok:
            raise BetAborted("skipped", check.reason)
        log.info("  book set at %g: %s %g (earlier exit matched %g)", sig.odds, side, stake, filled)

        cell, _ = await self._odds_box(pos.selection, side, need_price=False)
        slip = await self._open_slip(cell)
        await slip.locator(sel.ODDS_INPUT).fill(f"{sig.odds:g}")
        await self._set_unmatch(slip, True)
        return await self._submit(sig, slip, stake, risk, sig.odds)

    async def _cash_out(self, sig: BetSignal, pos: Position | None) -> BetResult:
        """Book set / loss cut now with the site's own Cashout, after cancelling waiting exits so they
        can't match later and undo the booking. pos is None for a trade the bot didn't open."""
        if pos is None:
            log.info("  no trade recorded by the bot; cashing out the site's position on this match")
        await self._open_match(pos.event if pos else _match_to_find(sig))
        if pos and pos.exit_price is not None:  # a Limit exit may still be waiting
            await self._cancel_unmatched()
        page = self._page
        button = page.locator(sel.CASHOUT_BUTTON).filter(visible=True).first
        if not await button.count():
            if self._s.dry_run:  # dry-run trades were never placed, so the site has nothing to cash out
                await self._screenshot(sig, "cashout")
                return BetResult("simulated", "dry run: no real bet, so no Cashout button; would cash out here")
            if await page.locator(sel.TURBO_CASHOUT).filter(visible=True).count():
                raise BetAborted("failed", "only Turbo Cashout (7% fee) is offered; not used, cash out by hand")
            raise BetAborted("failed", "no Cashout on the site for this match (no matched position?)")
        pl = _signed_number(await button.inner_text())
        await button.click()
        confirm = page.locator(sel.CASHOUT_CONFIRM).filter(visible=True).first
        await confirm.wait_for(timeout=5000)
        await self._screenshot(sig, "cashout")

        if self._s.dry_run:
            await page.locator(sel.CASHOUT_CANCEL).filter(visible=True).first.click()
            return BetResult("simulated", f"dry run: cashout of {pl:g} offered, not confirmed")
        await self._clear_toasts()
        await confirm.click()
        status, reason = await _wait_for_outcome(page)
        await self._screenshot(sig, f"cashout-{status}")
        return BetResult(status, f"cashout {pl:g}: {reason}")

    async def _cancel_unmatched(self) -> float:
        """Cancels this match's waiting (unmatched) bets in Open Bets and returns their total stake.
        In a dry run they are only counted."""
        page = self._page
        tab = page.locator(sel.UNMATCH_TAB).filter(visible=True).first
        if not await tab.count():
            raise BetAborted("failed", "Open Bets 'Unmatch' tab not found")
        await tab.click()
        await page.wait_for_timeout(1500)  # the list reloads from the server
        cancels = page.locator(sel.UNMATCH_CANCEL).filter(visible=True)
        count = await cancels.count()
        total = 0.0
        for i in range(count):
            cells = await cancels.nth(i).locator("xpath=ancestor::tr[1]/td").all_inner_texts()
            total += (_numbers(cells[-3]) or [0.0])[0] if len(cells) >= 3 else 0.0  # ..., stake, P/L, cancel
        if not count:
            return 0.0
        log.info("  %d waiting bet(s) on this match, stake %g", count, total)
        if self._s.dry_run:
            return total

        for _ in range(count):
            before = await cancels.count()
            await cancels.first.click()
            yes = page.locator(sel.CONFIRM_YES).filter(visible=True).first
            await yes.wait_for(timeout=5000)
            await yes.click()
            for _ in range(20):
                if await cancels.count() < before:
                    break
                await page.wait_for_timeout(500)
            else:
                raise BetAborted("failed", "a waiting bet did not disappear after cancelling")
        log.info("  cancelled %d waiting bet(s)", count)
        return total

    # --- page steps -----------------------------------------------------------------------------

    async def _open_list(self) -> None:
        page = self._page
        for attempt in range(2):
            await page.goto(self._s.site_url + sel.LIST_PATH)
            await page.locator(sel.EVENT_ROW).first.wait_for(timeout=20000)
            await self._dismiss_popups()
            if not await page.locator(sel.LOGGED_OUT).count():
                return
            if attempt == 0:
                await self._login()
        raise BetAborted("failed", "still logged out after logging in")

    async def _login(self) -> None:
        page = self._page
        await page.locator(sel.LOGGED_OUT).first.click()
        username = page.locator(sel.LOGIN_USERNAME).first
        await username.wait_for(timeout=15000)
        await username.fill(self._s.site_username)
        await page.locator(sel.LOGIN_PASSWORD).first.fill(self._s.site_password)
        await page.locator(sel.LOGIN_SUBMIT).first.click()
        await page.locator(sel.LOGGED_OUT).first.wait_for(state="hidden", timeout=20000)
        await page.wait_for_timeout(1500)  # the after-login ad appears a moment later
        await self._dismiss_popups()

    async def _open_match(self, event_name: str) -> None:
        """Finds the match in the home list and opens its match page."""
        await self._open_list()
        page = self._page
        rows = page.locator(sel.EVENT_ROW)
        names = await rows.locator(sel.EVENT_NAME).all_inner_texts()  # one name per row, same order
        index = _best_index(names, re.sub(r"\s+v\s+\?$", "", event_name))
        if index is None:
            raise BetAborted("failed", f"match not found on site: {event_name}")
        log.info("  site match: %s", names[index])
        await rows.nth(index).locator(sel.EVENT_NAME).click()
        await page.wait_for_url(sel.MATCH_URL, timeout=15000)
        await page.locator(sel.RUNNER_ROW).first.wait_for(timeout=15000)
        await self._dismiss_popups()

    async def _dismiss_popups(self) -> None:
        """Closes ads and notices (Escape, then their close icon) so they can't block clicks."""
        page = self._page
        overlay = page.locator(sel.POPUP_OVERLAY).filter(visible=True)
        for _ in range(4):
            if not await overlay.count():
                return
            log.info("  closing a pop-up on the site")
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)
            close = page.locator(sel.POPUP_CLOSE).filter(visible=True).first
            if await overlay.count() and await close.count():
                await close.click()
                await page.wait_for_timeout(500)
        if await overlay.count():
            raise BetAborted("failed", "a pop-up on the site could not be closed")

    async def _ensure_one_click_off(self) -> None:
        boxes = self._page.locator(sel.ONE_CLICK_CHECKBOX).filter(visible=True)
        for i in range(await boxes.count()):
            box = boxes.nth(i)
            if await box.is_checked():
                await box.uncheck()
                log.warning("  '1 Click Betting' was on; switched it off")
                if await box.is_checked():
                    raise BetAborted("failed", "could not switch off 1 Click Betting")

    async def _odds_box(self, selection: str, side: str, need_price: bool = True) -> tuple[Locator, float | None]:
        """Best back or lay box of the selection's row on the match page (first market = Match Odds)."""
        if side not in ("back", "lay"):
            raise BetAborted("failed", f"'{side}' is not valid for Match Odds")
        rows = self._page.locator(sel.RUNNER_ROW)
        row, row_boxes, best = None, 0, 0.0
        for i in range(await rows.count()):
            boxes = await rows.nth(i).locator(sel.PRICE_CELL).count()
            name_box = rows.nth(i).locator(sel.RUNNER_NAME).first
            if boxes not in (2, 6) or not await name_box.count():
                continue  # not a single team row
            score = name_score(selection, (await name_box.inner_text()).strip())
            if score >= NAME_MATCH_MIN and score > best:  # first row wins ties: Match Odds comes first
                row, row_boxes, best = rows.nth(i), boxes, score
        if row is None:
            raise BetAborted("failed", f"'{selection}' not found on the match page")
        if sel.SUSPENDED_TEXT in await row.inner_text():
            raise BetAborted("skipped", "market suspended")

        # 6 boxes: back3 back2 BACK1 | LAY1 lay2 lay3; 2 boxes: BACK1 | LAY1
        index = (2 if side == "back" else 3) if row_boxes == 6 else (0 if side == "back" else 1)
        cell = row.locator(sel.PRICE_CELL).nth(index)
        numbers = _numbers(await cell.inner_text())
        if not numbers and need_price:
            raise BetAborted("skipped", "no price on offer")
        return cell, (numbers[0] if numbers else None)

    async def _open_slip(self, cell: Locator) -> Locator:
        await self._ensure_one_click_off()
        await cell.click()
        slip = self._page.locator(sel.BET_SLIP).filter(visible=True).first
        try:
            await slip.wait_for(timeout=5000)
        except PlaywrightTimeout:
            if await self._page.locator(sel.LOGGED_OUT).count():
                raise BetAborted("failed", "not logged in on the site")
            raise BetAborted("failed", "bet slip did not open")
        return slip

    async def _set_unmatch(self, slip: Locator, on: bool) -> None:
        """The site remembers this tick between bets, so it is set explicitly every time."""
        box = slip.locator(sel.UNMATCH_CHECKBOX)
        if not await box.count():
            if on:
                raise BetAborted("failed", "this market doesn't offer 'Place Bet in Unmatch'")
            return
        await box.set_checked(on)

    async def _submit(self, sig: BetSignal, slip: Locator, stake: float, liability: float,
                      price: float | None) -> BetResult:
        await slip.locator(sel.STAKE_INPUT).fill(f"{stake:g}")
        await self._screenshot(sig, "slip")
        if self._s.dry_run:
            await slip.locator(sel.CANCEL_BET).click()
            return BetResult("simulated", "dry run: slip filled, not placed", price, stake, liability)

        await self._clear_toasts()
        await slip.locator(sel.PLACE_BET).click()
        status, reason = await _wait_for_outcome(self._page)
        await self._screenshot(sig, status)
        return BetResult(status, reason, price, stake, liability)

    async def _clear_toasts(self) -> None:
        """Waits for old messages to go, so they aren't mistaken for this bet's result."""
        try:
            await self._page.locator(sel.TOAST).first.wait_for(state="hidden", timeout=8000)
        except PlaywrightTimeout:
            pass

    # --- evidence -------------------------------------------------------------------------------

    async def _screenshot(self, sig: BetSignal, tag: str) -> None:
        await self._screenshot_named(f"{_slug(sig)}-{tag}")

    async def _save_page(self, sig: BetSignal, tag: str) -> None:
        """Screenshot + HTML of the page when something goes wrong, to fix selectors from."""
        path = await self._screenshot_named(f"{_slug(sig)}-{tag}")
        try:
            path.with_suffix(".html").write_text(await self._page.content(), encoding="utf-8")
        except Exception:
            log.warning("Saving page HTML failed", exc_info=True)
        log.info("  page saved: %s (+ .html)", path)

    async def _screenshot_named(self, name: str):
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{name}.png"
        try:
            await self._page.screenshot(path=str(path))
        except Exception:  # a failed screenshot must never stop a bet decision
            log.warning("Screenshot failed: %s", path, exc_info=True)
        return path


def _match_to_find(sig: BetSignal) -> str:
    """The tip's match name, or just its team when the AI couldn't tell the match ('? v ?')."""
    known = [side.strip() for side in re.split(r"\s+v\s+", sig.event) if side.strip(" ?")]
    return " v ".join(known) if known else sig.selection


def _slug(sig: BetSignal) -> str:
    return re.sub(r"[^a-z0-9]+", "-", sig.selection.lower()).strip("-")[:40]


def _best_index(names: list[str], target: str) -> int | None:
    if not names:
        return None
    target = target.lower()
    scores = [(fuzz.token_set_ratio(target, n.lower()), fuzz.ratio(target, n.lower()), i) for i, n in enumerate(names)]
    best, _, index = max(scores)
    return index if best >= NAME_MATCH_MIN else None


async def _wait_for_outcome(page: Page) -> tuple[str, str]:
    success = page.locator(sel.BET_SUCCESS).first
    error = page.locator(sel.BET_ERROR).first
    for _ in range(OUTCOME_WAIT_S * 2):
        if await success.is_visible():
            return "placed", (await success.inner_text()).strip()
        if await error.is_visible():
            return "failed", (await error.inner_text()).strip()
        await page.wait_for_timeout(500)
    return "unconfirmed", f"no confirmation within {OUTCOME_WAIT_S}s, check Open Bets on the site"


def _numbers(text: str) -> list[float]:
    return [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]


def _signed_number(text: str) -> float:
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", text)
    return float(match.group().replace(",", "")) if match else 0.0
