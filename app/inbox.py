"""Inbox synchronisation and in-thread replies.

Two jobs live here because both need the messaging UI:

- ``sync_inbox`` pulls host replies into deals, linking threads that predate
  send-time capture, and moves deals to HOST_REPLIED so the Closer can pick
  them up.
- ``send_reply`` delivers a prepared reply into an existing thread, gated by the
  Warden's verdict and the shared send budget.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from app import deals as deal_repo
from app import territories as territory_repo
from app.agent.chat_reader import fetch_inbox_chats
from app.browser_session import airbnb_page
from app.config import get_airbnb_base_url
from app.models import DealState, MessageStatus
from app.send_budget import Channel, reserved_send
from app.thread_linking import match_thread_to_deals

logger = logging.getLogger(__name__)

#: Statuses that mean the conversation is over, mirroring the v1 pre-filter.
DEAD_STATUSES = frozenset(
    {
        "invite expired",
        "dates are not available",
        "dates not available",
        "declined",
        "cancelled",
        "canceled",
        "withdrawn",
    }
)

_COMPOSER_SELECTORS = (
    '[data-testid="message-composer"] textarea',
    'textarea[aria-label*="message" i]',
    'textarea[placeholder*="message" i]',
    'div[role="textbox"][contenteditable="true"]',
    "textarea",
)


def thread_url_for(thread_id: str) -> str:
    """Canonical URL for an inbox thread."""
    return f"{get_airbnb_base_url().rstrip('/')}/messages/thread/{thread_id}"


def is_dead_status(status: str) -> bool:
    """Whether a booking status means the thread is finished."""
    lowered = (status or "").strip().lower()
    return any(dead in lowered for dead in DEAD_STATUSES)


def _thread_messages(thread) -> list:
    return list(getattr(thread, "messages", []) or [])


def absorb_thread(thread, *, db_path: Optional[str] = None) -> Optional[dict]:
    """Fold one scraped thread into its deal. Returns a summary, or ``None``.

    Threads that cannot be attributed to a deal are skipped rather than
    guessed at — see :mod:`app.thread_linking` for why.
    """
    thread_id = str(getattr(thread, "thread_id", "") or "")
    if not thread_id:
        return None

    deal = deal_repo.get_deal_by_thread(thread_id, db_path)
    if deal is None:
        candidates = deal_repo.get_deals_by_state(
            DealState.CONTACTED, DealState.QUALIFIED, db_path=db_path
        )
        matched = match_thread_to_deals(
            getattr(thread, "host_name", ""),
            getattr(thread, "listing_title", ""),
            candidates,
        )
        if matched is None:
            logger.info(
                "Inbox thread %s (%s) has no matching deal — skipping",
                thread_id,
                getattr(thread, "host_name", "?"),
            )
            return None
        deal_repo.link_thread(
            matched.id,
            thread_id,
            thread_url=thread_url_for(thread_id),
            via="fuzzy_match",
            db_path=db_path,
        )
        deal = deal_repo.get_deal(matched.id, db_path)

    new_inbound = 0
    last_sender = ""
    for message in _thread_messages(thread):
        sender = str(getattr(message, "sender", "") or "").lower()
        text = str(getattr(message, "text", "") or "").strip()
        last_sender = sender or last_sender
        if sender != "host" or not text:
            continue
        if deal_repo.record_inbound(
            deal.id,
            text,
            external_ts=str(getattr(message, "timestamp", "") or ""),
            db_path=db_path,
        ):
            new_inbound += 1

    status = str(getattr(thread, "booking_status", "") or "")
    if is_dead_status(status):
        deal_repo.escalate_or_close(
            deal.id, DealState.REJECTED, f"booking status: {status}", db_path=db_path
        )
        return {"deal_id": deal.id, "thread_id": thread_id, "status": "dead"}

    if new_inbound and last_sender == "host":
        deal_repo.advance_to(
            deal.id,
            DealState.HOST_REPLIED,
            reason=f"{new_inbound} new host message(s)",
            actor="inbox",
            db_path=db_path,
        )

    return {
        "deal_id": deal.id,
        "thread_id": thread_id,
        "new_inbound": new_inbound,
        "status": "updated",
    }


async def sync_inbox(
    *, max_threads: int = 20, headless: bool = True, db_path: Optional[str] = None
) -> dict:
    """Scrape the inbox and fold every thread into its deal."""
    threads = await fetch_inbox_chats(max_threads=max_threads, headless=headless)
    summary = {"threads": len(threads), "updated": 0, "skipped": 0, "new_inbound": 0}

    for thread in threads:
        result = absorb_thread(thread, db_path=db_path)
        if result is None:
            summary["skipped"] += 1
            continue
        summary["updated"] += 1
        summary["new_inbound"] += int(result.get("new_inbound", 0))

    logger.info(
        "📥 Inbox sync: %(threads)s thread(s), %(updated)s updated, "
        "%(new_inbound)s new host message(s), %(skipped)s unmatched",
        summary,
    )
    return summary


async def _fill_composer(page, text: str) -> bool:
    for selector in _COMPOSER_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            await locator.click(timeout=10_000)
            await locator.fill(text)
            return True
        except Exception:
            continue
    return False


async def _submit(page) -> bool:
    try:
        button = page.get_by_role("button", name=re.compile(r"^Send", re.I)).first
        if await button.count():
            await button.click(timeout=15_000)
            return True
    except Exception:
        pass
    try:
        await page.keyboard.press("Enter")
        return True
    except Exception:
        return False


async def send_reply_on_page(page, thread_id: str, text: str) -> None:
    """Type and send a reply in an open browser. Raises if delivery fails."""
    await page.goto(thread_url_for(thread_id), wait_until="domcontentloaded", timeout=60_000)
    try:
        await page.wait_for_load_state("load", timeout=30_000)
    except Exception:
        pass

    if not await _fill_composer(page, text):
        raise RuntimeError("Could not find the message composer in the thread")
    if not await _submit(page):
        raise RuntimeError("Could not submit the reply")


async def send_reply(
    deal_id: int,
    message_id: int,
    *,
    headless: bool = True,
    db_path: Optional[str] = None,
) -> dict:
    """Deliver a staged reply, consuming one slot of the shared send budget.

    The message must already have been reviewed by the Warden; a blocked draft
    is refused here as a second line of defence.
    """
    deal = deal_repo.get_deal(deal_id, db_path)
    if deal is None:
        raise KeyError(f"No deal with id {deal_id}")
    if not deal.thread_id:
        raise RuntimeError(f"Deal {deal_id} has no linked thread to reply in")

    staged = next(
        (m for m in deal_repo.get_messages(deal_id, db_path) if m.id == message_id), None
    )
    if staged is None:
        raise KeyError(f"No message with id {message_id} on deal {deal_id}")
    if staged.status is not MessageStatus.PENDING:
        return {"deal_id": deal_id, "status": staged.status.value, "sent": False}

    try:
        async with airbnb_page(headless=headless) as page:
            async with reserved_send(Channel.NEGOTIATION, db_path):
                await send_reply_on_page(page, deal.thread_id, staged.body)
        deal_repo.mark_message_sent(message_id, db_path)
        if deal.territory_id:
            territory_repo.record_send(deal.territory_id, db_path)
        logger.info("💬 Replied to %s on deal %s", deal.host_name, deal_id)
        return {"deal_id": deal_id, "status": "sent", "sent": True}
    except Exception as exc:
        deal_repo.mark_message_failed(message_id, str(exc), db_path)
        raise
