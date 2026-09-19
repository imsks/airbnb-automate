"""Prompts that consume enriched lead context and the credential fact sheet.

Two changes from the v1 prompts:

- Identity and reach are injected from policy rather than hardcoded in the
  prompt, so the Warden validates a draft against the same facts the writer was
  given. A prompt that hardcodes "150k followers" cannot be checked against
  anything.
- The writer gets the host's own words, their bio and real guest reviews, so it
  can say something only a person who read the page would say.
"""

from __future__ import annotations

from app.models import Lead, Listing
from app.policy import GuardrailPolicy

SCRIBE_PROMPT_VERSION = "scribe-v2"
CLOSER_PROMPT_VERSION = "closer-v2"
TERMS_PROMPT_VERSION = "terms-v1"

_GUARDRAIL_BLOCK = """
Hard rules. Breaking any of these gets the message blocked before it is sent:
- Never state a specific calendar date. Offer availability windows only
  ("anytime in November or December"), never "Dec 12".
- Never promise more than: {deliverables}.
- Never share a phone number, email, or any handle other than {handles}.
- Never suggest moving the conversation or the payment off Airbnb.
- Never claim more reach than: {followers}.
- {price_rule}
"""


def guardrail_block(policy: GuardrailPolicy) -> str:
    """The policy rendered as prompt text, so writer and Warden agree on limits."""
    if policy.max_price_per_night <= 0:
        price_rule = (
            "Never agree to pay anything. You are asking for a free collaboration; "
            "if the host insists on payment, say you will check and stop there."
        )
    else:
        price_rule = (
            f"Never agree to pay more than {policy.max_price_per_night:g} "
            f"{policy.currency} per night."
        )
    return _GUARDRAIL_BLOCK.format(
        deliverables=", ".join(policy.allowed_deliverables) or "nothing",
        handles=policy.credential_facts.get("handles", "(none)"),
        followers=policy.credential_facts.get("followers", "(unspecified)"),
        price_rule=price_rule,
    )


SCRIBE_SYSTEM = """You write the first message to an Airbnb host, proposing a stay
in exchange for travel content.

You are writing as {name}, {role}, with {followers} followers across {handles}.

What makes these messages work:
- Prove you read the listing. Reference something specific the host wrote, an
  unusual amenity, or something a guest said in a review. Generic praise reads
  as a template and gets ignored.
- Lead with what the host gets, not what you want.
- Sound like one person writing to another. No marketing voice, no bullet lists.
- 100-160 words. Warm, direct, easy to say yes to.
- Close with a low-pressure invitation, never a hard ask.

Never invent facts about the property or about yourself.
{guardrails}
Output only the message text."""

SCRIBE_HUMAN = """Property: {title}
Host: {host_name}
Location: {location}
Type: {property_type} · {bedrooms} bed · sleeps {guests}
Price: {price_per_night} {currency} per night
Rating: {rating} from {review_count} reviews

What the host says about it:
{description}

Host's bio:
{host_bio}

What guests said:
{reviews}

Notable amenities:
{amenities}

{territory_note}

Write the opening message to {host_name}."""


CLOSER_SYSTEM = """You are negotiating a content collaboration with an Airbnb host,
writing as {name}.

Your goal, in order of preference:
1. A free stay in exchange for content.
2. A heavily discounted stay.
3. A warm "not now" you can return to later.

How to negotiate well:
- Mirror the host's tone and length. A short question gets a short answer.
- Answer what they actually asked before adding anything new.
- If they hesitate on a free stay, pivot to a discount without being asked twice.
- If they say no clearly, thank them warmly and leave the door open. Do not push.
- Never repeat a pitch they have already declined.

You are on round {round_number} of this conversation. If you have already made
the same ask twice, change the offer or gracefully close.
{guardrails}
Output only the reply text."""

CLOSER_HUMAN = """Property: {place_name}
Host: {host_name}
Location: {location}
Booking status: {booking_status}

Conversation so far:
{conversation}

Write your reply to {host_name}."""


TERMS_SYSTEM = """You extract agreed terms from an Airbnb negotiation thread.

Report only what the host has explicitly agreed to. Do not infer, do not
optimistically round, and do not treat a proposal as an agreement. If the host
has not clearly agreed to something, leave that field null.

Reply with JSON only:
{
  "agreed": true|false,
  "price_per_night": <number or null>,
  "currency": "<code or empty>",
  "discount_pct": <number or null>,
  "window_start": "<YYYY-MM or empty>",
  "window_end": "<YYYY-MM or empty>",
  "nights": <number or null>,
  "deliverables": ["<what you committed to>", ...],
  "confidence": 0.0-1.0,
  "reason": "<one sentence>"
}
"agreed" is true only when the host has accepted a stay on identifiable terms."""

TERMS_HUMAN = """Conversation:
{conversation}

Extract the agreed terms."""


def _join(items, limit: int, empty: str = "(none listed)") -> str:
    chosen = [str(i).strip() for i in (items or [])[:limit] if str(i).strip()]
    return "\n".join(f"- {item}" for item in chosen) if chosen else empty


def build_scribe_prompt(
    listing: Listing,
    lead: Lead | None,
    policy: GuardrailPolicy,
    territory_note: str = "",
) -> tuple[str, str]:
    """Render the Scribe's ``(system, human)`` pair for one listing."""
    facts = policy.credential_facts
    system = SCRIBE_SYSTEM.format(
        name=facts.get("name", ""),
        role=facts.get("role", ""),
        followers=facts.get("followers", ""),
        handles=facts.get("handles", ""),
        guardrails=guardrail_block(policy),
    )
    human = SCRIBE_HUMAN.format(
        title=listing.title or "(untitled)",
        host_name=listing.host_name or "there",
        location=listing.location or "",
        property_type=listing.property_type or "place",
        bedrooms=listing.bedrooms,
        guests=listing.guests,
        price_per_night=listing.price_per_night,
        currency=listing.currency,
        rating=listing.rating,
        review_count=listing.review_count,
        description=(lead.description[:1200] if lead and lead.description else "(not available)"),
        host_bio=(lead.host_bio[:600] if lead and lead.host_bio else "(not available)"),
        reviews=_join(lead.review_excerpts if lead else [], 3, "(no reviews yet)"),
        amenities=_join(
            (lead.amenities if lead and lead.amenities else listing.amenities), 10
        ),
        territory_note=territory_note or "",
    )
    return system, human


def build_closer_prompt(
    *,
    place_name: str,
    host_name: str,
    location: str,
    booking_status: str,
    conversation: str,
    round_number: int,
    policy: GuardrailPolicy,
) -> tuple[str, str]:
    """Render the Closer's ``(system, human)`` pair for one thread."""
    system = CLOSER_SYSTEM.format(
        name=policy.credential_facts.get("name", ""),
        round_number=round_number,
        guardrails=guardrail_block(policy),
    )
    human = CLOSER_HUMAN.format(
        place_name=place_name or "your place",
        host_name=host_name or "there",
        location=location or "",
        booking_status=booking_status or "(unknown)",
        conversation=conversation or "(no messages yet)",
    )
    return system, human
