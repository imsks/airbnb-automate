"""The Closer: negotiates one deal at a time, and actually sends.

v1's negotiator rescanned the whole inbox, picked a single thread, printed a
draft, and stopped at a TODO. It had no memory between runs and no link back to
the listing, so every cycle re-derived context from scraped text.

The Closer works from a Deal instead. The conversation is already stored, the
round count is known, and the reply goes through the Warden and the shared send
budget before it reaches a host.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app import deals as deal_repo
from app.agent.llm import get_llm
from app.agent.prompts_v2 import (
    CLOSER_PROMPT_VERSION,
    TERMS_HUMAN,
    TERMS_PROMPT_VERSION,
    TERMS_SYSTEM,
    build_closer_prompt,
)
from app.agent.runs import tracked_invoke
from app.models import Deal, DealState, MessageKind
from app.policy import GuardrailPolicy, load_policy
from app.warden import review as warden_review

logger = logging.getLogger(__name__)


class NotNegotiable(Exception):
    """Raised when a deal is not in a state the Closer may act on."""


_NEGOTIABLE_STATES = frozenset(
    {DealState.HOST_REPLIED, DealState.NEGOTIATING, DealState.CONTACTED}
)


def draft_reply(
    deal: Deal,
    *,
    policy: Optional[GuardrailPolicy] = None,
    job_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> tuple[str, int]:
    """Generate the next reply for a deal. Returns ``(text, agent_run_id)``."""
    active = policy or load_policy(db_path)
    conversation = deal_repo.conversation_text(deal.id, db_path)
    round_number = deal_repo.count_agent_replies(deal.id, db_path) + 1

    system, human = build_closer_prompt(
        place_name=deal.place_name,
        host_name=deal.host_name,
        location=deal.location,
        booking_status=deal.state_reason,
        conversation=conversation,
        round_number=round_number,
        policy=active,
    )
    return tracked_invoke(
        get_llm(),
        [SystemMessage(content=system), HumanMessage(content=human)],
        agent="closer",
        job_id=job_id,
        deal_id=deal.id,
        prompt_version=CLOSER_PROMPT_VERSION,
        db_path=db_path,
    )


def prepare_reply(
    deal_id: int,
    *,
    policy: Optional[GuardrailPolicy] = None,
    job_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> dict:
    """Draft, review and stage a reply without sending it.

    Splitting preparation from delivery keeps every LLM and guardrail decision
    testable without a browser, and means a send failure never loses the draft.
    """
    deal = deal_repo.get_deal(deal_id, db_path)
    if deal is None:
        raise KeyError(f"No deal with id {deal_id}")
    if deal.state not in _NEGOTIABLE_STATES:
        raise NotNegotiable(f"Deal {deal_id} is {deal.state.value}")

    active = policy or load_policy(db_path)
    text, run_id = draft_reply(deal, policy=active, job_id=job_id, db_path=db_path)

    round_number = deal_repo.count_agent_replies(deal.id, db_path) + 1
    message_id = deal_repo.record_message(
        deal_id,
        text,
        kind=MessageKind.NEGOTIATION,
        agent="closer",
        prompt_version=CLOSER_PROMPT_VERSION,
        agent_run_id=run_id,
        idempotency_key=f"negotiate:{deal_id}:{round_number}",
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
            "reply": text,
        }

    deal_repo.advance_to(
        deal_id,
        DealState.NEGOTIATING,
        reason=f"closer round {round_number}",
        actor="closer",
        db_path=db_path,
    )
    return {
        "deal_id": deal_id,
        "message_id": message_id,
        "status": "ready",
        "reply": text,
        "thread_id": deal.thread_id,
        "round": round_number,
    }


# --- Terms extraction ------------------------------------------------------


def _parse_terms(raw: str) -> dict:
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        return {"agreed": False, "reason": "no JSON in response"}
    try:
        return json.loads(match.group())
    except ValueError:
        return {"agreed": False, "reason": "unparseable JSON"}


def _as_float(value) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_int(value) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def extract_terms(
    deal_id: int,
    *,
    job_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> dict:
    """Read agreed terms out of a thread and advance the deal if one exists.

    A deal only reaches READY_TO_BOOK here. Booking needs a card, which no agent
    has, so this is the last state the system can reach on its own.
    """
    deal = deal_repo.get_deal(deal_id, db_path)
    if deal is None:
        raise KeyError(f"No deal with id {deal_id}")

    conversation = deal_repo.conversation_text(deal_id, db_path)
    if not conversation.strip():
        return {"agreed": False, "reason": "empty conversation"}

    raw, _ = tracked_invoke(
        get_llm(),
        [
            SystemMessage(content=TERMS_SYSTEM),
            HumanMessage(content=TERMS_HUMAN.format(conversation=conversation)),
        ],
        agent="terms",
        job_id=job_id,
        deal_id=deal_id,
        prompt_version=TERMS_PROMPT_VERSION,
        db_path=db_path,
    )

    terms = _parse_terms(raw)
    if not terms.get("agreed"):
        return {"agreed": False, "reason": terms.get("reason", "no agreement yet")}

    deal_repo.set_terms(
        deal_id,
        price_per_night=_as_float(terms.get("price_per_night")),
        currency=str(terms.get("currency", "") or ""),
        discount_pct=_as_float(terms.get("discount_pct")),
        window_start=str(terms.get("window_start", "") or ""),
        window_end=str(terms.get("window_end", "") or ""),
        nights=_as_int(terms.get("nights")),
        deliverables=[str(d) for d in (terms.get("deliverables") or [])],
        confidence=_as_float(terms.get("confidence")),
        booking_url=deal.thread_url or deal.listing_url,
        db_path=db_path,
    )
    deal_repo.advance_to(
        deal_id,
        DealState.READY_TO_BOOK,
        reason=terms.get("reason", "terms agreed"),
        actor="closer",
        db_path=db_path,
    )
    logger.info("🤝 Deal %s is ready to book: %s", deal_id, terms.get("reason", ""))
    return {"agreed": True, **terms}
