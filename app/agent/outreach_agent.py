"""LangGraph outreach agent — generates AI-crafted initial messages.

Instead of using a hardcoded template, this agent:
  1. Takes listing + host data
  2. Generates a personalised, high-converting first message via LLM
  3. Returns it for the existing outreach pipeline to send

Can be plugged into the CLI or the web UI wherever
``get_outreach_message_template()`` was previously used.
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from app.agent.llm import get_llm
from app.agent.prompts import OUTREACH_HUMAN, OUTREACH_SYSTEM
from app.models import Listing

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class OutreachState(TypedDict, total=False):
    """Shared state for the outreach generation graph."""

    listing: dict  # serialised Listing fields
    generated_message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def listing_to_dict(listing: Listing) -> dict:
    """Convert a Listing model to a plain dict for the graph state."""
    return {
        "place_name": listing.title or "your place",
        "host_name": listing.host_name or "Host",
        "location": listing.location or "",
        "price_per_night": listing.price_per_night,
        "currency": listing.currency,
        "rating": listing.rating,
        "review_count": listing.review_count,
        "property_type": listing.property_type or "Property",
        "guests": listing.guests,
        "bedrooms": listing.bedrooms,
        "bathrooms": listing.bathrooms,
        "superhost": "Yes" if listing.superhost else "No",
        "amenities": ", ".join(listing.amenities[:15]) if listing.amenities else "N/A",
    }


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


def generate_outreach_node(state: OutreachState) -> dict[str, Any]:
    """Generate a personalised outreach message for one listing."""
    llm = get_llm()
    data = state.get("listing", {})

    prompt = OUTREACH_HUMAN.format(
        place_name=data.get("place_name", "your place"),
        host_name=data.get("host_name", "Host"),
        location=data.get("location", ""),
        price_per_night=data.get("price_per_night", "N/A"),
        currency=data.get("currency", ""),
        rating=data.get("rating", "N/A"),
        review_count=data.get("review_count", "N/A"),
        property_type=data.get("property_type", "Property"),
        guests=data.get("guests", "N/A"),
        bedrooms=data.get("bedrooms", "N/A"),
        bathrooms=data.get("bathrooms", "N/A"),
        superhost=data.get("superhost", "No"),
        amenities=data.get("amenities", "N/A"),
    )

    response = llm.invoke([
        SystemMessage(content=OUTREACH_SYSTEM),
        HumanMessage(content=prompt),
    ])
    message = response.content.strip()
    logger.info("✍️  Generated outreach message for '%s'", data.get("place_name"))
    return {"generated_message": message}


# ---------------------------------------------------------------------------
# Build the graph
# ---------------------------------------------------------------------------


def build_outreach_graph() -> StateGraph:
    """Return a compiled LangGraph for outreach message generation."""
    graph = StateGraph(OutreachState)
    graph.add_node("generate", generate_outreach_node)
    graph.set_entry_point("generate")
    graph.add_edge("generate", END)
    return graph.compile()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_outreach_message(listing: Listing) -> str:
    """Generate an AI-crafted outreach message for a single listing.

    This replaces the static template approach.  The returned string is ready
    to be typed into the Airbnb messaging composer.
    """
    graph = build_outreach_graph()
    result = graph.invoke({"listing": listing_to_dict(listing)})
    return result.get("generated_message", "")


def generate_outreach_messages_batch(listings: list[Listing]) -> dict[str, str]:
    """Generate outreach messages for multiple listings.

    Returns a dict mapping ``listing.id`` → generated message.
    """
    results: dict[str, str] = {}
    for listing in listings:
        try:
            msg = generate_outreach_message(listing)
            results[listing.id] = msg
        except Exception as e:
            logger.error("Failed to generate message for %s: %s", listing.id, e)
            results[listing.id] = ""
    return results
