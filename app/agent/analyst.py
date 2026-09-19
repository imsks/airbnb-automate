"""The Analyst: decides which leads deserve one of our scarce sends.

Airbnb caps us at a handful of messages per window, so the binding constraint
is not how many hosts exist but which few we spend a send on. Scoring is
deliberately deterministic and explainable — a weighted sum over signals we can
point at — with an optional LLM pass that can only nudge the result.

The core insight it encodes: the hosts most likely to say yes are the ones with
an empty calendar and something to prove, not the established Superhosts.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.llm import get_llm
from app.agent.runs import tracked_invoke
from app.models import Lead, Listing

logger = logging.getLogger(__name__)

PROMPT_VERSION = "analyst-v1"

#: Must sum to 1.0 — asserted at import so a bad edit fails loudly.
WEIGHTS = {
    "host_hunger": 0.30,
    "long_stay_openness": 0.20,
    "negotiability": 0.10,
    "affordability": 0.15,
    "workation_fit": 0.15,
    "content_potential": 0.10,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "Analyst weights must sum to 1.0"

#: Above this many reviews a host is established and has no reason to need us.
_ESTABLISHED_REVIEWS = 50
#: Nightly price treated as "expensive" when normalising affordability.
_PRICE_REFERENCE = 8000.0

_WORKATION_TERMS = (
    "wifi",
    "wi-fi",
    "internet",
    "workspace",
    "work space",
    "desk",
    "kitchen",
    "washer",
    "air conditioning",
)
_CONTENT_TERMS = (
    "pool",
    "view",
    "villa",
    "beach",
    "mountain",
    "balcony",
    "terrace",
    "garden",
    "lake",
    "riverside",
    "sunset",
    "treehouse",
    "cabin",
    "heritage",
    "rooftop",
)

ANALYST_SYSTEM = """You assess Airbnb listings for a travel-content collaboration.

The creator asks hosts for a free or heavily discounted stay in exchange for
reels, photos and an honest review. Your job is to judge how receptive THIS host
is likely to be.

Hosts who say yes tend to have: a new or quiet listing, few reviews, no
Superhost badge, existing long-stay discounts, and a personal (not corporate)
tone. Hosts who say no tend to be: booked-out Superhosts, professional property
managers, and luxury listings with strong demand.

Reply with JSON only: {"adjustment": <number between -0.2 and 0.2>, "reason": "<one sentence>"}
The adjustment nudges a numeric score already computed from hard signals.
Return 0 if you have no strong view."""

ANALYST_HUMAN = """Listing: {title}
Location: {location}
Price: {price} {currency} per night
Rating: {rating} from {review_count} reviews
Superhost: {superhost}
Host bio: {host_bio}
Description: {description}
Guest reviews: {reviews}

Assess host receptiveness."""


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _host_hunger(lead: Lead, listing: Optional[Listing]) -> float:
    """How much this host likely needs bookings. Higher is better for us."""
    score = 0.0
    if not lead.host_is_superhost and not (listing and listing.superhost):
        score += 0.4

    reviews = listing.review_count if listing else 0
    score += 0.4 * _clamp(1.0 - reviews / _ESTABLISHED_REVIEWS)

    if lead.listing_age_months is not None and lead.listing_age_months <= 12:
        score += 0.2
    return _clamp(score)


def _affordability(listing: Optional[Listing]) -> float:
    """Cheaper markets convert better on a free-stay ask."""
    if not listing or not listing.price_per_night:
        return 0.5
    return _clamp(1.0 - listing.price_per_night / _PRICE_REFERENCE)


def _term_coverage(haystack: str, terms: tuple[str, ...], saturate_at: int) -> float:
    lowered = haystack.lower()
    hits = sum(1 for term in terms if term in lowered)
    return _clamp(hits / saturate_at)


def _lead_text(lead: Lead) -> str:
    return " ".join(
        [lead.description, lead.house_rules, " ".join(lead.amenities), lead.host_bio]
    )


def score_lead(
    lead: Lead, listing: Optional[Listing] = None
) -> tuple[float, dict[str, float], str]:
    """Score a lead from hard signals alone. Returns ``(score, breakdown, rationale)``."""
    text = _lead_text(lead)
    breakdown = {
        "host_hunger": _host_hunger(lead, listing),
        "long_stay_openness": 1.0 if lead.has_long_stay_discount else 0.0,
        # Manual review means a human reads the request and can be persuaded.
        "negotiability": 0.0 if lead.instant_book else 1.0,
        "affordability": _affordability(listing),
        "workation_fit": _term_coverage(text, _WORKATION_TERMS, 4),
        "content_potential": _term_coverage(text, _CONTENT_TERMS, 3),
    }
    score = sum(WEIGHTS[key] * value for key, value in breakdown.items())

    drivers = sorted(breakdown.items(), key=lambda kv: WEIGHTS[kv[0]] * kv[1], reverse=True)
    rationale = "; ".join(f"{name}={value:.2f}" for name, value in drivers[:3])
    return round(_clamp(score), 4), breakdown, rationale


def _parse_adjustment(raw: str) -> tuple[float, str]:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return 0.0, ""
    try:
        import json

        payload = json.loads(match.group())
    except (ValueError, TypeError):
        return 0.0, ""
    adjustment = float(payload.get("adjustment", 0.0) or 0.0)
    return max(-0.2, min(0.2, adjustment)), str(payload.get("reason", ""))


def score_lead_with_llm(
    lead: Lead,
    listing: Optional[Listing] = None,
    *,
    job_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> tuple[float, dict[str, float], str]:
    """Hard-signal score plus a bounded qualitative nudge.

    An LLM failure degrades to the deterministic score rather than blocking the
    pipeline — scoring must never be a single point of failure.
    """
    score, breakdown, rationale = score_lead(lead, listing)

    prompt = ANALYST_HUMAN.format(
        title=(listing.title if listing else "") or "(unknown)",
        location=(listing.location if listing else "") or "",
        price=(listing.price_per_night if listing else 0),
        currency=(listing.currency if listing else ""),
        rating=(listing.rating if listing else 0),
        review_count=(listing.review_count if listing else 0),
        superhost=lead.host_is_superhost or (listing.superhost if listing else False),
        host_bio=lead.host_bio[:800] or "(none)",
        description=lead.description[:1200] or "(none)",
        reviews=" | ".join(lead.review_excerpts[:3]) or "(none)",
    )

    try:
        raw, _ = tracked_invoke(
            get_llm(),
            [SystemMessage(content=ANALYST_SYSTEM), HumanMessage(content=prompt)],
            agent="analyst",
            job_id=job_id,
            prompt_version=PROMPT_VERSION,
            db_path=db_path,
        )
    except Exception as exc:
        logger.warning("Analyst LLM pass failed, keeping hard-signal score: %s", exc)
        return score, breakdown, rationale

    adjustment, reason = _parse_adjustment(raw)
    breakdown["llm_adjustment"] = adjustment
    adjusted = round(_clamp(score + adjustment), 4)
    if reason:
        rationale = f"{rationale}; llm: {reason}"
    return adjusted, breakdown, rationale
