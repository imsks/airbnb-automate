"""The Planner: the chief of staff that keeps the office busy.

This is the whole "agents spawn themselves" mechanism, and it is deliberately
boring: a tick that looks at the current state of the pipeline and enqueues the
work that is missing. Research begets routing, routing begets discovery,
discovery begets enrichment, enrichment begets scoring, scoring begets outreach.

The important discipline is backpressure. Sends are capped at a handful per
window, so the Planner refuses to queue outreach it has no budget to deliver.
Queueing a hundred sends that cannot run just hides the constraint.
"""

from __future__ import annotations

import logging
from typing import Optional

from app import campaigns as campaign_repo
from app import jobs, leads as lead_repo, territories as territory_repo
from app.jobs import JobType, Priority
from app.models import Campaign, CampaignStatus, DealState
from app.send_budget import remaining_sends

logger = logging.getLogger(__name__)

#: Keep a shallow send queue so the highest-scoring lead at send time wins,
#: rather than whichever lead happened to be scored first.
OUTREACH_QUEUE_DEPTH = 2
MAX_RESEARCH_PER_TICK = 5
MAX_ENRICH_PER_TICK = 10
MAX_SCORE_PER_TICK = 10

#: Deals backfilled from v1 have no campaign, so the default bucket is always
#: ticked alongside real campaigns — otherwise old threads are never negotiated.
DEFAULT_BUCKET = 0


def active_campaign_ids(db_path: Optional[str] = None) -> list[int]:
    """Buckets the Planner should work on: every active campaign, plus legacy."""
    ids = [
        c.id
        for c in campaign_repo.list_campaigns(db_path)
        if c.id is not None and c.status is CampaignStatus.ACTIVE
    ]
    return ids + [DEFAULT_BUCKET]


def plan_tick(
    campaign_id: Optional[int] = None, db_path: Optional[str] = None
) -> dict:
    """Enqueue whatever the pipeline is missing right now.

    With no ``campaign_id`` this plans for every active campaign. That is the
    default because a campaign created in the UI gets a fresh id, and a worker
    pinned to one bucket would research territories forever while never
    discovering, scoring or contacting anything.
    """
    buckets = (
        [campaign_id] if campaign_id is not None else active_campaign_ids(db_path)
    )

    # Territory research belongs to no campaign, so it is queued once per tick.
    queued: dict[str, int] = {"research": _queue_research(db_path)}
    for key in ("route", "discover", "enrich", "score", "outreach", "negotiate"):
        queued[key] = 0

    for bucket in buckets:
        queued["route"] += _queue_route(bucket, db_path)
        queued["discover"] += _queue_discovery(bucket, db_path)
        queued["enrich"] += _queue_enrichment(bucket, db_path)
        queued["score"] += _queue_scoring(bucket, db_path)
        queued["outreach"] += _queue_outreach(bucket, db_path)
        queued["negotiate"] += _queue_negotiations(bucket, db_path)

    total = sum(queued.values())
    if total:
        logger.info("🧭 Planner queued %d job(s): %s", total, _summarise(queued))
    else:
        logger.debug("🧭 Planner tick: nothing to queue")
    return {"queued": queued, "total": total, "campaigns": buckets}


def _summarise(queued: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in queued.items() if v)


def _queue_research(db_path: Optional[str]) -> int:
    count = 0
    for territory in territory_repo.territories_needing_research(
        limit=MAX_RESEARCH_PER_TICK, db_path=db_path
    ):
        if jobs.enqueue(
            JobType.RESEARCH_TERRITORY,
            {"territory_id": territory.id, "name": territory.name},
            priority=Priority.RESEARCH,
            idempotency_key=f"research:{territory.id}",
            db_path=db_path,
        ):
            count += 1
    return count


def _queue_route(campaign_id: int, db_path: Optional[str]) -> int:
    campaign = campaign_repo.get_campaign(campaign_id, db_path)
    if campaign is None or campaign.status.value != "active":
        return 0
    if campaign_repo.get_stops(campaign_id, db_path):
        return 0
    if not territory_repo.researched_territories(db_path):
        return 0
    queued = jobs.enqueue(
        JobType.PLAN_ROUTE,
        {"campaign_id": campaign_id},
        priority=Priority.RESEARCH,
        idempotency_key=f"route:{campaign_id}",
        campaign_id=campaign_id,
        db_path=db_path,
    )
    return 1 if queued else 0


