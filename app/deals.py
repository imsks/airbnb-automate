"""Deal repository and state machine.

A Deal is one host relationship. It owns the link between an outreach message we
sent and the inbox thread it became — the edge v1 never recorded, and without
which nothing can be closed end to end.

Every state change goes through :func:`transition`, which refuses illegal moves
and appends to ``deal_events``. The dashboard funnel is derived from that log.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from app.database import get_connection
from app.models import (
    Deal,
    DealEvent,
    DealState,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
    allowed_deal_transitions,
)

logger = logging.getLogger(__name__)


class IllegalTransition(Exception):
    """Raised when a deal is moved to a state it cannot reach from its current one."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _row_to_deal(row: sqlite3.Row) -> Deal:
    return Deal(
        id=row["id"],
        lead_id=row["lead_id"],
        listing_id=row["listing_id"],
        campaign_id=row["campaign_id"],
        territory_id=row["territory_id"],
        host_name=row["host_name"] or "",
        place_name=row["place_name"] or "",
        location=row["location"] or "",
        listing_url=row["listing_url"] or "",
        thread_id=row["thread_id"],
        thread_url=row["thread_url"] or "",
        thread_linked_via=row["thread_linked_via"] or "",
        state=DealState(row["state"]),
        state_reason=row["state_reason"] or "",
        agent_reply_count=row["agent_reply_count"],
        follow_up_count=row["follow_up_count"],
        last_inbound_at=_parse_ts(row["last_inbound_at"]),
        last_outbound_at=_parse_ts(row["last_outbound_at"]),
        agreed_price_per_night=row["agreed_price_per_night"],
        agreed_currency=row["agreed_currency"] or "",
        agreed_discount_pct=row["agreed_discount_pct"],
        agreed_window_start=row["agreed_window_start"] or "",
        agreed_window_end=row["agreed_window_end"] or "",
        agreed_nights=row["agreed_nights"],
        agreed_deliverables=json.loads(row["agreed_deliverables_json"] or "[]"),
        terms_confidence=row["terms_confidence"],
        booking_url=row["booking_url"] or "",
    )


def _row_to_message(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        deal_id=row["deal_id"],
        direction=MessageDirection(row["direction"]),
        kind=MessageKind(row["kind"]),
        body=row["body"],
        status=MessageStatus(row["status"]),
        error=row["error"] or "",
        blocked_reason=row["blocked_reason"] or "",
        agent=row["agent"] or "",
        prompt_version=row["prompt_version"] or "",
        agent_run_id=row["agent_run_id"],
        idempotency_key=row["idempotency_key"],
        external_ts=row["external_ts"] or "",
        legacy_outreach_id=row["legacy_outreach_id"],
        created_at=_parse_ts(row["created_at"]) or datetime.now(timezone.utc),
        sent_at=_parse_ts(row["sent_at"]),
    )


# --- Deals -----------------------------------------------------------------


def upsert_deal(
    listing_id: str,
    *,
    campaign_id: int = 0,
    lead_id: Optional[int] = None,
    territory_id: Optional[int] = None,
    host_name: str = "",
    place_name: str = "",
    location: str = "",
    listing_url: str = "",
    db_path: Optional[str] = None,
) -> int:
    """Get or create the deal for a listing within a campaign. Returns its id."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM deals WHERE listing_id = ? AND campaign_id = ?",
            (listing_id, campaign_id),
        ).fetchone()
        if row:
            return int(row["id"])
        cursor = conn.execute(
            """INSERT INTO deals
               (lead_id, listing_id, campaign_id, territory_id, host_name,
                place_name, location, listing_url, state)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                lead_id,
                listing_id,
                campaign_id,
                territory_id,
                host_name,
                place_name,
                location,
                listing_url,
                DealState.DISCOVERED.value,
            ),
        )
        deal_id = int(cursor.lastrowid or 0)
        conn.execute(
            """INSERT INTO deal_events (deal_id, from_state, to_state, reason, actor)
               VALUES (?, '', ?, 'created', 'system')""",
            (deal_id, DealState.DISCOVERED.value),
        )
        conn.commit()
        return deal_id
    finally:
        conn.close()


