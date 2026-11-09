"""Runs the paper algos on every in-play 20-over match that both iob7 (logged out) and Cricbuzz list.

    .venv\\Scripts\\python.exe -m algo_paper.live            runs until stopped (Ctrl+C)
    .venv\\Scripts\\python.exe -m algo_paper.live --once     one pass, to check the reading

Reads only: iob7's public match pages in a fresh headless browser (no login, so no bet can be placed) and
Cricbuzz's scorecards. Writes algo_paper/data/algo.db and live.log. Nothing is sent anywhere.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import async_playwright

from tradebot.fancy import Cricbuzz, Match
from tradebot.names import name_score

from .alerts import Alerts
from .model import balls_left_in_match, blended, logit, p_bat_first
from .paper import JUMP, LadderAlgo, Ledger, MatchView, Price, ValueAlgo

SITE = "https://www.iob7.com"
DATA = Path(__file__).resolve().parent / "data"
EVENT_ROW = 'div[class*="cursor-pointer md:flex"]:has(span.max-line-1)'
EVENT_NAME = "span.max-line-1"
READ_S = 20
SCORE_S = 30
LIST_S = 180
RELOAD_S = 900
LIVE = {"in progress", "innings break", "toss", "delay", "rain", "drink", "strategic timeout"}
STOP_WORDS = ("BOOKMAKER", "FANCY", "PREMIUM", "LIVE STREAMING", "PLACE BET", "OPEN BETS", "TIED MATCH",
              "COMPLETED MATCH", "TOSS", "WHO WILL")
SUSPENDED = {"SUSPENDED", "BALL RUNNING", "BALL START", "CLOSED"}

log = logging.getLogger("algo")


def _plain(name: str) -> str:
    words = re.sub(r"[^a-z ]", " ", name.lower()).split()
    return " ".join(w for w in words if w not in {"w", "women", "womens", "wmn", "fc", "cc", "xi"})


def same_side(a: str, b: str) -> bool:
    """'Kathmandu Gurkhas' (iob7) is Cricbuzz's 'Kathmandu Gorkhas'; 'Hobart Hurricanes W' its '... Women'."""
    pa, pb = _plain(a), _plain(b)
    if not pa or not pb:
        return False
    return pa == pb or pa in pb or pb in pa or name_score(pa, pb) >= 85


def _number(token: str) -> float | None:
    t = token.replace(",", "").strip()
    scale = 1.0
    if t[-1:] in "Kk":
        scale, t = 1e3, t[:-1]
    elif t[-1:] in "Mm":
        scale, t = 1e6, t[:-1]
    try:
        return float(t) * scale
    except ValueError:
        return None


def read_section(lines: list[str], starts, runners: list[str], rates: bool) -> dict[str, Price]:
    """Best back / lay of each runner in a market block of the match page's text: the runner's name, its book (P/L),
    then price and size of 3 backs and 3 lays. Bookmaker rates are paise over 1 (45 = 1.45, 213 = 3.13)."""
    begin = next((i for i, line in enumerate(lines) if starts(line.strip().upper())), None)
    if begin is None:
        return {}
    found: dict[str, dict] = {}
    current = None
    for line in lines[begin + 1:]:
        text = line.strip()
        upper = text.upper()
        if any(upper.startswith(w) for w in STOP_WORDS) or upper == "MATCH ODDS":
            break
        name = next((r for r in runners if text.lower() == r.lower()), None)
        if name:
            current = name
            found[name] = {"numbers": [], "suspended": False}
            continue
        if current is None:
            continue
        if upper in SUSPENDED:
            found[current]["suspended"] = True
            continue
        if text == "-":  # an empty box (bookmaker: no price on that level)
            found[current]["numbers"].append(None)
            continue
        value = _number(text)
        if value is not None:
            found[current]["numbers"].append(value)
    prices = {}
    for name, item in found.items():
        numbers = item["numbers"][1:]  # the first is the book
        if len(numbers) != 12:
            continue
        back, lay = numbers[4], numbers[6]
        if rates:
            back, lay = (1 + back / 100 if back else None), (1 + lay / 100 if lay else None)
        prices[name] = Price(back or None, lay or None, item["suspended"])
    return prices


