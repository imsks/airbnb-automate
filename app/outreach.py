"""Airbnb login and listing-page messaging, via Playwright.

Two responsibilities, both of which need a real browser:

1. **Login.** Airbnb blocks automated sign-in, so a human does it once and the
   session persists in a user-data directory (or an attached Chrome over CDP).
   Being signed in to Google is not the same as being signed in to Airbnb, so
   :func:`wait_for_airbnb_session_ready` blocks on DOM, cookies *and* a /trips
   check before declaring success.
2. **Sending.** :func:`_send_message_to_host` opens a listing, clicks "Contact
   host", types, sends, and reports the thread the send created.

Who sends and what they say is decided upstream by the Scribe, the Warden and
the send budget. This module only drives the browser.
"""

import asyncio
import logging
import re
import time
from typing import Awaitable, Callable, Optional
from urllib.parse import urlsplit

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
from app.config import get_airbnb_base_url
from app.messaging_errors import (
    ComposerUnavailable,
    DeliveryUnconfirmed,
    MessageRejected,
    SessionExpired,
)
from app.models import Listing
from app.thread_linking import capture_thread_reference

logger = logging.getLogger(__name__)


def _airbnb_origin() -> str:
    return get_airbnb_base_url().rstrip("/")


def _login_url() -> str:
    return f"{_airbnb_origin()}/login"


def _trips_url() -> str:
    return f"{_airbnb_origin()}/trips"


class AirbnbHostQuotaUIError(Exception):
    """Airbnb surfaced an in-app host messaging cap — stop and try again later."""


# Copy varies slightly by locale; match substrings seen in English UI.
_AIRBNB_HOST_QUOTA_MARKERS = (
    "already messaged several hosts",
    "wait a few hours before you can send",
    "you'll need to wait a few hours",
)

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
    hostname = urlsplit(page.url).hostname or ""
    if hostname not in (urlsplit(_airbnb_origin()).hostname, "www.airbnb.com", "www.airbnb.co.in"):
        return False
    try:
        await _raise_if_login_required(page)
    except SessionExpired:
        return False
    if await page.get_by_role("button", name=re.compile(r"^Log in or sign up$", re.I)).count():
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
    return False


async def _airbnb_trip_url_confirms_session(page: Page) -> bool:
    """A logged-in user can load /trips; guests are sent to /login (or the URL keeps login)."""
    try:
        await page.goto(
            _trips_url(), wait_until="domcontentloaded", timeout=30000
        )
        await _async_sleep_ms(2000)
        await _raise_if_login_required(page)
        u = (page.url or "").lower()
        if "/login" in u or "/signup" in u or "authenticate" in u:
            return False
        if "trips" in u or (_airbnb_origin() in u and "login" not in u and "signup" not in u):
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
                _airbnb_origin(), wait_until="domcontentloaded", timeout=20000
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
                                _airbnb_origin(),
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
                                    _airbnb_origin(),
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
                            _airbnb_origin(),
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
                    _login_url(), wait_until="domcontentloaded", timeout=30000
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
                        _login_url(), wait_until="domcontentloaded", timeout=30000
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
                                _airbnb_origin(),
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
                                    _airbnb_origin(),
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
            _airbnb_origin(), wait_until="domcontentloaded", timeout=30000
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
                _airbnb_origin(), wait_until="domcontentloaded", timeout=30000
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
                _airbnb_origin(), wait_until="domcontentloaded", timeout=20000
            )
            await _async_sleep_ms(2000)
            return await _session_fully_ready(page, context)
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
    r"^(?:Contact|Message)(?: the)? host$",
    re.IGNORECASE,
)

_COMPOSER_SELECTOR = (
    'textarea[name="message"], textarea[name="contactHostMessage"], '
    'textarea[aria-label*="message" i], textarea[placeholder*="message" i], '
    'textarea[data-testid*="message"], #message-textarea, '
    '[role="textbox"][contenteditable="true"], '
    '[data-testid*="message"] [contenteditable="true"]'
)


