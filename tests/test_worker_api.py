"""Integration tests: worker dispatch, planner backpressure, closer, API, brief."""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import campaigns as campaign_repo
from app import deals as deal_repo
from app import jobs, leads as lead_repo, policy as policy_mod
from app import territories as territory_repo
from app.agent import planner
from app.agent.chronicler import anomalies, daily_brief, north_star
from app.agent.closer import NotNegotiable, prepare_reply
from app.api.main import create_app
from app.database import get_connection, init_db
from app.jobs import JobType
from app.models import (
    Campaign,
    CampaignStatus,
    DealState,
    MessageKind,
    MessageStatus,
    TerritoryProfile,
)
from app.worker import Worker, sweep_stale

_REPOS = (
    "app.jobs",
    "app.deals",
    "app.leads",
    "app.territories",
    "app.campaigns",
    "app.policy",
    "app.agent.runs",
    "app.agent.chronicler",
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
    conn = get_connection(path)
    try:
        conn.execute("INSERT INTO searches (location) VALUES ('Goa, India')")
        conn.execute(
            """INSERT INTO listings (id, search_id, title, host_name, location)
               VALUES ('L1', 1, 'Sea Villa', 'Asha', 'Goa, India'),
                      ('L2', 1, 'Hill Hut', 'Ravi', 'Goa, India')"""
        )
        conn.commit()
    finally:
        conn.close()
    yield path
    os.unlink(path)


def _run(worker, once=True):
    import asyncio

    return asyncio.run(worker.run(once=once))


# --- Worker dispatch -------------------------------------------------------


def test_worker_runs_a_job_and_marks_it_done(db):
    job_id = jobs.enqueue(JobType.SWEEP_STALE, db_path=db)
    worker = Worker(db_path=db)
    worker._last_tick = 9e18  # suppress the planner tick
    _run(worker)
    assert jobs.get_job(job_id, db).status.value == "done"


def test_unknown_job_type_fails_rather_than_crashing_the_worker(db):
    job_id = jobs.enqueue("not_a_real_job", db_path=db)
    worker = Worker(db_path=db)
    worker._last_tick = 9e18
    _run(worker)
    job = jobs.get_job(job_id, db)
    assert job.status.value == "pending"
    assert "No handler" in job.last_error


def test_a_frozen_send_cancels_rather_than_retries(db):
    """Retrying into a kill switch just burns attempts; a human must release it."""
    from app.send_budget import SendingFrozen

    job_id = jobs.enqueue(JobType.SWEEP_STALE, db_path=db)
    worker = Worker(db_path=db)
    worker._last_tick = 9e18
    worker._handlers[JobType.SWEEP_STALE] = MagicMock(
        side_effect=SendingFrozen("kill switch engaged")
    )
    _run(worker)
    assert jobs.get_job(job_id, db).status.value == "cancelled"


def test_job_failure_is_retried_with_backoff(db):
    job_id = jobs.enqueue(JobType.SWEEP_STALE, max_attempts=3, db_path=db)
    worker = Worker(db_path=db)
    worker._last_tick = 9e18
    worker._handlers[JobType.SWEEP_STALE] = MagicMock(side_effect=RuntimeError("boom"))
    _run(worker)
    job = jobs.get_job(job_id, db)
    assert job.status.value == "pending"
    assert job.attempts == 1
    assert job.last_error == "boom"


# --- Stale sweeper ---------------------------------------------------------


def test_sweeper_retires_unanswered_deals(db):
    deal_id = deal_repo.upsert_deal("L1", host_name="Asha", db_path=db)
    deal_repo.advance_to(deal_id, DealState.CONTACTED, db_path=db)
    conn = get_connection(db)
    try:
        conn.execute(
            "UPDATE deals SET last_outbound_at = '2020-01-01T00:00:00' WHERE id = ?",
            (deal_id,),
        )
        conn.commit()
    finally:
        conn.close()

    assert sweep_stale(days=10, db_path=db)["swept"] == 1
    assert deal_repo.get_deal(deal_id, db).state is DealState.STALE


def test_sweeper_leaves_deals_that_got_a_reply(db):
    deal_id = deal_repo.upsert_deal("L1", db_path=db)
    deal_repo.advance_to(deal_id, DealState.CONTACTED, db_path=db)
    deal_repo.record_inbound(deal_id, "Interested!", db_path=db)
    conn = get_connection(db)
    try:
        conn.execute(
            "UPDATE deals SET last_outbound_at = '2020-01-01T00:00:00' WHERE id = ?",
            (deal_id,),
        )
        conn.commit()
    finally:
        conn.close()
    assert sweep_stale(days=10, db_path=db)["swept"] == 0


# --- Planner ---------------------------------------------------------------


def test_discovery_persists_scraped_listings_as_leads(db):
    """The scrape is the expensive half; dropping the results on the floor is
    the failure this guards against."""
    import asyncio

    from app.models import Listing

    territory_id = territory_repo.upsert_territory("Gokarna, Karnataka", db_path=db)
    scraped = [
        Listing(id="R1", title="Cliff House", host_name="Asha", location="Gokarna"),
        Listing(id="R2", title="Beach Hut", host_name="Ravi", location="Gokarna"),
    ]

    worker = Worker(db_path=db)
    job = MagicMock()
    job.payload = {"campaign_id": 0, "territory_id": territory_id}

    with patch("app.scraper.scrape_listings", AsyncMock(return_value=scraped)):
        result = asyncio.run(worker._discover_leads(job))

    assert result["leads"] == 2
    listing_ids = {l.listing_id for l in lead_repo.leads_needing_enrichment(db_path=db)}
    assert listing_ids == {"R1", "R2"}
    assert territory_repo.get_territory(territory_id, db).leads_discovered == 2


def test_discovery_on_an_unknown_territory_is_skipped(db):
    import asyncio

    worker = Worker(db_path=db)
    job = MagicMock()
    job.payload = {"campaign_id": 0, "territory_id": 9999}
    assert asyncio.run(worker._discover_leads(job)) == {"skipped": "territory missing"}


def test_a_blocking_handler_does_not_stall_the_event_loop(db):
    """The API shares a loop with the worker under `manage.py start`. A 13s
    blocking LLM call must not make the dashboard unreachable."""
    import asyncio
    import time as _time

    jobs.enqueue(JobType.SWEEP_STALE, db_path=db)
    worker = Worker(db_path=db)
    worker._last_tick = 9e18
    worker._handlers[JobType.SWEEP_STALE] = lambda job: _time.sleep(0.4) or {}

    async def scenario():
        heartbeats = 0

        async def pulse():
            nonlocal heartbeats
            while True:
                await asyncio.sleep(0.05)
                heartbeats += 1

        ticker = asyncio.create_task(pulse())
        leased = jobs.lease(worker.name, limit=1, db_path=worker.db_path)
        await worker._execute(leased[0])
        ticker.cancel()
        return heartbeats

    # A blocked loop would leave this at 0; off-loop execution keeps it ticking.
    assert asyncio.run(scenario()) >= 3


def test_async_handlers_still_run_on_the_loop(db):
    import asyncio

    job_id = jobs.enqueue(JobType.SYNC_INBOX, db_path=db)
    worker = Worker(db_path=db)
    worker._last_tick = 9e18

    async def handler(job):
        await asyncio.sleep(0)
        return {"threads": 0}

    worker._handlers[JobType.SYNC_INBOX] = handler
    _run(worker)
    assert jobs.get_job(job_id, db).result == {"threads": 0}


def test_planner_queues_research_for_new_territories(db):
    territory_repo.upsert_territory("Goa, India", db_path=db)
    territory_repo.upsert_territory("Manali, Himachal Pradesh", db_path=db)
    assert planner.plan_tick(db_path=db)["queued"]["research"] == 2


def test_planner_works_on_campaigns_it_was_not_told_about(db):
    """A campaign created in the UI gets a fresh id. A planner pinned to bucket
    0 would research territories forever and never discover anything."""
    campaign_id = planner.bootstrap_campaign(
        Campaign(
            name="Winter",
            window_start="2026-11",
            window_end="2026-11",
            status=CampaignStatus.ACTIVE,
        ),
        ["Goa, India"],
        db,
    )
    assert campaign_id != 0

    territory = territory_repo.get_territory_by_name("Goa, India", db)
    territory_repo.save_profile(
        TerritoryProfile(territory_id=territory.id, seasonality={"november": 0.9}),
        db_path=db,
    )

    result = planner.plan_tick(db_path=db)
    assert campaign_id in result["campaigns"]
    assert result["queued"]["route"] == 1


def test_planner_still_covers_the_legacy_bucket(db):
    """Deals backfilled from v1 have no campaign and must not be orphaned."""
    assert planner.DEFAULT_BUCKET in planner.active_campaign_ids(db)


def test_planner_ignores_paused_campaigns(db):
    campaign_id = planner.bootstrap_campaign(
        Campaign(name="Paused", status=CampaignStatus.PAUSED), ["Goa, India"], db
    )
    assert campaign_id not in planner.active_campaign_ids(db)


def test_planner_can_still_be_pinned_to_one_campaign(db):
    planner.bootstrap_campaign(
        Campaign(name="A", status=CampaignStatus.ACTIVE), ["Goa, India"], db
    )
    assert planner.plan_tick(0, db)["campaigns"] == [0]


def test_expired_session_freezes_sending_and_cancels_the_job(db):
    """Logged out, every browser job fails the same way — and sending while
    logged out is how an account gets flagged."""
    from app.agent.chat_reader import SessionExpired

    job_id = jobs.enqueue(JobType.SYNC_INBOX, db_path=db)
    worker = Worker(db_path=db)
    worker._last_tick = 9e18
    worker._handlers[JobType.SYNC_INBOX] = MagicMock(
        side_effect=SessionExpired("redirected to login")
    )
    _run(worker)

    assert jobs.get_job(job_id, db).status.value == "cancelled"
    assert policy_mod.sending_enabled(db) is False


def test_planner_does_not_requeue_the_same_research(db):
    territory_repo.upsert_territory("Goa, India", db_path=db)
    planner.plan_tick(db_path=db)
    assert planner.plan_tick(db_path=db)["queued"]["research"] == 0


def test_planner_respects_send_budget_backpressure(db, monkeypatch):
    """Queueing sends we cannot deliver hides the real constraint.

    A send is only ever queued for a deal whose draft is already staged, so
    each listing gets a pending outreach message first.
    """
    for listing_id in ("L1", "L2"):
        lead_id = lead_repo.upsert_lead(listing_id, db_path=db)
        deal_id = deal_repo.upsert_deal(
            listing_id, campaign_id=0, lead_id=lead_id, db_path=db
        )
        deal_repo.advance_to(deal_id, DealState.QUALIFIED, db_path=db)
        deal_repo.record_message(
            deal_id,
            "Hi there!",
            kind=MessageKind.OUTREACH,
            idempotency_key=f"outreach:0:{listing_id}",
            db_path=db,
        )

    monkeypatch.setattr("app.agent.planner.remaining_sends", lambda p=None: 0)
    assert planner.plan_tick(0, db)["queued"]["outreach"] == 0

    monkeypatch.setattr("app.agent.planner.remaining_sends", lambda p=None: 5)
    assert planner.plan_tick(0, db)["queued"]["outreach"] > 0


def test_planner_drafts_before_it_sends(db):
    """The office writes a draft first; only then is a send queued."""
    lead_id = lead_repo.upsert_lead("L1", db_path=db)
    lead_repo.save_enrichment(lead_id, {"description": "x"}, db_path=db)
    lead_repo.save_score(lead_id, 0.9, {}, db_path=db)

    # Nothing is staged yet, so no send can be queued — only a draft.
    result = planner.plan_tick(0, db)
    assert result["queued"]["draft"] == 1
    assert result["queued"]["outreach"] == 0


def test_planner_queues_negotiation_at_higher_priority_than_outreach(db, monkeypatch):
    lead_id = lead_repo.upsert_lead("L1", db_path=db)
    lead_repo.save_enrichment(lead_id, {"description": "x"}, db_path=db)
    lead_repo.save_score(lead_id, 0.9, {}, db_path=db)

    warm = deal_repo.upsert_deal("L2", host_name="Ravi", db_path=db)
    deal_repo.link_thread(warm, "T1", db_path=db)
    deal_repo.advance_to(warm, DealState.HOST_REPLIED, db_path=db)

    monkeypatch.setattr("app.agent.planner.remaining_sends", lambda p=None: 5)
    planner.plan_tick(0, db)

    leased = jobs.lease("w1", limit=2, db_path=db)
    assert leased[0].type == JobType.NEGOTIATE_DEAL


def test_planner_skips_negotiation_for_unlinked_deals(db):
    deal_id = deal_repo.upsert_deal("L1", db_path=db)
    deal_repo.advance_to(deal_id, DealState.HOST_REPLIED, db_path=db)
    assert planner.plan_tick(0, db)["queued"]["negotiate"] == 0


def test_bootstrap_campaign_seeds_territories(db):
    campaign_id = planner.bootstrap_campaign(
        Campaign(name="Winter", status=CampaignStatus.ACTIVE),
        ["Goa, India", "Gokarna, Karnataka"],
        db,
    )
    assert campaign_repo.get_campaign(campaign_id, db).name == "Winter"
    assert territory_repo.get_territory_by_name("Gokarna, Karnataka", db) is not None


# --- Closer ----------------------------------------------------------------


def _warm_deal(db, listing_id="L1"):
    deal_id = deal_repo.upsert_deal(listing_id, host_name="Asha", db_path=db)
    deal_repo.link_thread(deal_id, f"T-{listing_id}", db_path=db)
    deal_repo.advance_to(deal_id, DealState.HOST_REPLIED, db_path=db)
    deal_repo.record_inbound(deal_id, "Tell me more about the collab?", db_path=db)
    return deal_id


def _mock_llm(text):
    response = MagicMock()
    response.content = text
    response.usage_metadata = {"input_tokens": 10, "output_tokens": 20}
    llm = MagicMock()
    llm.invoke.return_value = response
    llm.model_name = "gpt-4o-mini"
    return llm


def test_closer_stages_a_clean_reply(db):
    deal_id = _warm_deal(db)
    reply = "Happy to share 2 reels and 10 photos, anytime in November or December!"
    with patch("app.agent.closer.get_llm", return_value=_mock_llm(reply)):
        result = prepare_reply(deal_id, db_path=db)

    assert result["status"] == "ready"
    assert deal_repo.get_deal(deal_id, db).state is DealState.NEGOTIATING
    staged = deal_repo.get_messages(deal_id, db)[-1]
    assert staged.status is MessageStatus.PENDING
    assert staged.agent == "closer"


def test_closer_reply_violating_policy_is_blocked_and_escalated(db):
    """The whole point of full autonomy: the Warden is the only thing in the way."""
    deal_id = _warm_deal(db)
    bad = "Sure — let's lock in Dec 12, and WhatsApp me on 9876543210."
    with patch("app.agent.closer.get_llm", return_value=_mock_llm(bad)):
        result = prepare_reply(deal_id, db_path=db)

    assert result["status"] == "blocked"
    assert deal_repo.get_deal(deal_id, db).state is DealState.NEEDS_HUMAN
    assert deal_repo.get_messages(deal_id, db)[-1].status is MessageStatus.BLOCKED


def test_closer_refuses_a_deal_that_is_not_negotiable(db):
    deal_id = deal_repo.upsert_deal("L1", db_path=db)
    with pytest.raises(NotNegotiable):
        prepare_reply(deal_id, db_path=db)


def test_closer_reply_is_idempotent_within_a_round(db):
    deal_id = _warm_deal(db)
    reply = "Sounds great, happy to share 2 reels."
    with patch("app.agent.closer.get_llm", return_value=_mock_llm(reply)):
        first = prepare_reply(deal_id, db_path=db)
        second = prepare_reply(deal_id, db_path=db)
    assert first["message_id"] == second["message_id"]


# --- Chronicler ------------------------------------------------------------


def test_north_star_is_zero_without_sends(db):
    assert north_star(db)["closes_per_100_messages"] == 0.0


def test_north_star_counts_closes_per_hundred(db):
    deal_id = deal_repo.upsert_deal("L1", db_path=db)
    msg_id = deal_repo.record_message(deal_id, "hi", db_path=db)
    deal_repo.mark_message_sent(msg_id, db_path=db)
    deal_repo.record_inbound(deal_id, "yes!", db_path=db)
    for state in (
        DealState.QUALIFIED,
        DealState.CONTACTED,
        DealState.HOST_REPLIED,
        DealState.TERMS_AGREED,
        DealState.READY_TO_BOOK,
    ):
        deal_repo.transition(deal_id, state, db_path=db)

    star = north_star(db)
    assert star["messages_sent"] == 1
    assert star["deals_closed"] == 1
    assert star["closes_per_100_messages"] == 100.0
    assert star["reply_rate_pct"] == 100.0


def test_anomalies_flag_the_kill_switch(db):
    policy_mod.freeze_sending("testing", db)
    assert any("Kill switch" in a for a in anomalies(db))


def test_anomalies_flag_contacted_deals_with_no_thread(db):
    deal_id = deal_repo.upsert_deal("L1", db_path=db)
    deal_repo.advance_to(deal_id, DealState.CONTACTED, db_path=db)
    assert any("no linked thread" in a for a in anomalies(db))


def test_daily_brief_has_every_section(db):
    brief = daily_brief(db)
    assert set(brief) >= {
        "north_star",
        "funnel",
        "activity",
        "queues",
        "anomalies",
        "budget",
        "jobs",
        "cost_by_agent",
        "prompt_performance",
    }


# --- API -------------------------------------------------------------------


@pytest.fixture
def client(db):
    return TestClient(create_app(db))


def test_dashboard_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Airbnb Automate" in response.text
    assert "Closes / 100 msgs" in response.text


def test_dashboard_has_no_campaign_form(client):
    """The office runs itself — there is nothing for a human to set up."""
    body = client.get("/loops").text
    assert "Create campaign &amp; start work" not in body
    assert "Destinations to consider" not in body
    assert "Standing office" in body


def test_dashboard_is_stats_and_the_rest_has_its_own_pages(client, db):
    """Counts stay on the dashboard. Lists live on their own pages."""
    jobs.enqueue(JobType.RESEARCH_TERRITORY, {"name": "Goa"}, db_path=db)
    home = client.get("/").text
    assert "At a glance" in home
    assert "Pipeline" in home
    assert "Job queue" in home
    assert "Prompt performance" in home
    assert "<h2>Messages" not in home
    assert "<h2>Needs a human" not in home
    assert "<h2>Logs" not in home
    assert 'href="/messages"' in home

    assert client.get("/messages").status_code == 200
    assert "Messages — drafts and delivery" in client.get("/messages").text
    assert "<h1>Needs a human</h1>" in client.get("/attention").text
    assert "<h1>Ready to book</h1>" in client.get("/ready").text
    loops = client.get("/loops").text
    assert "Office" in loops
    assert "Courier" in loops
    assert "Standing office" in loops


def test_dashboard_shows_both_loops(client, db):
    jobs.enqueue(JobType.RESEARCH_TERRITORY, {"name": "Goa"}, db_path=db)
    body = client.get("/loops").text
    assert "Loops" in body
    assert "Office" in body
    assert "Courier" in body


def test_dashboard_status_strip_summarises_the_glanceable_state(client):
    """The phone-first strip surfaces sending state and the human queues."""
    body = client.get("/").text
    strip = body.split('class="strip"', 1)[1].split("</div>", 1)[0]
    assert "sending" in strip
    assert "ready to book" in strip
    assert "needs you" in strip
    # Fresh install is not frozen, so the sending pill reads LIVE.
    assert "LIVE" in strip


def test_loop_activity_endpoints_read_the_durable_record(client, db):
    research = jobs.enqueue(JobType.RESEARCH_TERRITORY, {"name": "Goa"}, db_path=db)
    jobs.complete(research, {"territory": "Goa"}, db_path=db)

    office = client.get("/api/activity/office").json()
    assert any(row["type"] == JobType.RESEARCH_TERRITORY for row in office)
    assert client.get("/api/activity/courier").status_code == 200


def test_dashboard_shows_activity_and_controls(client):
    import logging

    from app.logging_config import setup_logging

    setup_logging()
    logging.getLogger("app.agent.scout").info("Scouting Goa, India")

    assert "Freeze sending" in client.get("/").text
    logs = client.get("/logs").text
    assert "Scouting Goa, India" in logs
    assert "<h2>Logs</h2>" in logs


def test_activity_endpoint_returns_app_logs_only(client):
    import logging

    from app.logging_config import setup_logging

    setup_logging()
    logging.getLogger("app.worker").info("worker says hello")
    logging.getLogger("httpx").info("library noise")

    messages = [r["message"] for r in client.get("/api/activity").json()]
    assert "worker says hello" in messages
    assert "library noise" not in messages


def test_brief_endpoint(client):
    assert client.get("/api/brief").status_code == 200


def test_kill_switch_endpoints_toggle_sending(client):
    assert client.post("/api/kill-switch/freeze").json()["sending_enabled"] is False
    assert client.get("/api/budget").json()["sending_enabled"] is False
    assert client.post("/api/kill-switch/resume").json()["sending_enabled"] is True


def test_policy_endpoint_updates_guardrails(client):
    response = client.post(
        "/api/policy",
        json={"max_price_per_night": 2500, "max_agent_replies_per_thread": 2},
    )
    body = response.json()
    assert body["max_price_per_night"] == 2500
    assert body["max_agent_replies_per_thread"] == 2
    assert client.get("/api/policy").json()["max_price_per_night"] == 2500


def test_missing_deal_returns_404(client):
    assert client.get("/api/deals/9999").status_code == 404


def test_deal_detail_includes_messages_and_events(client, db):
    deal_id = deal_repo.upsert_deal("L1", host_name="Asha", db_path=db)
    deal_repo.record_message(deal_id, "hello", db_path=db)
    body = client.get(f"/api/deals/{deal_id}").json()
    assert body["deal"]["host_name"] == "Asha"
    assert len(body["messages"]) == 1
    assert len(body["events"]) == 1


def test_job_queue_endpoint_reports_depth(client, db):
    from app import jobs
    from app.jobs import JobType, Priority

    jobs.enqueue(JobType.SYNC_INBOX, priority=Priority.INBOX_SYNC, db_path=db)
    assert client.get("/api/jobs").json()["pending"] == 1


# --- Needs-a-human actions -------------------------------------------------


def _needs_human_deal(db, listing_id="L1"):
    lead_id = lead_repo.upsert_lead(listing_id, db_path=db)
    deal_id = deal_repo.upsert_deal(
        listing_id, lead_id=lead_id, host_name="Asha", db_path=db
    )
    deal_repo.transition(deal_id, DealState.QUALIFIED, db_path=db)
    deal_repo.transition(deal_id, DealState.NEEDS_HUMAN, reason="host demand", db_path=db)
    return deal_id


def test_retry_deal_resets_to_qualified_and_enqueues_a_draft(client, db):
    deal_id = _needs_human_deal(db)
    body = client.post(f"/api/deals/{deal_id}/retry").json()
    assert body["status"] == "retrying"
    assert deal_repo.get_deal(deal_id, db).state is DealState.QUALIFIED
    assert jobs.queue_depth(db)["pending"] == 1


def test_retry_missing_deal_is_404(client):
    assert client.post("/api/deals/9999/retry").status_code == 404


def test_kill_deal_rejects_it_and_cancels_pending_jobs(client, db):
    deal_id = _needs_human_deal(db)
    jobs.enqueue(
        JobType.DRAFT_OUTREACH, {"listing_id": "L1"}, deal_id=deal_id, db_path=db
    )
    body = client.post(f"/api/deals/{deal_id}/kill").json()
    assert body["status"] == "killed"
    assert body["jobs_cancelled"] == 1
    assert deal_repo.get_deal(deal_id, db).state is DealState.REJECTED
    assert jobs.queue_depth(db).get("pending", 0) == 0


def test_kill_works_from_a_pre_contact_state(client, db):
    """A human can stop a deal that never got contacted (QUALIFIED -> terminal)."""
    lead_id = lead_repo.upsert_lead("L2", db_path=db)
    deal_id = deal_repo.upsert_deal("L2", lead_id=lead_id, db_path=db)
    deal_repo.transition(deal_id, DealState.QUALIFIED, db_path=db)
    assert client.post(f"/api/deals/{deal_id}/kill").json()["status"] == "killed"
    assert deal_repo.get_deal(deal_id, db).state is DealState.DISQUALIFIED


def test_delete_deal_removes_it_entirely(client, db):
    deal_id = _needs_human_deal(db)
    assert client.delete(f"/api/deals/{deal_id}").json()["status"] == "deleted"
    assert client.get(f"/api/deals/{deal_id}").status_code == 404
    assert client.delete(f"/api/deals/{deal_id}").status_code == 404


# --- Leads page ------------------------------------------------------------


def test_leads_api_lists_leads_with_listing_detail(client, db):
    lead_repo.upsert_lead("L1", db_path=db)
    rows = client.get("/api/leads").json()
    assert any(r["title"] == "Sea Villa" and r["listing_id"] == "L1" for r in rows)


def test_leads_page_renders_the_board(client, db):
    lead_repo.upsert_lead("L1", db_path=db)
    body = client.get("/leads").text
    assert "Sea Villa" in body
    assert "Leads" in body


def test_lead_detail_page_shows_booking_context(client, db):
    from app import portals

    lead_id = lead_repo.upsert_lead("L1", db_path=db)
    portals.save_context(
        "L1", "booking", {"rating": 9.1, "review_excerpts": ["Superb host"]},
        external_url="https://booking.com/x", match_confidence=0.92, db_path=db,
    )
    body = client.get(f"/leads/{lead_id}").text
    assert "Sea Villa" in body
    assert "Superb host" in body


def test_missing_lead_detail_is_404(client):
    assert client.get("/leads/9999").status_code == 404


# --- Portal context worker handler -----------------------------------------


def test_pull_portal_context_saves_a_confident_match(db):
    from app import portals
    from app.jobs import JobType
    from app.models import Job, JobStatus

    worker = Worker(db_path=db, role="office")
    worker._portal_fetcher = lambda q: {
        "title": "Sea Villa",
        "location": "Goa, India",
        "guests": 0,
        "rating": 9.0,
        "reviews": ["Lovely"],
        "url": "https://booking.com/x",
    }
    job = Job(
        id=1,
        type=JobType.PULL_PORTAL_CONTEXT,
        payload={"listing_id": "L1", "portal": "booking"},
        status=JobStatus.LEASED,
    )
    assert worker._pull_portal_context(job)["status"] == "saved"
    assert portals.get_context_for_listing("L1", db)[0]["payload"]["rating"] == 9.0


def test_pull_portal_context_is_a_noop_without_a_fetcher(db):
    from app import portals
    from app.jobs import JobType
    from app.models import Job, JobStatus

    worker = Worker(db_path=db, role="office")  # _portal_fetcher stays None
    job = Job(
        id=1,
        type=JobType.PULL_PORTAL_CONTEXT,
        payload={"listing_id": "L1", "portal": "booking"},
        status=JobStatus.LEASED,
    )
    assert worker._pull_portal_context(job)["status"] == "no_context"
    assert portals.get_context_for_listing("L1", db) == []


def test_pull_portal_context_skips_a_weak_match(db):
    from app import portals
    from app.jobs import JobType
    from app.models import Job, JobStatus

    worker = Worker(db_path=db, role="office")
    worker._portal_fetcher = lambda q: {"title": "Totally Different Place", "location": "Berlin"}
    job = Job(
        id=1,
        type=JobType.PULL_PORTAL_CONTEXT,
        payload={"listing_id": "L1", "portal": "booking"},
        status=JobStatus.LEASED,
    )
    assert worker._pull_portal_context(job)["status"] == "low_confidence"
    assert portals.get_context_for_listing("L1", db) == []
