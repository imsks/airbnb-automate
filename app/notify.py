"""Phone notifications for the three moments that actually need you.

The dashboard is the source of truth; this only pings you so you do not have to
watch it. It fires for exactly three things:

- a deal reaches ``ready_to_book`` — only you can pay;
- a deal lands in ``needs_human`` — a host wants something the rules forbid;
- the Airbnb session dies — the courier is blocked until you sign in again.

Notifications are opt-in. With no ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID``
this is a silent no-op that only logs, so tests and offline runs never touch the
network.
"""

from __future__ import annotations

import logging
import urllib.parse
import urllib.request
from enum import Enum
from typing import Optional

from app.config import get_telegram_credentials

logger = logging.getLogger(__name__)

_TELEGRAM_TIMEOUT_SECONDS = 10


class Moment(str, Enum):
    """The only events worth interrupting you for."""

    READY_TO_BOOK = "ready_to_book"
    NEEDS_HUMAN = "needs_human"
    SESSION_DEAD = "session_dead"


_PREFIX = {
    Moment.READY_TO_BOOK: "💳 Ready to book",
    Moment.NEEDS_HUMAN: "🙋 Needs you",
    Moment.SESSION_DEAD: "🔑 Sign in to Airbnb",
}


def notify(moment: Moment, text: str) -> bool:
    """Send one notification. Returns ``True`` only if it was actually delivered.

    Never raises: a failed ping must not crash the worker that was doing real
    work when it happened.
    """
    body = f"{_PREFIX.get(moment, '')}: {text}".strip()
    token, chat_id = get_telegram_credentials()
    if not token or not chat_id:
        logger.info("[notify:%s] %s", moment.value, text)
        return False

    try:
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": body}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=payload
        )
        with urllib.request.urlopen(request, timeout=_TELEGRAM_TIMEOUT_SECONDS) as response:
            ok = 200 <= getattr(response, "status", 200) < 300
        if ok:
            logger.info("[notify:%s] sent", moment.value)
        return ok
    except Exception as exc:  # noqa: BLE001 — a ping must never take down the caller
        logger.warning("[notify:%s] delivery failed: %s", moment.value, exc)
        return False


def notify_deal_state(state_value: str, deal) -> bool:
    """Ping for a deal that just entered a state only a human can move past."""
    host = getattr(deal, "host_name", "") or "a host"
    place = getattr(deal, "place_name", "") or getattr(deal, "location", "") or "a stay"
    if state_value == Moment.READY_TO_BOOK.value:
        return notify(Moment.READY_TO_BOOK, f"{host} — {place}. Your card is all that's left.")
    if state_value == Moment.NEEDS_HUMAN.value:
        reason = getattr(deal, "state_reason", "") or "a host asked for something the rules block"
        return notify(Moment.NEEDS_HUMAN, f"{host} — {place}. {reason}")
    return False


def notify_session_dead(reason: str = "") -> bool:
    """Ping that the courier is blocked until you sign in to Airbnb again."""
    detail = reason or "The Airbnb session expired."
    return notify(Moment.SESSION_DEAD, detail)