async def _raise_if_login_required(page: Page) -> None:
    path = urlsplit(page.url).path.lower()
    if re.match(r"^/(?:login|signup|authenticate)(?:/|$)", path):
        raise SessionExpired("Airbnb sign-in required; run make login before retrying.")
    headings = page.get_by_role(
        "heading", name=re.compile(r"^(?:Welcome back(?:,|\s)|Log in\b|Sign up$|Confirm it.s you$)", re.I)
    )
    for index in range(await headings.count()):
        if await headings.nth(index).is_visible():
            raise SessionExpired("Airbnb opened a login dialog, not a message box; run make login.")


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


def _message_scopes(page: Page):
    for f in page.frames:
        if f.is_detached():
            continue
        yield f


async def _open_contact_or_message_cta(
    p: Page, *, listing_id: Optional[str] = None, timeout_ms: int = 20000
) -> bool:
    await p.set_viewport_size({"width": 1920, "height": 1080})
    await _dismiss_obvious_cookies(p)
    links = p.locator('a[href*="/contact_host/"]')
    locs = [
        links,
        p.get_by_role("link", name=_CONTACT_CTA_RE),
        p.get_by_role("button", name=_CONTACT_CTA_RE),
    ]
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        await _raise_if_login_required(p)
        for loc in locs:
            for index in range(await loc.count()):
                element = loc.nth(index)
                if not await element.is_visible():
                    continue
                href = await element.get_attribute("href")
                if href:
                    target = urlsplit(href)
                    if target.netloc and target.netloc != urlsplit(p.url).netloc:
                        continue
                    if listing_id and f"/contact_host/{listing_id}/" not in target.path:
                        continue
                logger.info("[contact] Opening Message host; booking controls are excluded.")
                await element.scroll_into_view_if_needed()
                await element.click(timeout=10000)
                return True
        await _async_sleep_ms(250)
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


async def _wait_for_visible_composer(p: Page, *, timeout_ms: int = 15000) -> Locator:
    deadline = time.monotonic() + timeout_ms / 1000
    attempt = 0
    while time.monotonic() < deadline:
        await _raise_if_login_required(p)
        if attempt % 8 == 0:
            await _raise_if_airbnb_host_quota_screen(p)
        for sc in _message_scopes(p):
            locs = sc.locator(_COMPOSER_SELECTOR)
            for j in range(min(await locs.count(), 10)):
                el = locs.nth(j)
                if await el.is_visible() and await el.is_editable():
                    await el.scroll_into_view_if_needed()
                    logger.info("[composer] Message box is visible and editable.")
                    return el
        attempt += 1
        await _async_sleep_ms(250)
    await _raise_if_airbnb_host_quota_screen(p)
    raise ComposerUnavailable(
        f"No editable message box on {urlsplit(p.url).path}; no Send action was taken."
    )


async def _click_send_message(
    p: Page, *, before_send: Optional[Callable[[], Awaitable[None]]] = None
) -> bool:
    for sc in _message_scopes(p):
        buttons = sc.get_by_role("button", name=re.compile(r"^Send(?: message)?$", re.I))
        for index in range(await buttons.count()):
            button = buttons.nth(index)
            if not await button.is_visible() or not await button.is_enabled():
                continue
            await _raise_if_login_required(p)
            await _raise_if_airbnb_host_quota_screen(p)
            if before_send:
                await before_send()
            logger.info("[submit] Clicking Send once; waiting for a saved conversation.")
            try:
                await button.click(timeout=10000)
            except Exception as exc:
                raise DeliveryUnconfirmed("Send was attempted; do not retry without checking the thread.") from exc
            return True
    return False


_REJECTION_MARKERS = (
    "can't send your message yet",
    "links and contact info can't be shared",
    "please remove the info below",
)
#: Airbnb's "Why risk it? Stay on Airbnb" interstitial. It reads as advice but
#: it withholds the message, so treat it as a refusal.
_OFF_PLATFORM_MARKERS = (
    "why risk it",
    "communicating with a host outside of airbnb",
    "refers to communicating with a host outside",
)
_QUOTED_TERM_RE = re.compile(r"[\u201c\"']([^\u201d\"']{1,60})[\u201d\"']")


