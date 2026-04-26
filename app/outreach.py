"""Airbnb host outreach automation using Playwright.

Flow:
1. Session lives in a persistent user-data directory (or CDP) — Chrome signed in to Google is
   not the same as an Airbnb account.
2. :func:`wait_for_airbnb_session_ready` blocks until a real Airbnb sign-in (DOM + cookies
   and a /trips check), including sign up; outreach uses the same wait so messaging never runs
   as a guest.
3. For each listing, open the page, "Contact Host", type the message, and send; track status
   in the database.
"""

import asyncio
import logging
import re
from typing import Optional

from playwright.async_api import (
    async_playwright,
    BrowserContext,
    Locator,
    Page,
)

from app.browser_session import (
    close_airbnb_session,
    flush_profile_after_login,
    open_airbnb_browser,
    save_storage_state,
)
from app.config import get_outreach_message_template
from app.database import (
    create_outreach_messages,
    get_listings,
    get_outreach_messages,
    update_outreach_status,
)
from app.models import Listing, OutreachMessage, OutreachStatus
from app.outreach_quota import (
    record_successful_send,
    sleep_between_outreach_attempts,
    wait_until_send_allowed,
)

logger = logging.getLogger(__name__)


class AirbnbHostQuotaUIError(Exception):
    """Airbnb surfaced an in-app host messaging cap — stop and try again later."""


# Copy varies slightly by locale; match substrings seen in English UI.
_AIRBNB_HOST_QUOTA_MARKERS = (
    "already messaged several hosts",
    "wait a few hours before you can send",
    "you'll need to wait a few hours",
)

AIRBNB_BASE_URL = "https://www.airbnb.com"
LOGIN_URL = f"{AIRBNB_BASE_URL}/login"

# Named constants for timeouts and delays
LOGIN_CHECK_INTERVAL_MS = 5000
LOGIN_MAX_CHECKS = 60  # 60 checks × 5s = 5 minutes max wait
MESSAGE_DELAY_MS = 2000  # short pause inside single-message flow only


def _is_target_disconnected_error(exc: BaseException) -> bool:
    """True if Playwright lost the page/browser (user closed, crash, or profile lock)."""
    text = f"{type(exc).__name__} {exc}".lower()
    if "target" in text and "closed" in text:
        return True
    if "context" in text and "closed" in text:
        return True
    if "browser" in text and "closed" in text:
        return True
    if "econnrefused" in text or "epipe" in text or "broken pipe" in text:
        return True
    return False


def _first_open_page(context: BrowserContext) -> Optional[Page]:
    try:
        for p in context.pages:
            if not p.is_closed():
                return p
    except Exception:
        pass
    return None


async def _async_sleep_ms(ms: int) -> None:
    """Do not use Page.wait_for_timeout for idle pauses: it throws if the tab was closed."""
    await asyncio.sleep(ms / 1000.0)

_TRIPS_PATH = f"{AIRBNB_BASE_URL}/trips"
_PROFILE_SELECTORS = (
    '[data-testid="cypress-headernav-profile"], '
    'header a[href*="/users/"], '
    'a[href*="/account-settings"], '
    'button[aria-label*="profile"], button[aria-label*="Profile"], '
    'button[aria-label*="Account"], '
    'a[aria-label*="Profile"], a[aria-label*="profile"], '
    'img[data-testid="user-avatar"], '
    'nav [data-testid*="header"] [data-testid*="profile"]'
)


def cookies_indicate_airbnb_session(cookies: list) -> bool:
    """Heuristic: Airbnb session cookies (names vary; use domain + name hints)."""
    for c in cookies:
        dom = (c.get("domain") or "").lstrip(".").lower()
        name = (c.get("name") or "")
        nlow = name.lower()
        if "airbnb" not in dom and "airbnb" not in nlow:
            continue
        if "session" in nlow or nlow in (
            "_aat",
            "aaj",
            "_aaj",
        ):
            return True
    return False


