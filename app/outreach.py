"""Airbnb host outreach automation using Playwright.

Flow:
1. Open a browser (non-headless so user can log in if needed)
2. Check if the user is logged in; if not, pause for manual login
3. For each listing, navigate to its page, click "Contact Host", type the message, and send
4. Track status of each message in the database
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from app.config import get_browser_state_path, get_outreach_message_template
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


async def _load_browser_context(
    browser: Browser,
) -> BrowserContext:
    """Load browser context with saved state if available, otherwise create fresh."""
    state_path = get_browser_state_path()
    context_opts = {
        "viewport": {"width": 1920, "height": 1080},
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }

    if Path(state_path).exists():
        try:
            context_opts["storage_state"] = state_path
            context = await browser.new_context(**context_opts)
            logger.info("Loaded saved browser state from %s", state_path)
            return context
        except Exception as e:
            logger.warning("Failed to load saved state, starting fresh: %s", e)
            context_opts.pop("storage_state", None)

    return await browser.new_context(**context_opts)


async def _save_browser_state(context: BrowserContext) -> None:
    """Save browser cookies/session for reuse."""
    state_path = get_browser_state_path()
    try:
        await context.storage_state(path=state_path)
        logger.info("Saved browser state to %s", state_path)
    except Exception as e:
        logger.warning("Failed to save browser state: %s", e)


async def _ensure_logged_in(page: Page) -> bool:
    """Check if user is logged in to Airbnb. If not, navigate to login and wait.

    Returns True if logged in, False if login was not completed within timeout.
    """
    # Navigate to Airbnb and check for login indicators
    await page.goto(AIRBNB_BASE_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)

    # Check for profile menu (indicates logged in)
    profile_el = await page.query_selector(
        '[data-testid="cypress-headernav-profile"], '
        'button[aria-label*="profile"], '
        'button[aria-label*="Account"], '
        'img[data-testid="user-avatar"]'
    )

    if profile_el:
        logger.info("User is already logged in to Airbnb")
        return True

    # Not logged in — navigate to login page and wait for user
    logger.info("Not logged in. Navigating to login page — please log in manually.")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)

    # Wait up to 5 minutes for login to complete
    for _ in range(LOGIN_MAX_CHECKS):
        await page.wait_for_timeout(LOGIN_CHECK_INTERVAL_MS)
        profile_el = await page.query_selector(
            '[data-testid="cypress-headernav-profile"], '
            'button[aria-label*="profile"], '
            'button[aria-label*="Account"], '
            'img[data-testid="user-avatar"]'
        )
        if profile_el:
            logger.info("Login successful!")
            return True

        # Also check if URL changed away from login
        current_url = page.url
        if AIRBNB_BASE_URL in current_url and "/login" not in current_url.split("?")[0]:
            logger.info("Login appears successful (redirected away from login)")
            return True

    logger.error("Login timeout — user did not complete login within 5 minutes")
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

    1. Creates outreach message records if they don't exist
    2. Opens a browser (non-headless) for the user to log in
    3. Sends personalized messages to each host
    4. Updates status in the database

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

    async with async_playwright() as p:
        # Always non-headless for outreach (user needs to log in)
        browser = await p.chromium.launch(headless=False)
        context = await _load_browser_context(browser)
        page = await context.new_page()

        try:
            # Ensure user is logged in
            logged_in = await _ensure_logged_in(page)
            if not logged_in:
                logger.error("Cannot proceed without login")
                for msg in pending:
                    update_outreach_status(
                        msg.id, OutreachStatus.FAILED, "Login required", db_path
                    )
                    summary["failed"] += 1
                return summary

            # Save state after successful login
            await _save_browser_state(context)

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

            # Save state after outreach
            await _save_browser_state(context)

        except Exception as e:
            logger.error("Outreach error: %s", e)
        finally:
            await browser.close()

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
