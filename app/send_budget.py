"""The shared send budget.

Airbnb caps how many hosts you can message. v1 only counted first-touch
outreach, so switching the negotiator to auto-send would have silently doubled
the real send rate against an unchanged cap. Every outbound message — outreach
and negotiation alike — now reserves from the same window.

Negotiation still wins when both are queued, but that is a *priority* decision
made by the job queue, not an exemption from the budget.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from app.config import (
    get_outreach_max_sends_per_window,
    get_outreach_rate_window_seconds,
)
from app.database import (
    outreach_send_log_count_in_window,
    outreach_send_log_oldest_in_window,
    outreach_send_log_prune,
)
from app.outreach_quota import (
    record_successful_send,
    sleep_between_outreach_attempts,
    wait_until_send_allowed,
)
from app.messaging_errors import DeliveryUnconfirmed
from app.policy import sending_enabled, single_message_authorized

logger = logging.getLogger(__name__)


class SendingFrozen(RuntimeError):
    """Raised when the kill switch is engaged. Never retry past this."""


class Channel:
    """What kind of message is spending budget. Recorded for reporting only."""

    OUTREACH = "outreach"
    NEGOTIATION = "negotiation"
    FOLLOW_UP = "follow_up"


def budget_status(db_path: Optional[str] = None) -> dict:
    """Current window usage, for the dashboard and Planner backpressure."""
    window = float(get_outreach_rate_window_seconds())
    max_sends = get_outreach_max_sends_per_window()
    outreach_send_log_prune(db_path)
    used = outreach_send_log_count_in_window(db_path, window)
    oldest = outreach_send_log_oldest_in_window(db_path, window)
    return {
        "used": used,
        "max": max_sends,
        "remaining": max(0, max_sends - used),
        "window_seconds": window,
        "next_slot_at": (oldest + window) if oldest and used >= max_sends else None,
        "sending_enabled": sending_enabled(db_path),
    }


def remaining_sends(db_path: Optional[str] = None) -> int:
    """How many more messages fit in the current window."""
    return int(budget_status(db_path)["remaining"])


@asynccontextmanager
async def reserved_send(
    channel: str, db_path: Optional[str] = None, *, message_id: Optional[int] = None,
    authorization: Optional[str] = None,
):
    """Hold a budget slot for one message, recording it only if the body is sent.

    The kill switch is re-checked after the quota wait: that wait can last hours,
    and a freeze issued during it must still take effect.
    """
    def permitted() -> bool:
        if authorization:
            return single_message_authorized(message_id, authorization, db_path)
        return sending_enabled(db_path)

    if not permitted():
        raise SendingFrozen("kill switch engaged before send")

    await wait_until_send_allowed(db_path)

    if not permitted():
        raise SendingFrozen("kill switch engaged while waiting for a budget slot")

    started = time.time()
    try:
        yield
    except DeliveryUnconfirmed:
        # A submission with no receipt may still count against Airbnb's quota.
        record_successful_send(db_path)
        raise
    record_successful_send(db_path)
    logger.info(
        "Send budget: %s message consumed a slot (%.1fs in reservation), %d left",
        channel,
        time.time() - started,
        remaining_sends(db_path),
    )


__all__ = [
    "Channel",
    "SendingFrozen",
    "budget_status",
    "remaining_sends",
    "reserved_send",
    "record_successful_send",
    "sleep_between_outreach_attempts",
    "wait_until_send_allowed",
]
