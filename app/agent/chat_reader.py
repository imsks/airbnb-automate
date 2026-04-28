"""Read Airbnb inbox chats via Playwright browser automation.

This module opens the Airbnb messaging inbox using the persisted browser
session, scrapes conversation threads, and returns structured data that
the negotiation agent can act on.
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


def _airbnb_origin() -> str:
    return get_airbnb_base_url().rstrip("/")


async def _async_sleep_ms(ms: int) -> None:
    await asyncio.sleep(ms / 1000.0)


async def _get_inbox_threads(page: Page) -> list[dict]:
    """Scrape the inbox sidebar to get thread metadata."""
    threads: list[dict] = []

    # Wait for inbox to load
    await page.wait_for_selector(
        '[data-testid="messaging-inbox-list-item"], '
        '[class*="inbox"] a, '
        '[role="listitem"]',
        timeout=15000,
    )
    await _async_sleep_ms(2000)

    # Get all thread items from the inbox sidebar
    items = await page.query_selector_all(
        '[data-testid="messaging-inbox-list-item"], '
        '[class*="inbox"] a[href*="/messaging/thread/"], '
        'a[href*="/messaging/thread/"]'
    )

    for item in items:
        try:
            href = await item.get_attribute("href") or ""
            thread_id_match = re.search(r"/messaging/thread/(\d+)", href)
            thread_id = thread_id_match.group(1) if thread_id_match else ""

            text_content = (await item.inner_text()).strip()
            lines = [ln.strip() for ln in text_content.split("\n") if ln.strip()]
            host_name = lines[0] if lines else "Host"

            threads.append({
                "thread_id": thread_id,
                "host_name": host_name,
                "href": href,
            })
        except Exception as e:
            logger.debug("Error reading inbox item: %s", e)
            continue

    return threads


async def _read_thread_messages(page: Page) -> list[ChatMessage]:
    """Read messages from the currently open thread."""
    messages: list[ChatMessage] = []
    await _async_sleep_ms(2000)

    # Wait for messages to load
    try:
        await page.wait_for_selector(
            '[data-testid="message-row"], '
            '[class*="message"], '
            '[class*="chat-message"]',
            timeout=10000,
        )
    except Exception:
        logger.debug("No message elements found in thread")
        return messages

    # Try to get message elements
    msg_elements = await page.query_selector_all(
        '[data-testid="message-row"], '
        '[class*="MessageBody"], '
        '[class*="message-content"]'
    )

    for el in msg_elements:
        try:
            text = (await el.inner_text()).strip()
            if not text:
                continue

            # Determine sender — Airbnb typically styles user messages differently
            classes = (await el.get_attribute("class")) or ""
            parent = await el.evaluate_handle("el => el.parentElement")
            parent_classes = (await parent.evaluate("el => el.className || ''")) or ""

            # Heuristic: user messages are often right-aligned or have specific classes
            is_user = any(
                kw in (classes + " " + parent_classes).lower()
                for kw in ("outgoing", "sent", "self", "right", "you")
            )

            # Try to get timestamp
            time_el = await el.query_selector("time, [datetime], [class*='time']")
            timestamp = ""
            if time_el:
                timestamp = (await time_el.get_attribute("datetime")) or (
                    await time_el.inner_text()
                ).strip()

            messages.append(
                ChatMessage(
                    sender="user" if is_user else "host",
                    text=text,
                    timestamp=timestamp,
                )
            )
        except Exception as e:
            logger.debug("Error reading message: %s", e)
            continue

    return messages


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
            await page.goto(inbox_url, wait_until="domcontentloaded", timeout=30000)
            await _async_sleep_ms(3000)

            # If redirected to guest inbox, try that URL
            if "/hosting/inbox" not in page.url:
                guest_inbox = f"{_airbnb_origin()}/messaging"
                await page.goto(guest_inbox, wait_until="domcontentloaded", timeout=30000)
                await _async_sleep_ms(3000)

            # Get thread list from sidebar
            thread_meta = await _get_inbox_threads(page)
            logger.info("Found %d threads in inbox", len(thread_meta))

            for meta in thread_meta[:max_threads]:
                try:
                    # Click on the thread to open it
                    thread_url = meta.get("href", "")
                    if thread_url:
                        full_url = (
                            thread_url
                            if thread_url.startswith("http")
                            else f"{_airbnb_origin()}{thread_url}"
                        )
                        await page.goto(
                            full_url,
                            wait_until="domcontentloaded",
                            timeout=15000,
                        )
                        await _async_sleep_ms(2000)

                    messages = await _read_thread_messages(page)

                    thread = ChatThread(
                        thread_id=meta.get("thread_id", ""),
                        host_name=meta.get("host_name", "Host"),
                        messages=messages,
                    )
                    threads.append(thread)
                    logger.info(
                        "Read thread with %s: %d messages",
                        thread.host_name,
                        len(messages),
                    )
                except Exception as e:
                    logger.warning("Error reading thread %s: %s", meta.get("thread_id"), e)
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
    return asyncio.run(fetch_inbox_chats(max_threads=max_threads, headless=headless))
