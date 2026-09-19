"""FastAPI shell: dashboard and control surface.

This process never touches Playwright. Every action that needs a browser is
written to the ``jobs`` table and picked up by the worker, which is what lets
the API stay responsive while a scrape runs for minutes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app import campaigns as campaign_repo
from app import deals as deal_repo
from app import jobs, leads as lead_repo, policy as policy_mod, territories as territory_repo
from app.agent import planner
from app.agent.chronicler import daily_brief
from app.api.dashboard import render_dashboard
from app.database import init_db
from app.jobs import JobType, Priority
from app.locations_md import project_locations_md, read_locations_md
from app.logging_config import recent_activity
from app.models import Campaign, CampaignStatus, DealState
from app.send_budget import budget_status

logger = logging.getLogger(__name__)


class CampaignRequest(BaseModel):
    """A new travel campaign and the places it may consider."""

    name: str
    goal: str = ""
    window_start: str = Field("", description="YYYY-MM")
    window_end: str = Field("", description="YYYY-MM")
    origin: str = ""
    guests: int = 2
    stay_nights: int = 7
    places: list[str] = Field(default_factory=list)


class PolicyRequest(BaseModel):
    """Guardrail values to update. Omitted fields are left unchanged."""

    max_price_per_night: Optional[float] = None
    max_agent_replies_per_thread: Optional[int] = None
    allowed_deliverables: Optional[list[str]] = None
    allow_specific_dates: Optional[bool] = None


def _suggested_places() -> list[str]:
    """Destinations from locations.md, offered as a starting point in the UI."""
    path = project_locations_md(Path(__file__).resolve().parents[2])
    return read_locations_md(path) if path.exists() else []


def create_app(db_path: Optional[str] = None) -> FastAPI:
    """Build the API. ``db_path`` is for tests; production uses the configured DB."""
    init_db(db_path)
    app = FastAPI(title="Airbnb Automate", version="2.0")

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return render_dashboard(
            daily_brief(db_path),
            activity=recent_activity(40),
            campaigns=campaign_repo.list_campaigns(db_path),
            suggested_places=_suggested_places(),
        )

    @app.get("/api/activity")
    def activity(limit: int = 60) -> list[dict]:
        """What the agents have been doing, newest first."""
        return recent_activity(limit)

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

    # --- Campaigns ---------------------------------------------------------

    @app.get("/api/campaigns")
    def list_campaigns() -> list[dict]:
        return [c.model_dump(mode="json") for c in campaign_repo.list_campaigns(db_path)]

    @app.post("/api/campaigns")
    def create_campaign(request: CampaignRequest) -> dict:
        campaign = Campaign(
            name=request.name,
            goal=request.goal,
            window_start=request.window_start,
            window_end=request.window_end,
            origin=request.origin,
            guests=request.guests,
            stay_nights=request.stay_nights,
            status=CampaignStatus.ACTIVE,
        )
        places = request.places or _suggested_places()
        campaign_id = planner.bootstrap_campaign(campaign, places, db_path)
        queued = planner.plan_tick(campaign_id, db_path)
        return {
            "campaign_id": campaign_id,
            "territories": len(places),
            "queued": queued["total"],
        }

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

    @app.post("/api/campaigns/{campaign_id}/tick")
    def tick(campaign_id: int) -> dict:
        return planner.plan_tick(campaign_id, db_path)

    # --- Jobs --------------------------------------------------------------

    @app.get("/api/jobs")
    def job_queue() -> dict:
        return jobs.queue_depth(db_path)

    @app.post("/api/jobs/sync-inbox")
    def queue_inbox_sync() -> dict:
        job_id = jobs.enqueue(
            JobType.SYNC_INBOX, priority=Priority.INBOX_SYNC, db_path=db_path
        )
        return {"job_id": job_id}

    @app.post("/api/jobs/sweep")
    def queue_sweep() -> dict:
        job_id = jobs.enqueue(
            JobType.SWEEP_STALE, priority=Priority.HOUSEKEEPING, db_path=db_path
        )
        return {"job_id": job_id}

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