def get_deal(deal_id: int, db_path: Optional[str] = None) -> Optional[Deal]:
    """Fetch one deal by id."""
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
        return _row_to_deal(row) if row else None
    finally:
        conn.close()


def get_deal_by_thread(thread_id: str, db_path: Optional[str] = None) -> Optional[Deal]:
    """Fetch the deal linked to an inbox thread."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM deals WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return _row_to_deal(row) if row else None
    finally:
        conn.close()


def get_deals_by_state(
    *states: DealState, campaign_id: Optional[int] = None, db_path: Optional[str] = None
) -> list[Deal]:
    """All deals currently in any of ``states``."""
    if not states:
        return []
    conn = get_connection(db_path)
    try:
        placeholders = ",".join("?" * len(states))
        sql = f"SELECT * FROM deals WHERE state IN ({placeholders})"
        params: list[Any] = [s.value for s in states]
        if campaign_id is not None:
            sql += " AND campaign_id = ?"
            params.append(campaign_id)
        sql += " ORDER BY updated_at DESC"
        return [_row_to_deal(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def transition(
    deal_id: int,
    to_state: DealState,
    *,
    reason: str = "",
    actor: str = "system",
    metadata: Optional[dict[str, Any]] = None,
    db_path: Optional[str] = None,
) -> Deal:
    """Move a deal to ``to_state``, appending a ``deal_events`` row.

    Raises :class:`IllegalTransition` if the move is not permitted from the
    deal's current state.
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM deals WHERE id = ?", (deal_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"No deal with id {deal_id}")

        from_state = DealState(row["state"])
        if to_state != from_state and to_state not in allowed_deal_transitions(from_state):
            raise IllegalTransition(
                f"Deal {deal_id}: {from_state.value} -> {to_state.value} is not allowed"
            )

        conn.execute(
            """UPDATE deals SET state = ?, state_reason = ?,
                      updated_at = CURRENT_TIMESTAMP
                WHERE id = ?""",
            (to_state.value, reason, deal_id),
        )
        conn.execute(
            """INSERT INTO deal_events
               (deal_id, from_state, to_state, reason, actor, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                deal_id,
                from_state.value,
                to_state.value,
                reason,
                actor,
                json.dumps(metadata or {}),
            ),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM deals WHERE id = ?", (deal_id,)
        ).fetchone()
        logger.info(
            "Deal %s: %s -> %s (%s)", deal_id, from_state.value, to_state.value, reason
        )
        return _row_to_deal(updated)
    finally:
        conn.close()


def advance_to(
    deal_id: int,
    target: DealState,
    *,
    reason: str = "",
    actor: str = "system",
    db_path: Optional[str] = None,
) -> Deal:
    """Walk the happy path forward to ``target``, skipping states already passed.

    Callers know the outcome ("this deal is now contacted") without having to
    know which intermediate states it still needs to cross.
    """
    order = [
        DealState.DISCOVERED,
        DealState.QUALIFIED,
        DealState.CONTACTED,
        DealState.HOST_REPLIED,
        DealState.NEGOTIATING,
        DealState.TERMS_AGREED,
        DealState.READY_TO_BOOK,
        DealState.BOOKED,
        DealState.STAYED,
        DealState.CONTENT_DELIVERED,
    ]
    deal = get_deal(deal_id, db_path)
    if deal is None:
        raise KeyError(f"No deal with id {deal_id}")
    if deal.state not in order or target not in order:
        return transition(
            deal_id, target, reason=reason, actor=actor, db_path=db_path
        )

    start, end = order.index(deal.state), order.index(target)
    if end <= start:
        return deal
    for state in order[start + 1 : end + 1]:
        deal = transition(deal_id, state, reason=reason, actor=actor, db_path=db_path)
    return deal


def escalate(
    deal_id: int,
    reason: str,
    *,
    actor: str = "warden",
    db_path: Optional[str] = None,
) -> Optional[Deal]:
    """Hand a deal to a human. Returns ``None`` if it is already terminal."""
    deal = get_deal(deal_id, db_path)
    if deal is None:
        raise KeyError(f"No deal with id {deal_id}")
    if DealState.NEEDS_HUMAN not in allowed_deal_transitions(deal.state):
        logger.warning(
            "Deal %s is %s and cannot be escalated: %s", deal_id, deal.state.value, reason
        )
        return None
    return transition(
        deal_id, DealState.NEEDS_HUMAN, reason=reason, actor=actor, db_path=db_path
    )


def escalate_or_close(
    deal_id: int,
    target: DealState,
    reason: str,
    *,
    actor: str = "system",
    db_path: Optional[str] = None,
) -> Optional[Deal]:
    """Move a deal to a terminal state, tolerating one that is already finished.

    Inbox syncs re-read the same dead threads every cycle, so a no-op is the
    correct outcome rather than an error.
    """
    deal = get_deal(deal_id, db_path)
    if deal is None:
        raise KeyError(f"No deal with id {deal_id}")
    if target not in allowed_deal_transitions(deal.state):
        return None
    return transition(deal_id, target, reason=reason, actor=actor, db_path=db_path)


def link_thread(
    deal_id: int,
    thread_id: str,
    *,
    thread_url: str = "",
    via: str = "send_capture",
    db_path: Optional[str] = None,
) -> bool:
    """Attach an inbox thread to a deal.

    Returns ``False`` if the thread is already linked to a different deal, which
    means the fuzzy matcher guessed wrong and a human should look.
    """
    conn = get_connection(db_path)
    try:
        existing = conn.execute(
            "SELECT id FROM deals WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if existing and int(existing["id"]) != deal_id:
            logger.warning(
                "Thread %s already linked to deal %s, refusing to relink to %s",
                thread_id,
                existing["id"],
                deal_id,
            )
            return False
        conn.execute(
            """UPDATE deals SET thread_id = ?, thread_url = ?, thread_linked_via = ?,
                      updated_at = CURRENT_TIMESTAMP
                WHERE id = ?""",
            (thread_id, thread_url, via, deal_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def set_terms(
    deal_id: int,
    *,
    price_per_night: Optional[float] = None,
    currency: str = "",
    discount_pct: Optional[float] = None,
    window_start: str = "",
    window_end: str = "",
    nights: Optional[int] = None,
    deliverables: Optional[list[str]] = None,
    confidence: Optional[float] = None,
    booking_url: str = "",
    db_path: Optional[str] = None,
) -> None:
    """Record the terms the Closer extracted from a thread."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """UPDATE deals
                  SET agreed_price_per_night = ?, agreed_currency = ?,
                      agreed_discount_pct = ?, agreed_window_start = ?,
                      agreed_window_end = ?, agreed_nights = ?,
                      agreed_deliverables_json = ?, terms_confidence = ?,
                      booking_url = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?""",
            (
                price_per_night,
                currency,
                discount_pct,
                window_start,
                window_end,
                nights,
                json.dumps(deliverables or []),
                confidence,
                booking_url,
                deal_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_events(deal_id: int, db_path: Optional[str] = None) -> list[DealEvent]:
    """The full transition history for a deal, oldest first."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM deal_events WHERE deal_id = ? ORDER BY id ASC", (deal_id,)
        ).fetchall()
        return [
            DealEvent(
                id=r["id"],
                deal_id=r["deal_id"],
                from_state=r["from_state"] or "",
                to_state=r["to_state"],
                reason=r["reason"] or "",
                actor=r["actor"] or "",
                metadata=json.loads(r["metadata_json"] or "{}"),
                created_at=_parse_ts(r["created_at"]) or datetime.now(timezone.utc),
            )
            for r in rows
        ]
    finally:
        conn.close()


