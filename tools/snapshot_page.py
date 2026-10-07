"""Opens the betting site so you can save a copy of a page for setting up the bot's selectors.

    python -m tools.snapshot_page

A Chrome window opens (BROWSER_CHANNEL in .env; same profile the bot uses, so logging in here
also logs the bot in).
Go to a page, press Enter in this terminal, and its HTML + screenshot are saved to
data/snapshots/. Useful pages: the Tennis and Cricket lists, and one match page with
Match Odds (and for cricket Bookmaker + Fancy) visible, ideally with a bet slip open.
Type q + Enter to finish. Nothing is clicked for you.
"""
import asyncio
from datetime import datetime

from playwright.async_api import async_playwright

from tradebot.config import DATA_DIR, load_settings
from tradebot.exchange_bot import launch_browser

SNAPSHOT_DIR = DATA_DIR / "snapshots"


async def main() -> None:
    settings = load_settings()
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        ctx = await launch_browser(pw, settings, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(settings.site_url)
        while True:
            answer = await asyncio.to_thread(input, "\nPress Enter to save the current page (q + Enter to quit): ")
            if answer.strip().lower() == "q":
                break
            stem = SNAPSHOT_DIR / f"{datetime.now():%Y%m%d-%H%M%S}"
            stem.with_suffix(".html").write_text(await page.content(), encoding="utf-8")
            await page.screenshot(path=str(stem.with_suffix(".png")), full_page=True)
            print(f"Saved {stem}.html / .png  ({page.url})")
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
