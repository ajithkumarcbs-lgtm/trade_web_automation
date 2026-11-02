"""Drives iob7.com in Chrome the way a person would: finds the match, bets from the slip, books trades with
the site's Cashout, and cancels waiting bets from Open Bets."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import BrowserContext, Locator, Page, Playwright, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout
from rapidfuzz import fuzz

from . import site_selectors as sel
from .config import DATA_DIR, Settings
from .fancy import PLAYER_KINDS, Session, plain_team, site_session_row
from .models import BetSignal
from .names import NAME_MATCH_MIN, code_fits, contains_player, name_score
from .risk import RiskManager, book, book_set_stake, exit_plan, exit_side, lay_instead_of_back, liability
from .store import Position

log = logging.getLogger(__name__)

BROWSER_PROFILE = DATA_DIR / "browser"  # keeps the login between runs
SCREENSHOT_DIR = DATA_DIR / "screenshots"
OUTCOME_WAIT_S = 20  # the site holds Match Odds bets for 6 s before accepting them
SUSPEND_RETRIES = 3  # tries while a market is suspended, 1 s apart
HEDGE_ROUND_UP = 0.8  # a booking bet of 80-100% of the site's Min Bet is placed at the Min Bet
# A short name the AI matched to a listed side ('Tallon' = 'T Griekspoor') is kept this long; one it couldn't
# match is asked again after RESOLVE_AGAIN (entries are retried every 10 s).
RESOLVED_KEEP = timedelta(hours=3)
RESOLVE_AGAIN = timedelta(minutes=3)  # each ask can be 3 models' calls: kept rare on the free tier
RESOLVE_BACKOFF = 7  # and doubled each time it stays unanswered (up to 6 h): an order can wait for days on a match gone
RESOLVE_LIST_MAX = 200  # matches shown to the AI
FANCY_OPEN_TRIES = 4  # looks at a fancy row 2 s apart while it says BALL RUNNING


@dataclass
class BetResult:
    status: str  # placed | simulated | unconfirmed | skipped | failed
    reason: str
    price: float | None = None
    stake: float = 0.0
    liability: float = 0.0
    screenshot: Path | None = None  # saved page when something failed
    pl: float | None = None  # cashouts: the profit/loss locked, as the site's Cashout button showed it
    name: str = ""  # entries: the site's names of the runner and the match, so the trade is recorded
    event: str = ""  # under them and later booksets find it however the tip spelt the player


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


@dataclass
class FancyQuote:
    """What the site offered for a session tip when it came."""
    name: str  # the site's row, e.g. '15 Over MLSW'
    line: float  # for the tip's side: NO 128 / YES 129
    rate: float
    other_line: float  # the other side's
    closed: bool = False  # BALL RUNNING the whole time looked at: the numbers are the last shown
    slip: str = ""  # paper: what the bet slip did when opened on it (nothing is placed)


@dataclass
class TeamQuote:
    """The other team of a match, as the site shows it now."""
    other: str = ""
    event: str = ""
    lay: float | None = None
    back: float | None = None
    gone: bool = False  # the site doesn't list the match, or it hasn't just one other team
    problem: str = ""  # why there are no prices
    team_back: float | None = None  # the tipped team's own back price


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
        self._lock = asyncio.Lock()  # one browser action at a time: tips, auto-bookset checks, keep-alive
        self._row_name = ""  # site's name of the team/player last chosen on the match page
        self._row: Locator | None = None  # and its row
        self._match_name = ""  # site's name of the match opened last
        self._saved_for: set[str] = set()  # instructions whose failure page was already saved
        # (tip's name, its match, the listed matches) -> the listed side meant, by the AI (set by the caller)
        self.resolve_name: Callable[[str, str, list[str]], Awaitable[str | None]] | None = None
        self._resolved: dict[str, tuple[datetime, str | None, int]] = {}  # name -> when asked, the side, times unanswered

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @contextlib.asynccontextmanager
    async def _using_page(self):
        """One browser action at a time. Headless, the page is left blank afterwards: the site's live odds
        keep a server's CPU busy redrawing (paid for on a small cloud server), and every action opens the
        site's list fresh anyway, so nothing is lost."""
        async with self._lock:
            try:
                yield
            finally:
                if self._s.headless:
                    await self._park()

    async def _park(self) -> None:
        try:
            await self._page.goto("about:blank")
        except Exception:
            log.warning("  could not leave the site page idle", exc_info=True)

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._ctx = await launch_browser(self._pw, self._s, self._s.headless)
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        for attempt in (1, 2):  # the site is sometimes slow to show its match list
            try:
                await self._open_list()
                break
            except (BetAborted, PlaywrightTimeout) as exc:
                reason = str(exc).splitlines()[0]
                shot = await self._screenshot_named("start-failed")
                log.warning("Start attempt %d failed: %s (screenshot %s)", attempt, reason, shot)
                if attempt == 2:
                    raise SystemExit(f"Could not open {self._s.site_url}: {reason}\nScreenshot: {shot}\n"
                                     f"If the site asks for an OTP, log in once by hand with "
                                     f"`python -m tools.snapshot_page`, then start the bot again.")
        log.info("Logged in to %s", self._s.site_url)
        if self._s.headless:
            await self._park()

    async def close(self) -> None:
        if self._ctx:
            await self._ctx.close()
        if self._pw:
            await self._pw.stop()

    async def place(self, sig: BetSignal, position: Position | None, require_green: bool = False) -> BetResult:
        """position is the trade an exit or close acts on (from the precheck).
        require_green: a bookset only goes through when it locks a profit (>= 0); else status 'retry'.
        A close the site can't take right now also returns 'retry', so the caller tries again later."""
        async with self._using_page():
            try:
                if sig.action == "close":
                    return await self._cash_out(sig, position, require_green)
                if sig.action == "exit":
                    result = await self._place_exit(sig, position)
                else:
                    result = await self._place_entry(sig)
                if result.status == "failed" and _entry_retryable(result.reason):  # e.g. price moved in the delay
                    result.status = "retry"
                return result
            except BetAborted as exc:
                status = exc.status
                if sig.action != "close" and status in ("failed", "skipped") and _entry_retryable(exc.reason):
                    status = "retry"  # passes by itself: the caller tries again
                shot = None
                if status == "failed" or (status == "retry" and sig.key() not in self._saved_for):
                    self._saved_for.add(sig.key())  # one saved page per instruction, not one per retry
                    shot = await self._save_page(sig, "failed")
                await self._close_slip()
                return BetResult(status, exc.reason, exc.price, screenshot=shot if status == "failed" else None)
            except PlaywrightTimeout as exc:
                await self._close_slip()
                # a slow page must not lose an instruction: try again
                return BetResult("retry", f"timed out: {str(exc).splitlines()[0]}")

    async def _close_slip(self) -> None:
        """Cancels a half-filled bet slip after an abort, so nothing is left ready to place."""
        try:
            cancel = self._page.locator(sel.BET_SLIP).filter(visible=True).locator(sel.CANCEL_BET).first
            if await cancel.count():
                await cancel.click(timeout=3000)
        except Exception:
            log.warning("  could not close the bet slip", exc_info=True)

    async def exit_price(self, pos: Position) -> float | None:
        """Best price on offer right now to close this trade (the lay price for a back), or None if the
        market is suspended or the match can't be found. Reads only; places nothing."""
        async with self._using_page():
            try:
                await self._open_match(pos.event, pos.selection)
                _, price = await self._odds_box(pos.selection, exit_side(pos), need_price=False)
                return price
            except (BetAborted, PlaywrightTimeout) as exc:
                log.info("  price check for %s: %s", pos.selection, exc)
                return None

    async def quote(self, sig: BetSignal) -> float | None:
        """The price on offer for this entry right now, None if the site doesn't list it yet. Reads only."""
        async with self._using_page():
            try:
                name = await self._open_match(sig.event, sig.selection, sig.aka)
                _, price = await self._odds_box(name, sig.side, need_price=False)
                return price
            except (BetAborted, PlaywrightTimeout) as exc:
                log.info("  price of %s when tipped: %s", sig.selection, exc)
                return None

    async def other_team_quote(self, event: str, team: str) -> TeamQuote:
        """The prices now of the team playing this one (SK Astro's method lays it). Reads only."""
        async with self._using_page():
            try:
                name = await self._open_match(event, team)
                _, team_back = await self._odds_box(name, "back", need_price=False)  # finds the team's row
                other = await self._other_runner()
                if other is None:
                    return TeamQuote(gone=True, problem=f"{team}: this market has more than two runners")
                _, lay = await self._odds_box(other, "lay", need_price=False)
                _, back = await self._odds_box(other, "back", need_price=False)
                return TeamQuote(other, self._match_name, lay, back, team_back=team_back)
            except BetAborted as exc:
                return TeamQuote(gone=exc.status == "failed", problem=exc.reason)
            except PlaywrightTimeout as exc:
                return TeamQuote(problem=str(exc).splitlines()[0])

    async def fancy_quote(self, event: str, session: Session, side: str) -> FancyQuote | None:
        """The site's line and rate for a session tip right now, None when it doesn't list that session. Reads only;
        in a dry run it also opens the bet slip on the row and cancels it, to prove the clicks. There is no code that
        places a fancy bet: these tips stay on paper."""
        team = session.team
        if session.kind in PLAYER_KINDS:  # the match's name doesn't have the player: by a team the tip's match names
            team = next((side for side in event.split(" v ") if side.strip(" ?")), "")
        if session.kind == "toss" or not team:
            return None
        async with self._using_page():
            try:
                await self._open_match(event, plain_team(team))
                rows = self._page.locator(sel.FANCY_ROW)
                await rows.first.wait_for(timeout=8000)
                names = await rows.evaluate_all("(els, name) => els.map(e => e.querySelector(name)?.innerText || '')",
                                                sel.FANCY_NAME)
                index = site_session_row(names, session)
                if index is None:
                    log.info("  fancy: the site lists no %s %s row: %s", session.team, session.number, names[:12])
                    return None
                row = rows.nth(index)
                for attempt in range(FANCY_OPEN_TRIES):
                    closed = bool(await row.locator(sel.FANCY_CLOSED).count())
                    if not closed or attempt == FANCY_OPEN_TRIES - 1:
                        break
                    await self._page.wait_for_timeout(2000)
                cells = row.locator(sel.PRICE_CELL)  # NO | YES
                no, yes = _numbers(await cells.nth(0).inner_text()), _numbers(await cells.nth(1).inner_text())
                if len(no) < 2 or len(yes) < 2:  # '-': the row is shown without a price
                    log.info("  fancy: %s has no price now", names[index])
                    return None
                ours, other = (yes, no) if side == "yes" else (no, yes)
                quote = FancyQuote(names[index].strip(), ours[0], ours[1], other[0], closed)
                if self._s.dry_run and not closed:
                    quote.slip = await self._try_fancy_slip(cells.nth(1 if side == "yes" else 0), session)
                return quote
            except (BetAborted, PlaywrightTimeout) as exc:
                log.info("  fancy: no site line for %s: %s", session.team, str(exc).splitlines()[0])
                return None

    async def _try_fancy_slip(self, cell: Locator, session: Session) -> str:
        """Opens the bet slip on a fancy box and cancels it. Its fields are saved when it doesn't open as expected."""
        try:
            slip = await self._open_slip(cell)
            low, high = await _slip_limits(slip)
            await self._screenshot_named(f"fancy-slip-{session.team}".replace(" ", "-"))
            await slip.locator(sel.CANCEL_BET).click()
            return f"slip opened, limits {low}-{high}, cancelled"
        except (BetAborted, PlaywrightTimeout) as exc:
            path = await self._screenshot_named("fancy-slip-failed")
            with contextlib.suppress(Exception):
                path.with_suffix(".html").write_text(await self._page.content(), encoding="utf-8")
            await self._close_slip()
            return f"slip check failed: {str(exc).splitlines()[0]}"

    async def keep_alive(self) -> None:
        """Reloads the site so the session doesn't time out, logging in again if it did."""
        async with self._using_page():
            try:
                await self._open_list()
            except (BetAborted, PlaywrightTimeout) as exc:
                log.warning("keep-alive: %s", exc)

    # --- trades ---------------------------------------------------------------------------------

    async def _place_entry(self, sig: BetSignal) -> BetResult:
        """Back or lay at the best price on offer now, if it is still close to the tip's price.
        A back above MAX_BACK_ODDS (the site refuses those) is placed as a lay of the opponent instead.
        The result carries the site's names of the runner and match, which the trade is recorded under."""
        name = await self._open_match(sig.event, sig.selection, sig.aka)
        match = self._match_name
        limit = self._s.max_back_odds
        for attempt in range(2):
            # with a back limit the row is found even without a back price: the site may show none above it
            cell, price = await self._odds_box_when_open(name, sig.side, need_price=not (sig.side == "back" and limit))
            runner = self._row_name
            log.info("  live %s price for %s: %s (tip %s)", sig.side, runner, price, sig.odds or sig.when_odds)
            if sig.side == "back" and limit and (price is None or price >= limit):
                # e.g. tip "back X 15": lay the other runner at its lay price (1.07 = back 15.3) right now
                result = await self._place_back_as_lay(sig, price)
                break
            decision = self._risk.price_check(sig, price)
            if not decision.ok:
                raise BetAborted("skipped", decision.reason, price)
            await self._check_balance(decision.liability, price)
            slip = await self._open_slip(cell)
            log.info("  bet slip open")
            await self._set_unmatch(slip, False)  # take the price now; never leave an entry waiting
            slip_price = _to_float(await slip.locator(sel.ODDS_INPUT).input_value())
            if slip_price is not None and slip_price != price:  # moved between reading the box and clicking
                decision = self._risk.price_check(sig, slip_price)
                if not decision.ok:
                    raise BetAborted("skipped", f"price moved in the slip: {decision.reason}", slip_price)
                price = slip_price
            result = await self._submit(sig, slip, decision.stake, decision.liability, price)
            if decision.reason != "ok":  # e.g. IGNORE_SLIPPAGE in a dry run
                result.reason = f"{result.reason} [{decision.reason}]"
            moved = result.status == "failed" and sel.PRICE_MOVED_ERROR in result.reason.lower()
            if not (moved and attempt == 0):
                break
            log.info("  price moved before the bet was accepted; checking the new price once more")
        result.name, result.event = runner, match
        return result

    async def _place_back_as_lay(self, sig: BetSignal, back_price: float | None) -> BetResult:
        """Back A above MAX_BACK_ODDS = lay B (the only other runner) at B's lay price b, with lay stake
        = back stake x (a - 1) where a = b / (b - 1): the same win if A wins, the same loss (the back stake)
        if A loses. The trade is recorded as the back on A, so exits and booksets work as usual.
        back_price is A's back price on the site, None when the site shows none (above its limit)."""
        limit = self._s.max_back_odds
        shown = f"{back_price:g}" if back_price else "not shown"
        other = await self._other_runner()
        if other is None:
            raise BetAborted("skipped", f"back price {shown} (limit {limit:g}) and this market has more than two "
                                        f"runners, so it can't be placed as a lay", back_price)
        cell, lay_price = await self._odds_box_when_open(other, "lay")
        if not lay_price or lay_price <= 1:
            raise BetAborted("skipped", f"no lay price on {other}", back_price)
        back_odds, _, _ = lay_instead_of_back(1, lay_price)
        if back_price is None and back_odds < limit:  # not above the limit: A just has no back price right now
            raise BetAborted("skipped", "no price on offer", back_price)
        decision = self._risk.price_check(sig, back_odds)  # compared with the tip as the back it stands for
        if not decision.ok:
            raise BetAborted("skipped", decision.reason, round(back_odds, 2))
        back_odds, lay_stake, risk = lay_instead_of_back(decision.stake, lay_price)
        await self._check_balance(risk, lay_price)
        slip = await self._open_slip(cell)
        log.info("  back %s (price %s, limit %g): laying %s %g @ %g instead (= back @ %.2f)", sig.selection, shown,
                 limit, other, lay_stake, lay_price, back_odds)
        await self._set_unmatch(slip, False)
        slip_price = _to_float(await slip.locator(sel.ODDS_INPUT).input_value())
        if slip_price is not None and slip_price != lay_price:  # moved between reading the box and clicking
            back_odds, lay_stake, risk = lay_instead_of_back(decision.stake, slip_price)
            moved = self._risk.price_check(sig, back_odds)
            if not moved.ok:
                raise BetAborted("skipped", f"price moved in the slip: {moved.reason}", round(back_odds, 2))
            lay_price = slip_price
        lay_sig = sig.model_copy(update={"side": "lay", "selection": other})
        result = await self._submit(lay_sig, slip, lay_stake, risk, lay_price)
        placed = result.stake or lay_stake  # the site's Max Bet may have cut the lay
        back_stake = round(placed / (back_odds - 1), 2)  # what the lay stands for, as a back on A
        result.reason = (f"{result.reason}; placed as LAY {other} {placed:g} @ {lay_price:g} = BACK {sig.selection} "
                         f"{back_stake:g} @ {back_odds:.2f} (the site takes no back above {limit:g})")
        result.price, result.stake, result.liability = round(back_odds, 2), back_stake, back_stake
        return result

    async def _other_runner(self) -> str | None:
        """Name of the only other runner in the market of the row chosen last (None with a draw or more)."""
        if self._row is None:
            return None
        rows = self._row.locator("xpath=..").locator(sel.RUNNER_ROW)
        names = []
        for i in range(await rows.count()):
            row = rows.nth(i)
            name_box = row.locator(sel.RUNNER_NAME).first
            if await row.locator(sel.PRICE_CELL).count() not in (2, 6) or not await name_box.count():
                continue
            names.append((await name_box.inner_text()).strip())
        others = [n for n in names if n != self._row_name]
        return others[0] if len(names) == 2 and len(others) == 1 else None

    async def cancel_waiting(self, pos: Position) -> float:
        """Cancels the waiting (unmatched) bets on this trade's match; returns their total stake."""
        async with self._using_page():
            await self._open_match(pos.event, pos.selection)
            return await self._cancel_unmatched()

    async def _odds_box_when_open(self, selection: str, side: str,
                                  need_price: bool = True) -> tuple[Locator, float | None]:
        """Tennis markets lock for a moment during points: retries SUSPEND_RETRIES times, 1 s apart."""
        for attempt in range(1, SUSPEND_RETRIES + 1):
            try:
                return await self._odds_box(selection, side, need_price)
            except BetAborted as exc:
                if exc.status != "skipped" or attempt == SUSPEND_RETRIES:
                    raise
                log.info("  %s, retry %d/%d in 1 s", exc.reason, attempt, SUSPEND_RETRIES - 1)
                await self._page.wait_for_timeout(1000)

    async def _check_balance(self, needed: float, price: float | None) -> None:
        """INSUFFICIENT_BALANCE: never click when the balance can't cover the bet's worst-case loss.
        A dry run carries on (nothing is placed) so the clicks can still be tested."""
        balance, _ = await self._balance()
        log.info("  balance %s, this bet can lose %g", balance, needed)
        if balance is None or balance >= needed:
            return
        message = f"INSUFFICIENT_BALANCE: balance {balance:g} < {needed:g} needed"
        if not self._s.dry_run:
            raise BetAborted("skipped", message, price)
        log.warning("  %s (dry run continues; a live run would skip)", message)

    async def _place_exit(self, sig: BetSignal, pos: Position) -> BetResult:
        """Book the trade at the tipster's price: an equal-profit opposite bet that waits (unmatched) until
        the price is reached. A newer exit replaces the earlier waiting one."""
        await self._open_match(pos.event, pos.selection)
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
        if side == "back" and self._s.max_back_odds and sig.odds > self._s.max_back_odds:
            raise BetAborted("skipped", f"a waiting exit would be a back at {sig.odds:g}, above MAX_BACK_ODDS "
                                        f"{self._s.max_back_odds:g}: book it with a bookset instead")
        cell, _ = await self._odds_box(pos.selection, side, need_price=False)
        if not self._s.dry_run and not await self._site_has_position():
            # never hedge a trade the site doesn't show: that would be a new bet, not a book set
            raise BetAborted("skipped", "the site shows no matched position on this match (entry not confirmed), "
                                        "so no hedge was placed")
        check = self._risk.check_exit_size(side, stake, risk, pos)
        if not check.ok:
            raise BetAborted("skipped", check.reason)
        log.info("  book set at %g: %s %g (earlier exit matched %g)", sig.odds, side, stake, filled)

        slip = await self._open_slip(cell)
        await slip.locator(sel.ODDS_INPUT).fill(f"{sig.odds:g}")
        await self._set_unmatch(slip, True)
        return await self._submit(sig, slip, stake, risk, sig.odds, hedge=True)

    async def _cash_out(self, sig: BetSignal, pos: Position | None, require_green: bool = False) -> BetResult:
        """Book set / loss cut now with the site's own Cashout, after cancelling waiting exits so they
        can't match later and undo the booking. pos is None for a trade the bot didn't open.
        Raises BetAborted('retry') when it can't be done right now (red while waiting for green, or the site
        says the bet is still going on / suspended), so the caller tries again."""
        if pos is None:
            log.info("  no trade recorded by the bot; cashing out the site's position on this match")
            name = await self._open_match(_match_to_find(sig), sig.selection, sig.aka)
        else:
            name = await self._open_match(pos.event, pos.selection)
        if pos and pos.exit_price is not None:  # a Limit exit may still be waiting
            await self._cancel_unmatched()
        page = self._page
        button = page.locator(sel.CASHOUT_BUTTON).filter(visible=True).first
        if not await button.count():
            # Markets like Test matches (with a Draw) have no Cashout button, and Turbo Cashout charges 7%:
            # book by hand instead, as a person would.
            return await self._hedge_now(sig, name, pos, require_green)
        pl = _signed_number(await button.inner_text())
        if require_green and pl < 0 and not self._s.dry_run:
            raise BetAborted("retry", f"Cashout is red ({pl:+g}); waiting for it to turn green")
        await button.click()
        confirm = page.locator(sel.CASHOUT_CONFIRM).filter(visible=True).first
        await confirm.wait_for(timeout=5000)
        await self._screenshot(sig, "cashout")

        if self._s.dry_run:
            await page.locator(sel.CASHOUT_CANCEL).filter(visible=True).first.click()
            return BetResult("simulated", f"dry run: cashout of {pl:g} offered, not confirmed", pl=pl)
        before = await self._balance()
        await self._clear_toasts()
        await confirm.click()
        status, reason = await _wait_for_outcome(page)
        status, reason = await self._confirm_by_balance(status, reason, before)
        await self._screenshot(sig, f"cashout-{status}")
        if status == "failed" and _retryable(reason):  # e.g. "Your bet is still on going."
            raise BetAborted("retry", f"cashout {pl:+g} refused for now: {reason}")
        return BetResult(status, f"cashout {pl:g}: {reason}", pl=pl)

    async def _hedge_now(self, sig: BetSignal, name: str, pos: Position | None,
                         require_green: bool = False) -> BetResult:
        """Books the trade by hand: one opposite bet on the trade's runner (name) at the best price now, sized
        from the site's own book (the P/L shown under each name) so every result pays the same."""
        await self._odds_box(name, "back", need_price=False)  # finds the runner's row
        mine, others = await self._site_book()
        if mine is None or not others:
            raise BetAborted("failed", "could not read the book (P/L under the names) for this market")
        if max(others) - min(others) > 0.01:
            raise BetAborted("failed", "the position covers several runners; book it by hand")
        win, lose = mine, others[0]
        if abs(win - lose) < 0.01:
            if not (self._s.dry_run and pos and pos.price):
                # e.g. an earlier attempt went through without a confirmation: the trade is booked already
                return BetResult("placed", f"already booked on the site: every result pays {win:+g}", pl=win)
            win, lose = book(pos.side, pos.stake, pos.price)  # dry run: the trade was never really placed
        side = "lay" if win > lose else "back"

        try:
            cell, price = await self._odds_box_when_open(name, side)
        except BetAborted as exc:
            if exc.status == "skipped":  # suspended / no price for now: try again
                raise BetAborted("retry", exc.reason) from exc
            raise
        if side == "back" and self._s.max_back_odds and price and price > self._s.max_back_odds:
            return await self._hedge_back_as_lay(sig, win, lose, require_green)
        slip = await self._open_slip(cell)
        await self._set_unmatch(slip, False)  # take the price on offer now
        price = _to_float(await slip.locator(sel.ODDS_INPUT).input_value()) or price
        stake = book_set_stake(win, lose, price)
        locked = round(lose + stake if side == "lay" else lose - stake, 2)
        log.info("  book by hand: %s now %+g, others %+g -> %s %g @ %g locks %+g on every result",
                 self._row_name, win, lose, side, stake, price, locked)
        if require_green and locked < 0 and not self._s.dry_run:
            raise BetAborted("retry", f"booking now would lock {locked:+g}; waiting for it to turn green")
        result = await self._submit(sig, slip, stake, liability(side, stake, price), price, hedge=True)
        if result.status == "failed" and _retryable(result.reason):
            raise BetAborted("retry", f"booking refused for now: {result.reason}")
        result.pl = locked
        result.reason = f"booked by hand ({side} {result.stake:g} @ {price:g}, locks {locked:+g}): {result.reason}"
        return result

    async def _hedge_back_as_lay(self, sig: BetSignal, win: float, lose: float, require_green: bool) -> BetResult:
        """Booking needs a back above MAX_BACK_ODDS (a lay trade whose runner drifted): lay the other runner
        instead, sized so every result pays the same."""
        name = self._row_name
        other = await self._other_runner()
        if other is None:
            raise BetAborted("failed", f"booking needs a back on {name} above MAX_BACK_ODDS and this market has "
                                       f"more than two runners: book it by hand")
        try:
            cell, lay_price = await self._odds_box_when_open(other, "lay")
        except BetAborted as exc:
            if exc.status == "skipped":
                raise BetAborted("retry", exc.reason) from exc
            raise
        slip = await self._open_slip(cell)
        await self._set_unmatch(slip, False)
        lay_price = _to_float(await slip.locator(sel.ODDS_INPUT).input_value()) or lay_price
        back_odds, _, _ = lay_instead_of_back(1, lay_price)
        back_stake = book_set_stake(win, lose, back_odds)
        _, lay_stake, risk = lay_instead_of_back(back_stake, lay_price)
        locked = round(lose - back_stake, 2)
        log.info("  book by hand: back %s @ %.2f is above %g -> lay %s %g @ %g locks %+g on every result",
                 name, back_odds, self._s.max_back_odds, other, lay_stake, lay_price, locked)
        if require_green and locked < 0 and not self._s.dry_run:
            raise BetAborted("retry", f"booking now would lock {locked:+g}; waiting for it to turn green")
        lay_sig = sig.model_copy(update={"side": "lay", "selection": other})
        result = await self._submit(lay_sig, slip, lay_stake, risk, lay_price, hedge=True)
        if result.status == "failed" and _retryable(result.reason):
            raise BetAborted("retry", f"booking refused for now: {result.reason}")
        result.pl = locked
        result.reason = (f"booked by hand (lay {other} {result.stake:g} @ {lay_price:g} = back {name} @ "
                         f"{back_odds:.2f}, locks {locked:+g}): {result.reason}")
        return result

    async def _site_book(self) -> tuple[float | None, list[float]]:
        """P/L the site shows under each runner of the chosen row's market: (this runner, the others)."""
        if self._row is None:
            return None, []
        rows = self._row.locator("xpath=..").locator(sel.RUNNER_ROW)
        mine, others = None, []
        for i in range(await rows.count()):
            row = rows.nth(i)
            name_box = row.locator(sel.RUNNER_NAME).first
            if await row.locator(sel.PRICE_CELL).count() not in (2, 6) or not await name_box.count():
                continue
            book_box = row.locator(sel.RUNNER_BOOK).first
            value = _signed_number(await book_box.inner_text()) if await book_box.count() else 0.0
            if (await name_box.inner_text()).strip() == self._row_name:
                mine = value
            else:
                others.append(value)
        return mine, others

    async def listed_matches(self) -> list[str] | None:
        """Names of the matches the home page lists now (None if the page couldn't be read)."""
        async with self._using_page():
            try:
                await self._open_list()
                return await self._page.locator(sel.EVENT_ROW).locator(sel.EVENT_NAME).all_inner_texts()
            except (BetAborted, PlaywrightTimeout) as exc:
                log.warning("could not read the match list: %s", exc)
                return None

    async def _site_has_position(self) -> bool:
        """True when the site shows a matched position: a 'Cashout: <P/L>' button, or a P/L other than 0
        under the runners of the chosen row's market (markets without Cashout, e.g. with a Draw)."""
        page = self._page
        if (await page.locator(sel.CASHOUT_BUTTON).filter(visible=True).count()
                or await page.locator(sel.TURBO_CASHOUT).filter(visible=True).count()):
            return True
        mine, others = await self._site_book()
        return any(abs(v) > 0.001 for v in [mine or 0.0, *others])

    async def _in_open_bets(self, name: str, stake: float, odds: float | None) -> str | None:
        """Looks for the bet in the Open Bets panel (Matched, then Unmatch tab) by player, stake and odds.
        Returns the tab it was found in, or None."""
        page = self._page
        word = max(re.findall(r"[A-Za-z]{3,}", name) or [name], key=len)
        for label, tab_selector in (("Matched", sel.MATCHED_TAB), ("Unmatch", sel.UNMATCH_TAB)):
            tab = page.locator(tab_selector).filter(visible=True).first
            if not await tab.count():
                continue
            await tab.click()
            await page.wait_for_timeout(1200)  # the list reloads from the server
            rows = page.locator("tr").filter(visible=True).filter(has_text=re.compile(re.escape(word), re.I))
            for i in range(await rows.count()):
                values = _numbers(await rows.nth(i).inner_text())
                if any(abs(v - stake) < 0.01 for v in values) and (
                        odds is None or any(abs(v - odds) < 1e-6 for v in values)):
                    return label
        return None

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

    async def _open_match(self, event_name: str, selection: str = "", aka: str = "") -> str:
        """Finds the match in the home list and opens its match page. Only matches that include the
        selection (the player being bet on) are considered. aka is the rules' spelling of the same player
        ('gea' where the AI wrote 'Gaea'): tried too, and when the two names are in different matches nothing
        is opened. Returns the name that found the match, to find the runner's row with."""
        await self._open_list()
        page = self._page
        rows = page.locator(sel.EVENT_ROW)
        names = await rows.locator(sel.EVENT_NAME).all_inner_texts()  # one name per row, same order
        target = re.sub(r"\s+v\s+\?$", "", event_name)
        tried = [selection] + ([aka] if aka.strip(" ?") and aka.lower() != selection.lower() else [])
        found = {name: index for name in tried if (index := _best_index(names, target, name)) is not None}
        if not found and self.resolve_name:
            found = await self._found_by_ai(names, event_name, tried)
        if not found:
            named = [name for name in tried if name.strip(" ?")]
            who = f" with {' / '.join(named)}" if named else ""
            raise BetAborted("failed", f"match not found on site: {event_name}{who}")
        if len(set(found.values())) > 1:
            raise BetAborted("failed", f"'{selection}' (AI) and '{aka}' (rules) are in different matches on the "
                                       f"site ({' / '.join(names[i] for i in found.values())}): not sure which "
                                       f"is meant, nothing placed")
        name, index = next(iter(found.items()))
        log.info("  site match: %s%s", names[index], f" (found by the rules' spelling '{name}')"
                 if name != selection else "")
        self._match_name = names[index].strip()
        await rows.nth(index).locator(sel.EVENT_NAME).click()
        await page.wait_for_url(sel.MATCH_URL, timeout=15000)
        await page.locator(sel.RUNNER_ROW).first.wait_for(timeout=15000)
        await self._dismiss_popups()
        return name

    async def _found_by_ai(self, names: list[str], event_name: str, tried: list[str]) -> dict[str, int]:
        """A one-word name the list doesn't have (first name 'Tallon', code 'pkr'): the AI picks the listed side it
        means ('T Griekspoor'), which must be in the list and fit any initial shown. Returns {side: match index}."""
        for name in tried:
            key = name.strip(" ?").lower()
            if " " in key or len(key) < 2:  # 'bh' = Brisbane Heat
                continue
            when, side, tries = self._resolved.get(key, (datetime.min, None, 0))
            fresh = datetime.now() - when < (RESOLVED_KEEP if side else RESOLVE_AGAIN * 2 ** min(tries, RESOLVE_BACKOFF))
            if fresh and side is None:
                continue
            if not fresh or _best_index(names, side, side) is None:
                try:
                    side = await self.resolve_name(name.strip(" ?"), event_name, names[:RESOLVE_LIST_MAX])
                except Exception as exc:  # the AI being down must not stop the retries
                    log.warning("  could not ask the AI who '%s' is: %s", name, exc)
                    side = None
                if side and not _initials_fit(key, side):
                    log.warning("  AI says '%s' is %s, but the site's initial doesn't fit: not used", name, side)
                    side = None
                self._resolved[key] = (datetime.now(), side, 0 if side else tries + 1)
            index = _best_index(names, side, side) if side else None
            if index is not None:
                log.info("  '%s' is %s in %s (AI, from the site's list)", name, side, names[index])
                return {side: index}
        return {}

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
        count = await boxes.count()
        for i in range(count):
            box = boxes.nth(i)
            if await box.is_checked():
                await box.uncheck()
                log.warning("  '1 Click Betting' was ON (a click would bet instantly); switched it off")
                if await box.is_checked():
                    raise BetAborted("failed", "could not switch off 1 Click Betting")
        log.info("  1 Click Betting is off (%d switch(es) checked)", count)

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
            listed = (await name_box.inner_text()).strip()
            score = name_score(selection, listed)
            if score < NAME_MATCH_MIN and (code_fits(selection, listed)  # 'af' on the 'Amsterdam Flames' row
                                           or _first_name_of(selection, listed)):  # 'Tallon' on 'Tallon Griekspoor'
                score = NAME_MATCH_MIN
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
        row_name = (await row.locator(sel.RUNNER_NAME).first.inner_text()).strip()
        self._row_name = row_name  # the site's spelling, used to find the bet in Open Bets afterwards
        self._row = row
        log.info("  Match Odds row '%s' (name score %.0f), %s box %d of %d shows %s", row_name, best,
                 "BACK (blue)" if side == "back" else "LAY (pink)", index + 1, row_boxes, numbers[:1] or "no price")
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
                      price: float | None, hedge: bool = False) -> BetResult:
        """Fills the slip like a person, checks it shows exactly what was intended, then places it.
        hedge: a booking bet, which may be rounded up to the site's Min Bet when just below it."""
        low, high = await _slip_limits(slip)
        capped = ""
        if high is not None and high <= 0:
            raise BetAborted("skipped", "the site's Max Bet is 0: this market takes no bets", price)
        if high is not None and stake > high:  # bet what the site allows rather than miss the tip
            capped = f"stake capped from {stake:g} to the site's Max Bet {high:g}"
            log.warning("  %s", capped)
            liability, stake = liability * high / stake, high
        if hedge and low is not None and HEDGE_ROUND_UP * low <= stake < low:
            capped = f"hedge rounded up from {stake:g} to the site's Min Bet {low:g}"
            log.info("  %s", capped)
            liability, stake = liability * low / stake, low
        if low is not None and stake < low and self._s.dry_run and self._s.ignore_slippage and stake > 0:
            # paper test of a small unit: the tip is still followed, so its result counts
            capped = f"paper test: stake {stake:g} raised to the site's Min Bet {low:g} (live would skip it)"
            log.info("  %s", capped)
            liability, stake = liability * low / stake, low
        if low is not None and stake < low:  # never raise an entry's stake above what was asked
            raise BetAborted("skipped", f"stake {stake:g} is below the site's Min Bet {low:g}", price)

        await self._enter_stake(slip, stake)
        shown_odds = _to_float(await slip.locator(sel.ODDS_INPUT).input_value())
        if price is not None and (shown_odds is None or abs(shown_odds - price) > 1e-6):
            raise BetAborted("failed", f"slip shows odds {shown_odds}, expected {price:g}", price)
        log.info("  slip checked: odds %s, stake %g, limits %s-%s", shown_odds, stake, low, high)
        await self._screenshot(sig, "slip")
        if self._s.dry_run:
            await slip.locator(sel.CANCEL_BET).click()
            note = f"; {capped}" if capped else ""
            return BetResult("simulated", f"dry run: slip filled and checked, not placed{note}", price, stake, liability)

        place = slip.locator(sel.PLACE_BET)
        for _ in range(10):
            if await place.is_enabled():
                break
            await self._page.wait_for_timeout(300)
        else:
            raise BetAborted("failed", "Placebet stayed disabled", price)
        before = await self._balance()
        await self._clear_toasts()
        await place.click()
        log.info("  Placebet clicked (odds %s, stake %g); waiting for the site", shown_odds, stake)
        status, reason = await _wait_for_outcome(self._page)
        status, reason = await self._confirm_by_balance(status, reason, before)
        if status != "failed":
            listed = await self._in_open_bets(self._row_name or sig.selection, stake, shown_odds)
            if listed:
                if status == "unconfirmed":
                    status, reason = "placed", f"no message shown, but listed in Open Bets ({listed})"
                else:
                    reason = f"{reason}; listed in Open Bets ({listed})"
            elif status == "placed":
                reason = f"{reason}; not yet listed in Open Bets"
        if capped:
            reason = f"{reason}; {capped}"
        log.info("  result: %s, %s", status.upper(), reason)
        await self._screenshot(sig, status)
        return BetResult(status, reason, price, stake, liability)

    async def _enter_stake(self, slip: Locator, stake: float) -> None:
        """Clicks the matching quick-stake button (e.g. 1K for 1000) like a person, else types the amount;
        then checks the stake box really shows it."""
        stake_box = slip.locator(sel.STAKE_INPUT)
        buttons = slip.locator(sel.QUICK_STAKE)
        how = "typed"
        for i in range(await buttons.count()):
            label = await buttons.nth(i).inner_text()
            if _stake_label(label) == stake:
                await buttons.nth(i).click()
                how = f"quick-stake button {label.strip()}"
                break
        if _to_float(await stake_box.input_value()) != stake:
            how = "typed"
            await stake_box.fill(f"{stake:g}")
        if _to_float(await stake_box.input_value()) != stake:  # last resort: type it key by key
            how = "typed key by key"
            await stake_box.click()
            await stake_box.press("Control+A")
            await stake_box.press_sequentially(f"{stake:g}", delay=100)
        shown = _to_float(await stake_box.input_value())
        if shown != stake:
            raise BetAborted("failed", f"slip shows stake {shown}, expected {stake:g}")
        log.info("  stake %g entered (%s) and checked", stake, how)

    async def _balance(self) -> tuple[float | None, float | None]:
        values = []
        for selector in (sel.BALANCE, sel.EXPOSURE):
            box = self._page.locator(selector).filter(visible=True).first
            values.append(_signed_number(await box.inner_text()) if await box.count() else None)
        return values[0], values[1]

    async def _confirm_by_balance(self, status: str, reason: str,
                                  before: tuple[float | None, float | None]) -> tuple[str, str]:
        """A bet the site accepts changes BAL or EXP. Uses that as a second confirmation next to the message."""
        if status == "failed" or before == (None, None):
            return status, reason
        for _ in range(16):  # up to 8 s for the header to refresh
            after = await self._balance()
            if after != before:
                note = f"BAL/EXP {before[0]}/{before[1]} -> {after[0]}/{after[1]}"
                if status == "unconfirmed":
                    return "placed", f"no message shown, but {note}"
                return status, f"{reason} ({note})"
            await self._page.wait_for_timeout(500)
        if status == "placed":
            return status, f"{reason} (BAL/EXP unchanged so far: check Open Bets)"
        return status, reason

    async def _clear_toasts(self) -> None:
        """Waits for old messages to go, so they aren't mistaken for this bet's result."""
        try:
            await self._page.locator(sel.TOAST).first.wait_for(state="hidden", timeout=8000)
        except PlaywrightTimeout:
            pass

    # --- evidence -------------------------------------------------------------------------------

    async def _screenshot(self, sig: BetSignal, tag: str) -> None:
        await self._screenshot_named(f"{_slug(sig)}-{tag}")

    async def _save_page(self, sig: BetSignal, tag: str) -> Path:
        """Screenshot + HTML of the page when something goes wrong, to fix selectors from."""
        path = await self._screenshot_named(f"{_slug(sig)}-{tag}")
        try:
            path.with_suffix(".html").write_text(await self._page.content(), encoding="utf-8")
        except Exception:
            log.warning("Saving page HTML failed", exc_info=True)
        log.info("  page saved: %s (+ .html)", path)
        return path

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