def _normalise_quotes(text: str) -> str:
    return text.replace("\u2019", "'").replace("\u2018", "'")


def parse_rejection_terms(text: str) -> list[str]:
    """Terms Airbnb named as unacceptable, taken from its own refusal notice."""
    flat = _normalise_quotes(" ".join(text.split()))
    if not any(marker in flat.lower() for marker in _REJECTION_MARKERS):
        return []
    tail = flat.split("before sending", 1)[-1]
    seen, terms = set(), []
    for candidate in _QUOTED_TERM_RE.findall(tail):
        cleaned = candidate.strip(" :.")
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            terms.append(cleaned)
    return terms


async def _raise_if_message_rejected(page: Page) -> None:
    """Airbnb vets a first message and says so when it refuses to deliver it."""
    try:
        text = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        return
    flat = _normalise_quotes(" ".join(text.split())).lower()
    if any(marker in flat for marker in _REJECTION_MARKERS):
        terms = parse_rejection_terms(text)
        detail = ", ".join(repr(t) for t in terms) or "contact details or links"
        raise MessageRejected(
            f"Airbnb refused the message and did not send it. Remove {detail}, then retry.",
            terms,
        )
    if any(marker in flat for marker in _OFF_PLATFORM_MARKERS):
        raise MessageRejected(
            "Airbnb read the message as arranging contact off-platform and did not "
            "send it. Remove social handles and any off-platform invitation, then retry.",
            [],
        )


#: Conversation chrome. The composer is deliberately not in this list: a filled
#: box is not proof the message left the page.
_CONVERSATION_SELECTOR = (
    '[data-testid="message-list"], '
    '[data-testid="message-thread-item-list-container"], '
    '[role="group"][data-item-id], [data-message-id], '
    '[data-name="message-content-wrapper"]'
)
#: How long to watch the inbox for the new row before giving up.
_INBOX_LOOKUP_SECONDS = 8
_VIEW_CONVERSATION_RE = re.compile(
    r"(?:view|go to|see|open)\s+(?:the\s+)?(?:conversation|messages?|threads?)",
    re.I,
)


async def _conversation_visible(page: Page) -> bool:
    """True when a conversation is on screen, whether or not the bubble text matched."""
    loc = page.locator(_CONVERSATION_SELECTOR)
    try:
        count = min(await loc.count(), 8)
    except Exception:
        return False
    for index in range(count):
        try:
            if await loc.nth(index).is_visible():
                return True
        except Exception:
            continue
    return False


def choose_inbox_row(
    rows: list[tuple[str, str, str]], *, host_name: str = "", location: str = ""
) -> tuple[Optional[str], str]:
    """Pick the newest inbox row for the host we just wrote to.

    Rows are newest first. Airbnb's preview says "Enquiry sent", not the
    message, so the host and the city are what identify the thread.
    """
    host = " ".join(host_name.split()).lower()
    city = location.split(",")[0].split("(")[0].strip().lower()

    def flat(row: tuple[str, str, str]) -> str:
        return " ".join(row[2].split()).lower()

    if host and city:
        both = [row for row in rows if host in flat(row) and city in flat(row)]
        if both:
            return both[0][0], both[0][1]
    if host:
        named = [row for row in rows if host in flat(row)]
        if named:
            return named[0][0], named[0][1]
        # A named host who is missing from the list is not "the newest row
        # in that city". That row is a different conversation.
        return None, ""
    if city:
        placed = [row for row in rows if city in flat(row)]
        if placed:
            return placed[0][0], placed[0][1]
    return None, ""


