"""Airbnb host outreach automation using Playwright.

Flow:
1. User logs in once via the "Login to Airbnb" button (separate step)
2. Login session is persisted in a Chromium user-data directory on disk
3. Outreach re-uses the same profile — no login prompt during messaging
4. For each listing, navigate to its page, click "Contact Host", type the message, and send
5. Track status of each message in the database
"""

import asyncio
import logging
from typing import Optional

from playwright.async_api import async_playwright, BrowserContext, Page

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

logger = logging.getLogger(__name__)

AIRBNB_BASE_URL = "https://www.airbnb.com"
LOGIN_URL = f"{AIRBNB_BASE_URL}/login"

# Named constants for timeouts and delays
LOGIN_CHECK_INTERVAL_MS = 5000
LOGIN_MAX_CHECKS = 60  # 60 checks × 5s = 5 minutes max wait
MESSAGE_DELAY_MS = 2000

_PROFILE_SELECTORS = (
    '[data-testid="cypress-headernav-profile"], '
    'button[aria-label*="profile"], '
    'button[aria-label*="Account"], '
    'img[data-testid="user-avatar"]'
)


async def _is_logged_in(page: Page) -> bool:
    """Detect logged-in state (DOM + session cookies; Airbnb often hides nav until slow JS)."""
    for _ in range(3):
        if await page.query_selector(_PROFILE_SELECTORS):
            return True
        await page.wait_for_timeout(1000)
    # Cookie fallback: OAuth / new UI may not match legacy selectors
    try:
        cookies = await page.context.cookies("https://www.airbnb.com")
    except Exception:
        cookies = await page.context.cookies()
    for c in cookies:
        d = (c.get("domain") or "").lower()
        n = (c.get("name") or "").lower()
        if "airbnb" not in d and "airbnb" not in n:
            continue
        if "session" in n or n in ("_aat", "aaj"):
            return True
    return False


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
            # Check if already logged in
            await page.goto(
                AIRBNB_BASE_URL, wait_until="domcontentloaded", timeout=30000
            )
            await page.wait_for_timeout(2000)
            if await _is_logged_in(page):
                logger.info("Already logged in to Airbnb — no action needed.")
                await flush_profile_after_login(context)
                return True

            # Navigate to login page and wait for user to complete login
            logger.info(
                "Not logged in. Opening Airbnb login page — log in (email, Google, or Apple)."
            )
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)

            for _ in range(LOGIN_MAX_CHECKS):
                await page.wait_for_timeout(LOGIN_CHECK_INTERVAL_MS)
                if await _is_logged_in(page):
                    logger.info("Login successful!")
                    await flush_profile_after_login(context)
                    return True
                current_url = page.url
                if (AIRBNB_BASE_URL in current_url and "/login" not in current_url.split("?")[0]) and await _is_logged_in(page):
                    logger.info(
                        "Login appears successful (session detected after redirect)."
                    )
                    await flush_profile_after_login(context)
                    return True

            logger.error(
                "Login timeout — user did not complete login within 5 minutes"
            )
            return False
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
            await page.wait_for_timeout(2000)
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
    await page.goto(listing_url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)

    # Look for "Contact Host" or "Message Host" button
    contact_btn = await page.query_selector(
        'a[href*="contact_host"], '
        'button:has-text("Contact Host"), '
        'button:has-text("Message Host"), '
        'a:has-text("Contact Host"), '
        'a:has-text("Message Host"), '
        '[data-testid="homes-pdp-cta-btn"] a, '
        'a[href*="/contact/"]'
    )

    if not contact_btn:
        # Try scrolling down to find the button
        await page.evaluate("window.scrollBy(0, 1000)")
        await page.wait_for_timeout(2000)
        contact_btn = await page.query_selector(
            'a[href*="contact_host"], '
            'button:has-text("Contact Host"), '
            'button:has-text("Message Host"), '
            'a:has-text("Contact Host"), '
            'a:has-text("Message Host")'
        )

    if not contact_btn:
        raise Exception("Could not find 'Contact Host' button on the listing page")

    await contact_btn.click()
    await page.wait_for_timeout(3000)

    # Find the message textarea
    textarea = await page.query_selector(
        'textarea[name="message"], '
        'textarea[data-testid="message-textarea"], '
        'textarea[placeholder*="message"], '
        '#message-textarea, '
        'textarea'
    )

    if not textarea:
        raise Exception("Could not find message textarea on the contact page")

    # Type the message
    await textarea.click()
    await textarea.fill(message)
    await page.wait_for_timeout(1000)

    # Find and click the send button
    send_btn = await page.query_selector(
        'button[type="submit"], '
        'button:has-text("Send message"), '
        'button:has-text("Send"), '
        '[data-testid="submit-btn"]'
    )

    if not send_btn:
        raise Exception("Could not find 'Send' button")

    await send_btn.click()
    await page.wait_for_timeout(3000)

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
        return {"total": 0, "sent": 0, "failed": 0, "skipped": 0}

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
        }

    summary = {"total": len(messages), "sent": 0, "failed": 0, "skipped": 0}

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
            # Verify the user is logged in
            await page.goto(
                AIRBNB_BASE_URL, wait_until="domcontentloaded", timeout=30000
            )
            await page.wait_for_timeout(3000)

            if not await _is_logged_in(page):
                logger.error(
                    "Not logged in! Please use 'Login to Airbnb' from the UI first."
                )
                for msg in pending:
                    update_outreach_status(
                        msg.id,
                        OutreachStatus.FAILED,
                        "Not logged in — use 'Login to Airbnb' first",
                        db_path,
                    )
                    summary["failed"] += 1
                return summary

            # Send messages
            for msg in pending:
                # Find the corresponding listing
                listing = next(
                    (lst for lst in listings if lst.id == msg.listing_id), None
                )
                if not listing:
                    update_outreach_status(
                        msg.id, OutreachStatus.SKIPPED, "Listing not found", db_path
                    )
                    summary["skipped"] += 1
                    continue

                update_outreach_status(msg.id, OutreachStatus.SENDING, "", db_path)

                try:
                    await _send_message_to_host(page, listing, msg.message)
                    update_outreach_status(msg.id, OutreachStatus.SENT, "", db_path)
                    summary["sent"] += 1
                    logger.info(
                        "✅ Sent message to %s (%s)",
                        msg.host_name,
                        msg.place_name,
                    )
                except Exception as e:
                    error_msg = str(e)
                    update_outreach_status(
                        msg.id, OutreachStatus.FAILED, error_msg, db_path
                    )
                    summary["failed"] += 1
                    logger.error(
                        "❌ Failed to send to %s: %s", msg.host_name, error_msg
                    )

                # Small delay between messages to avoid rate limiting
                await page.wait_for_timeout(MESSAGE_DELAY_MS)

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