def _sides(match_name: str) -> list[str]:
    return [side.strip() for side in re.split(r"\s+(?:v|vs)\s+", match_name, flags=re.IGNORECASE)]


def _slug(sig: BetSignal) -> str:
    return re.sub(r"[^a-z0-9]+", "-", sig.selection.lower()).strip("-")[:40]


def _first_name_of(name: str, listed: str) -> bool:
    """A single name the tipster gave ('Tallon', 'Dayana') is the first name of this listed player."""
    word = name.strip().lower()
    parts = re.split(r"[^a-z]+", listed.lower())
    return " " not in word and len(word) >= 3 and len(parts) >= 2 and parts[0] == word


def _initials_fit(name: str, side: str) -> bool:
    """The site shows some players with the start of the first name ('T Griekspoor', 'Ha Shi'): the tipster's
    first name must begin with it ('Tallon', 'Han'). Longer first words ('Pokhara Avengers') aren't checked."""
    words = [w for w in re.split(r"[^a-z]+", side.lower()) if w]
    return len(words) < 2 or len(words[0]) > 3 or name.lower().startswith(words[0])


def _best_index(names: list[str], target: str, selection: str = "") -> int | None:
    """Index of the listed match that best fits the tip (typos allowed); ties go to the closest full name,
    so 'South Africa' picks 'South Africa v Australia' over 'South Africa Legends v Australia Legends'.
    With a selection, only matches that include that player/team are considered."""
    target = target.lower()
    must_have = selection if selection.strip(" ?") else ""
    scores = [(round(name_score(target, n)), fuzz.ratio(target, n.lower()), -i) for i, n in enumerate(names)
              if not must_have or contains_player(must_have, n)]
    if not scores and must_have:
        # A team code the tipster made up ('af' = Amsterdam Flames), or a player's first name ('Tallon' in
        # 'Tallon Griekspoor v Brandon Nakashima'): the one listed match with a side it fits. Two candidates
        # ('af' = Afghanistan too) is a guess, so none.
        fits = [i for i, n in enumerate(names)
                if any(code_fits(must_have, side) or _first_name_of(must_have, side) for side in _sides(n))]
        return fits[0] if len(fits) == 1 else None
    if not scores:
        return None
    best, _, index = max(scores)
    # With a player named, his surname being listed decides (the site lists surnames only, 'Svrcina v To
    # Etcheverry'); the rest of the name only ranks the candidates.
    return -index if must_have or best >= NAME_MATCH_MIN else None


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


