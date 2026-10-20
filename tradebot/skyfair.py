"""sky-fair.website: what is known from its saved home page (Oct 2026).

Its login has a Validation code shown as an image (img.verify-code) and sends bot-detection data, so a
person types that code. The bot fills the username and password, then waits for the person to finish.
The betting steps (match list, match page, bet slip) need logged-in pages saved with
`python -m tools.snapshot_page` before they can be built.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from playwright.async_api import Page

log = logging.getLogger(__name__)

# Header login form (shown only while logged out)
LOGIN_USERNAME = "#username"
LOGIN_PASSWORD = "#password"
LOGIN_CODE = "#verifyCode"  # the person types the 4 digits shown in img.verify-code next to it
LOGIN_BUTTON = "button.button-header-login"
LOGGED_OUT = "#verifyCode"
LOGIN_WAIT_MIN = 10


def is_skyfair(site_url: str) -> bool:
    return "sky-fair" in site_url.lower()


async def logged_in(page: Page) -> bool:
    return not await page.locator(LOGGED_OUT).filter(visible=True).count()


async def assisted_login(page: Page, username: str, password: str,
                         notify: Callable[[str], Awaitable[None]]) -> bool:
    """Fills username and password and waits up to LOGIN_WAIT_MIN minutes for a person to type the
    verification code and press Login. Returns True once logged in."""
    if await logged_in(page):
        return True
    await page.locator(LOGIN_USERNAME).fill(username)
    await page.locator(LOGIN_PASSWORD).fill(password)
    await page.locator(LOGIN_CODE).click()  # cursor ready for the code
    await notify("🔐 *sky-fair login needed*: in the bot's Chrome window, type the 4-digit Validation "
                 f"code and press Login (waiting {LOGIN_WAIT_MIN} min).")
    for _ in range(LOGIN_WAIT_MIN * 30):
        if await logged_in(page):
            log.info("Logged in to sky-fair")
            return True
        await page.wait_for_timeout(2000)
    return False
