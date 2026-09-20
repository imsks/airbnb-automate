"""The Chronicler: what happened, and what needs you.

You chose dashboard-only with no push notifications, which means this is the
single place a problem becomes visible. So the brief leads with the two queues
that need a human — deals ready to book and deals the Warden stopped — and
surfaces an anomaly banner when the system has gone quiet, because silence and
"nothing to report" look identical otherwise.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import deals as deal_repo
from app import jobs
from app.agent.runs import cost_by_agent
from app.database import get_connection
from app.models import DealState
from app.send_budget import budget_status

logger = logging.getLogger(__name__)

#: No successful send in this long, with work queued, means something is wrong.
QUIET_HOURS_BEFORE_ALARM = 24


def _since(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def north_star(db_path: Optional[str] = None) -> dict:
    """Deals closed per 100 messages sent — the only number that matters.

    Volume is capped by Airbnb, so the system can only improve by converting
    better, never by sending more.
    """
    conn = get_connection(db_path)
    try:
        sent = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE status = 'sent'"
        ).fetchone()["n"]
        replied = conn.execute(
            "SELECT COUNT(DISTINCT deal_id) AS n FROM messages WHERE direction = 'inbound'"
        ).fetchone()["n"]
        closed = conn.execute(
            """SELECT COUNT(*) AS n FROM deals
                WHERE state IN ('ready_to_book', 'booked', 'stayed', 'content_delivered')"""
        ).fetchone()["n"]
    finally:
        conn.close()

    return {
        "messages_sent": sent,
        "threads_with_replies": replied,
        "deals_closed": closed,
        "reply_rate_pct": round(100.0 * replied / sent, 1) if sent else 0.0,
        "closes_per_100_messages": round(100.0 * closed / sent, 1) if sent else 0.0,
    }


def reply_rate_by_prompt_version(db_path: Optional[str] = None) -> list[dict]:
    """Reply rate per prompt version — the lever that actually moves the metric."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT m.prompt_version,
                      COUNT(*) AS sent,
                      SUM(CASE WHEN EXISTS (
                            SELECT 1 FROM messages r
                             WHERE r.deal_id = m.deal_id
                               AND r.direction = 'inbound'
                               AND r.id > m.id
                          ) THEN 1 ELSE 0 END) AS replied
                 FROM messages m
                WHERE m.direction = 'outbound' AND m.status = 'sent'
                  AND m.prompt_version != ''
                GROUP BY m.prompt_version
                ORDER BY sent DESC"""
        ).fetchall()
    finally:
        conn.close()

    out = []
    for row in rows:
        sent = row["sent"] or 0
        replied = row["replied"] or 0
        out.append(
            {
                "prompt_version": row["prompt_version"],
                "sent": sent,
                "replied": replied,
                "reply_rate_pct": round(100.0 * replied / sent, 1) if sent else 0.0,
            }
        )
    return out


def recent_activity(hours: int = 24, db_path: Optional[str] = None) -> dict:
    """What moved in the last window."""
    conn = get_connection(db_path)
    try:
        cutoff = _since(hours)
        sent = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE status='sent' AND sent_at >= ?",
            (cutoff,),
        ).fetchone()["n"]
        received = conn.execute(
            """SELECT COUNT(*) AS n FROM messages
                WHERE direction='inbound' AND created_at >= ?""",
            (cutoff,),
        ).fetchone()["n"]
        blocked = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE status='blocked' AND created_at >= ?",
            (cutoff,),
        ).fetchone()["n"]
        transitions = conn.execute(
            """SELECT to_state, COUNT(*) AS n FROM deal_events
                WHERE created_at >= ? GROUP BY to_state""",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    return {
        "window_hours": hours,
        "messages_sent": sent,
        "host_replies": received,
        "messages_blocked": blocked,
        "transitions": {r["to_state"]: r["n"] for r in transitions},
    }


def action_queues(db_path: Optional[str] = None) -> dict:
    """The two lists that need you, since nothing here pushes a notification."""
    ready = deal_repo.get_deals_by_state(DealState.READY_TO_BOOK, db_path=db_path)
    blocked = deal_repo.get_deals_by_state(DealState.NEEDS_HUMAN, db_path=db_path)
    return {
        "ready_to_book": [
            {
                "deal_id": d.id,
                "host": d.host_name,
                "place": d.place_name,
                "location": d.location,
                "price": d.agreed_price_per_night,
                "discount_pct": d.agreed_discount_pct,
                "window": f"{d.agreed_window_start} → {d.agreed_window_end}".strip(" →"),
                "deliverables": d.agreed_deliverables,
                "url": d.booking_url or d.thread_url or d.listing_url,
            }
            for d in ready
        ],
        "needs_human": [
            {
                "deal_id": d.id,
                "host": d.host_name,
                "place": d.place_name,
                "reason": d.state_reason,
                "url": d.thread_url or d.listing_url,
            }
            for d in blocked
        ],
    }


def anomalies(db_path: Optional[str] = None) -> list[str]:
    """Conditions that would otherwise sit unnoticed until you open the app."""
    alerts: list[str] = []
    budget = budget_status(db_path)

    if not budget["sending_enabled"]:
        alerts.append("Kill switch is engaged — nothing is being sent.")

    activity = recent_activity(QUIET_HOURS_BEFORE_ALARM, db_path)
    depth = jobs.queue_depth(db_path)
    if activity["messages_sent"] == 0 and depth.get("pending", 0) > 0:
        alerts.append(
            f"No messages sent in {QUIET_HOURS_BEFORE_ALARM}h but "
            f"{depth['pending']} job(s) are queued — the worker may be stuck."
        )
    if depth.get("failed", 0):
        alerts.append(
            f"{depth['failed']} job(s) have exhausted their retries — use Retry "
            "failed jobs once the cause is fixed."
        )
    if activity["messages_blocked"]:
        alerts.append(
            f"The Warden blocked {activity['messages_blocked']} draft(s) in the last "
            f"{QUIET_HOURS_BEFORE_ALARM}h."
        )

    conn = get_connection(db_path)
    try:
        unlinked = conn.execute(
            """SELECT COUNT(*) AS n FROM deals
                WHERE state = 'contacted' AND thread_id IS NULL"""
        ).fetchone()["n"]
    finally:
        conn.close()
    if unlinked:
        alerts.append(
            f"{unlinked} contacted deal(s) have no linked thread — replies to them "
            "cannot be tracked."
        )
    return alerts


def daily_brief(db_path: Optional[str] = None) -> dict:
    """Everything the dashboard shows, assembled in one pass."""
    brief = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "north_star": north_star(db_path),
        "funnel": deal_repo.funnel_counts(db_path=db_path),
        "activity": recent_activity(24, db_path),
        "queues": action_queues(db_path),
        "anomalies": anomalies(db_path),
        "budget": budget_status(db_path),
        "jobs": jobs.queue_depth(db_path),
        "cost_by_agent": cost_by_agent(db_path),
        "prompt_performance": reply_rate_by_prompt_version(db_path),
    }
    logger.debug(
        "📰 Brief: %d ready to book, %d need a human, %d anomaly(ies)",
        len(brief["queues"]["ready_to_book"]),
        len(brief["queues"]["needs_human"]),
        len(brief["anomalies"]),
    )
    return brief