def choose_thread_link(
    links: list[tuple[str, str, str]], message: str, host_name: str = ""
) -> tuple[Optional[str], str]:
    """Pick the thread a send just created from ``(id, url, text)`` links.

    The message preview wins. A host name is used only when one row matches.
    Several unnamed threads are left alone rather than guessed.
    """
    snippet = " ".join(message.split())[:60].lower()
    host = " ".join(host_name.split()).lower()
    if snippet:
        for thread_id, url, text in links:
            if snippet in " ".join(text.split()).lower():
                return thread_id, url
    if host:
        named = [item for item in links if host in " ".join(item[2].split()).lower()]
        if len(named) == 1:
            return named[0][0], named[0][1]
    if len(links) == 1:
        return links[0][0], links[0][1]
    return None, ""


async def _inbox_rows(page: Page) -> list[tuple[str, str, str]]:
    """``(thread_id, url, text)`` from Airbnb's inbox list.

    The row's address is ``#``. The id is ``data-testid="inbox_list_<id>"``.
    """
    try:
        raw = await page.eval_on_selector_all(
            'a[data-testid^="inbox_list_"]',
            """els => els.map(a => ({
                testid: a.getAttribute('data-testid') || '',
                text: [a.innerText, a.getAttribute('aria-label')].filter(Boolean).join(' ')
            }))""",
        )
    except Exception:
        return []
    origin = _airbnb_origin()
    rows: list[tuple[str, str, str]] = []
    for item in raw:
        thread_id = (item.get("testid") or "").replace("inbox_list_", "", 1)
        if not thread_id.isdigit():
            continue
        rows.append((thread_id, f"{origin}/guest/messages/{thread_id}", item.get("text") or ""))
    return rows


async def _thread_from_inbox(
    page: Page, message: str, host_name: str, location: str = ""
) -> tuple[Optional[str], str]:
    """Open the inbox and read the thread this send became.

    A listing-page send stays on the listing. The conversation is the newest
    inbox row for that host. ``/guest/inbox`` also auto-opens the previous
    thread, so the address bar is not evidence — only a matching row is.
    """
    origin = _airbnb_origin()
    for path in ("/guest/inbox", "/hosting/inbox"):
        try:
            await page.goto(f"{origin}{path}", wait_until="domcontentloaded", timeout=30000)
        except Exception:
            continue
        deadline = time.monotonic() + _INBOX_LOOKUP_SECONDS
        thread_id, thread_url = None, ""
        while True:
            await _raise_if_login_required(page)
            await _raise_if_message_rejected(page)
            thread_id, thread_url = choose_inbox_row(
                await _inbox_rows(page), host_name=host_name, location=location
            )
            if thread_id or time.monotonic() >= deadline:
                break
            await _async_sleep_ms(500)
        if not thread_id:
            continue
        logger.info(
            "[inbox] Send is in thread %s (%d chars)",
            thread_id,
            len(" ".join(message.split())),
        )
        return thread_id, thread_url
    return None, ""


