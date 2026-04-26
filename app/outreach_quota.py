"""Global sliding-window limit for successful host messages (all searches, one cap)."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

from app.config import (
    get_outreach_inter_message_delay_seconds,
    get_outreach_max_sends_per_window,
    get_outreach_rate_window_seconds,
)
from app.database import (
    outreach_send_log_count_in_window,
    outreach_send_log_oldest_in_window,
    outreach_send_log_prune,
    outreach_send_log_record,
)

logger = logging.getLogger(__name__)


async def wait_until_send_allowed(db_path: Optional[str] = None) -> None:
    """Block until a new send is allowed under the sliding window (or return immediately)."""
    max_s = get_outreach_max_sends_per_window()
    window = float(get_outreach_rate_window_seconds())
    while True:
        outreach_send_log_prune(db_path)
        n = outreach_send_log_count_in_window(db_path, window)
        if n < max_s:
            return
        oldest = outreach_send_log_oldest_in_window(db_path, window)
        if oldest is None:
            return
        buffer = 5.0 + random.uniform(0, 25)
        wait_s = max(1.0, oldest + window - time.time() + buffer)
        logger.warning(
            "Outreach quota: %s/%s sends in the last %.1fh — sleeping %.0fs for next slot",
            n,
            max_s,
            window / 3600.0,
            wait_s,
        )
        await asyncio.sleep(wait_s)


def record_successful_send(db_path: Optional[str] = None) -> None:
    """Call after Airbnb accepts a message (counts toward the sliding window)."""
    outreach_send_log_record(db_path)


async def sleep_between_outreach_attempts() -> None:
    """Spread attempts out even within a quota window."""
    base = get_outreach_inter_message_delay_seconds()
    if base <= 0:
        return
    jitter = random.uniform(0, min(45.0, base * 0.25))
    await asyncio.sleep(base + jitter)
