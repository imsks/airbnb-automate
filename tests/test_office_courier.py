"""The office/courier split: who may lease what, and who may send.

The whole point of the split is that planning and drafting keep running with no
browser and no human, while delivery is quarantined to the one role that holds
the Airbnb session.
"""

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import deals as deal_repo
from app import jobs, leads as lead_repo
from app.database import get_connection, init_db
from app.jobs import COURIER_JOB_TYPES, OFFICE_JOB_TYPES, JobType, job_types_for_role
from app.models import DealState, MessageKind, MessageStatus
from app.worker import Worker

_REPOS = (
    "app.jobs",
    "app.deals",
    "app.leads",
    "app.territories",
    "app.campaigns",
    "app.policy",
    "app.agent.runs",
    "app.worker",
)


@pytest.fixture
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    for module in _REPOS:
        monkeypatch.setattr(
            f"{module}.get_connection", lambda p=None: get_connection(path), raising=False
        )
    monkeypatch.setenv("OUTREACH_MAX_SENDS_PER_WINDOW", "5")
    monkeypatch.setenv("OUTREACH_RATE_WINDOW_SECONDS", "3600")
    monkeypatch.setenv("OUTREACH_INTER_MESSAGE_DELAY_SECONDS", "0")
    conn = get_connection(path)
    try:
        conn.execute("INSERT INTO searches (location) VALUES ('Goa, India')")
        conn.execute(
            """INSERT INTO listings (id, search_id, title, host_name, location)
               VALUES ('L1', 1, 'Sea Villa', 'Asha', 'Goa, India')"""
        )
        conn.commit()
    finally:
        conn.close()
    yield path
    os.unlink(path)


# --- Role partitioning -----------------------------------------------------


def test_office_and_courier_job_types_are_disjoint():
    assert OFFICE_JOB_TYPES.isdisjoint(COURIER_JOB_TYPES)


def test_drafting_is_office_and_sending_is_courier():
    assert JobType.DRAFT_OUTREACH in OFFICE_JOB_TYPES
    assert JobType.SEND_OUTREACH in COURIER_JOB_TYPES
    assert JobType.PROPOSE_TERRITORIES in OFFICE_JOB_TYPES


def test_role_filter_returns_no_type_filter_for_all():
    assert job_types_for_role("all") is None
    assert job_types_for_role("office") == OFFICE_JOB_TYPES
    assert job_types_for_role("courier") == COURIER_JOB_TYPES


def test_office_worker_will_not_lease_a_browser_job(db):
    jobs.enqueue(JobType.SEND_OUTREACH, {"lead_id": 1}, idempotency_key="s1", db_path=db)
    office = Worker(db_path=db, role="office")
    leased = jobs.lease(office.name, types=office._lease_types, db_path=db)
    assert leased == []


def test_courier_worker_will_not_lease_an_office_job(db):
    jobs.enqueue(JobType.DRAFT_OUTREACH, {"lead_id": 1}, db_path=db)
    courier = Worker(db_path=db, role="courier")
    leased = jobs.lease(courier.name, types=courier._lease_types, db_path=db)
    assert leased == []


# --- The office never sends ------------------------------------------------


def _enriched_lead(db, listing_id="L1"):
    lead_id = lead_repo.upsert_lead(listing_id, db_path=db)
    lead_repo.save_enrichment(lead_id, {"description": "A sunlit villa."}, db_path=db)
    lead_repo.save_score(lead_id, 0.9, {}, db_path=db)
    return lead_id


def _mock_llm(text):
    response = MagicMock()
    response.content = text
    response.usage_metadata = {"input_tokens": 10, "output_tokens": 20}
    llm = MagicMock()
    llm.invoke.return_value = response
    llm.model_name = "gpt-4o-mini"
    return llm


