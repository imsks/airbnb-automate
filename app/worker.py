"""The worker: the only process that drives a browser.

Keeping this separate from the API is a hard rule, not a preference. Playwright
is async and a scrape can run for minutes; if request handlers drove it, the
browser profile and the event loop would be contended by whoever happened to
hit an endpoint.

Here the API only ever writes rows to the ``jobs`` table. This process leases
them, does the work, and is the sole owner of the browser session.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import Any, Callable, Optional

from app import campaigns as campaign_repo
from app import deals as deal_repo
from app import inbox, jobs, leads as lead_repo, territories as territory_repo
from app.agent import planner
from app.agent.analyst import score_lead_with_llm
from app.agent.chat_reader import SessionExpired
from app.agent.chronicler import daily_brief
from app.agent.closer import NotNegotiable, extract_terms, prepare_reply
from app.agent.router import plan_route
from app.agent.scout import research_territory
from app.database import get_connection, get_listings, init_db
from app.jobs import JobType
from app.listing_detail import scrape_listing_detail
from app.models import DealState, Job
from app.policy import freeze_sending
from app.send_budget import SendingFrozen

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5.0
PLANNER_TICK_SECONDS = 300.0
STALE_AFTER_DAYS = 10


class Worker:
    """Leases jobs and runs them until stopped."""

    def __init__(
        self,
        *,
        campaign_id: Optional[int] = None,
        headless: bool = True,
        db_path: Optional[str] = None,
        name: str = "worker-1",
    ) -> None:
        self.campaign_id = campaign_id
        self.headless = headless
        self.db_path = db_path
        self.name = name
        self._stop = False
        self._last_tick = 0.0
        self._handlers: dict[str, Callable[[Job], Any]] = {
            JobType.RESEARCH_TERRITORY: self._research_territory,
            JobType.PLAN_ROUTE: self._plan_route,
            JobType.DISCOVER_LEADS: self._discover_leads,
            JobType.ENRICH_LEAD: self._enrich_lead,
            JobType.SCORE_LEAD: self._score_lead,
            JobType.SEND_OUTREACH: self._send_outreach,
            JobType.SYNC_INBOX: self._sync_inbox,
            JobType.NEGOTIATE_DEAL: self._negotiate_deal,
            JobType.EXTRACT_TERMS: self._extract_terms,
            JobType.SWEEP_STALE: self._sweep_stale,
            JobType.DAILY_BRIEF: self._daily_brief,
            JobType.PLANNER_TICK: self._planner_tick,
        }

    def stop(self) -> None:
        """Ask the loop to finish the current job and exit."""
        self._stop = True

    async def run(self, once: bool = False) -> None:
        """Drain the queue until stopped."""
        scope = (
            "all active campaigns"
            if self.campaign_id is None
            else f"campaign {self.campaign_id}"
        )
        logger.info("👷 %s started (%s)", self.name, scope)
        while not self._stop:
            jobs.reclaim_expired_leases(self.db_path)
            await self._maybe_tick()

            leased = jobs.lease(self.name, limit=1, db_path=self.db_path)
            if not leased:
                if once:
                    return
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            await self._execute(leased[0])
            if once:
                return

    async def _maybe_tick(self) -> None:
        if time.time() - self._last_tick < PLANNER_TICK_SECONDS:
            return
        self._last_tick = time.time()
        planner.plan_tick(self.campaign_id, self.db_path)

    async def _execute(self, job: Job) -> None:
        handler = self._handlers.get(job.type)
        if handler is None:
            jobs.fail(job.id, f"No handler for job type {job.type!r}", self.db_path)
            return

        logger.info("▶️  %s #%s (attempt %s)", job.type, job.id, job.attempts)
        try:
            result = handler(job)
            if asyncio.iscoroutine(result):
                result = await result
            jobs.complete(job.id, result if isinstance(result, dict) else {}, self.db_path)
        except SessionExpired as exc:
            # Every browser job will fail the same way until a human signs in,
            # and sending while logged out is how accounts get flagged.
            jobs.cancel(job.id, str(exc), self.db_path)
            freeze_sending(f"Airbnb session expired: {exc}", self.db_path)
            logger.error("🔑 %s — sending frozen until you log in again", exc)
        except SendingFrozen as exc:
            # Retrying would just hit the switch again; a human must release it.
            jobs.cancel(job.id, str(exc), self.db_path)
            logger.warning("🛑 %s #%s cancelled: %s", job.type, job.id, exc)
        except NotNegotiable as exc:
            jobs.cancel(job.id, str(exc), self.db_path)
        except Exception as exc:
            status = jobs.fail(job.id, str(exc), self.db_path)
            logger.error("❌ %s #%s failed (%s): %s", job.type, job.id, status.value, exc)

    # --- Handlers ----------------------------------------------------------

    def _research_territory(self, job: Job) -> dict:
        territory_id = int(job.payload["territory_id"])
        territory = territory_repo.get_territory(territory_id, self.db_path)
        if territory is None:
            return {"skipped": "territory missing"}
        research_territory(
            territory_id,
            territory.name,
            country=territory.country,
            job_id=job.id,
            db_path=self.db_path,
        )
        return {"territory_id": territory_id}

    def _plan_route(self, job: Job) -> dict:
        campaign_id = int(job.payload["campaign_id"])
        campaign = campaign_repo.get_campaign(campaign_id, self.db_path)
        if campaign is None:
            return {"skipped": "campaign missing"}

        territories = territory_repo.researched_territories(self.db_path)
        profiles = {
            t.id: territory_repo.get_current_profile(t.id, self.db_path)
            for t in territories
        }
        stops = plan_route(campaign, territories, profiles)
        campaign_repo.save_stops(campaign_id, stops, self.db_path)
        return {"stops": len(stops), "itinerary": [s.territory_name for s in stops]}

    async def _discover_leads(self, job: Job) -> dict:
        from app.scraper import scrape_listings

        campaign_id = int(job.payload.get("campaign_id", 0))
        territory_id = int(job.payload["territory_id"])
        territory = territory_repo.get_territory(territory_id, self.db_path)
        if territory is None:
            return {"skipped": "territory missing"}

        campaign = campaign_repo.get_campaign(campaign_id, self.db_path)
        listings = await scrape_listings(
            territory.name,
            guests=campaign.guests if campaign else 2,
            headless=self.headless,
        )
        search_id = _record_search(territory.name, listings, self.db_path)

        for listing in listings:
            lead_repo.upsert_lead(
                listing.id,
                campaign_id=campaign_id,
                territory_id=territory_id,
                search_id=search_id,
                db_path=self.db_path,
            )
        territory_repo.record_discovery(territory_id, len(listings), self.db_path)
        return {"territory": territory.name, "leads": len(listings)}

    async def _enrich_lead(self, job: Job) -> dict:
        lead_id = int(job.payload["lead_id"])
        lead = lead_repo.get_lead(lead_id, self.db_path)
        if lead is None:
            return {"skipped": "lead missing"}

        listing = _find_listing(lead.listing_id, self.db_path)
        detail = await scrape_listing_detail(
            lead.listing_id,
            url=listing.url if listing else "",
            headless=self.headless,
        )
        lead_repo.save_enrichment(lead_id, detail, self.db_path)
        return {"lead_id": lead_id, "amenities": len(detail.get("amenities", []))}

    def _score_lead(self, job: Job) -> dict:
        lead_id = int(job.payload["lead_id"])
        lead = lead_repo.get_lead(lead_id, self.db_path)
        if lead is None:
            return {"skipped": "lead missing"}

        listing = _find_listing(lead.listing_id, self.db_path)
        score, breakdown, rationale = score_lead_with_llm(
            lead, listing, job_id=job.id, db_path=self.db_path
        )
        lead_repo.save_score(lead_id, score, breakdown, rationale, self.db_path)

        deal_id = deal_repo.upsert_deal(
            lead.listing_id,
            campaign_id=lead.campaign_id,
            lead_id=lead_id,
            territory_id=lead.territory_id,
            host_name=listing.host_name if listing else "",
            place_name=listing.title if listing else "",
            location=listing.location if listing else "",
            listing_url=listing.url if listing else "",
            db_path=self.db_path,
        )
        deal_repo.advance_to(
            deal_id,
            DealState.QUALIFIED,
            reason=f"score {score:.2f}",
            actor="analyst",
            db_path=self.db_path,
        )
        return {"lead_id": lead_id, "score": score}

    async def _send_outreach(self, job: Job) -> dict:
        from app.agent.scribe import send_outreach_for_lead

        return await send_outreach_for_lead(
            int(job.payload["lead_id"]),
            headless=self.headless,
            job_id=job.id,
            db_path=self.db_path,
        )

    async def _sync_inbox(self, job: Job) -> dict:
        return await inbox.sync_inbox(
            max_threads=int(job.payload.get("max_threads", 20)),
            headless=self.headless,
            db_path=self.db_path,
        )

    async def _negotiate_deal(self, job: Job) -> dict:
        deal_id = int(job.payload["deal_id"])
        prepared = prepare_reply(deal_id, job_id=job.id, db_path=self.db_path)
        if prepared["status"] != "ready":
            return prepared

        await inbox.send_reply(
            deal_id,
            prepared["message_id"],
            headless=self.headless,
            db_path=self.db_path,
        )
        jobs.enqueue(
            JobType.EXTRACT_TERMS,
            {"deal_id": deal_id},
            priority=jobs.Priority.INBOX_SYNC,
            idempotency_key=f"terms:{deal_id}:{prepared['round']}",
            deal_id=deal_id,
            db_path=self.db_path,
        )
        return {"deal_id": deal_id, "status": "sent", "round": prepared["round"]}

    def _extract_terms(self, job: Job) -> dict:
        return extract_terms(
            int(job.payload["deal_id"]), job_id=job.id, db_path=self.db_path
        )

    def _sweep_stale(self, job: Job) -> dict:
        return sweep_stale(days=STALE_AFTER_DAYS, db_path=self.db_path)

    def _daily_brief(self, job: Job) -> dict:
        return daily_brief(self.db_path)

    def _planner_tick(self, job: Job) -> dict:
        return planner.plan_tick(self.campaign_id, self.db_path)


def sweep_stale(days: int = STALE_AFTER_DAYS, db_path: Optional[str] = None) -> dict:
    """Retire contacted deals the host never answered.

    Without this the funnel fills with threads that will never move, and the
    Planner keeps counting them as live pipeline.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT id FROM deals
                WHERE state = 'contacted'
                  AND (last_inbound_at IS NULL)
                  AND (last_outbound_at IS NOT NULL AND last_outbound_at < ?)""",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        deal_repo.escalate_or_close(
            int(row["id"]),
            DealState.STALE,
            f"no reply in {days} days",
            actor="sweeper",
            db_path=db_path,
        )
    logger.info("🧹 Swept %d stale deal(s)", len(rows))
    return {"swept": len(rows)}


def _record_search(location: str, listings: list, db_path: Optional[str]) -> int:
    from app.database import create_search, save_listings, update_search_status
    from app.models import Search, SearchStatus

    search_id = create_search(Search(location=location), db_path)
    save_listings(search_id, listings, db_path)
    update_search_status(search_id, SearchStatus.COMPLETED, len(listings), db_path)
    return search_id


def _find_listing(listing_id: str, db_path: Optional[str]):
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT search_id FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()
        search_id = row["search_id"] if row else None
    finally:
        conn.close()
    if search_id is None:
        return None
    return next(
        (l for l in get_listings(search_id, db_path) if l.id == listing_id), None
    )


def main(
    campaign_id: Optional[int] = None,
    headless: bool = True,
    db_path: Optional[str] = None,
    once: bool = False,
) -> None:
    """Run a worker until interrupted."""
    from app.logging_config import setup_logging

    setup_logging()
    init_db(db_path)
    worker = Worker(campaign_id=campaign_id, headless=headless, db_path=db_path)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: worker.stop())
        except ValueError:  # not on the main thread
            pass

    asyncio.run(worker.run(once=once))
    logger.info("👷 Worker stopped")


if __name__ == "__main__":  # pragma: no cover
    main()
