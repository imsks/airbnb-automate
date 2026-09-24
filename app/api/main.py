"""FastAPI shell: dashboard and control surface.

This process never touches Playwright. Every action that needs a browser is
written to the ``jobs`` table and picked up by the worker, which is what lets
the API stay responsive while a scrape runs for minutes.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app import activity as activity_feeds
from app import campaigns as campaign_repo
from app import deals as deal_repo
from app import jobs, leads as lead_repo, policy as policy_mod, territories as territory_repo
from app import portals as portal_repo
from app.agent.chronicler import daily_brief
from app.api.dashboard import (
    render_attention,
    render_dashboard,
    render_lead_detail,
    render_leads,
    render_logs,
    render_loops,
    render_messages,
    render_ready,
)
from app.database import get_listing, init_db
from app.jobs import JobType, Priority
from app.logging_config import recent_activity
from app.models import DealState
from app.send_budget import budget_status

logger = logging.getLogger(__name__)


class PolicyRequest(BaseModel):
    """Guardrail values to update. Omitted fields are left unchanged."""

    max_price_per_night: Optional[float] = None
    max_agent_replies_per_thread: Optional[int] = None
    allowed_deliverables: Optional[list[str]] = None
    allow_specific_dates: Optional[bool] = None


def create_app(db_path: Optional[str] = None) -> FastAPI:
    """Build the API. ``db_path`` is for tests; production uses the configured DB."""
    init_db(db_path)
    app = FastAPI(title="Airbnb Automate", version="2.0")

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return render_dashboard(daily_brief(db_path))

    @app.get("/messages", response_class=HTMLResponse)
    def messages_page() -> str:
        return render_messages(daily_brief(db_path).get("messages", []))

    @app.get("/attention", response_class=HTMLResponse)
    def attention_page() -> str:
        return render_attention(daily_brief(db_path)["queues"]["needs_human"])

    @app.get("/ready", response_class=HTMLResponse)
    def ready_page() -> str:
        return render_ready(daily_brief(db_path)["queues"]["ready_to_book"])

    @app.get("/loops", response_class=HTMLResponse)
    def loops_page() -> str:
        return render_loops(
            activity_feeds.office_feed(25, db_path),
            activity_feeds.courier_feed(25, db_path),
            campaign_repo.list_campaigns(db_path),
        )

    @app.get("/logs", response_class=HTMLResponse)
    def logs_page() -> str:
        return render_logs(recent_activity(40))

    @app.get("/api/activity")
    def activity(limit: int = 60) -> list[dict]:
        """What the agents have been doing, newest first."""
        return recent_activity(limit)

    @app.get("/api/activity/office")
    def office_activity(limit: int = 25) -> list[dict]:
        """The office loop's recent work, from the durable job record."""
        return activity_feeds.office_feed(limit, db_path)

    @app.get("/api/activity/courier")
    def courier_activity(limit: int = 25) -> list[dict]:
        """The courier loop's recent work, from the durable job record."""
        return activity_feeds.courier_feed(limit, db_path)

    @app.get("/api/brief")
    def brief() -> dict:
        return daily_brief(db_path)

    @app.get("/api/funnel")
    def funnel(campaign_id: Optional[int] = None) -> dict:
        return deal_repo.funnel_counts(campaign_id, db_path)

    @app.get("/api/budget")
    def budget() -> dict:
        return budget_status(db_path)

    # --- Deals -------------------------------------------------------------

    @app.get("/api/deals")
    def list_deals(state: Optional[str] = None, campaign_id: int = 0) -> list[dict]:
        states = (
            [DealState(state)]
            if state
            else [s for s in DealState if s != DealState.DISCOVERED]
        )
        found = deal_repo.get_deals_by_state(
            *states, campaign_id=campaign_id, db_path=db_path
        )
        return [d.model_dump(mode="json") for d in found]

    @app.get("/api/deals/{deal_id}")
    def get_deal(deal_id: int) -> dict:
        deal = deal_repo.get_deal(deal_id, db_path)
        if deal is None:
            raise HTTPException(status_code=404, detail="Deal not found")
        return {
            "deal": deal.model_dump(mode="json"),
            "messages": [
                m.model_dump(mode="json") for m in deal_repo.get_messages(deal_id, db_path)
            ],
            "events": [
                e.model_dump(mode="json") for e in deal_repo.get_events(deal_id, db_path)
            ],
        }

    @app.post("/api/deals/{deal_id}/booked")
    def mark_booked(deal_id: int) -> dict:
        """Confirm you paid. This is the one step no agent can take for you."""
        deal = deal_repo.transition(
            deal_id,
            DealState.BOOKED,
            reason="booked by human",
            actor="human",
            db_path=db_path,
        )
        return deal.model_dump(mode="json")

    @app.post("/api/deals/{deal_id}/reject")
    def reject_deal(deal_id: int, reason: str = "") -> dict:
        deal = deal_repo.transition(
            deal_id,
            DealState.REJECTED,
            reason=reason or "closed by human",
            actor="human",
            db_path=db_path,
        )
        return deal.model_dump(mode="json")

    @app.post("/api/deals/{deal_id}/retry")
    def retry_deal(deal_id: int) -> dict:
        """Start this host over: clear the thread and let the office redraft."""
        deal = deal_repo.get_deal(deal_id, db_path)
        if deal is None:
            raise HTTPException(status_code=404, detail="Deal not found")
        deal_repo.reset_for_retry(deal_id, db_path)
        if deal.lead_id is not None:
            jobs.enqueue(
                JobType.DRAFT_OUTREACH,
                {"lead_id": deal.lead_id, "listing_id": deal.listing_id},
                priority=Priority.DRAFTING,
                campaign_id=deal.campaign_id,
                deal_id=deal_id,
                db_path=db_path,
            )
        return {"deal_id": deal_id, "status": "retrying"}

    @app.post("/api/deals/{deal_id}/kill")
    def kill_deal(deal_id: int, reason: str = "") -> dict:
        """Stop all work on a deal: reject it and cancel its pending jobs."""
        deal = deal_repo.get_deal(deal_id, db_path)
        if deal is None:
            raise HTTPException(status_code=404, detail="Deal not found")
        deal_repo.abandon(
            deal_id, reason=reason or "stopped by human", actor="human", db_path=db_path
        )
        cancelled = jobs.cancel_for_deal(deal_id, db_path=db_path)
        return {"deal_id": deal_id, "status": "killed", "jobs_cancelled": cancelled}

    @app.delete("/api/deals/{deal_id}")
    def delete_deal(deal_id: int) -> dict:
        """Hard-delete a deal and its messages/events."""
        deleted = deal_repo.delete(deal_id, db_path)
        if not deleted:
            raise HTTPException(status_code=404, detail="Deal not found")
        return {"deal_id": deal_id, "status": "deleted"}

    # --- Leads -------------------------------------------------------------

    @app.get("/api/leads")
    def list_leads(campaign_id: int = 0) -> list[dict]:
        return lead_repo.list_leads_with_listing(campaign_id, db_path=db_path)

    @app.get("/leads", response_class=HTMLResponse)
    def leads_page() -> str:
        return render_leads(lead_repo.list_leads_with_listing(db_path=db_path))

    @app.get("/leads/{lead_id}", response_class=HTMLResponse)
    def lead_detail_page(lead_id: int) -> str:
        lead = lead_repo.get_lead(lead_id, db_path)
        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        listing = get_listing(lead.listing_id, db_path)
        context = portal_repo.get_context_for_listing(lead.listing_id, db_path=db_path)
        return render_lead_detail(lead, listing, context)

    # --- Campaigns ---------------------------------------------------------

    @app.get("/api/campaigns")
    def list_campaigns() -> list[dict]:
        return [c.model_dump(mode="json") for c in campaign_repo.list_campaigns(db_path)]

    @app.get("/api/campaigns/{campaign_id}/itinerary")
    def itinerary(campaign_id: int) -> list[dict]:
        stops = campaign_repo.get_stops(campaign_id, db_path)
        out = []
        for stop in stops:
            territory = territory_repo.get_territory(stop.territory_id, db_path)
            payload = stop.model_dump(mode="json")
            payload["territory_name"] = territory.name if territory else ""
            out.append(payload)
        return out

    # --- Jobs --------------------------------------------------------------

    @app.get("/api/jobs")
    def job_queue() -> dict:
        return jobs.queue_depth(db_path)

    # --- Policy & kill switch ----------------------------------------------

    @app.get("/api/policy")
    def get_policy() -> dict:
        return policy_mod.load_policy(db_path).model_dump(mode="json")

    @app.post("/api/policy")
    def update_policy(request: PolicyRequest) -> dict:
        mapping = {
            policy_mod.KEY_PRICE_CEILING: request.max_price_per_night,
            policy_mod.KEY_MAX_AGENT_REPLIES: request.max_agent_replies_per_thread,
            policy_mod.KEY_ALLOWED_DELIVERABLES: request.allowed_deliverables,
            policy_mod.KEY_ALLOW_SPECIFIC_DATES: request.allow_specific_dates,
        }
        for key, value in mapping.items():
            if value is not None:
                policy_mod.set_policy_value(key, value, db_path)
        return policy_mod.load_policy(db_path).model_dump(mode="json")

    @app.post("/api/kill-switch/freeze")
    def freeze(reason: str = "") -> dict:
        policy_mod.freeze_sending(reason, db_path)
        return {"sending_enabled": False}

    @app.post("/api/kill-switch/resume")
    def resume() -> dict:
        policy_mod.resume_sending(db_path)
        return {"sending_enabled": True}

    # --- Leads -------------------------------------------------------------

    @app.get("/api/leads/top")
    def top_leads(campaign_id: int = 0, limit: int = 20) -> list[dict]:
        found = lead_repo.top_unsent_leads(campaign_id, limit, db_path=db_path)
        return [l.model_dump(mode="json") for l in found]

    return app


app = create_app()
