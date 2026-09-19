"""Integration tests: worker dispatch, planner backpressure, closer, API, brief."""

import os
import tempfile
from unittest.mock import MagicMock, patch

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
from app.models import Campaign, CampaignStatus, DealState, MessageStatus
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


def test_planner_queues_research_for_new_territories(db):
    territory_repo.upsert_territory("Goa, India", db_path=db)
    territory_repo.upsert_territory("Manali, Himachal Pradesh", db_path=db)
    assert planner.plan_tick(0, db)["queued"]["research"] == 2


def test_planner_does_not_requeue_the_same_research(db):
    territory_repo.upsert_territory("Goa, India", db_path=db)
    planner.plan_tick(0, db)
    assert planner.plan_tick(0, db)["queued"]["research"] == 0


def test_planner_respects_send_budget_backpressure(db, monkeypatch):
    """Queueing sends we cannot deliver hides the real constraint."""
    for listing_id in ("L1", "L2"):
        lead_id = lead_repo.upsert_lead(listing_id, db_path=db)
        lead_repo.save_enrichment(lead_id, {"description": "x"}, db_path=db)
        lead_repo.save_score(lead_id, 0.9, {}, db_path=db)

    monkeypatch.setattr("app.agent.planner.remaining_sends", lambda p=None: 0)
    assert planner.plan_tick(0, db)["queued"]["outreach"] == 0

    monkeypatch.setattr("app.agent.planner.remaining_sends", lambda p=None: 5)
    assert planner.plan_tick(0, db)["queued"]["outreach"] > 0


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


def test_campaign_creation_seeds_territories(client):
    response = client.post(
        "/api/campaigns",
        json={
            "name": "Winter tour",
            "window_start": "2026-11",
            "window_end": "2027-02",
            "places": ["Goa, India", "Gokarna, Karnataka"],
        },
    )
    assert response.status_code == 200
    assert response.json()["territories"] == 2
    assert len(client.get("/api/campaigns").json()) == 1


def test_missing_deal_returns_404(client):
    assert client.get("/api/deals/9999").status_code == 404


def test_deal_detail_includes_messages_and_events(client, db):
    deal_id = deal_repo.upsert_deal("L1", host_name="Asha", db_path=db)
    deal_repo.record_message(deal_id, "hello", db_path=db)
    body = client.get(f"/api/deals/{deal_id}").json()
    assert body["deal"]["host_name"] == "Asha"
    assert len(body["messages"]) == 1
    assert len(body["events"]) == 1


def test_queueing_an_inbox_sync_creates_a_job(client, db):
    assert client.post("/api/jobs/sync-inbox").json()["job_id"]
    assert client.get("/api/jobs").json()["pending"] == 1
