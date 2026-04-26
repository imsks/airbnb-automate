"""Single place to open Chrome/Chromium for Airbnb: search, login, and outreach.

Supports:
- **Persistent profile** (``BROWSER_USER_DATA_DIR``) — same folder for every run
- **Optional CDP** (``CHROME_CDP_URL``) — attach to *your* Chrome with remote debugging
- **Cookie backup** — ``data/browser_state.json`` via :func:`save_storage_state`

The scraper must use this module; otherwise search runs in a fresh Chromium with no login.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional, Tuple

from playwright.async_api import Browser, BrowserContext, Page, Playwright

from app.config import (
    get_browser_state_path,
    get_browser_user_agent,
    get_browser_user_data_dir,
    get_chrome_cdp_url,
    get_playwright_channel,
)

logger = logging.getLogger(__name__)

_CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
]
_IGNORE_DEFAULT_AUTOMATION = "--enable-automation"


def _viewport() -> dict:
    return {"width": 1920, "height": 1080}


def _context_base_options() -> dict:
    opts: dict = {"viewport": _viewport()}
    ua = get_browser_user_agent()
    if ua:
        opts["user_agent"] = ua
    return opts


def _launch_basics(*, headless: bool) -> dict:
    """Launch options without ``ignore_default_args`` (caller adds once)."""
    kwargs: dict = {
        "headless": headless,
        "args": list(_CHROMIUM_ARGS),
    }
    channel = get_playwright_channel()
    if channel:
        kwargs["channel"] = channel
        logger.info("Using Playwright channel: %s", channel)
    return kwargs


async def _merge_cookies_from_state_file(context: BrowserContext) -> None:
    """Optional: add cookies from a previous :func:`save_storage_state` export."""
    state_path = Path(get_browser_state_path())
    if not state_path.is_file():
        return
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        cookies = data.get("cookies") or []
        if not cookies:
            return
        await context.add_cookies(cookies)
        logger.info("Merged %d cookies from browser_state.json", len(cookies))
    except Exception as e:  # pragma: no cover
        logger.debug("Optional cookie merge from browser_state.json skipped: %s", e)


async def save_storage_state(context: BrowserContext) -> None:
    """Persist cookies/localStorage to ``browser_state.json``."""
    path = get_browser_state_path()
    try:
        await context.storage_state(path=path)
        logger.info("Saved session backup to %s", path)
    except Exception as e:  # pragma: no cover
        logger.warning("Could not save browser_state.json: %s", e)


async def open_airbnb_browser(
    pw: Playwright,
    *,
    headless: bool = False,
) -> Tuple[BrowserContext, Page, Optional[Browser], bool]:
    """Open or attach a browser for Airbnb.

    Returns: ``context``, ``page``, ``browser`` (if any; None for persistent),
    ``uses_cdp``. When ``uses_cdp`` is True, :func:`close_airbnb_session` only
    disconnects Playwright; your Chrome window keeps running.
    """
    cdp_url = get_chrome_cdp_url()
    if cdp_url:
        logger.info("Connecting to existing Chrome via CDP: %s", cdp_url)
        browser = await pw.chromium.connect_over_cdp(cdp_url)
        contexts = browser.contexts
        if not contexts:
            context = await browser.new_context(**_context_base_options())
        else:
            context = contexts[0]
        if context.pages:
            page = context.pages[0]
        else:
            page = await context.new_page()
        return context, page, browser, True

    user_data_dir = get_browser_user_data_dir()
    base = _context_base_options()
    launch = _launch_basics(headless=headless)

    if user_data_dir:
        Path(user_data_dir).mkdir(parents=True, exist_ok=True)
        context = await pw.chromium.launch_persistent_context(
            user_data_dir,
            ignore_default_args=[_IGNORE_DEFAULT_AUTOMATION],
            **{**base, **launch},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await _merge_cookies_from_state_file(context)
        logger.info("Using persistent browser profile at %s", user_data_dir)
        return context, page, None, False

    browser = await pw.chromium.launch(
        ignore_default_args=[_IGNORE_DEFAULT_AUTOMATION],
        **launch,
    )
    ctx_kwargs = {**base}
    p = Path(get_browser_state_path())
    if p.is_file():
        ctx_kwargs["storage_state"] = str(p)
    context = await browser.new_context(**ctx_kwargs)
    page = await context.new_page()
    return context, page, browser, False


async def close_airbnb_session(
    context: BrowserContext,
    browser: Optional[Browser],
    *,
    uses_cdp: bool = False,
) -> None:
    if uses_cdp and browser is not None:
        # Disconnects Playwright; leaves the user's Chrome process running
        try:
            await browser.close()
        except Exception:  # pragma: no cover
            pass
        return
    try:
        await context.close()
    except Exception:  # pragma: no cover
        pass
    if browser is not None:
        try:
            await browser.close()
        except Exception:  # pragma: no cover
            pass


async def flush_profile_after_login(context: BrowserContext) -> None:
    """Write ``browser_state.json`` and pause so the profile can flush to disk."""
    await save_storage_state(context)
    await asyncio.sleep(1.5)
