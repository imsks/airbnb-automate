"""The Scribe: writes and delivers the first message to a host.

The upgrade over v1 is what goes into the prompt. v1 passed the search-card
fields — title, price, rating — which produce a message any listing could have
received. The Scribe writes from the host's own description, their bio and real
guest reviews, so the opening line is something only someone who read the page
could say.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app import deals as deal_repo
from app import leads as lead_repo
from app import territories as territory_repo
from app.agent.llm import get_llm
from app.agent.prompts_v2 import SCRIBE_PROMPT_VERSION, build_scribe_prompt
from app.agent.runs import tracked_invoke
from app.browser_session import airbnb_page
from app.messaging_errors import DeliveryUnconfirmed, MessageRejected, SessionExpired
from app.models import DealState, Lead, Listing, MessageKind, MessageStatus
from app.policy import GuardrailPolicy, freeze_sending, load_policy, single_message_authorized
from app.send_budget import Channel, reserved_send
from app.warden import review as warden_review

logger = logging.getLogger(__name__)

#: How many times to rewrite a draft that only breaks mechanical wording rules.
_MAX_REVISIONS = 2


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
    preview: bool = False,
    authorization: Optional[str] = None,
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

    key = f"outreach:{lead.campaign_id}:{lead.listing_id}"
    stored = deal_repo.get_message_by_key(key, db_path)
    if stored and stored.status in (MessageStatus.SENT, MessageStatus.SENDING):
        logger.warning("[message #%s] Already submitted or awaiting verification; no second send.", stored.id)
        return {"deal_id": deal_id, "message_id": stored.id, "status": stored.status.value}

    logger.info(
        "[lead #%s] %s | %s | fit=%.2f | enriched=%s",
        lead_id, listing.title, listing.location, lead.collab_fit_score or 0, lead.is_enriched,
    )
    if stored:
        text, message_id = stored.body, stored.id
        logger.info("[draft #%s] Reusing the exact saved draft; no new LLM call.", message_id)
    else:
        logger.info("[draft] Writing from the listing description, amenities, and guest reviews.")
        text, run_id = compose(
            listing, lead, policy=active, job_id=job_id, deal_id=deal_id, db_path=db_path
        )
        message_id = deal_repo.record_message(
            deal_id, text, kind=MessageKind.OUTREACH, agent="scribe",
            prompt_version=SCRIBE_PROMPT_VERSION, agent_run_id=run_id,
            idempotency_key=key, db_path=db_path,
        )
        text = deal_repo.get_message(message_id, db_path).body

    single_allowed = single_message_authorized(message_id, authorization, db_path)
    check_kill = not (preview or single_allowed)
    # A draft Airbnb would refuse is a wording problem, not a judgement call, so
    # rewrite it a bounded number of times before spending a human's attention.
    # Never rewrite under a single-send token: the token is bound to one message id.
    may_revise = authorization is None

    for attempt in range(_MAX_REVISIONS + 1):
        verdict = warden_review(
            text, deal=deal, policy=active, db_path=db_path,
            check_kill_switch=check_kill,
        )
        logger.info("[draft #%s | %d words]\n%s\n[end draft #%s]", message_id, len(text.split()), text, message_id)
        if verdict.allowed or not verdict.revisable_only or not may_revise or attempt == _MAX_REVISIONS:
            break
        logger.warning(
            "[draft #%s] Airbnb would refuse this wording; rewriting (attempt %d of %d). %s",
            message_id, attempt + 1, _MAX_REVISIONS, verdict.reason,
        )
        deal_repo.mark_message_rejected(message_id, verdict.reason, db_path)
        text, run_id = compose(
            listing, lead, policy=active, job_id=job_id, deal_id=deal_id, db_path=db_path
        )
        message_id = deal_repo.record_message(
            deal_id, text, kind=MessageKind.OUTREACH, agent="scribe",
            prompt_version=SCRIBE_PROMPT_VERSION, agent_run_id=run_id,
            idempotency_key=key, db_path=db_path,
        )
        text = deal_repo.get_message(message_id, db_path).body

    if not verdict.allowed:
        deal_repo.mark_message_blocked(message_id, verdict.reason, db_path)
        if not verdict.is_operational_only:
            deal_repo.escalate(deal_id, verdict.reason, db_path=db_path)
        return {
            "deal_id": deal_id,
            "message_id": message_id,
            "status": "blocked",
            "reason": verdict.reason,
            "message": text,
        }

    logger.info("[policy #%s] Draft passed the configured checks. %s", message_id, "Preview only; no send." if preview else "Delivery gate will recheck before Send.")
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
    authorization: Optional[str] = None,
) -> dict:
    """Compose, review, send and link a first-touch message for one lead."""
    from app.outreach import _send_message_to_host

    prepared = await asyncio.to_thread(
        prepare_outreach, lead_id, job_id=job_id, db_path=db_path, authorization=authorization
    )
    if prepared["status"] != "ready":
        return prepared

    deal_id = prepared["deal_id"]
    message_id = prepared["message_id"]
    listing = prepared["listing"]

    async def before_send() -> None:
        deal = deal_repo.get_deal(deal_id, db_path)
        if deal.state not in (DealState.DISCOVERED, DealState.QUALIFIED):
            raise ValueError("Deal changed while the composer was opening; nothing was submitted.")
        verdict = warden_review(
            prepared["message"], deal=deal, db_path=db_path,
            check_kill_switch=not single_message_authorized(message_id, authorization, db_path),
        )
        if not verdict.allowed:
            raise PermissionError(verdict.reason)
        deal_repo.begin_message_delivery(
            message_id, prepared["message"], db_path, authorization=authorization
        )

    try:
        async with airbnb_page(headless=headless) as page:
            async with reserved_send(
                Channel.OUTREACH, db_path, message_id=message_id, authorization=authorization
            ):
                thread_id, thread_url = await _send_message_to_host(
                    page, listing, prepared["message"], before_send=before_send
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

        logger.info("[success] Message #%s delivered | host=%s | thread=%s | %s", message_id, listing.host_name or "host", thread_id, thread_url)
        return {"deal_id": deal_id, "message_id": message_id, "status": "sent", "thread_id": thread_id, "thread_url": thread_url}
    except MessageRejected as exc:
        # Airbnb vetted the text and refused it, so nothing was delivered and
        # the system is healthy. Release the draft for a rewrite; stay unfrozen.
        deal_repo.mark_message_rejected(message_id, str(exc), db_path)
        logger.error("[rejected #%s] %s", message_id, exc)
        raise
    except DeliveryUnconfirmed as exc:
        deal_repo.mark_message_unconfirmed(message_id, str(exc), db_path)
        deal_repo.escalate(deal_id, str(exc), db_path=db_path)
        freeze_sending("Delivery requires verification; automatic retry stopped.", db_path)
        logger.error("[unconfirmed #%s] %s", message_id, exc)
        raise
    except SessionExpired:
        deal_repo.mark_message_failed(message_id, "Sign-in required before any submission", db_path)
        freeze_sending("Airbnb sign-in required", db_path)
        raise
    except Exception as exc:
        current = deal_repo.get_message(message_id, db_path)
        if current.status is MessageStatus.SENDING:
            deal_repo.mark_message_unconfirmed(message_id, str(exc), db_path)
            freeze_sending("Submission outcome is uncertain; inspect the thread.", db_path)
            raise DeliveryUnconfirmed("Submission may have happened; no automatic retry.") from exc
        deal_repo.mark_message_failed(message_id, str(exc), db_path)
        logger.error("[message #%s] %s", message_id, exc)
        raise
