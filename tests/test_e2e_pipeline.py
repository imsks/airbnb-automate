"""End-to-end: one deal from discovery to ready-to-book.

Every LLM call and every browser call is mocked, so this exercises the wiring —
job queue, state machine, Warden, send budget, thread linking, terms extraction
— without touching the network.
"""

import asyncio
import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import deals as deal_repo
from app import jobs, leads as lead_repo, territories as territory_repo
from app.agent import planner
from app.agent.chronicler import daily_brief
from app.api.dashboard import render_dashboard
from app.database import get_connection, init_db
from app.jobs import JobType
from app.models import Campaign, CampaignStatus, DealState, MessageStatus
from app.worker import Worker

_PATCH_TARGETS = (
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

_SCOUT_JSON = json.dumps(
    {
        "summary": "Superb in winter.",
        "seasonality": {"november": 0.95, "december": 0.9, "january": 0.85},
        "connectivity_score": 0.8,
        "connectivity_note": "Solid 4G.",
        "content_score": 0.9,
        "content_angles": ["beach shacks at sunrise"],
        "cost_band": "low",
        "cost_score": 0.8,
        "events": [],
        "caveats": [],
    }
)
_OUTREACH_TEXT = (
    "Hi Asha! Your note about the sunrise deck at Sea Villa stopped me scrolling. "
    "I'm a remote engineer who makes travel content, and I'd love to stay and "
    "share 2 reels, 10 photos and 1 honest review. I'm flexible — anytime in "
    "November or December works. No pressure at all!"
)
_REPLY_TEXT = (
    "That sounds great, Asha! Happy to make it work around your calendar — "
    "any week in November or December suits me."
)
_TERMS_JSON = json.dumps(
    {
        "agreed": True,
        "price_per_night": 0,
        "currency": "INR",
        "discount_pct": 100,
        "window_start": "2026-11",
        "window_end": "2026-12",
        "nights": 7,
        "deliverables": ["2 reels", "10 photos", "1 review"],
        "confidence": 0.9,
        "reason": "Host agreed to a free week in exchange for content",
    }
)


@pytest.fixture
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    for module in _PATCH_TARGETS:
        monkeypatch.setattr(
            f"{module}.get_connection", lambda p=None: get_connection(path), raising=False
        )
    monkeypatch.setenv("OUTREACH_MAX_SENDS_PER_WINDOW", "5")
    monkeypatch.setenv("OUTREACH_RATE_WINDOW_SECONDS", "3600")
    monkeypatch.setenv("OUTREACH_INTER_MESSAGE_DELAY_SECONDS", "0")
    yield path
    os.unlink(path)


def _llm(*responses):
    """An LLM that returns each response in turn, repeating the last one."""
    queue = list(responses)

    def invoke(_messages):
        content = queue.pop(0) if len(queue) > 1 else queue[0]
        response = MagicMock()
        response.content = content
        response.usage_metadata = {"input_tokens": 50, "output_tokens": 100}
        return response

    llm = MagicMock()
    llm.invoke.side_effect = invoke
    llm.model_name = "gpt-4o-mini"
    return llm


def _run_worker_until_idle(worker, limit=30):
    async def drain():
        for _ in range(limit):
            leased = jobs.lease(worker.name, limit=1, db_path=worker.db_path)
            if not leased:
                return
            await worker._execute(leased[0])

    asyncio.run(drain())


def _seed_listing(path, listing_id="L1"):
    conn = get_connection(path)
    try:
        conn.execute("INSERT INTO searches (location) VALUES ('Goa, India')")
        conn.execute(
            """INSERT INTO listings
               (id, search_id, title, host_name, location, price_per_night,
                currency, review_count, superhost, url)
               VALUES (?, 1, 'Sea Villa', 'Asha', 'Goa, India', 2200, 'INR', 3, 0,
                       'https://www.airbnb.com/rooms/L1')""",
            (listing_id,),
        )
        conn.commit()
    finally:
        conn.close()


def test_research_and_routing_produce_an_itinerary(db):
    from app import campaigns as campaign_repo

    campaign_id = planner.bootstrap_campaign(
        Campaign(
            name="Winter",
            window_start="2026-11",
            window_end="2026-11",
            origin="Delhi",
            status=CampaignStatus.ACTIVE,
        ),
        ["Goa, India"],
        db,
    )
    planner.plan_tick(campaign_id, db)

    worker = Worker(campaign_id=campaign_id, db_path=db)
    with patch("app.agent.scout.get_llm", return_value=_llm(_SCOUT_JSON)):
        _run_worker_until_idle(worker)

    territory = territory_repo.get_territory_by_name("Goa, India", db)
    profile = territory_repo.get_current_profile(territory.id, db)
    assert profile.month_score("november") == 0.95
    assert territory.status.value == "researched"

    planner.plan_tick(campaign_id, db)
    _run_worker_until_idle(worker)

    stops = campaign_repo.get_stops(campaign_id, db)
    assert [s.target_month for s in stops] == ["2026-11"]


def test_deal_walks_from_discovered_to_ready_to_book(db):
    _seed_listing(db)
    lead_id = lead_repo.upsert_lead("L1", db_path=db)
    lead_repo.save_enrichment(
        lead_id,
        {
            "description": "A sunlit villa with a pool and fast wifi.",
            "amenities": ["Wifi", "Dedicated workspace"],
            "review_excerpts": ["The sunrise from the deck is unreal"],
            "has_long_stay_discount": True,
            "instant_book": False,
            "listing_age_months": 6,
        },
        db_path=db,
    )

    worker = Worker(db_path=db)

    # 1. Score → the deal appears, qualified.
    jobs.enqueue(JobType.SCORE_LEAD, {"lead_id": lead_id}, db_path=db)
    with patch("app.agent.analyst.get_llm", return_value=_llm('{"adjustment": 0.05}')):
        _run_worker_until_idle(worker)

    deal = deal_repo.get_deals_by_state(DealState.QUALIFIED, db_path=db)[0]
    assert deal.host_name == "Asha"
    assert lead_repo.get_lead(lead_id, db).collab_fit_score > 0.5

    # 2. Outreach → sent, thread linked, state CONTACTED.
    jobs.enqueue(
        JobType.SEND_OUTREACH,
        {"lead_id": lead_id},
        idempotency_key=f"outreach:0:L1",
        db_path=db,
    )
    send = AsyncMock(return_value=("T900", "https://airbnb.com/messages/thread/T900"))
    with patch("app.agent.scribe.get_llm", return_value=_llm(_OUTREACH_TEXT)), patch(
        "app.outreach._send_message_to_host", send
    ), patch("app.agent.scribe.open_airbnb_browser", AsyncMock(
        return_value=(MagicMock(), MagicMock(), None, False)
    )), patch("app.agent.scribe.close_airbnb_session", AsyncMock()):
        _run_worker_until_idle(worker)

    deal = deal_repo.get_deal(deal.id, db)
    assert deal.state is DealState.CONTACTED
    assert deal.thread_id == "T900"
    assert deal.thread_linked_via == "send_capture"
    assert deal_repo.get_messages(deal.id, db)[0].status is MessageStatus.SENT

    # 3. Host replies.
    deal_repo.record_inbound(deal.id, "Interesting! What would you make?", db_path=db)
    deal_repo.advance_to(deal.id, DealState.HOST_REPLIED, db_path=db)

    # 4. Negotiate → reply sent, terms job queued.
    planner.plan_tick(0, db)
    with patch("app.agent.closer.get_llm", return_value=_llm(_REPLY_TEXT, _TERMS_JSON)), \
         patch("app.inbox.open_airbnb_browser", AsyncMock(
             return_value=(MagicMock(), MagicMock(), None, False))), \
         patch("app.inbox.close_airbnb_session", AsyncMock()), \
         patch("app.inbox.send_reply_on_page", AsyncMock()):
        _run_worker_until_idle(worker)

    deal = deal_repo.get_deal(deal.id, db)
    assert deal.state is DealState.READY_TO_BOOK
    assert deal.agreed_discount_pct == 100
    assert deal.agreed_window_start == "2026-11"
    assert deal.agreed_deliverables == ["2 reels", "10 photos", "1 review"]

    # Every transition is on the record.
    events = deal_repo.get_events(deal.id, db)
    reached = [e.to_state for e in events]
    for state in ("discovered", "qualified", "contacted", "host_replied",
                  "negotiating", "terms_agreed", "ready_to_book"):
        assert state in reached, f"missing transition to {state}"

    # Both sends came out of one shared budget.
    from app.send_budget import budget_status

    assert budget_status(db)["used"] == 2

    # And the brief surfaces it as work for a human.
    brief = daily_brief(db)
    assert len(brief["queues"]["ready_to_book"]) == 1
    assert brief["north_star"]["deals_closed"] == 1
    assert "Sea Villa" in render_dashboard(brief)


def test_retrying_an_outreach_job_never_sends_twice(db):
    """The failure mode idempotency keys exist to prevent."""
    _seed_listing(db)
    lead_id = lead_repo.upsert_lead("L1", db_path=db)
    lead_repo.save_enrichment(lead_id, {"description": "x"}, db_path=db)

    from app.agent.scribe import send_outreach_for_lead

    send = AsyncMock(return_value=("T900", ""))
    with patch("app.agent.scribe.get_llm", return_value=_llm(_OUTREACH_TEXT)), patch(
        "app.outreach._send_message_to_host", send
    ), patch("app.agent.scribe.open_airbnb_browser", AsyncMock(
        return_value=(MagicMock(), MagicMock(), None, False)
    )), patch("app.agent.scribe.close_airbnb_session", AsyncMock()):
        asyncio.run(send_outreach_for_lead(lead_id, db_path=db))
        second = asyncio.run(send_outreach_for_lead(lead_id, db_path=db))

    assert second["status"] == "already_contacted"
    assert send.await_count == 1

    deal = deal_repo.get_deals_by_state(DealState.CONTACTED, db_path=db)[0]
    assert len(deal_repo.get_messages(deal.id, db)) == 1

    from app.send_budget import budget_status

    assert budget_status(db)["used"] == 1


def test_kill_switch_mid_campaign_stops_the_next_send(db):
    """Two independent layers refuse: the Warden first, the budget behind it."""
    from app import policy as policy_mod
    from app.agent.scribe import send_outreach_for_lead

    _seed_listing(db)
    lead_id = lead_repo.upsert_lead("L1", db_path=db)
    lead_repo.save_enrichment(lead_id, {"description": "x"}, db_path=db)
    policy_mod.freeze_sending("suspected flag", db)

    send = AsyncMock(return_value=("T900", ""))
    with patch("app.agent.scribe.get_llm", return_value=_llm(_OUTREACH_TEXT)), patch(
        "app.outreach._send_message_to_host", send
    ), patch("app.agent.scribe.open_airbnb_browser", AsyncMock(
        return_value=(MagicMock(), MagicMock(), None, False)
    )), patch("app.agent.scribe.close_airbnb_session", AsyncMock()):
        result = asyncio.run(send_outreach_for_lead(lead_id, db_path=db))

    assert result["status"] == "blocked"
    assert "kill_switch" in result["reason"]
    assert send.await_count == 0

    # The draft is kept, and the deal is parked for a human rather than lost.
    deal = deal_repo.get_deal(result["deal_id"], db)
    assert deal.state is DealState.NEEDS_HUMAN
    assert deal_repo.get_messages(deal.id, db)[0].status is MessageStatus.BLOCKED

    from app.send_budget import budget_status

    assert budget_status(db)["used"] == 0