async def _context_airbnb_cookies_suggest_session(context: BrowserContext) -> bool:
    try:
        cookies = await context.cookies("https://www.airbnb.com")
    except Exception:
        try:
            cookies = await context.cookies()
        except Exception:
            return False
    return cookies_indicate_airbnb_session(cookies)


async def _is_logged_in(page: Page) -> bool:
    """Detect logged-in state (DOM + session cookies; Airbnb often hides nav until slow JS)."""
    if page.is_closed():
        return False
    for _ in range(3):
        try:
            if await page.query_selector(_PROFILE_SELECTORS):
                return True
        except Exception as e:  # pragma: no cover
            if _is_target_disconnected_error(e):
                return False
            raise
        await _async_sleep_ms(1000)
    if await _context_airbnb_cookies_suggest_session(page.context):
        return True
    return False


async def _any_page_looks_logged_in(context: BrowserContext) -> bool:
    """Useful when Google/Apple sign-in opened a new tab; session applies to the whole context."""
    for pg in list(context.pages):
        if pg.is_closed():
            continue
        try:
            if await _is_logged_in(pg):
                return True
        except Exception:
            continue
    return await _context_airbnb_cookies_suggest_session(context)


async def _airbnb_trip_url_confirms_session(page: Page) -> bool:
    """A logged-in user can load /trips; guests are sent to /login (or the URL keeps login)."""
    try:
        await page.goto(
            _TRIPS_PATH, wait_until="domcontentloaded", timeout=30000
        )
        await _async_sleep_ms(2000)
        u = (page.url or "").lower()
        if "/login" in u or "/signup" in u or "authenticate" in u:
            return False
        if "trips" in u or (AIRBNB_BASE_URL in u and "login" not in u and "signup" not in u):
            return True
    except Exception as e:  # pragma: no cover
        logger.debug("trips session check: %s", e)
    return False


async def _session_fully_ready(page: Page, context: BrowserContext) -> bool:
    """DOM/cookies and a protected page do not send us back to the login form."""
    if not await _any_page_looks_logged_in(context):
        return False
    if page.is_closed():
        alt = _first_open_page(context)
        if alt is None or alt.is_closed():
            return False
        page = alt
    if not page.url or "about:blank" in page.url:
        try:
            await page.goto(
                AIRBNB_BASE_URL, wait_until="domcontentloaded", timeout=20000
            )
        except Exception:
            pass
    main = page
    for pg in list(context.pages):
        if pg.is_closed():
            continue
        if "airbnb.com" in (pg.url or "") and "login" not in (pg.url or "").lower():
            main = pg
            break
    if await _airbnb_trip_url_confirms_session(main):
        return True
    return False