def test_draft_outreach_stages_a_message_without_sending(db):
    lead_id = _enriched_lead(db)
    worker = Worker(db_path=db)
    job = MagicMock()
    job.id = 1
    job.payload = {"lead_id": lead_id, "listing_id": "L1"}

    send = AsyncMock()
    with patch("app.agent.scribe.get_llm", return_value=_mock_llm("Hi Asha! Lovely villa.")), patch(
        "app.outreach._send_message_to_host", send
    ):
        result = worker._draft_outreach(job)

    assert result["status"] == "ready"
    assert send.await_count == 0  # the office never touches the browser
    deal = deal_repo.get_deals_by_state(DealState.QUALIFIED, DealState.DISCOVERED, db_path=db)[0]
    staged = deal_repo.get_messages(deal.id, db)[-1]
    assert staged.status is MessageStatus.PENDING
    assert staged.kind is MessageKind.OUTREACH


# --- The courier only delivers what already exists --------------------------


def test_courier_send_requires_an_existing_draft(db):
    """With no staged draft, the courier composes nothing and sends nothing."""
    lead_id = _enriched_lead(db)

    from app.agent.scribe import deliver_outreach_for_lead

    compose = MagicMock()
    send = AsyncMock()
    with patch("app.agent.scribe.compose", compose), patch(
        "app.outreach._send_message_to_host", send
    ):
        result = asyncio.run(deliver_outreach_for_lead(lead_id, db_path=db))

    assert result["status"] == "no_draft"
    assert compose.call_count == 0
    assert send.await_count == 0


def test_courier_delivers_a_staged_draft(db):
    lead_id = _enriched_lead(db)
    deal_id = deal_repo.upsert_deal("L1", campaign_id=0, lead_id=lead_id, db_path=db)
    deal_repo.advance_to(deal_id, DealState.QUALIFIED, db_path=db)
    deal_repo.record_message(
        deal_id,
        "Hi Asha! Your villa looks lovely.",
        kind=MessageKind.OUTREACH,
        idempotency_key="outreach:0:L1",
        db_path=db,
    )

    from app.agent.scribe import deliver_outreach_for_lead

    @patch("app.agent.scribe.compose")
    def run(compose):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_page(headless=True):
            yield MagicMock()

        send = AsyncMock(return_value=("T900", "https://airbnb.com/thread/T900"))
        with patch("app.agent.scribe.airbnb_page", fake_page), patch(
            "app.outreach._send_message_to_host", send
        ):
            result = asyncio.run(deliver_outreach_for_lead(lead_id, db_path=db))
        assert compose.call_count == 0  # delivered the saved draft, never composed
        return result

    result = run()
    assert result["status"] == "sent"
    assert deal_repo.get_deal(deal_id, db).state is DealState.CONTACTED


def test_courier_sends_a_draft_the_kill_switch_had_paused(db):
    """A kill-switch hold is a pause. Once sending is on, that draft goes out."""
    from app import policy as policy_mod
    from app.agent.scribe import deliver_outreach_for_lead

    lead_id = _enriched_lead(db)
    deal_id = deal_repo.upsert_deal("L1", campaign_id=0, lead_id=lead_id, db_path=db)
    deal_repo.advance_to(deal_id, DealState.QUALIFIED, db_path=db)
    message_id = deal_repo.record_message(
        deal_id,
        "Hi Asha! Your villa looks lovely.",
        kind=MessageKind.OUTREACH,
        idempotency_key="outreach:0:L1",
        db_path=db,
    )
    policy_mod.freeze_sending("earlier send was unconfirmed", db)
    deal_repo.mark_message_blocked(
        message_id, "[kill_switch] sending is frozen by the kill switch", db
    )

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_page(headless=True):
        yield MagicMock()

    send = AsyncMock(return_value=("T900", "https://airbnb.com/thread/T900"))
    with patch("app.agent.scribe.airbnb_page", fake_page), patch(
        "app.outreach._send_message_to_host", send
    ):
        held = asyncio.run(deliver_outreach_for_lead(lead_id, db_path=db))
        assert held["status"] == "blocked"
        assert send.await_count == 0

        policy_mod.resume_sending(db)
        sent = asyncio.run(deliver_outreach_for_lead(lead_id, db_path=db))

    assert sent["status"] == "sent"
    assert send.await_count == 1
