"""Saves pages of the betting site, so the bot's clicks can be built and checked against them.

    python -m tools.snapshot_page          # guided: the pages the bot needs, one by one
    python -m tools.snapshot_page --free   # save whatever page is open, as often as you like

A Chrome window opens with the bot's own profile, so logging in here also logs the bot in. On sky-fair
your username and password are filled in for you; you type the Validation code and press Login.
Each page's HTML + screenshot is saved to data/snapshots/. Nothing else is clicked for you.
"""
import argparse
import asyncio
import re
from datetime import datetime

from playwright.async_api import async_playwright

from tradebot import skyfair
from tradebot.config import DATA_DIR, load_settings
from tradebot.exchange_bot import launch_browser

SNAPSHOT_DIR = DATA_DIR / "snapshots"

GUIDE = [
    ("tennis-list", "Open the list of tennis matches (click 'Tennis' in the menu)"),
    ("match-page", "Click a live tennis match so its MATCH ODDS show (blue back / pink lay boxes)"),
    ("bet-slip", "Click one BLUE odds box so the bet slip opens. Do NOT press Place Bet"),
    ("open-bets", "Open the page or panel listing your bets (open / matched / unmatched)"),
]


async def say(text: str) -> None:
    print(re.sub(r"[*_]", "", text))


async def save(page, name: str) -> None:
    stem = SNAPSHOT_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{name}"
    stem.with_suffix(".html").write_text(await page.content(), encoding="utf-8")
    await page.screenshot(path=str(stem.with_suffix(".png")), full_page=True)
    print(f"Saved {stem.name}.html / .png  ({page.url})")


async def main(free: bool) -> None:
    settings = load_settings()
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    site = "skyfair" if skyfair.is_skyfair(settings.site_url) else "site"
    async with async_playwright() as pw:
        ctx = await launch_browser(pw, settings, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(settings.site_url)
        await page.wait_for_timeout(3000)

        if site == "skyfair":
            ok = await skyfair.assisted_login(page, settings.site_username, settings.site_password, say)
            print("Logged in." if ok else "Still logged out; the pages below will be the logged-out versions.")

        for number, (name, instruction) in enumerate([] if free else GUIDE, 1):
            answer = await asyncio.to_thread(
                input, f"\nStep {number}/{len(GUIDE)}: {instruction}.\nThen press Enter (s = skip, q = quit): ")
            if answer.strip().lower() == "q":
                break
            if answer.strip().lower() != "s":
                await save(page, f"{site}-{name}")
        else:
            while True:
                answer = await asyncio.to_thread(input, "\nPress Enter to save the current page (q = quit): ")
                if answer.strip().lower() == "q":
                    break
                await save(page, f"{site}-page")
        await ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--free", action="store_true", help="save any page, no guided steps")
    asyncio.run(main(ap.parse_args().free))
