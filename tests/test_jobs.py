"""Tests for the durable job queue."""

import os
import tempfile
import time

import pytest

from app import jobs
from app.database import init_db
from app.jobs import JobType, Priority
from app.models import JobStatus


@pytest.fixture
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    monkeypatch.setattr("app.jobs.get_connection", lambda p=None: _conn(path))
    yield path
    os.unlink(path)


def _conn(path):
    from app.database import get_connection

    return get_connection(path)


def test_enqueue_returns_job_id(db):
    job_id = jobs.enqueue(JobType.RESEARCH_TERRITORY, {"name": "Goa, India"})
    assert job_id
    job = jobs.get_job(job_id)
    assert job.type == JobType.RESEARCH_TERRITORY
    assert job.payload == {"name": "Goa, India"}
    assert job.status is JobStatus.PENDING


def test_a_kill_switch_block_can_be_sent_again(db):
    job_id = jobs.enqueue(
        JobType.SEND_OUTREACH, {"lead_id": 1}, idempotency_key="outreach:1:L1"
    )
    jobs.complete(
        job_id,
        {
            "status": "blocked",
            "reason": "[kill_switch] sending is frozen by the kill switch",
        },
    )
    assert jobs.enqueue(
        JobType.SEND_OUTREACH, {"lead_id": 1}, idempotency_key="outreach:1:L1"
    ) is None
    assert jobs.reopen_paused_send("outreach:1:L1") == job_id
    assert jobs.get_job(job_id).status is JobStatus.PENDING


def test_a_message_that_was_actually_sent_is_not_reopened(db):
    job_id = jobs.enqueue(
        JobType.SEND_OUTREACH, {"lead_id": 1}, idempotency_key="outreach:1:L2"
    )
    jobs.complete(job_id, {"status": "sent"})
    assert jobs.reopen_paused_send("outreach:1:L2") is None
    assert jobs.get_job(job_id).status is JobStatus.DONE


def test_duplicate_idempotency_key_is_ignored(db):
    first = jobs.enqueue(JobType.ENRICH_LEAD, {"lead_id": 1}, idempotency_key="lead-1")
    second = jobs.enqueue(JobType.ENRICH_LEAD, {"lead_id": 1}, idempotency_key="lead-1")
    assert first
    assert second is None
    assert jobs.queue_depth()["pending"] == 1


def test_sending_job_without_idempotency_key_is_rejected(db):
    """A retried send that lacks a key would message a host twice."""
    with pytest.raises(ValueError, match="idempotency_key"):
        jobs.enqueue(JobType.SEND_OUTREACH, {"deal_id": 1})
    with pytest.raises(ValueError, match="idempotency_key"):
        jobs.enqueue(JobType.NEGOTIATE_DEAL, {"deal_id": 1})


def test_sending_job_with_idempotency_key_is_accepted(db):
    assert jobs.enqueue(
        JobType.SEND_OUTREACH, {"deal_id": 1}, idempotency_key="send-deal-1"
    )


def test_lease_orders_by_priority_then_age(db):
    jobs.enqueue(JobType.RESEARCH_TERRITORY, priority=Priority.RESEARCH)
    jobs.enqueue(JobType.SEND_OUTREACH, priority=Priority.OUTREACH, idempotency_key="a")
    jobs.enqueue(JobType.NEGOTIATE_DEAL, priority=Priority.NEGOTIATION, idempotency_key="b")

    leased = jobs.lease("w1", limit=3)
    assert [j.type for j in leased] == [
        JobType.NEGOTIATE_DEAL,
        JobType.SEND_OUTREACH,
        JobType.RESEARCH_TERRITORY,
    ]


def test_lease_marks_leased_and_counts_attempt(db):
    job_id = jobs.enqueue(JobType.SYNC_INBOX)
    (leased,) = jobs.lease("w1")
    assert leased.id == job_id
    assert leased.status is JobStatus.LEASED
    assert leased.attempts == 1
    assert leased.lease_owner == "w1"
    assert jobs.get_job(job_id).status is JobStatus.LEASED


def test_leased_job_is_not_handed_out_twice(db):
    jobs.enqueue(JobType.SYNC_INBOX)
    assert len(jobs.lease("w1")) == 1
    assert jobs.lease("w2") == []


def test_job_scheduled_in_the_future_is_not_leasable(db):
    jobs.enqueue(JobType.DAILY_BRIEF, delay_seconds=60)
    assert jobs.lease("w1") == []


def test_lease_can_filter_by_type(db):
    jobs.enqueue(JobType.SYNC_INBOX)
    jobs.enqueue(JobType.RESEARCH_TERRITORY)
    leased = jobs.lease("w1", types=[JobType.RESEARCH_TERRITORY], limit=5)
    assert [j.type for j in leased] == [JobType.RESEARCH_TERRITORY]


def test_complete_records_result(db):
    job_id = jobs.enqueue(JobType.SCORE_LEAD)
    jobs.lease("w1")
    jobs.complete(job_id, {"score": 0.8})
    job = jobs.get_job(job_id)
    assert job.status is JobStatus.DONE
    assert job.result == {"score": 0.8}


