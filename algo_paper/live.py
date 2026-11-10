"""Runs the paper algos on every in-play 20-over match that both iob7 (logged out) and Cricbuzz list.

    .venv\\Scripts\\python.exe -m algo_paper.live            runs until stopped (Ctrl+C)
    .venv\\Scripts\\python.exe -m algo_paper.live --once     one pass, to check the reading

Reads only: iob7's public match pages in a fresh headless browser (no login, so no bet can be placed) and
Cricbuzz's scorecards. Writes algo_paper/data/algo.db and live.log. Nothing is sent anywhere.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
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
# Memory, next to the live bot on a 1 GB server. 10 Nov: the algo's Chrome grew past its 220 MB cap during the WBBL game
# and was killed, and at 15:22 the guard stopped the algo with 59 MB left on the server.
RELOAD_S = 300  # a match tab is loaded fresh this often: a live page's memory only grows
RECYCLE_S = 3600  # the whole browser is started again this often,
RECYCLE_MB = 190  # or when its anon memory is this much (MemoryMax 220M; 119 MB idle on 10 Nov, + 47 MB file cache), at most every
RECYCLE_GAP_S = 600  # this often
NO_MARKET_S = 900  # a live match iob7 doesn't list is looked for again after this: each look loads the heavy home page
ARGS = ["--disable-dev-shm-usage", "--disable-gpu", "--renderer-process-limit=2", "--disable-extensions",
        "--disable-background-networking"]
LIVE = {"in progress", "innings break", "toss", "delay", "rain", "drink", "strategic timeout"}
STOP_WORDS = ("BOOKMAKER", "FANCY", "PREMIUM", "LIVE STREAMING", "PLACE BET", "OPEN BETS", "TIED MATCH",
              "COMPLETED MATCH", "TOSS", "WHO WILL")
SUSPENDED = {"SUSPENDED", "BALL RUNNING", "BALL START", "CLOSED"}
SUMMARY_AT = (23, 30)  # the daily Telegram summary, local time
TEST_START = "2026-11-09T19:00"  # the week's test began 9 Nov 19:45

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


def _cgroup_mb() -> int | None:
    """The anon memory this service uses now (its systemd cgroup, on the server), None elsewhere. File cache is left out:
    the kernel takes it back by itself, and it made an idle algo look like 178 MB."""
    try:
        path = Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1]
        stat = Path(f"/sys/fs/cgroup{path}/memory.stat").read_text()
        return int(re.search(r"^anon (\d+)$", stat, re.MULTILINE)[1]) // 2 ** 20
    except (OSError, IndexError, ValueError, TypeError):
        return None


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
        self.no_market: dict[int, float] = {}  # Cricbuzz match id -> when iob7 was last found not to list it
        self.pw = self.browser = self.ctx = self.list_page = None
        self.launched = 0.0
        now_dt = datetime.now()
        # started after today's summary time: today's summary went out already (or the day began before the test)
        self.summary_day = now_dt.date() if (now_dt.hour, now_dt.minute) >= SUMMARY_AT else None

    def daily_text(self, day: str) -> str:
        """The day's summary for Telegram: each algo's closed trades, and the test so far."""
        stats, week = self.ledger.day_stats(day), self.ledger.totals_since(TEST_START)
        lines = [f"📊 ALGO DAILY · {datetime.strptime(day, '%Y-%m-%d'):%d %b} (paper, no real money)",
                 f"Matches read: {self.ledger.matches_on(day)}"]
        for algo in ("value-mo", "value-bm", "ladder-mo"):
            n, up, down, pl = stats.get(algo, [0, 0, 0, 0.0])
            lines.append(f"{algo}: {n} trades ({up} up, {down} down) {pl:+.2f}")
        total = sum(s[3] for s in stats.values())
        week_n, week_pl = sum(n for n, _ in week.values()), sum(pl for _, pl in week.values())
        lines += [f"Today: {total:+.2f}", f"Test so far (from 9 Nov): {week_n} trades, {week_pl:+.2f}",
                  f"Open now: {self.ledger.open_trades()}"]
        return "\n".join(lines)

    async def _launch(self) -> None:
        try:
            self.browser = await self.pw.chromium.launch(headless=True, args=ARGS)
        except Exception:  # no bundled Chromium (this PC): the installed Chrome
            self.browser = await self.pw.chromium.launch(headless=True, channel="chrome", args=ARGS)
        self.ctx = await self.browser.new_context(viewport={"width": 1400, "height": 1000})  # fresh: logged out
        # prices are text: no pictures, video or fonts, to keep memory low next to the live bot
        await self.ctx.route("**/*", lambda route: route.abort() if route.request.resource_type in
                             ("image", "media", "font") else route.continue_())
        self.list_page = await self.ctx.new_page()
        self.launched = time.time()

    def _recycle_why(self) -> str:
        """Why the browser should be started again now ('' = it shouldn't)."""
        if not self.browser.is_connected():
            return "the browser is gone"
        age = time.time() - self.launched
        if age > RECYCLE_S:
            return "hourly"
        used = _cgroup_mb()
        if used is not None and used > RECYCLE_MB and age > RECYCLE_GAP_S:
            return f"the service uses {used} MB"
        return ""

    async def _relaunch(self, why: str) -> None:
        """A new browser, and each tracked match's page opened in it again: the algos and their trades carry on."""
        log.info("browser started again: %s", why)
        with contextlib.suppress(Exception):
            await self.browser.close()
        await self._launch()
        for tracked in list(self.tracked.values()):
            await self._open_tab(tracked)

    async def _open_tab(self, t: Tracked) -> None:
        try:
            t.tab = await self.ctx.new_page()
            await t.tab.goto(self.urls[t.event], wait_until="domcontentloaded", timeout=60000)
            t.loaded = time.time()
        except Exception:
            log.exception("could not open %s again", t.event)

    async def _reopen(self, t: Tracked) -> None:
        """A page read failed (a hung or crashed tab): a new tab, or a new browser if it is gone."""
        if not self.browser.is_connected():
            return await self._relaunch("the browser is gone")
        with contextlib.suppress(Exception):
            await t.tab.close()
        await self._open_tab(t)

    async def run(self) -> None:
        async with async_playwright() as pw:
            self.pw = pw
            await self._launch()
            last_list = 0.0
            while True:
                why = "" if self.once else self._recycle_why()
                if why:
                    await self._relaunch(why)
                if time.time() - last_list > LIST_S or self.once:
                    try:
                        await self.refresh()
                    except Exception:
                        log.exception("match list")
                    with contextlib.suppress(Exception):  # the home page's live odds are heavy: not kept open
                        await self.list_page.goto("about:blank")
                    last_list = time.time()
                    if self.once:
                        await asyncio.sleep(10)  # let the new tabs show their prices
                for tracked in list(self.tracked.values()):
                    try:
                        await self.read(tracked)
                    except PlaywrightError:
                        log.exception("read %s", tracked.event)
                        await self._reopen(tracked)
                    except Exception:
                        log.exception("read %s", tracked.event)
                now_dt = datetime.now()
                if (not self.once and (now_dt.hour, now_dt.minute) >= SUMMARY_AT
                        and self.summary_day != now_dt.date()):
                    self.summary_day = now_dt.date()
                    self.alerts.add(self.daily_text(now_dt.strftime("%Y-%m-%d")))
                if self.alerts.queue:
                    await asyncio.to_thread(self.alerts.flush)
                if self.once:
                    break
                await asyncio.sleep(READ_S)
            await self.browser.close()

    async def _settle_left(self) -> None:
        """Open paper trades of a match no longer tracked (10 Nov: the algo was stopped during the WBBL game) are settled
        from the match's Cricbuzz scorecard once it is over."""
        for ev in {ev for algo in self.algos for ev in algo.open} - set(self.tracked):
            try:
                cb_id = self.ledger.cb_id_of(ev)
                match = await self.cricbuzz.match_by_id(cb_id) if cb_id else None
                if match is None:
                    continue
                finished, winner = winner_of(match, [half.strip() for half in ev.split(" v ")])
                if finished:
                    for algo in self.algos:
                        algo.settle(ev, winner)
                    log.info("SETTLED %s, not tracked any more: %s", ev, match.status)
            except Exception:
                log.exception("settle %s", ev)

    async def refresh(self) -> None:
        matches = await self.cricbuzz.refresh_list()
        await self._settle_left()
        soon = datetime.now() + timedelta(minutes=20)
        live = [m for m in matches if m.fmt.upper() in ("T20", "T20I")
                and not re.search(r"T10|sixes|hundred|\b100\b", m.series, re.IGNORECASE)  # Cricbuzz calls T10 'T20'
                and (m.state.lower() in LIVE or (m.state.lower() == "preview" and m.start <= soon))]
        wanted = [m for m in live if not any(t.match.match_id == m.match_id for t in self.tracked.values())
                  and time.time() - self.no_market.get(m.match_id, 0.0) > NO_MARKET_S]
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
                self.no_market[match.match_id] = time.time()
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
