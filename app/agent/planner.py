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
import time
from typing import Optional

from app import campaigns as campaign_repo
from app import deals as deal_repo
from app import jobs, leads as lead_repo, territories as territory_repo
from app.jobs import JobType, Priority
from app.models import Campaign, CampaignStatus, DealState
from app.policy import host_messaging_delay_seconds
from app.send_budget import remaining_sends

logger = logging.getLogger(__name__)

#: Keep a shallow send queue so the highest-scoring lead at send time wins,
#: rather than whichever lead happened to be scored first.
OUTREACH_QUEUE_DEPTH = 2
MAX_RESEARCH_PER_TICK = 10
MAX_ENRICH_PER_TICK = 25
MAX_SCORE_PER_TICK = 25
#: Drafting runs ahead of the send budget so the courier always has ready
#: messages waiting. Kept a comfortable buffer ahead of a 20-per-window budget
#: so a hands-off office is never idle waiting on the Scribe.
MAX_DRAFTS_PER_TICK = 15
#: Best-effort portal corroboration; low priority, so it never crowds out sends.
MAX_PORTAL_PULLS_PER_TICK = 10

#: Deals backfilled from v1 have no campaign, so the default bucket is always
#: ticked alongside real campaigns — otherwise old threads are never negotiated.
DEFAULT_BUCKET = 0

#: The one standing campaign the autonomous office works out of. Its presence is
#: what switches on place-proposing; explicit campaigns created by a human do
#: not auto-propose.
SYSTEM_CAMPAIGN_NAME = "Standing deal office"
SYSTEM_CAMPAIGN_GOAL = "Continuously find content-for-stay deals, all over India and abroad."
#: Seeded once as VISITED so the Proposer never sends you back where you have been.
ALREADY_VISITED = ("Malaysia", "Maldives")


def get_system_campaign_id(db_path: Optional[str] = None) -> Optional[int]:
    """The id of the standing office campaign, or ``None`` if it does not exist yet."""
    for campaign in campaign_repo.list_campaigns(db_path):
        if (
            campaign.name == SYSTEM_CAMPAIGN_NAME
            and campaign.status is CampaignStatus.ACTIVE
            and campaign.id is not None
        ):
            return campaign.id
    return None


def ensure_system_campaign(db_path: Optional[str] = None) -> int:
    """Create the standing office campaign once, seeding places you have visited.

    Idempotent: returns the existing campaign if it is already there. This is
    what replaces the old "fill in a campaign form" step — the office simply
    starts working the moment a worker comes up.
    """
    existing = get_system_campaign_id(db_path)
    if existing is not None:
        return existing

    campaign_id = campaign_repo.create_campaign(
        Campaign(
            name=SYSTEM_CAMPAIGN_NAME,
            goal=SYSTEM_CAMPAIGN_GOAL,
            status=CampaignStatus.ACTIVE,
        ),
        db_path,
    )
    for place in ALREADY_VISITED:
        territory_repo.mark_visited(place, db_path=db_path)
    logger.info("🏢 Standing deal office ready (campaign #%s)", campaign_id)
    return campaign_id


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

    # Proposing new places and researching them belong to no campaign, so they
    # are queued once per tick rather than per bucket.
    queued: dict[str, int] = {
        "propose": _queue_propose(db_path),
        "research": _queue_research(db_path),
    }
    for key in (
        "route", "discover", "enrich", "score", "draft", "outreach",
        "negotiate", "portal",
    ):
        queued[key] = 0

    for bucket in buckets:
        queued["route"] += _queue_route(bucket, db_path)
        queued["discover"] += _queue_discovery(bucket, db_path)
        queued["enrich"] += _queue_enrichment(bucket, db_path)
        queued["score"] += _queue_scoring(bucket, db_path)
        queued["draft"] += _queue_drafts(bucket, db_path)
        queued["outreach"] += _queue_outreach(bucket, db_path)
        queued["negotiate"] += _queue_negotiations(bucket, db_path)
        queued["portal"] += _queue_portal_context(bucket, db_path)

    total = sum(queued.values())
    if total:
        logger.info("🧭 Planner queued %d job(s): %s", total, _summarise(queued))
    else:
        logger.debug("🧭 Planner tick: nothing to queue")
    return {"queued": queued, "total": total, "campaigns": buckets}