def _entry_retryable(reason: str) -> bool:
    """Entry / limit-exit problems that can pass by themselves, so the instruction is tried again: a suspended
    market, a moved price, a slow page, a match not listed yet. Not: balance, Min Bet, your own limits."""
    text = reason.lower()
    if any(word in text for word in ("insufficient", "min bet", "max_liability", "daily", "units", "1 click",
                                     "not valid")):
        return False
    return _retryable(reason) or any(word in text for word in (
        "not found", "no price", "slippage", "outside", "price moved", "did not open", "not logged in",
        "max bet is 0", "stayed disabled", "timed out", "could not", "waiting"))


def _retryable(message: str) -> bool:
    """Site refusals that pass by themselves: a point in play, the bet delay, a suspended market."""
    text = message.lower()
    return any(word in text for word in ("on going", "ongoing", "going on", "suspend", "running", "try again",
                                         "unmatch"))


def _to_float(text: str | None) -> float | None:
    try:
        return float(str(text).replace(",", "").strip())
    except ValueError:
        return None


def _stake_label(text: str) -> float | None:
    """Value of a quick-stake button or a limit: '100' -> 100, '1K' -> 1000, '25K' -> 25000.
    None for MIN STAKE, MAX STAKE, ALL IN, CLEAR and anything else that isn't an amount."""
    match = re.fullmatch(r"\s*(\d[\d,]*(?:\.\d+)?)\s*([kKlLmM]?)\s*", text or "")
    if not match:
        return None
    multiplier = {"": 1, "k": 1_000, "l": 100_000, "m": 1_000_000}[match.group(2).lower()]
    return float(match.group(1).replace(",", "")) * multiplier


def _parse_limits(text: str) -> tuple[float | None, float | None]:
    """'Min Bet: 10 Max Bet: 25000' -> (10, 25000)."""
    found = []
    for label in ("Min Bet", "Max Bet"):
        # a K/L/M suffix must touch the number and not start a word ("10 Max" is 10, not 10 million)
        match = re.search(label + r"\s*:?\s*(\d[\d,]*(?:\.\d+)?(?:[kKlLmM](?![a-zA-Z]))?)", text)
        found.append(_stake_label(match.group(1)) if match else None)
    return found[0], found[1]


async def _slip_limits(slip: Locator) -> tuple[float | None, float | None]:
    box = slip.get_by_text(re.compile(sel.SLIP_LIMITS_TEXT)).first
    if not await box.count():
        return None, None
    return _parse_limits(await box.inner_text())


def _numbers(text: str) -> list[float]:
    return [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]


def _signed_number(text: str) -> float:
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", text)
    return float(match.group().replace(",", "")) if match else 0.0
