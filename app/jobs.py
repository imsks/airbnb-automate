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

    PROPOSE_TERRITORIES = "propose_territories"
    RESEARCH_TERRITORY = "research_territory"
    PLAN_ROUTE = "plan_route"
    DISCOVER_LEADS = "discover_leads"
    ENRICH_LEAD = "enrich_lead"
    SCORE_LEAD = "score_lead"
    DRAFT_OUTREACH = "draft_outreach"
    SEND_OUTREACH = "send_outreach"
    SYNC_INBOX = "sync_inbox"
    NEGOTIATE_DEAL = "negotiate_deal"
    EXTRACT_TERMS = "extract_terms"
    SWEEP_STALE = "sweep_stale"
    DAILY_BRIEF = "daily_brief"
    PLANNER_TICK = "planner_tick"
    PULL_PORTAL_CONTEXT = "pull_portal_context"


#: Job types that put a message in front of a host. These must always carry an
#: idempotency key — a retry that re-sends is damage we cannot undo.
SENDING_JOB_TYPES: frozenset[str] = frozenset(
    {JobType.SEND_OUTREACH, JobType.NEGOTIATE_DEAL}
)

#: The office plans and writes. None of these touch Playwright, so they keep
#: running even when the Airbnb session is dead. Drafting an opening message is
#: office work; *delivering* it is not.
OFFICE_JOB_TYPES: frozenset[str] = frozenset(
    {
        JobType.PROPOSE_TERRITORIES,
        JobType.RESEARCH_TERRITORY,
        JobType.PLAN_ROUTE,
        JobType.SCORE_LEAD,
        JobType.DRAFT_OUTREACH,
        JobType.EXTRACT_TERMS,
        JobType.SWEEP_STALE,
        JobType.DAILY_BRIEF,
        JobType.PLANNER_TICK,
        JobType.PULL_PORTAL_CONTEXT,
    }
)

#: The courier is the only role that holds the browser: it discovers and
#: enriches listings, delivers saved drafts, syncs the inbox and negotiates.
COURIER_JOB_TYPES: frozenset[str] = frozenset(
    {
        JobType.DISCOVER_LEADS,
        JobType.ENRICH_LEAD,
        JobType.SEND_OUTREACH,
        JobType.SYNC_INBOX,
        JobType.NEGOTIATE_DEAL,
    }
)


def job_types_for_role(role: str) -> Optional[frozenset[str]]:
    """The job types a worker of ``role`` may lease.

    ``"all"`` (the local single-process default) leases everything, so it
    returns ``None`` — the sentinel :func:`lease` reads as "no type filter".
    """
    normalised = (role or "all").strip().lower()
    if normalised == "office":
        return OFFICE_JOB_TYPES
    if normalised == "courier":
        return COURIER_JOB_TYPES
    return None


class Priority:
    """Lower runs first. A warm thread outranks any cold outreach."""

    NEGOTIATION = 10
    INBOX_SYNC = 20
    OUTREACH = 50
    #: Writing a draft outranks discovery/enrichment so the courier rarely
    #: starves, but stays below a live send.
    DRAFTING = 60
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


def reopen_paused_send(
    idempotency_key: str, db_path: Optional[str] = None
) -> Optional[int]:
    """Run a finished send job again when the kill switch parked it unsent.

    A job that completed as ``blocked`` never reached a host. Leaving it
    ``done`` would make the idempotency key swallow every later attempt.
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT id, status, result_json, last_error FROM jobs WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is None or row["status"] not in (JobStatus.DONE.value, JobStatus.FAILED.value):
            return None
        result = json.loads(row["result_json"] or "{}")
        reason = f"{result.get('reason') or ''} {row['last_error'] or ''}"
        if result.get("status") == "sent" or "kill_switch" not in reason:
            return None
        conn.execute(
            """UPDATE jobs
                  SET status = ?, result_json = '{}', last_error = '',
                      attempts = 0, run_after = ?, lease_until = NULL,
                      lease_owner = '', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?""",
            (JobStatus.PENDING.value, time.time(), row["id"]),
        )
        conn.commit()
        return int(row["id"])
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
            # Release the key. It exists to stop duplicate *queued* work, not to
            # poison a lead forever — without this, a job that failed under a bug
            # can never be retried once the bug is fixed. Double sends are
            # prevented separately by messages.idempotency_key.
            conn.execute(
                """UPDATE jobs
                      SET status = ?, last_error = ?, lease_until = NULL,
                          lease_owner = '', idempotency_key = NULL,
                          updated_at = CURRENT_TIMESTAMP
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


def defer_pending(job_type: str, until: float, db_path: Optional[str] = None) -> int:
    """Hold queued and in-flight jobs of one type until ``until``.

    Used when Airbnb's own cap is up, so the courier stops clicking Send.
    Finished jobs are left as they are. Returns how many rows moved.
    """
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """UPDATE jobs
                  SET status = ?, run_after = ?, lease_until = NULL,
                      lease_owner = '', updated_at = CURRENT_TIMESTAMP
                WHERE type = ? AND status IN (?, ?) AND run_after < ?""",
            (
                JobStatus.PENDING.value,
                until,
                job_type,
                JobStatus.PENDING.value,
                JobStatus.LEASED.value,
                until,
            ),
        )
        conn.commit()
        return cursor.rowcount
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


def cancel_for_deal(deal_id: int, reason: str = "", db_path: Optional[str] = None) -> int:
    """Cancel every not-yet-finished job attached to a deal. Returns the count.

    Backs the Needs-a-human "Kill" action: stop the office from doing any more
    work on a deal a human has decided to abandon.
    """
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """UPDATE jobs
                  SET status = ?, last_error = ?, lease_until = NULL,
                      lease_owner = '', updated_at = CURRENT_TIMESTAMP
                WHERE deal_id = ? AND status IN (?, ?)""",
            (
                JobStatus.CANCELLED.value,
                (reason or "deal killed by human")[:2000],
                deal_id,
                JobStatus.PENDING.value,
                JobStatus.LEASED.value,
            ),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def retry_failed(job_type: Optional[str] = None, db_path: Optional[str] = None) -> int:
    """Put terminally-failed jobs back in the queue. Returns how many were reset."""
    conn = get_connection(db_path)
    try:
        sql = (
            "UPDATE jobs SET status = ?, attempts = 0, run_after = 0, "
            "last_error = '', updated_at = CURRENT_TIMESTAMP WHERE status = ?"
        )
        params: list[Any] = [JobStatus.PENDING.value, JobStatus.FAILED.value]
        if job_type:
            sql += " AND type = ?"
            params.append(job_type)
        cursor = conn.execute(sql, params)
        conn.commit()
        if cursor.rowcount:
            logger.info("Requeued %d failed job(s)", cursor.rowcount)
        return cursor.rowcount
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


def count_active(job_type: str, db_path: Optional[str] = None) -> int:
    """How many jobs of ``job_type`` are pending or leased right now.

    The planner uses this to keep at most one open proposal in flight rather
    than piling identical "think of new places" jobs onto the queue.
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM jobs
                WHERE type = ? AND status IN (?, ?)""",
            (job_type, JobStatus.PENDING.value, JobStatus.LEASED.value),
        ).fetchone()
        return int(row["n"]) if row else 0
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