async def wait_for_airbnb_session_ready(
    page: Page,
    context: BrowserContext,
) -> bool:
    """Block until the user is signed in *to Airbnb* (not only Chrome), then flush profile.

    Polls: home/login DOM, all tabs, cookies, and a ``/trips`` navigation check. Opens ``/login``
    if still a guest, reloads the login page periodically to pick up OAuth, and allows several
    minutes for sign up / sign in.

    Idle pauses use :func:`asyncio.sleep` (not ``Page.wait_for_timeout``) so a closed tab does
    not turn a 5s wait into a crash. If every tab is closed, we try ``context.new_page()`` and
    reopen ``/login`` once.

    There is no supported way to "log in to Airbnb" purely via a server-side API for personal
    accounts; the session must exist in a real browser (cookies + storage).
    """
    work = page
    try:
        try:
            if not work.is_closed():
                await work.bring_to_front()
        except Exception as e:
            if _is_target_disconnected_error(e):
                logger.error(
                    "Browser or tab is already closed. Restart outreach and do not close the "
                    "window until you are signed in to Airbnb."
                )
                return False
            raise
        if await _session_fully_ready(work, context):
            logger.info("Airbnb session is ready (already signed in).")
            for pg in list(context.pages):
                u = (pg.url or "")
                if "airbnb.com" in u and "trips" in u:
                    try:
                        if not pg.is_closed():
                            await pg.goto(
                                AIRBNB_BASE_URL,
                                wait_until="domcontentloaded",
                                timeout=30000,
                            )
                    except Exception:  # pragma: no cover
                        pass
                    break
            else:
                for pg in list(context.pages):
                    if "airbnb.com" in (pg.url or ""):
                        try:
                            if not pg.is_closed():
                                await pg.goto(
                                    AIRBNB_BASE_URL,
                                    wait_until="domcontentloaded",
                                    timeout=30000,
                                )
                        except Exception:  # pragma: no cover
                            pass
                        break
                else:
                    w = _first_open_page(context) or work
                    if not w.is_closed():
                        await w.goto(
                            AIRBNB_BASE_URL,
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
            await flush_profile_after_login(context)
            return True

        logger.info(
            "Not signed in to Airbnb yet. Opening the login page. Complete sign in or sign up in "
            "the window (including in a new tab for Google/Apple if one opens). **Do not close** "
            "this window. Waiting up to %d minutes before continuing.",
            (LOGIN_MAX_CHECKS * LOGIN_CHECK_INTERVAL_MS) // 60_000,
        )
        try:
            w = _first_open_page(context) or work
            if not w.is_closed():
                await w.goto(
                    LOGIN_URL, wait_until="domcontentloaded", timeout=30000
                )
                work = w
        except Exception as e:
            if _is_target_disconnected_error(e):
                logger.error(
                    "Connection to the browser was lost. Use a dedicated user-data directory "
                    "(not the same folder as a running Chrome), or set CHROME_CDP_URL to attach "
                    "to your own Chrome, and do not close the window during login."
                )
                return False
            logger.warning("Could not open login URL: %s", e)

        for attempt in range(LOGIN_MAX_CHECKS):
            try:
                await _async_sleep_ms(LOGIN_CHECK_INTERVAL_MS)
            except asyncio.CancelledError:
                raise

            live = _first_open_page(context)
            if live is not None:
                work = live
            else:
                try:
                    work = await context.new_page()
                    await work.goto(
                        LOGIN_URL, wait_until="domcontentloaded", timeout=30000
                    )
                    logger.info("Opened a new tab for login (no usable tab was left).")
                except Exception as e:
                    if _is_target_disconnected_error(e):
                        logger.error(
                            "Browser was closed. Keep the browser window open until you are "
                            "signed in to Airbnb, and avoid using the same profile in two "
                            "Chromes at once."
                        )
                        return False
                    logger.warning("Could not open a new login tab: %s", e)
                    return False

            for pg in list(context.pages):
                if pg.is_closed():
                    continue
                try:
                    await pg.bring_to_front()
                except Exception:
                    pass
                try:
                    if not await _is_logged_in(pg):
                        continue
                    if await _airbnb_trip_url_confirms_session(pg):
                        try:
                            await pg.goto(
                                AIRBNB_BASE_URL,
                                wait_until="domcontentloaded",
                                timeout=30000,
                            )
                        except Exception:  # pragma: no cover
                            pass
                        logger.info("Airbnb sign-in complete after %d checks.", attempt + 1)
                        await flush_profile_after_login(context)
                        return True
                except Exception as e:
                    if _is_target_disconnected_error(e):
                        logger.error("Browser or tab closed during login check.")
                        return False
                    raise

            w2 = _first_open_page(context)
            if w2 and await _any_page_looks_logged_in(context):
                try:
                    if await _airbnb_trip_url_confirms_session(w2):
                        try:
                            if not w2.is_closed():
                                await w2.goto(
                                    AIRBNB_BASE_URL,
                                    wait_until="domcontentloaded",
                                    timeout=30000,
                                )
                        except Exception:  # pragma: no cover
                            pass
                        logger.info("Airbnb sign-in complete (session + trips check).")
                        await flush_profile_after_login(context)
                        return True
                except Exception as e:
                    if _is_target_disconnected_error(e):
                        return False
                    raise
            # Intentionally no page.reload on /login or /signup: refreshing interrupts multi-step
            # sign up and re-built forms. The browser profile + browser_state.json persist the
            # session after success — no in-app credential storage.
    except Exception as e:
        if _is_target_disconnected_error(e):
            logger.error(
                "Session wait stopped because the browser or tab was closed. Leave the window open "
                "while signing in; do not run two Chromes on the same user-data directory."
            )
            return False
        raise

    logger.error(
        "Timeout: no confirmed Airbnb account session (try signing in on airbnb.com/login)."
    )
    return False


async def _use_airbnb_page_for_outreach(
    page: Page, context: BrowserContext
) -> Page:
    """Prefer a tab on airbnb.com (not the login form) for subsequent navigation."""
    for pg in list(context.pages):
        if pg.is_closed():
            continue
        u = (pg.url or "").lower()
        if "airbnb.com" in u and "/login" not in u and "/signup" not in u:
            try:
                await pg.bring_to_front()
            except Exception:
                pass
            return pg
    w = _first_open_page(context) or page
    if w.is_closed():
        w = await context.new_page()
    try:
        await w.goto(
            AIRBNB_BASE_URL, wait_until="domcontentloaded", timeout=30000
        )
    except Exception:  # pragma: no cover
        pass
    return w


# ---------------------------------------------------------------------------
# Dedicated login flow
# ---------------------------------------------------------------------------


async def login_to_airbnb() -> bool:
    """Open a browser for the user to manually log in to Airbnb.

    The session is persisted in the user-data directory so that subsequent
    outreach runs can reuse it without another login.

    Returns True if the user successfully logged in within the timeout.
    """
    async with async_playwright() as pw:
        context, browser, uses_cdp = None, None, False
        try:
            context, page, browser, uses_cdp = await open_airbnb_browser(
                pw, headless=False
            )
        except Exception as e:
            logger.error("Could not start browser for login: %s", e)
            return False

        try:
            await page.goto(
                AIRBNB_BASE_URL, wait_until="domcontentloaded", timeout=30000
            )
            await _async_sleep_ms(1500)
            return await wait_for_airbnb_session_ready(page, context)
        finally:
            try:
                await save_storage_state(context)
            except Exception:  # pragma: no cover
                pass
            await close_airbnb_session(context, browser, uses_cdp=uses_cdp)


def login_to_airbnb_sync() -> bool:
    """Synchronous wrapper for login_to_airbnb."""
    return asyncio.run(login_to_airbnb())


async def check_airbnb_login_status() -> bool:
    """Check session using the same browser mode as search/outreach (headless for speed)."""
    async with async_playwright() as pw:
        context, page, browser, uses_cdp = None, None, None, False
        try:
            context, page, browser, uses_cdp = await open_airbnb_browser(
                pw, headless=True
            )
            await page.goto(
                AIRBNB_BASE_URL, wait_until="domcontentloaded", timeout=20000
            )
            await _async_sleep_ms(2000)
            return await _is_logged_in(page)
        except Exception as e:
            logger.debug("Login status check failed: %s", e)
            return False
        finally:
            if context is not None:
                await close_airbnb_session(context, browser, uses_cdp=uses_cdp)


def check_airbnb_login_status_sync() -> bool:
    """Synchronous wrapper for check_airbnb_login_status."""
    try:
        return asyncio.run(check_airbnb_login_status())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Outreach messaging
# ---------------------------------------------------------------------------


_CONTACT_CTA_RE = re.compile(
    r"Contact( the)? host|Message( the)? host|^Message$|Check availability|Send a message|Contact( host)?",
    re.IGNORECASE,
)


async def _dismiss_obvious_cookies(p: Page) -> None:
    for sel in (
        'div[role="dialog"] button:has-text("Accept")',
        "button:has-text(\"OK\")",
        "button:has-text(\"Accept all\")",
    ):
        try:
            b = p.locator(sel).first
            if await b.count() > 0 and await b.is_visible():
                await b.click(timeout=2500)
                await _async_sleep_ms(400)
        except Exception:
            pass


async def _try_expand_collapsed_panels(p: Page) -> None:
    for rx in (r"^Show more$", r"^Read more$", r"^View more$", r"^More$"):
        try:
            el = p.get_by_text(re.compile(rx, re.IGNORECASE)).first
            if await el.count() > 0 and await el.is_visible():
                await el.scroll_into_view_if_needed()
                await el.click(timeout=2000, force=True)
                await _async_sleep_ms(500)
        except Exception:
            pass


async def _click_first_sensible(loc: Locator, *, timeout_ms: int = 20_000) -> bool:
    try:
        n = await loc.count()
        for i in range(min(n, 40)):
            el = loc.nth(i)
            try:
                vis = await el.is_visible()
            except Exception:
                vis = False
            if not vis:
                continue
            try:
                await el.scroll_into_view_if_needed()
                await _async_sleep_ms(200)
                await el.click(timeout=timeout_ms)
                return True
            except Exception:
                try:
                    await el.click(timeout=timeout_ms, force=True)
                    return True
                except Exception:
                    continue
    except Exception as e:  # pragma: no cover
        logger.debug("_click_first_sensible: %s", e)
    return False


def _message_scopes(page: Page):
    for f in page.frames:
        if f.is_detached():
            continue
        yield f


async def _open_contact_or_message_cta(p: Page) -> bool:
    await p.set_viewport_size({"width": 1920, "height": 1080})
    await _dismiss_obvious_cookies(p)

    locs = [
        p.locator('a[href*="contact_host"]'),
        p.locator('a[href*="/contact/"]'),
        p.locator('a[href*="/messaging"]'),
        p.get_by_role("link", name=_CONTACT_CTA_RE),
        p.get_by_role("button", name=_CONTACT_CTA_RE),
        p.locator('[data-testid="homes-pdp-cta-btn"] a'),
        p.locator('[data-testid*="pdp-cta"] a, [data-testid*="pdp-cta"] button'),
    ]

    for scroll_y in (0, 300, 700, 1200, 2000, 3600, 5500, 8000, 12_000):
        await p.evaluate("y => window.scrollTo(0, y)", scroll_y)
        await _async_sleep_ms(500)
        for loc in locs:
            if await _click_first_sensible(loc, timeout_ms=15_000):
                return True

    # Full page one more pass
    for loc in locs:
        if await _click_first_sensible(loc, timeout_ms=15_000):
            return True
    return False


async def _raise_if_airbnb_host_quota_screen(page: Page) -> None:
    """If Airbnb shows the host-message rate banner, raise :class:`AirbnbHostQuotaUIError`."""
    try:
        body = page.locator("body")
        text = (await body.inner_text(timeout=8000)).lower()
    except Exception:
        return
    for marker in _AIRBNB_HOST_QUOTA_MARKERS:
        if marker in text:
            raise AirbnbHostQuotaUIError(
                "Airbnb limit: you've messaged several hosts — wait a few hours "
                "before sending more (in-app cap)."
            )


async def _wait_for_visible_composer(p: Page) -> Locator:
    for attempt in range(45):
        if attempt % 4 == 0:
            await _raise_if_airbnb_host_quota_screen(p)
        await _try_expand_collapsed_panels(p)
        for sc in _message_scopes(p):
            try:
                locs = sc.locator(
                    'textarea[name="message"], '
                    'textarea[placeholder*="essage"], '
                    'textarea[data-testid*="message"], '
                    "#message-textarea, textarea"
                )
            except Exception:
                locs = sc.locator("textarea")
            try:
                count = await locs.count()
            except Exception:
                count = 0
            for j in range(min(count, 10)):
                el = locs.nth(j)
                try:
                    if await el.is_visible():
                        await el.scroll_into_view_if_needed()
                        await _async_sleep_ms(200)
                        return el
                except Exception:
                    continue
        await _async_sleep_ms(700)
    await _raise_if_airbnb_host_quota_screen(p)
    raise Exception(
        "Message composer (textarea) did not become visible — try a wider window or complete "
        "any required steps in the message panel"
    )


async def _click_send_message(p: Page) -> bool:
    send_list = [
        p.get_by_role("button", name=re.compile(r"^Send( message)?$", re.I)),
        p.locator("button[type=\"submit\"]").filter(
            has_text=re.compile("Send|Submit", re.I)
        ),
        p.locator("[data-testid*=\"submit\"]"),
        p.locator("button").filter(has_text=re.compile("^Send$|^Send message$", re.I)),
    ]
    for loc in send_list:
        if await _click_first_sensible(loc, timeout_ms=35_000):
            return True
    for sc in _message_scopes(p):
        for loc in [
            sc.get_by_role("button", name=re.compile("Send", re.I)),
            sc.locator("button").filter(has_text=re.compile("Send", re.I)),
        ]:
            if await _click_first_sensible(loc, timeout_ms=20_000):
                return True
    return False


async def _send_message_to_host(
    page: Page,
    listing: Listing,
    message: str,
) -> None:
    """Navigate to a listing and send a message to the host.

    Raises Exception if the message could not be sent.
    """
    listing_url = listing.url
    if not listing_url:
        listing_url = f"{AIRBNB_BASE_URL}/rooms/{listing.id}"

    logger.info("Opening listing: %s", listing_url)
    await page.goto(listing_url, wait_until="domcontentloaded", timeout=60_000)
    try:
        await page.wait_for_load_state("load", timeout=45_000)
    except Exception:  # pragma: no cover
        pass
    await _async_sleep_ms(2000)
    await _raise_if_airbnb_host_quota_screen(page)

    if not await _open_contact_or_message_cta(page):
        raise Exception(
            "Could not find or activate Contact / Message on the listing (try scrolling the page yourself once)"
        )

    await _async_sleep_ms(2000)
    await _raise_if_airbnb_host_quota_screen(page)
    ta = await _wait_for_visible_composer(page)
    await _raise_if_airbnb_host_quota_screen(page)
    await ta.click()
    await ta.fill(message)
    await _async_sleep_ms(400)

    if not await _click_send_message(page):
        raise Exception("Send / Send message control stayed hidden — panel may need to be expanded")

    await _async_sleep_ms(2000)
    await _raise_if_airbnb_host_quota_screen(page)
    logger.info("Message sent to %s for '%s'", listing.host_name or "host", listing.title)


async def run_outreach(
    search_id: int,
    message_template: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict:
    """Run the outreach process for all listings in a search.

    Prerequisites: the user must have logged in via ``login_to_airbnb()``
    first so that the persistent profile has a valid Airbnb session.

    1. Creates outreach message records if they don't exist
    2. Opens a browser with the persisted session
    3. Verifies login — if not logged in, marks all pending messages as failed
    4. Sends personalized messages to each host
    5. Updates status in the database

    Returns a summary dict with counts of sent/failed/skipped messages.
    """
    if message_template is None:
        message_template = get_outreach_message_template()

    # Get listings for this search
    listings = get_listings(search_id, db_path)
    if not listings:
        logger.warning("No listings found for search %d", search_id)
        return {
            "total": 0,
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            "airbnb_rate_limited": False,
        }

    # Create outreach records for new listings
    create_outreach_messages(search_id, listings, message_template, db_path)

    # Get all outreach messages (including previously created)
    messages = get_outreach_messages(search_id, db_path)
    pending = [m for m in messages if m.status == OutreachStatus.PENDING]

    if not pending:
        logger.info("No pending outreach messages for search %d", search_id)
        return {
            "total": len(messages),
            "sent": sum(1 for m in messages if m.status == OutreachStatus.SENT),
            "failed": sum(1 for m in messages if m.status == OutreachStatus.FAILED),
            "skipped": sum(1 for m in messages if m.status == OutreachStatus.SKIPPED),
            "airbnb_rate_limited": False,
        }

    summary = {
        "total": len(messages),
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "airbnb_rate_limited": False,
    }

    async with async_playwright() as pw:
        context, browser, uses_cdp = None, None, False
        try:
            context, page, browser, uses_cdp = await open_airbnb_browser(
                pw, headless=False
            )
        except Exception as e:
            logger.error("Could not start browser for outreach: %s", e)
            for msg in pending:
                update_outreach_status(
                    msg.id,
                    OutreachStatus.FAILED,
                    f"Browser failed to start: {e}",
                    db_path,
                )
                summary["failed"] += 1
            return summary

        try:
            await page.goto(
                AIRBNB_BASE_URL, wait_until="domcontentloaded", timeout=30000
            )
            await _async_sleep_ms(1500)

            if not await wait_for_airbnb_session_ready(page, context):
                for msg in pending:
                    update_outreach_status(
                        msg.id,
                        OutreachStatus.FAILED,
                        "Airbnb sign-in not completed in time. Finish sign up or sign in in the browser, then start outreach again.",
                        db_path,
                    )
                    summary["failed"] += 1
                return summary

            page = await _use_airbnb_page_for_outreach(page, context)

            # Send messages (global sliding-window cap + spacing — see outreach_quota / .env)
            for idx, msg in enumerate(pending):
                listing = next(
                    (lst for lst in listings if lst.id == msg.listing_id), None
                )
                if not listing:
                    update_outreach_status(
                        msg.id, OutreachStatus.SKIPPED, "Listing not found", db_path
                    )
                    summary["skipped"] += 1
                    await sleep_between_outreach_attempts()
                    continue

                await wait_until_send_allowed(db_path)
                update_outreach_status(msg.id, OutreachStatus.SENDING, "", db_path)

                try:
                    await _send_message_to_host(page, listing, msg.message)
                    update_outreach_status(msg.id, OutreachStatus.SENT, "", db_path)
                    summary["sent"] += 1
                    record_successful_send(db_path)
                    logger.info(
                        "✅ Sent message to %s (%s)",
                        msg.host_name,
                        msg.place_name,
                    )
                except AirbnbHostQuotaUIError as e:
                    error_msg = str(e)
                    update_outreach_status(
                        msg.id, OutreachStatus.SKIPPED, error_msg, db_path
                    )
                    summary["skipped"] += 1
                    summary["airbnb_rate_limited"] = True
                    logger.error("🛑 Airbnb host messaging cap: %s", error_msg)
                    for msg2 in pending[idx + 1 :]:
                        update_outreach_status(
                            msg2.id,
                            OutreachStatus.SKIPPED,
                            "Paused: Airbnb rate limit — try again in a few hours.",
                            db_path,
                        )
                        summary["skipped"] += 1
                    break
                except Exception as e:
                    error_msg = str(e)
                    update_outreach_status(
                        msg.id, OutreachStatus.FAILED, error_msg, db_path
                    )
                    summary["failed"] += 1
                    logger.error(
                        "❌ Failed to send to %s: %s", msg.host_name, error_msg
                    )

                await sleep_between_outreach_attempts()

        except Exception as e:
            logger.error("Outreach error: %s", e)
        finally:
            if context is not None:
                await close_airbnb_session(context, browser, uses_cdp=uses_cdp)

    return summary


def run_outreach_sync(
    search_id: int,
    message_template: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict:
    """Synchronous wrapper for run_outreach."""
    return asyncio.run(
        run_outreach(
            search_id=search_id,
            message_template=message_template,
            db_path=db_path,
        )
    )