def score_state(match: Match) -> tuple[int, int, int, int, int | None, str] | None:
    """(innings, runs, wickets, balls, target, short name batting first) from the scorecard, or None."""
    if 1 not in match.innings or match.innings[1].total is None:
        return None
    first = match.innings[1]
    if 2 in match.innings and match.innings[2].total is not None:
        runs, wkts, overs = match.innings[2].total
        number = 2
    elif match.state.lower() == "innings break" or first.total[1] >= 10 or first.total[2] >= 20:
        runs, wkts, overs, number = 0, 0, 0.0, 2
    else:
        runs, wkts, overs = first.total
        number = 1
    balls = int(overs) * 6 + round((overs - int(overs)) * 10)
    target = first.total[0] + 1 if number == 2 else None
    return number, int(runs), int(wkts), balls, target, first.team


def gender_of(match: Match) -> str:
    text = " ".join(n for n, _ in match.teams) + " " + match.series
    return "female" if re.search(r"\bwomen", text, re.IGNORECASE) else "male"


def winner_of(match: Match, runners: list[str]) -> tuple[bool, str | None]:
    """(finished, the winning runner or None for a tie / no result)."""
    status = match.status or ""
    if match.state.lower() not in ("complete", "abandon", "no result") and " won " not in f" {status} ":
        return False, None
    if "tied" in status.lower():  # Match Odds settle on the super over
        m = re.search(r"\(([^()]+?) won the super over", status, re.IGNORECASE)
    else:
        m = re.match(r"(.+?) won\b", status)
    if not m:
        return True, None
    team = m.group(1).strip()
    full = [n for n, s in match.teams if team in (n, s)] or [team]
    return True, next((r for r in runners if same_side(r, full[0])), None)


@dataclass
class Tracked:
    event: str
    runners: list[str]
    match: Match
    tab: object
    gender: str
    offset: float | None = None
    left_at_anchor: int = 0
    last_score: float = 0.0
    loaded: float = field(default_factory=time.time)
    history: list = field(default_factory=list)  # (time, market chance of the side batting first)
    no_trade: str = ""
    done: bool = False


