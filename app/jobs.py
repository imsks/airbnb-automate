"""Durable job queue.

Enqueueing jobs is how the office multiplies work: the Planner emits research
jobs, research emits discovery jobs, discovery emits scoring jobs, and so on.
Roles are fixed; the *work* is what spawns.

Leases are held by a single worker process today, but claiming uses
``BEGIN IMMEDIATE`` so a second worker can never grab the same row.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import time
import uuid
from typing import Any, Iterable, Optional

from app.database import get_connection
from app.models import Job, JobStatus

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 600
DEFAULT_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 30.0
_BACKOFF_CAP_SECONDS = 3600.0


class JobType:
    """Canonical job type names. Workers dispatch on these."""

    RESEARCH_TERRITORY = "research_territory"
    PLAN_ROUTE = "plan_route"
    DISCOVER_LEADS = "discover_leads"
    ENRICH_LEAD = "enrich_lead"
    SCORE_LEAD = "score_lead"
    SEND_OUTREACH = "send_outreach"
    SYNC_INBOX = "sync_inbox"
    NEGOTIATE_DEAL = "negotiate_deal"
    EXTRACT_TERMS = "extract_terms"
    SWEEP_STALE = "sweep_stale"
    DAILY_BRIEF = "daily_brief"
    PLANNER_TICK = "planner_tick"


#: Job types that put a message in front of a host. These must always carry an
#: idempotency key — a retry that re-sends is damage we cannot undo.
SENDING_JOB_TYPES: frozenset[str] = frozenset(
    {JobType.SEND_OUTREACH, JobType.NEGOTIATE_DEAL}
)


class Priority:
    """Lower runs first. A warm thread outranks any cold outreach."""

    NEGOTIATION = 10
    INBOX_SYNC = 20
    OUTREACH = 50
    ENRICHMENT = 70
    RESEARCH = 90
    HOUSEKEEPING = 200


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        type=row["type"],
        payload=json.loads(row["payload_json"] or "{}"),
        status=JobStatus(row["status"]),
        priority=row["priority"],
        run_after=row["run_after"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        idempotency_key=row["idempotency_key"],
        lease_until=row["lease_until"],
        lease_owner=row["lease_owner"] or "",
        last_error=row["last_error"] or "",
        result=json.loads(row["result_json"] or "{}"),
        campaign_id=row["campaign_id"],
        deal_id=row["deal_id"],
    )


def enqueue(
    job_type: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    priority: int = Priority.OUTREACH,
    delay_seconds: float = 0.0,
    idempotency_key: Optional[str] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    campaign_id: Optional[int] = None,
    deal_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> Optional[int]:
    """Queue a job. Returns its id, or ``None`` if the idempotency key already exists.

    Raises ``ValueError`` for a sending job without an idempotency key.
    """
    if job_type in SENDING_JOB_TYPES and not idempotency_key:
        raise ValueError(
            f"{job_type!r} sends a message to a host and requires an idempotency_key"
        )

    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO jobs
               (type, payload_json, status, priority, run_after, max_attempts,
                idempotency_key, campaign_id, deal_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_type,
                json.dumps(payload or {}),
                JobStatus.PENDING.value,
                priority,
                time.time() + max(0.0, delay_seconds),
                max_attempts,
                idempotency_key,
                campaign_id,
                deal_id,
            ),
        )
        conn.commit()
        if cursor.rowcount == 0:
            logger.debug("Job %s skipped: idempotency key %s exists", job_type, idempotency_key)
            return None
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def reclaim_expired_leases(db_path: Optional[str] = None) -> int:
    """Return crashed-worker jobs to the pending pool. Returns how many were freed."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """UPDATE jobs
                  SET status = ?, lease_until = NULL, lease_owner = '',
                      updated_at = CURRENT_TIMESTAMP
                WHERE status = ? AND lease_until IS NOT NULL AND lease_until < ?""",
            (JobStatus.PENDING.value, JobStatus.LEASED.value, time.time()),
        )
        conn.commit()
        if cursor.rowcount:
            logger.warning("Reclaimed %d expired job lease(s)", cursor.rowcount)
        return cursor.rowcount
    finally:
        conn.close()


def lease(
    owner: Optional[str] = None,
    *,
    types: Optional[Iterable[str]] = None,
    limit: int = 1,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    db_path: Optional[str] = None,
) -> list[Job]:
    """Claim up to ``limit`` runnable jobs, highest priority and oldest first."""
    owner = owner or f"worker-{uuid.uuid4().hex[:8]}"
    now = time.time()
    conn = get_connection(db_path)
    # Explicit transaction control: the driver's implicit BEGIN is deferred, which
    # would let two workers read the same row before either writes.
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        sql = (
            "SELECT * FROM jobs WHERE status = ? AND run_after <= ?"
        )
        params: list[Any] = [JobStatus.PENDING.value, now]
        type_list = list(types) if types else []
        if type_list:
            sql += f" AND type IN ({','.join('?' * len(type_list))})"
            params.extend(type_list)
        sql += " ORDER BY priority ASC, id ASC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        leased: list[Job] = []
        for row in rows:
            conn.execute(
                """UPDATE jobs
                      SET status = ?, lease_until = ?, lease_owner = ?,
                          attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?""",
                (JobStatus.LEASED.value, now + lease_seconds, owner, row["id"]),
            )
            job = _row_to_job(row)
            job.status = JobStatus.LEASED
            job.attempts = row["attempts"] + 1
            job.lease_owner = owner
            job.lease_until = now + lease_seconds
            leased.append(job)
        conn.execute("COMMIT")
        return leased
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def complete(
    job_id: int,
    result: Optional[dict[str, Any]] = None,
    db_path: Optional[str] = None,
) -> None:
    """Mark a job done."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """UPDATE jobs
                  SET status = ?, result_json = ?, lease_until = NULL,
                      lease_owner = '', last_error = '',
                      updated_at = CURRENT_TIMESTAMP
                WHERE id = ?""",
            (JobStatus.DONE.value, json.dumps(result or {}), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def _backoff_seconds(attempts: int) -> float:
    delay = min(_BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)), _BACKOFF_CAP_SECONDS)
    return delay + random.uniform(0, delay * 0.25)


def fail(job_id: int, error: str, db_path: Optional[str] = None) -> JobStatus:
    """Record a failure: reschedule with backoff, or give up at ``max_attempts``."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT attempts, max_attempts FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"No job with id {job_id}")

        if row["attempts"] >= row["max_attempts"]:
            new_status = JobStatus.FAILED
            conn.execute(
                """UPDATE jobs
                      SET status = ?, last_error = ?, lease_until = NULL,
                          lease_owner = '', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?""",
                (new_status.value, error[:2000], job_id),
            )
        else:
            new_status = JobStatus.PENDING
            conn.execute(
                """UPDATE jobs
                      SET status = ?, last_error = ?, run_after = ?,
                          lease_until = NULL, lease_owner = '',
                          updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?""",
                (
                    new_status.value,
                    error[:2000],
                    time.time() + _backoff_seconds(row["attempts"]),
                    job_id,
                ),
            )
        conn.commit()
        return new_status
    finally:
        conn.close()


def cancel(job_id: int, reason: str = "", db_path: Optional[str] = None) -> None:
    """Cancel a job so it is never retried."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """UPDATE jobs
                  SET status = ?, last_error = ?, lease_until = NULL,
                      lease_owner = '', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?""",
            (JobStatus.CANCELLED.value, reason[:2000], job_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_job(job_id: int, db_path: Optional[str] = None) -> Optional[Job]:
    """Fetch one job by id."""
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None
    finally:
        conn.close()


def queue_depth(db_path: Optional[str] = None) -> dict[str, int]:
    """Job counts by status, for the dashboard."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}
    finally:
        conn.close()