def _summarise(queued: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in queued.items() if v)


def _queue_propose(db_path: Optional[str]) -> int:
    """Ask for new places when the standing office is running low on candidates.

    Only the standing office proposes — a human-made campaign works the list it
    was given. At most one proposal is ever in flight.
    """
    if get_system_campaign_id(db_path) is None:
        return 0

    from app.agent.proposer import MIN_LIVE_TERRITORIES

    if territory_repo.live_territory_count(db_path) >= MIN_LIVE_TERRITORIES:
        return 0
    if jobs.count_active(JobType.PROPOSE_TERRITORIES, db_path) > 0:
        return 0
    queued = jobs.enqueue(
        JobType.PROPOSE_TERRITORIES,
        priority=Priority.RESEARCH,
        db_path=db_path,
    )
    return 1 if queued else 0


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


def _queue_drafts(campaign_id: int, db_path: Optional[str]) -> int:
    """Write opening drafts ahead of the send budget (office work, no browser).

    Drafting deliberately ignores the send budget: a frozen or budget-starved
    system should still have ready messages waiting the moment it can send.
    """
    count = 0
    for lead in lead_repo.top_unsent_leads(
        campaign_id, limit=MAX_DRAFTS_PER_TICK, db_path=db_path
    ):
        if jobs.enqueue(
            JobType.DRAFT_OUTREACH,
            {"lead_id": lead.id, "listing_id": lead.listing_id},
            priority=Priority.DRAFTING,
            idempotency_key=f"draft:{campaign_id}:{lead.listing_id}",
            campaign_id=campaign_id,
            db_path=db_path,
        ):
            count += 1
    return count


def _queue_portal_context(campaign_id: int, db_path: Optional[str]) -> int:
    """Pull best-effort Booking.com context for enriched listings that lack it."""
    from app import portals

    count = 0
    for listing_id in portals.listings_missing_context(
        campaign_id, portal="booking", limit=MAX_PORTAL_PULLS_PER_TICK, db_path=db_path
    ):
        if jobs.enqueue(
            JobType.PULL_PORTAL_CONTEXT,
            {"listing_id": listing_id, "portal": "booking"},
            priority=Priority.HOUSEKEEPING,
            campaign_id=campaign_id,
            idempotency_key=f"portal:booking:{listing_id}",
            db_path=db_path,
        ):
            count += 1
    return count


def _queue_outreach(campaign_id: int, db_path: Optional[str]) -> int:
    """Queue delivery of staged drafts, never more than the budget can deliver.

    A send is only queued once the office has produced a draft for it — the
    courier never composes.
    """
    budget = remaining_sends(db_path)
    if budget <= 0:
        logger.info("Send budget exhausted — not queueing outreach this tick")
        return 0

    released = deal_repo.release_kill_switch_drafts(db_path)
    if released:
        logger.info("Released %d draft(s) the kill switch had paused", released)

    count = 0
    for deal in deal_repo.deals_ready_to_send(
        campaign_id, limit=min(budget, OUTREACH_QUEUE_DEPTH), db_path=db_path
    ):
        if deal.lead_id is None:
            continue
        key = f"outreach:{campaign_id}:{deal.listing_id}"
        delay = host_messaging_delay_seconds(db_path)
        queued = jobs.enqueue(
            JobType.SEND_OUTREACH,
            {"lead_id": deal.lead_id, "listing_id": deal.listing_id},
            priority=Priority.OUTREACH,
            delay_seconds=delay,
            idempotency_key=key,
            campaign_id=campaign_id,
            deal_id=deal.id,
            db_path=db_path,
        ) or jobs.reopen_paused_send(key, db_path)
        if queued and delay > 0:
            jobs.defer_pending(JobType.SEND_OUTREACH, time.time() + delay, db_path)
        if queued:
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