class Runner:
    def __init__(self, once: bool) -> None:
        self.once = once
        self.ledger = Ledger(DATA / "algo.db")
        self.alerts = Alerts()
        notify = None if once else self.alerts.add
        self.algos = [ValueAlgo("value-mo", self.ledger, log, notify), ValueAlgo("value-bm", self.ledger, log, notify),
                      LadderAlgo("ladder-mo", self.ledger, log, notify)]
        log.info("Telegram alerts %s", "on" if self.alerts.on else "off (no ALGO_BOT_TOKEN / ALGO_CHAT_ID in .env)")
        if notify:
            notify("📐 Algo paper test running (no login, no real money). Alerts: entries, bookings, results.")
        self.cricbuzz = Cricbuzz()
        self.tracked: dict[str, Tracked] = {}
        self.urls: dict[str, str] = {}

    async def run(self) -> None:
        async with async_playwright() as pw:
            args = ["--disable-dev-shm-usage", "--disable-gpu", "--renderer-process-limit=2"]
            try:
                browser = await pw.chromium.launch(headless=True, args=args)
            except Exception:  # no bundled Chromium (this PC): the installed Chrome
                browser = await pw.chromium.launch(headless=True, channel="chrome", args=args)
            ctx = await browser.new_context(viewport={"width": 1400, "height": 1000})  # fresh: logged out
            # prices are text: no pictures, video or fonts, to keep memory low next to the live bot
            await ctx.route("**/*", lambda route: route.abort() if route.request.resource_type in
                            ("image", "media", "font") else route.continue_())
            self.list_page = await ctx.new_page()
            self.ctx = ctx
            last_list = 0.0
            while True:
                if time.time() - last_list > LIST_S or self.once:
                    try:
                        await self.refresh()
                    except Exception:
                        log.exception("match list")
                    last_list = time.time()
                    if self.once:
                        await asyncio.sleep(10)  # let the new tabs show their prices
                for tracked in list(self.tracked.values()):
                    try:
                        await self.read(tracked)
                    except Exception:
                        log.exception("read %s", tracked.event)
                if self.alerts.queue:
                    await asyncio.to_thread(self.alerts.flush)
                if self.once:
                    break
                await asyncio.sleep(READ_S)
            await browser.close()

    async def refresh(self) -> None:
        matches = await self.cricbuzz.refresh_list()
        soon = datetime.now() + timedelta(minutes=20)
        live = [m for m in matches if m.fmt.upper() in ("T20", "T20I")
                and not re.search(r"T10|sixes|hundred|\b100\b", m.series, re.IGNORECASE)  # Cricbuzz calls T10 'T20'
                and (m.state.lower() in LIVE or (m.state.lower() == "preview" and m.start <= soon))]
        wanted = [m for m in live if not any(t.match.match_id == m.match_id for t in self.tracked.values())]
        if not wanted:
            log.info("tracking %d match(es); no new live T20", len(self.tracked))
            return
        names = await self._home_names()
        for match in wanted:
            sides = [n for n, _ in match.teams]
            event = next((n for n in names if " v " in n and all(any(same_side(half, s) for s in sides)
                                                                   for half in n.split(" v "))), None)
            if not event:
                log.info("no iob7 market for %s (%s)", " v ".join(sides), match.series)
                continue
            url = self.urls.get(event) or await self._url_of(event)
            if not url:
                continue
            tab = await self.ctx.new_page()
            await tab.goto(url, wait_until="domcontentloaded", timeout=60000)
            runners = [half.strip() for half in event.split(" v ")]
            self.tracked[event] = Tracked(event, runners, match, tab, gender_of(match))
            log.info("TRACKING %s (%s, %s) %s", event, match.series, gender_of(match), url)

    async def _home_names(self) -> list[str]:
        """The events on the home page, once its list has filled in (it loads in parts)."""
        page = self.list_page
        await page.goto(SITE + "/", wait_until="domcontentloaded", timeout=60000)
        await page.locator(EVENT_ROW).first.wait_for(timeout=30000)
        names: list[str] = []
        for _ in range(3):
            await page.wait_for_timeout(3000)
            now_listed = await page.locator(EVENT_ROW).locator(EVENT_NAME).all_inner_texts()
            if len(now_listed) <= len(names):
                break
            names = now_listed
        return names

    async def _url_of(self, event: str) -> str | None:
        page = self.list_page
        rows = page.locator(EVENT_ROW)
        names = await rows.locator(EVENT_NAME).all_inner_texts()
        if event not in names:
            return None
        await rows.nth(names.index(event)).click()
        await page.wait_for_timeout(3000)
        url = page.url
        await self._home_names()
        if "/sports/details/" not in url:
            return None
        self.urls[event] = url
        return url

    async def read(self, t: Tracked) -> None:
        if time.time() - t.last_score > SCORE_S:
            await self.cricbuzz.refresh_scorecard(t.match)
            t.last_score = time.time()
            if re.search(r"\bDLS\b|D/L|reduced|\d+ overs? (?:per side|match)", t.match.status or "", re.IGNORECASE):
                t.no_trade = t.no_trade or f"rain rules: {t.match.status}"
        finished, winner = winner_of(t.match, t.runners)
        if finished:
            for algo in self.algos:
                algo.settle(t.event, winner)
            log.info("FINISHED %s: %s", t.event, t.match.status)
            today = self.ledger.totals_since(datetime.now().strftime("%Y-%m-%dT00:00"))
            lines = "\n".join(f"{name}: {n} trades, {pl:+.2f}" for name, (n, pl) in sorted(today.items())) or "no trades"
            if not self.once:
                self.alerts.add(f"🏁 ALGO match over: {t.event}\n{t.match.status}\nToday (paper):\n{lines}")
            await t.tab.close()
            self.tracked.pop(t.event, None)
            return
        if time.time() - t.loaded > RELOAD_S:
            await t.tab.reload(wait_until="domcontentloaded")
            t.loaded = time.time()
            await t.tab.wait_for_timeout(4000)
        text = await t.tab.inner_text("body")
        lines = text.splitlines()
        mo = read_section(lines, lambda u: u == "MATCH ODDS", t.runners, rates=False)
        bm = read_section(lines, lambda u: u.startswith("BOOKMAKER"), t.runners, rates=True)
        if mo or bm:
            self.ledger.snapshot(t.event, mo, bm)
        state = score_state(t.match)
        if not state or len(mo) < 2:
            if self.once:
                log.info("%s: score %s, match odds %s, bookmaker %s", t.event, state, mo, bm)
            return
        innings, runs, wkts, balls, target, short = state
        bat_first = next((r for r in t.runners if any(same_side(r, n) for n in t.match.side_names(short))), None)
        if bat_first is None:
            return
        other = next(r for r in t.runners if r != bat_first)
        p_model = p_bat_first(t.gender, innings, runs, balls, wkts, target)
        a, b = mo[bat_first], mo[other]
        p_market = None
        if a.ok() and b.ok():
            ia, ib = 2 / (a.back + a.lay), 2 / (b.back + b.lay)
            p_market = ia / (ia + ib)
        left = balls_left_in_match(innings, balls)
        if t.offset is None and p_market is not None and 0.0 < p_model < 1.0:
            t.offset, t.left_at_anchor = logit(p_market) - logit(p_model), max(left, 1)
            log.info("ANCHOR %s: %s (batted first) market %.2f, score model %.2f (innings %d: %d/%d after %d balls%s)",
                     t.event, bat_first, p_market, p_model, innings, runs, wkts, balls,
                     f", target {target}" if target else "")
        if t.offset is None:
            return
        p = blended(p_model, t.offset, left, t.left_at_anchor)
        self.ledger.state(t.event, t.match.match_id, (innings, runs, wkts, balls, target), bat_first, p_model, p,
                          p_market)
        now_s = time.time()
        if p_market is not None:
            t.history.append((now_s, p_market))
        t.history = [(s, q) for s, q in t.history if now_s - s <= 70]
        jumped = bool(t.history) and max(q for _, q in t.history) - min(q for _, q in t.history) >= JUMP
        if t.no_trade:
            jumped = True  # rain rules: no new trades; open ones are still managed
        chances = {bat_first: p, other: 1 - p}
        overs = f"{balls // 6}.{balls % 6} ov"
        context = (f"{bat_first} {runs}/{wkts} ({overs})" if innings == 1 else
                   f"{other} {runs}/{wkts} ({overs}), need {target - runs} off {120 - balls}")
        for algo in self.algos:
            prices = bm if algo.name.endswith("-bm") else mo
            if len(prices) < 2:
                continue
            algo.on_read(MatchView(t.event, bat_first, other, chances, prices, jumped, context))
        if self.once:
            log.info("%s: innings %d %d/%d (%d balls%s), %s batted first: model %.2f blended %.2f market %s | MO %s | BM %s",
                     t.event, innings, runs, wkts, balls, f", target {target}" if target else "", bat_first, p_model, p,
                     f"{p_market:.2f}" if p_market else "-", mo, bm)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(DATA / "live.log", encoding="utf-8"), logging.StreamHandler()])
    log.info("paper algo start (no login, no bets)%s", " - one pass" if args.once else "")
    asyncio.run(Runner(args.once).run())


if __name__ == "__main__":
    main()