def funnel_counts(
    campaign_id: Optional[int] = None, db_path: Optional[str] = None
) -> dict[str, int]:
    """Deal counts per state, for the dashboard funnel."""
    conn = get_connection(db_path)
    try:
        sql = "SELECT state, COUNT(*) AS n FROM deals"
        params: list[Any] = []
        if campaign_id is not None:
            sql += " WHERE campaign_id = ?"
            params.append(campaign_id)
        sql += " GROUP BY state"
        counts = {row["state"]: row["n"] for row in conn.execute(sql, params)}
        return {state.value: counts.get(state.value, 0) for state in DealState}
    finally:
        conn.close()


# --- Messages --------------------------------------------------------------


def record_message(
    deal_id: int,
    body: str,
    *,
    direction: MessageDirection = MessageDirection.OUTBOUND,
    kind: MessageKind = MessageKind.OUTREACH,
    status: MessageStatus = MessageStatus.PENDING,
    agent: str = "",
    prompt_version: str = "",
    agent_run_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    external_ts: str = "",
    legacy_outreach_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> int:
    """Append a message to a deal.

    When ``idempotency_key`` is already present the existing row's id is
    returned and nothing is written — this is what stops a job retry from
    sending a host a second copy.
    """
    conn = get_connection(db_path)
    try:
        if idempotency_key:
            row = conn.execute(
                "SELECT id FROM messages WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if row:
                return int(row["id"])
        cursor = conn.execute(
            """INSERT INTO messages
               (deal_id, direction, kind, body, status, agent, prompt_version,
                agent_run_id, idempotency_key, external_ts, legacy_outreach_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                deal_id,
                direction.value,
                kind.value,
                body,
                status.value,
                agent,
                prompt_version,
                agent_run_id,
                idempotency_key,
                external_ts,
                legacy_outreach_id,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def mark_message_sent(message_id: int, db_path: Optional[str] = None) -> None:
    """Flag a message as delivered and stamp the deal's last outbound time."""
    conn = get_connection(db_path)
    try:
        now = _now_iso()
        conn.execute(
            "UPDATE messages SET status = ?, sent_at = ?, error = '' WHERE id = ?",
            (MessageStatus.SENT.value, now, message_id),
        )
        conn.execute(
            """UPDATE deals SET last_outbound_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = (SELECT deal_id FROM messages WHERE id = ?)""",
            (now, message_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_message_failed(
    message_id: int, error: str, db_path: Optional[str] = None
) -> None:
    """Flag a message as undeliverable."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE messages SET status = ?, error = ? WHERE id = ?",
            (MessageStatus.FAILED.value, error[:2000], message_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_message_blocked(
    message_id: int, reason: str, db_path: Optional[str] = None
) -> None:
    """Flag a message the Warden refused to let through."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE messages SET status = ?, blocked_reason = ? WHERE id = ?",
            (MessageStatus.BLOCKED.value, reason[:2000], message_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_messages(deal_id: int, db_path: Optional[str] = None) -> list[Message]:
    """All messages on a deal, oldest first."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM messages WHERE deal_id = ? ORDER BY id ASC", (deal_id,)
        ).fetchall()
        return [_row_to_message(r) for r in rows]
    finally:
        conn.close()


def conversation_text(deal_id: int, db_path: Optional[str] = None) -> str:
    """The thread rendered for an LLM prompt, delivered messages only."""
    lines = []
    for msg in get_messages(deal_id, db_path):
        if msg.status in (MessageStatus.BLOCKED, MessageStatus.FAILED):
            continue
        speaker = "Host" if msg.direction == MessageDirection.INBOUND else "Me"
        lines.append(f"{speaker}: {msg.body.strip()}")
    return "\n\n".join(lines)


def count_agent_replies(deal_id: int, db_path: Optional[str] = None) -> int:
    """Delivered agent-authored replies on a thread — the per-thread reply cap."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM messages
                WHERE deal_id = ? AND direction = ? AND status = ? AND agent != ''""",
            (deal_id, MessageDirection.OUTBOUND.value, MessageStatus.SENT.value),
        ).fetchone()
        return int(row["n"]) if row else 0
    finally:
        conn.close()


def record_inbound(
    deal_id: int,
    body: str,
    *,
    external_ts: str = "",
    db_path: Optional[str] = None,
) -> Optional[int]:
    """Store a host message, skipping exact duplicates already on the thread."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            """SELECT id FROM messages
                WHERE deal_id = ? AND direction = ? AND body = ? AND external_ts = ?""",
            (deal_id, MessageDirection.INBOUND.value, body, external_ts),
        ).fetchone()
        if row:
            return None
        cursor = conn.execute(
            """INSERT INTO messages
               (deal_id, direction, kind, body, status, external_ts)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                deal_id,
                MessageDirection.INBOUND.value,
                MessageKind.HOST.value,
                body,
                MessageStatus.RECEIVED.value,
                external_ts,
            ),
        )
        conn.execute(
            """UPDATE deals SET last_inbound_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?""",
            (_now_iso(), deal_id),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()