def test_failure_below_max_attempts_reschedules_with_backoff(db):
    job_id = jobs.enqueue(JobType.SCORE_LEAD, max_attempts=3)
    jobs.lease("w1")
    assert jobs.fail(job_id, "boom") is JobStatus.PENDING
    job = jobs.get_job(job_id)
    assert job.status is JobStatus.PENDING
    assert job.last_error == "boom"
    assert job.run_after > time.time()


def test_failure_at_max_attempts_gives_up(db):
    job_id = jobs.enqueue(JobType.SCORE_LEAD, max_attempts=2)
    jobs.lease("w1")
    jobs.fail(job_id, "one")
    # Backoff pushes run_after forward, so lease again by hand.
    jobs.lease("w1")  # no-op; job is not yet runnable
    _force_runnable(db, job_id)
    jobs.lease("w1")
    assert jobs.fail(job_id, "two") is JobStatus.FAILED
    assert jobs.get_job(job_id).status is JobStatus.FAILED


def test_a_terminally_failed_job_releases_its_idempotency_key(db):
    """Otherwise a job that failed under a bug can never be queued again once
    the bug is fixed, and its lead is stranded forever."""
    job_id = jobs.enqueue(JobType.ENRICH_LEAD, {"lead_id": 7}, max_attempts=1, idempotency_key="enrich:7")
    jobs.lease("w1")
    assert jobs.fail(job_id, "browser exploded") is JobStatus.FAILED
    assert jobs.get_job(job_id).idempotency_key is None

    requeued = jobs.enqueue(
        JobType.ENRICH_LEAD, {"lead_id": 7}, idempotency_key="enrich:7"
    )
    assert requeued and requeued != job_id


def test_retry_failed_requeues_everything(db):
    first = jobs.enqueue(JobType.ENRICH_LEAD, max_attempts=1)
    second = jobs.enqueue(JobType.SCORE_LEAD, max_attempts=1)
    for job_id in (first, second):
        jobs.lease("w1")
        jobs.fail(job_id, "boom")

    assert jobs.retry_failed() == 2
    assert jobs.get_job(first).status is JobStatus.PENDING
    assert jobs.get_job(first).attempts == 0


def test_retry_failed_can_target_one_type(db):
    first = jobs.enqueue(JobType.ENRICH_LEAD, max_attempts=1)
    second = jobs.enqueue(JobType.SCORE_LEAD, max_attempts=1)
    for job_id in (first, second):
        jobs.lease("w1")
        jobs.fail(job_id, "boom")

    assert jobs.retry_failed(JobType.ENRICH_LEAD) == 1
    assert jobs.get_job(first).status is JobStatus.PENDING
    assert jobs.get_job(second).status is JobStatus.FAILED


def _force_runnable(path, job_id):
    conn = _conn(path)
    try:
        conn.execute("UPDATE jobs SET run_after = 0 WHERE id = ?", (job_id,))
        conn.commit()
    finally:
        conn.close()


def test_expired_lease_is_reclaimed(db):
    job_id = jobs.enqueue(JobType.SYNC_INBOX)
    jobs.lease("crashed-worker", lease_seconds=-1)
    assert jobs.reclaim_expired_leases() == 1
    assert jobs.get_job(job_id).status is JobStatus.PENDING
    assert len(jobs.lease("w2")) == 1


def test_cancel_removes_job_from_rotation(db):
    job_id = jobs.enqueue(JobType.SYNC_INBOX)
    jobs.cancel(job_id, "no longer needed")
    assert jobs.get_job(job_id).status is JobStatus.CANCELLED
    assert jobs.lease("w1") == []


def test_defer_pending_holds_queued_sends_and_leaves_finished_ones(db):
    waiting = jobs.enqueue(
        JobType.SEND_OUTREACH, {"lead_id": 1}, idempotency_key="outreach:1"
    )
    finished = jobs.enqueue(JobType.RESEARCH_TERRITORY, {"name": "Goa"})
    jobs.complete(finished)
    jobs.lease("courier", types=[JobType.SEND_OUTREACH])
    until = time.time() + 1000
    assert jobs.defer_pending(JobType.SEND_OUTREACH, until) == 1
    held = jobs.get_job(waiting)
    assert held.status is JobStatus.PENDING
    assert held.run_after == pytest.approx(until, abs=1)
    assert held.lease_owner == ""
    assert jobs.get_job(finished).status is JobStatus.DONE
    assert jobs.lease("courier", types=[JobType.SEND_OUTREACH]) == []


def test_queue_depth_reports_counts_by_status(db):
    jobs.enqueue(JobType.SYNC_INBOX)
    second = jobs.enqueue(JobType.SCORE_LEAD)
    jobs.lease("w1", types=[JobType.SCORE_LEAD])
    jobs.complete(second)
    depth = jobs.queue_depth()
    assert depth["pending"] == 1
    assert depth["done"] == 1
