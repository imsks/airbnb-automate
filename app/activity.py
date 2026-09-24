"""Durable activity feeds for the dashboard, split by loop.

The in-memory log ring buffer only reflects one process, so it goes dark the
moment the office and the courier run as separate containers. These feeds read
the ``jobs`` table instead — the shared, durable record of what each loop has
been doing — so the phone view is the same no matter which process serves it.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from app.database import get_connection
from app.jobs import COURIER_JOB_TYPES, OFFICE_JOB_TYPES, JobType

#: Short, human labels for each job type, phrased as an activity line.
_LABELS = {
    JobType.PROPOSE_TERRITORIES: "Proposing new places",
    JobType.RESEARCH_TERRITORY: "Researching a place",
    JobType.PLAN_ROUTE: "Planning the route",
    JobType.SCORE_LEAD: "Scoring a lead",
    JobType.DRAFT_OUTREACH: "Writing a draft",
    JobType.EXTRACT_TERMS: "Reading agreed terms",
    JobType.SWEEP_STALE: "Retiring stale threads",
    JobType.DAILY_BRIEF: "Building the brief",
    JobType.PLANNER_TICK: "Planning work",
    JobType.PULL_PORTAL_CONTEXT: "Pulling Booking.com context",
    JobType.DISCOVER_LEADS: "Discovering listings",
    JobType.ENRICH_LEAD: "Reading a listing page",
    JobType.SEND_OUTREACH: "Sending outreach",
    JobType.SYNC_INBOX: "Syncing the inbox",
    JobType.NEGOTIATE_DEAL: "Negotiating",
}


def _summarise(result_json: Optional[str], last_error: Optional[str], status: str) -> str:
    """One short line describing how a job turned out."""
    if status in ("pending", "leased"):
        return "queued" if status == "pending" else "running"
    if status in ("failed", "cancelled") and last_error:
        return last_error[:120]
    try:
        result: dict[str, Any] = json.loads(result_json or "{}")
    except (TypeError, ValueError):
        result = {}
    if not result:
        return status
    # Prefer the most human field the handlers tend to return.
    for key in ("territory", "status", "leads", "score", "proposed", "stops", "round"):
        if key in result:
            return f"{key}: {result[key]}"
    return status


def _feed(job_types, limit: int, db_path: Optional[str]) -> list[dict]:
    types = list(job_types)
    conn = get_connection(db_path)
    try:
        placeholders = ",".join("?" * len(types))
        rows = conn.execute(
            f"""SELECT type, status, result_json, last_error, updated_at
                  FROM jobs
                 WHERE type IN ({placeholders})
                 ORDER BY updated_at DESC, id DESC
                 LIMIT ?""",
            (*types, limit),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "type": row["type"],
            "label": _LABELS.get(row["type"], row["type"]),
            "status": row["status"],
            "detail": _summarise(row["result_json"], row["last_error"], row["status"]),
            "when": row["updated_at"],
        }
        for row in rows
    ]


def office_feed(limit: int = 20, db_path: Optional[str] = None) -> list[dict]:
    """What the office (planning, research, drafting) has been doing, newest first."""
    return _feed(OFFICE_JOB_TYPES, limit, db_path)


def courier_feed(limit: int = 20, db_path: Optional[str] = None) -> list[dict]:
    """What the courier (browser, sending, negotiating) has been doing, newest first."""
    return _feed(COURIER_JOB_TYPES, limit, db_path)
