"""Read Airbnb inbox chats via Playwright browser automation.

Scrapes conversation threads from the Airbnb inbox using the persisted
browser session.  Uses ``aria-label`` attributes and ``data-testid``
selectors matching the actual Airbnb DOM structure (verified against
real HTML snapshots in ``test/chat_list.html`` and ``test/chat.html``).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from playwright.async_api import (
    Page,
    BrowserContext,
    async_playwright,
)

from app.browser_session import (
    close_airbnb_session,
    open_airbnb_browser,
    save_storage_state,
)
from app.config import get_airbnb_base_url

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ChatMessage:
    """A single message in an Airbnb conversation."""

    sender: str  # "host" or "user"
    text: str
    timestamp: str = ""


@dataclass
class ChatThread:
    """An Airbnb conversation thread with a host."""

    thread_id: str
    host_name: str
    listing_title: str = ""
    listing_url: str = ""
    messages: list[ChatMessage] = field(default_factory=list)
    booking_status: str = ""
    location: str = ""

    @property
    def last_message(self) -> Optional[ChatMessage]:
        return self.messages[-1] if self.messages else None

    @property
    def conversation_text(self) -> str:
        """Format the conversation for LLM consumption."""
        lines: list[str] = []
        for m in self.messages:
            role = "Host" if m.sender == "host" else "You"
            ts = f" ({m.timestamp})" if m.timestamp else ""
            lines.append(f"**{role}**{ts}: {m.text}")
        return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _airbnb_origin() -> str:
    return get_airbnb_base_url().rstrip("/")


async def _async_sleep_ms(ms: int) -> None:
    await asyncio.sleep(ms / 1000.0)


# Regex helpers for parsing aria-label text on inbox items
_CONV_WITH_RE = re.compile(
    r"Conversation with (.+?)\.\s*Last message", re.IGNORECASE
)
_BOOKING_STATUS_RE = re.compile(
    r"Booking status is ([^.]+)", re.IGNORECASE
)
_LOCATION_RE = re.compile(
    r"\bin\s+([A-Z][\w\s\-]+?)\.?\s*$", re.IGNORECASE
)

# Regex helpers for parsing aria-label on message groups
_ARIA_SENDER_RE = re.compile(
    r"(?:(?:Yesterday|Today|Most Recent Message)\.\s*)?(\S+)\s+sent\s+",
    re.IGNORECASE,
)
_ARIA_TIMESTAMP_RE = re.compile(
    r"\.\s*Sent\s+((?:Yesterday|Today),?\s*\d{1,2}:\d{2}\s*[ap]m)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Inbox thread-list scraping
# ---------------------------------------------------------------------------


async def _get_inbox_threads(page: Page) -> list[dict]:
    """Scrape the inbox sidebar to get thread metadata.

    Uses ``a[data-testid^="inbox_list_"]`` links and their accessible
    ``<span>`` elements which contain structured conversation summaries
    such as host name, last message, booking status, and location.
    """
    threads: list[dict] = []

    # ── Wait for the inbox container ──
    logger.debug("Waiting for inbox container…")
    try:
        await page.wait_for_selector(
            '[data-testid="inbox-container-marker"], '
            '#list_inbox, '
            'a[data-testid^="inbox_list_"]',
            timeout=15000,
        )
    except Exception as exc:
        logger.warning(
            "Inbox container not found with primary selectors: %s", exc
        )
        # Fallback: any list-based container
        try:
            await page.wait_for_selector(
                '[role="group"][aria-label*="Conversations"], '
                '[data-listroot="true"]',
                timeout=10000,
            )
        except Exception:
            logger.error("Could not locate inbox thread list at all")
            return threads

    await _async_sleep_ms(2000)

    # ── Find thread links ──
    items = await page.query_selector_all('a[data-testid^="inbox_list_"]')
    logger.info(
        "Found %d thread link(s) via a[data-testid^='inbox_list_']",
        len(items),
    )

    if not items:
        # Fallback selector
        items = await page.query_selector_all(
            '[data-listrow="true"] a[data-item-index]'
        )
        logger.info(
            "Fallback: found %d thread link(s) via [data-listrow] a",
            len(items),
        )

    # ── Parse each thread item ──
    for idx, item in enumerate(items):
        try:
            # Thread ID from data-testid="inbox_list_{id}"
            testid = (await item.get_attribute("data-testid")) or ""
            thread_id = testid.replace("inbox_list_", "") if testid else ""

            if not thread_id:
                # Try the parent wrapper: <div id="inbox_list_{id}">
                parent_id = await item.evaluate(
                    "el => (el.closest('[id^=\"inbox_list_\"]') || {}).id || ''"
                )
                thread_id = parent_id.replace("inbox_list_", "")

            # Accessible summary span (hidden but information-rich)
            summary_span = await item.query_selector("span.a8jt5op")
            full_text = ""
            if summary_span:
                full_text = (await summary_span.inner_text()).strip()
            if not full_text:
                full_text = (await item.get_attribute("aria-label")) or ""

            logger.debug(
                "  Thread link #%d: testid=%s summary=%.120s",
                idx, testid, full_text,
            )

            # Host name
            host_name = "Host"
            hm = _CONV_WITH_RE.search(full_text)
            if hm:
                host_name = hm.group(1).strip()
            else:
                # Fallback: visible name element in the row
                name_text = await item.evaluate(
                    """el => {
                        const row = el.closest('[data-listrow]') || el.parentElement;
                        const nameEl = row && row.querySelector('.oj9ozqm');
                        return nameEl ? nameEl.textContent.trim() : '';
                    }"""
                )
                if name_text:
                    host_name = name_text

            # Booking status
            booking_status = ""
            bm = _BOOKING_STATUS_RE.search(full_text)
            if bm:
                booking_status = bm.group(1).strip()

            # Location
            location = ""
            lm = _LOCATION_RE.search(full_text)
            if lm:
                location = lm.group(1).strip()

            thread_data = {
                "thread_id": thread_id,
                "host_name": host_name,
                "href": f"/messaging/thread/{thread_id}" if thread_id else "",
                "booking_status": booking_status,
                "location": location,
                "summary": full_text[:200],
            }
            threads.append(thread_data)
            logger.info(
                "  📌 Thread #%d: id=%s host=%s status=%s location=%s",
                idx + 1,
                thread_id,
                host_name,
                booking_status or "(none)",
                location or "(none)",
            )
        except Exception as e:
            logger.warning("Error reading inbox item #%d: %s", idx, e)
            continue

    return threads


# ---------------------------------------------------------------------------
# Thread message scraping
# ---------------------------------------------------------------------------


async def _read_thread_messages(
    page: Page, host_name: str = ""
) -> list[ChatMessage]:
    """Read messages from the currently open thread.

    Uses ``div[role="group"][data-item-id]`` message groups.  Sender is
    identified by the presence of a host-profile button
    (``data-testid="message-thread-profile-link"`` or
    ``aria-label`` containing "Host").  Message text comes from the
    ``.t12j2ntd`` content div; timestamps from ``.d1fakvie`` spans.
    """
    messages: list[ChatMessage] = []
    await _async_sleep_ms(2000)

    # ── Wait for message list ──
    try:
        await page.wait_for_selector(
            '[data-testid="message-list"], '
            '[data-testid="message-thread-item-list-container"]',
            timeout=10000,
        )
    except Exception:
        logger.warning("Message list container not found in thread")
        return messages

    # ── Collect message groups (skip the "Start of Conversation" sentinel) ──
    msg_groups = await page.query_selector_all(
        'div[role="group"][data-item-id]:not([data-item-id="-1"])'
    )
    logger.debug("Found %d message group(s) in thread", len(msg_groups))

    for idx, group in enumerate(msg_groups):
        try:
            item_id = (await group.get_attribute("data-item-id")) or ""
            aria_label = (await group.get_attribute("aria-label")) or ""

            # ── Determine sender ──
            # Primary: host messages contain a profile-link button
            host_btn = await group.query_selector(
                'button[data-testid="message-thread-profile-link"], '
                'button[aria-label*="Host"]'
            )
            is_host = host_btn is not None

            # Secondary: cross-check sender name in aria-label
            sender_name = ""
            sm = _ARIA_SENDER_RE.search(aria_label)
            if sm:
                sender_name = sm.group(1)
                if host_name and sender_name.lower() == host_name.lower():
                    is_host = True

            sender = "host" if is_host else "user"

            # ── Message text ──
            text = ""
            text_el = await group.query_selector(".t12j2ntd")
            if text_el:
                text = (await text_el.inner_text()).strip()

            if not text:
                # Fallback: message-content-wrapper
                content_el = await group.query_selector(
                    '[data-name="message-content-wrapper"]'
                )
                if content_el:
                    text = (await content_el.inner_text()).strip()

            if not text and sender_name and " sent " in aria_label:
                # Last resort: extract from aria-label
                marker = f"{sender_name} sent "
                start = aria_label.find(marker)
                if start >= 0:
                    start += len(marker)
                    end = aria_label.rfind(". Sent ")
                    if end > start:
                        text = aria_label[start:end].replace("..", "\n")

            if not text:
                logger.debug(
                    "  Skipping message group #%d (item_id=%s): no text",
                    idx, item_id,
                )
                continue

            # ── Timestamp ──
            timestamp = ""
            tm = _ARIA_TIMESTAMP_RE.search(aria_label)
            if tm:
                timestamp = tm.group(1).strip()
            else:
                time_el = await group.query_selector("span.d1fakvie")
                if time_el:
                    timestamp = (await time_el.inner_text()).strip()

            messages.append(
                ChatMessage(sender=sender, text=text, timestamp=timestamp)
            )
            logger.debug(
                "  💬 Message #%d: sender=%s ts=%s text=%.80s…",
                idx + 1,
                sender,
                timestamp or "(none)",
                text,
            )
        except Exception as e:
            logger.warning("Error reading message group #%d: %s", idx, e)
            continue

    return messages


# ---------------------------------------------------------------------------
# Main fetch flow
# ---------------------------------------------------------------------------


async def fetch_inbox_chats(
    *, max_threads: int = 20, headless: bool = True
) -> list[ChatThread]:
    """Open the Airbnb inbox and return conversation threads.

    Requires a valid Airbnb session (run login first).
    """
    threads: list[ChatThread] = []

    async with async_playwright() as pw:
        context: Optional[BrowserContext] = None
        browser = None
        uses_cdp = False
        try:
            context, page, browser, uses_cdp = await open_airbnb_browser(
                pw, headless=headless
            )

            # Navigate to inbox
            inbox_url = f"{_airbnb_origin()}/hosting/inbox"
            logger.info("Opening Airbnb inbox: %s", inbox_url)
            await page.goto(
                inbox_url, wait_until="domcontentloaded", timeout=30000
            )
            await _async_sleep_ms(3000)

            # Check we landed on an inbox page — /hosting/inbox often
            # redirects to /hosting/messages/{thread_id} which still
            # renders the sidebar thread list we need.
            current_url = page.url
            logger.info("Current URL after navigation: %s", current_url)

            if (
                "/hosting/inbox" not in current_url
                and "/hosting/messages" not in current_url
                and "/messaging" not in current_url
            ):
                guest_inbox = f"{_airbnb_origin()}/messaging"
                logger.info(
                    "Redirected away from inbox — trying guest inbox: %s",
                    guest_inbox,
                )
                await page.goto(
                    guest_inbox,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await _async_sleep_ms(3000)
                logger.info("Current URL: %s", page.url)

            # ── Scrape thread list ──
            thread_meta = await _get_inbox_threads(page)
            logger.info("Found %d thread(s) in inbox", len(thread_meta))

            # ── Open each thread and read messages ──
            for i, meta in enumerate(thread_meta[:max_threads]):
                thread_id = meta.get("thread_id", "")
                host_name = meta.get("host_name", "Host")

                if not thread_id:
                    logger.warning(
                        "Skipping thread #%d: missing thread_id", i
                    )
                    continue

                try:
                    thread_url = (
                        f"{_airbnb_origin()}/messaging/thread/{thread_id}"
                    )
                    logger.info(
                        "Opening thread %s (%s): %s",
                        thread_id,
                        host_name,
                        thread_url,
                    )
                    await page.goto(
                        thread_url,
                        wait_until="domcontentloaded",
                        timeout=15000,
                    )
                    await _async_sleep_ms(2000)

                    messages = await _read_thread_messages(
                        page, host_name=host_name
                    )

                    thread = ChatThread(
                        thread_id=thread_id,
                        host_name=host_name,
                        messages=messages,
                        booking_status=meta.get("booking_status", ""),
                        location=meta.get("location", ""),
                    )
                    threads.append(thread)
                    logger.info(
                        "✅ Thread %s (%s): %d message(s) | status=%s | location=%s",
                        thread_id,
                        host_name,
                        len(messages),
                        thread.booking_status or "(none)",
                        thread.location or "(none)",
                    )
                except Exception as e:
                    logger.warning(
                        "Error reading thread %s (%s): %s",
                        thread_id,
                        host_name,
                        e,
                    )
                    continue

        except Exception as e:
            logger.error("Failed to fetch inbox chats: %s", e)
        finally:
            if context is not None:
                try:
                    await save_storage_state(context)
                except Exception:
                    pass
                await close_airbnb_session(context, browser, uses_cdp=uses_cdp)

    return threads


def fetch_inbox_chats_sync(
    *, max_threads: int = 20, headless: bool = True
) -> list[ChatThread]:
    """Synchronous wrapper for :func:`fetch_inbox_chats`."""
    return asyncio.run(
        fetch_inbox_chats(max_threads=max_threads, headless=headless)
    )
