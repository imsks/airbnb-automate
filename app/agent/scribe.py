"""The Scribe: writes and delivers the first message to a host.

The upgrade over v1 is what goes into the prompt. v1 passed the search-card
fields — title, price, rating — which produce a message any listing could have
received. The Scribe writes from the host's own description, their bio and real
guest reviews, so the opening line is something only someone who read the page
could say.
"""

from __future__ import annotations

import logging
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app import deals as deal_repo
from app import leads as lead_repo
from app import territories as territory_repo
from app.agent.llm import get_llm
from app.agent.prompts_v2 import SCRIBE_PROMPT_VERSION, build_scribe_prompt
from app.agent.runs import tracked_invoke
from app.browser_session import close_airbnb_session, open_airbnb_browser
from app.models import DealState, Lead, Listing, MessageKind
from app.policy import GuardrailPolicy, load_policy
from app.send_budget import Channel, reserved_send
from app.warden import review as warden_review

logger = logging.getLogger(__name__)


def territory_note(territory_id: Optional[int], db_path: Optional[str] = None) -> str:
    """One line of local colour from the Scout, if the place has been researched."""
    if not territory_id:
        return ""
    profile = territory_repo.get_current_profile(territory_id, db_path)
    if profile is None or not profile.content_angles:
        return ""
    return (
        "Context about the area (use only if it fits naturally): "
        + "; ".join(profile.content_angles[:3])
    )


def compose(
    listing: Listing,
    lead: Optional[Lead],
    *,
    policy: Optional[GuardrailPolicy] = None,
    job_id: Optional[int] = None,
    deal_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> tuple[str, int]:
    """Write the opening message. Returns ``(text, agent_run_id)``."""
    active = policy or load_policy(db_path)
    system, human = build_scribe_prompt(
        listing,
        lead,
        active,
        territory_note(lead.territory_id if lead else None, db_path),
    )
    return tracked_invoke(
        get_llm(),
        [SystemMessage(content=system), HumanMessage(content=human)],
        agent="scribe",
        job_id=job_id,
        deal_id=deal_id,
        prompt_version=SCRIBE_PROMPT_VERSION,
        db_path=db_path,
    )


def prepare_outreach(
    lead_id: int,
    *,
    policy: Optional[GuardrailPolicy] = None,
    job_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> dict:
    """Write, review and stage an opening message without sending it."""
    from app.worker import _find_listing

    lead = lead_repo.get_lead(lead_id, db_path)
    if lead is None:
        raise KeyError(f"No lead with id {lead_id}")

    listing = _find_listing(lead.listing_id, db_path) or Listing(id=lead.listing_id)
    active = policy or load_policy(db_path)

    deal_id = deal_repo.upsert_deal(
        lead.listing_id,
        campaign_id=lead.campaign_id,
        lead_id=lead_id,
        territory_id=lead.territory_id,
        host_name=listing.host_name,
        place_name=listing.title,
        location=listing.location,
        listing_url=listing.url,
        db_path=db_path,
    )
    deal = deal_repo.get_deal(deal_id, db_path)
    if deal.state not in (DealState.DISCOVERED, DealState.QUALIFIED):
        return {"deal_id": deal_id, "status": "already_contacted"}

    text, run_id = compose(
        listing, lead, policy=active, job_id=job_id, deal_id=deal_id, db_path=db_path
    )
    message_id = deal_repo.record_message(
        deal_id,
        text,
        kind=MessageKind.OUTREACH,
        agent="scribe",
        prompt_version=SCRIBE_PROMPT_VERSION,
        agent_run_id=run_id,
        idempotency_key=f"outreach:{lead.campaign_id}:{lead.listing_id}",
        db_path=db_path,
    )

    verdict = warden_review(text, deal=deal, policy=active, db_path=db_path)
    if not verdict.allowed:
        deal_repo.mark_message_blocked(message_id, verdict.reason, db_path)
        deal_repo.escalate(deal_id, verdict.reason, db_path=db_path)
        return {
            "deal_id": deal_id,
            "message_id": message_id,
            "status": "blocked",
            "reason": verdict.reason,
        }

    return {
        "deal_id": deal_id,
        "message_id": message_id,
        "status": "ready",
        "listing": listing,
        "message": text,
    }


async def send_outreach_for_lead(
    lead_id: int,
    *,
    headless: bool = True,
    job_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> dict:
    """Compose, review, send and link a first-touch message for one lead."""
    from app.outreach import _send_message_to_host

    prepared = prepare_outreach(lead_id, job_id=job_id, db_path=db_path)
    if prepared["status"] != "ready":
        return prepared

    deal_id = prepared["deal_id"]
    message_id = prepared["message_id"]
    listing = prepared["listing"]

    context, page, browser, uses_cdp = await open_airbnb_browser(headless=headless)
    try:
        async with reserved_send(Channel.OUTREACH, db_path):
            thread_id, thread_url = await _send_message_to_host(
                page, listing, prepared["message"]
            )
        deal_repo.mark_message_sent(message_id, db_path)
        deal_repo.advance_to(
            deal_id,
            DealState.CONTACTED,
            reason="outreach sent",
            actor="scribe",
            db_path=db_path,
        )
        if thread_id:
            deal_repo.link_thread(
                deal_id, thread_id, thread_url=thread_url, db_path=db_path
            )

        lead = lead_repo.get_lead(lead_id, db_path)
        if lead and lead.territory_id:
            territory_repo.record_send(lead.territory_id, db_path)

        logger.info("✅ Outreach sent to %s", listing.host_name or "host")
        return {"deal_id": deal_id, "status": "sent", "thread_id": thread_id}
    except Exception as exc:
        deal_repo.mark_message_failed(message_id, str(exc), db_path)
        raise
    finally:
        await close_airbnb_session(context, browser, uses_cdp)