async def _wait_for_delivery(
    page: Page,
    message: str,
    *,
    timeout_ms: int = 20000,
    clicked: bool = False,
    host_name: str = "",
    location: str = "",
) -> tuple[str, str]:
    # The listing page rarely navigates. Look briefly, then open the inbox.
    wait_s = min(timeout_ms / 1000, 5 if clicked else timeout_ms / 1000)
    deadline = time.monotonic() + wait_s
    thread_id, thread_url = None, ""
    while time.monotonic() < deadline:
        await _raise_if_login_required(page)
        await _raise_if_message_rejected(page)
        await _raise_if_airbnb_host_quota_screen(page)
        # Only the address bar counts here. A listing page contains other
        # message links, and treating the only one as "the" thread attaches
        # this send to a conversation that was already open.
        thread_id, thread_url = await capture_thread_reference(page)
        if thread_id:
            break
        view_thread = page.get_by_role("link", name=_VIEW_CONVERSATION_RE)
        try:
            if await view_thread.count() and await view_thread.first.is_visible():
                await view_thread.first.click(timeout=5000)
        except Exception:
            logger.debug("[verify] View-conversation click did not complete", exc_info=True)
        await _async_sleep_ms(250)
    else:
        if clicked:
            thread_id, thread_url = await _thread_from_inbox(
                page, message, host_name, location
            )
        if not thread_id:
            raise DeliveryUnconfirmed(
                "Send was clicked, but no conversation was open afterwards. No automatic retry."
            )
        logger.info("[verified] Conversation found in the inbox; thread=%s", thread_id)
        return str(thread_id), thread_url

    try:
        await page.reload(wait_until="domcontentloaded", timeout=30000)
    except Exception:
        logger.info(
            "[verified] Thread URL stayed %s; the reload did not finish",
            thread_id,
        )
        return str(thread_id or ""), thread_url or ""
    reload_deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < reload_deadline:
        await _raise_if_login_required(page)
        await _raise_if_message_rejected(page)
        await _raise_if_airbnb_host_quota_screen(page)
        confirmed_id, confirmed_url = await capture_thread_reference(page)
        if confirmed_id or await _conversation_visible(page):
            thread_id = confirmed_id or thread_id
            thread_url = confirmed_url or thread_url
            break
        await _async_sleep_ms(250)
    else:
        if clicked and thread_id:
            logger.info("[verified] Conversation stayed open; thread=%s", thread_id)
            return str(thread_id), thread_url or ""
        raise DeliveryUnconfirmed(
            "The conversation was not open after reloading the thread. No automatic retry."
        )
    logger.info(
        "[verified] Conversation is open after reload; thread=%s message_chars=%s",
        thread_id or "unlinked",
        len(" ".join(message.split())),
    )
    return str(thread_id or ""), thread_url or ""


async def _send_message_to_host(
    page: Page,
    listing: Listing,
    message: str,
    *,
    before_send: Optional[Callable[[], Awaitable[None]]] = None,
) -> tuple[Optional[str], str]:
    """Navigate to a listing and send a message to the host.

    Returns the ``(thread_id, thread_url)`` the send produced, so the deal can
    be linked to the conversation it started. Either may be empty when Airbnb
    keeps you on the listing page.

    Raises Exception if the message could not be sent.
    """
    listing_url = listing.url
    if not listing_url:
        listing_url = f"{_airbnb_origin()}/rooms/{listing.id}"

    logger.info("[listing] %s | %s | host=%s", listing.title, listing.location, listing.host_name or "not yet extracted")
    logger.info("[navigate] %s", listing_url.split("?")[0])
    await page.goto(listing_url, wait_until="domcontentloaded", timeout=60_000)
    try:
        await page.wait_for_load_state("load", timeout=45_000)
    except Exception:  # pragma: no cover
        pass
    await _async_sleep_ms(2000)
    await _raise_if_login_required(page)
    await _raise_if_airbnb_host_quota_screen(page)

    if not await _open_contact_or_message_cta(page, listing_id=listing.id):
        raise ComposerUnavailable(
            "No Message host control was found; booking buttons were deliberately not clicked."
        )

    await _async_sleep_ms(2000)
    await _raise_if_airbnb_host_quota_screen(page)
    ta = await _wait_for_visible_composer(page)
    await _raise_if_airbnb_host_quota_screen(page)
    await ta.click()
    await ta.fill(message)
    await _async_sleep_ms(400)

    if not await _click_send_message(page, before_send=before_send):
        raise ComposerUnavailable("No enabled Send message button found; nothing was submitted.")
    try:
        return await _wait_for_delivery(
            page,
            message,
            clicked=True,
            host_name=listing.host_name or "",
            location=listing.location or "",
        )
    except (DeliveryUnconfirmed, MessageRejected, AirbnbHostQuotaUIError):
        raise
    except Exception as exc:
        logger.exception("[send] Confirmation failed after the click")
        raise DeliveryUnconfirmed(
            "Submission outcome is uncertain; inspect the conversation before retrying. "
            f"({type(exc).__name__}: {exc})"
        ) from exc
