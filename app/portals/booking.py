"""Booking.com context connector — read-only, best-effort.

The office already found the property on Airbnb; this looks the same stay up on
Booking.com and returns a normalized bundle of corroborating signal. It is
strictly best-effort: any failure, missing network, or empty result returns
``None`` and the office simply drafts without external context.

Real Booking.com scraping is intentionally not wired here (it is fragile and
network-bound). A ``fetcher`` is injected in tests and can be supplied in
production once a scraper exists; with no fetcher this connector is a clean
no-op, which is the safe default.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PropertyQuery:
    """The identity of an Airbnb listing, as a portal lookup needs it."""

    listing_id: str
    title: str = ""
    location: str = ""
    host_name: str = ""
    guests: int = 0
    price_per_night: float = 0.0
    currency: str = ""

    @classmethod
    def from_listing(cls, listing) -> "PropertyQuery":
        return cls(
            listing_id=getattr(listing, "id", "") or "",
            title=getattr(listing, "title", "") or "",
            location=getattr(listing, "location", "") or "",
            host_name=getattr(listing, "host_name", "") or "",
            guests=getattr(listing, "guests", 0) or 0,
            price_per_night=getattr(listing, "price_per_night", 0.0) or 0.0,
            currency=getattr(listing, "currency", "") or "",
        )


#: A fetcher takes a query and returns a raw candidate dict (or None). Injected
#: so the connector is testable and never requires the network by default.
Fetcher = Callable[[PropertyQuery], Optional[dict]]


class BookingConnector:
    """Fetch and normalize Booking.com context for one property."""

    portal = "booking"

    def __init__(self, fetcher: Optional[Fetcher] = None) -> None:
        self._fetch = fetcher

    def fetch_context(self, query: PropertyQuery) -> Optional[dict]:
        """Return normalized context, or ``None`` if unavailable (best-effort)."""
        if self._fetch is None:
            # No scraper wired: degrade cleanly rather than pretend.
            return None
        try:
            raw = self._fetch(query)
        except Exception as exc:  # never let a portal failure break the office
            logger.info("Booking.com context unavailable for %s: %s", query.listing_id, exc)
            return None
        if not raw:
            return None
        return self._normalize(raw, query)

    @staticmethod
    def _normalize(raw: dict, query: PropertyQuery) -> dict:
        """Coerce a raw candidate into the shape the matcher and UI expect."""
        return {
            "title": raw.get("title") or query.title,
            "location": raw.get("location") or query.location,
            "guests": raw.get("guests") or query.guests,
            "price_per_night": raw.get("price_per_night") or raw.get("price"),
            "price_band": raw.get("price_band") or "",
            "rating": raw.get("rating"),
            "review_count": raw.get("review_count"),
            "amenities": list(raw.get("amenities") or []),
            "review_excerpts": list(raw.get("review_excerpts") or raw.get("reviews") or []),
            "url": raw.get("url") or raw.get("external_url") or "",
        }