def _queue_discovery(campaign_id: int, db_path: Optional[str]) -> int:
    count = 0
    for stop in campaign_repo.get_stops(campaign_id, db_path):
        if stop.status != "planned":
            continue
        if jobs.enqueue(
            JobType.DISCOVER_LEADS,
            {
                "campaign_id": campaign_id,
                "territory_id": stop.territory_id,
                "target_month": stop.target_month,
            },
            priority=Priority.ENRICHMENT,
            idempotency_key=f"discover:{campaign_id}:{stop.territory_id}",
            campaign_id=campaign_id,
            db_path=db_path,
        ):
            count += 1
    return count


def _queue_enrichment(campaign_id: int, db_path: Optional[str]) -> int:
    count = 0
    for lead in lead_repo.leads_needing_enrichment(
        campaign_id, limit=MAX_ENRICH_PER_TICK, db_path=db_path
    ):
        if jobs.enqueue(
            JobType.ENRICH_LEAD,
            {"lead_id": lead.id, "listing_id": lead.listing_id},
            priority=Priority.ENRICHMENT,
            idempotency_key=f"enrich:{lead.id}",
            campaign_id=campaign_id,
            db_path=db_path,
        ):
            count += 1
    return count


def _queue_scoring(campaign_id: int, db_path: Optional[str]) -> int:
    count = 0
    for lead in lead_repo.leads_needing_scoring(
        campaign_id, limit=MAX_SCORE_PER_TICK, db_path=db_path
    ):
        if jobs.enqueue(
            JobType.SCORE_LEAD,
            {"lead_id": lead.id},
            priority=Priority.ENRICHMENT,
            idempotency_key=f"score:{lead.id}",
            campaign_id=campaign_id,
            db_path=db_path,
        ):
            count += 1
    return count


def _queue_outreach(campaign_id: int, db_path: Optional[str]) -> int:
    """Queue first-touch sends, but never more than the budget can deliver."""
    budget = remaining_sends(db_path)
    if budget <= 0:
        logger.info("Send budget exhausted — not queueing outreach this tick")
        return 0

    count = 0
    for lead in lead_repo.top_unsent_leads(
        campaign_id, limit=min(budget, OUTREACH_QUEUE_DEPTH), db_path=db_path
    ):
        if jobs.enqueue(
            JobType.SEND_OUTREACH,
            {"lead_id": lead.id, "listing_id": lead.listing_id},
            priority=Priority.OUTREACH,
            idempotency_key=f"outreach:{campaign_id}:{lead.listing_id}",
            campaign_id=campaign_id,
            db_path=db_path,
        ):
            count += 1
    return count


def _queue_negotiations(campaign_id: int, db_path: Optional[str]) -> int:
    """Queue replies for warm threads. These outrank outreach in the queue."""
    from app import deals as deal_repo

    count = 0
    for deal in deal_repo.get_deals_by_state(
        DealState.HOST_REPLIED, campaign_id=campaign_id, db_path=db_path
    ):
        if not deal.thread_id:
            continue
        round_number = deal_repo.count_agent_replies(deal.id, db_path) + 1
        if jobs.enqueue(
            JobType.NEGOTIATE_DEAL,
            {"deal_id": deal.id},
            priority=Priority.NEGOTIATION,
            idempotency_key=f"negotiate:{deal.id}:{round_number}",
            campaign_id=campaign_id,
            deal_id=deal.id,
            db_path=db_path,
        ):
            count += 1
    return count


def bootstrap_campaign(
    campaign: Campaign, places: list[str], db_path: Optional[str] = None
) -> int:
    """Create a campaign and seed its candidate territories."""
    campaign_id = campaign_repo.create_campaign(campaign, db_path)
    for place in places:
        territory_repo.upsert_territory(place.strip(), db_path=db_path)
    logger.info("Campaign %s seeded with %d territories", campaign_id, len(places))
    return campaign_id
