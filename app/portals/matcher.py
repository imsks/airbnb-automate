"""Decide whether a portal candidate is really the same property as our listing.

Deliberately small and dependency-free: a wrong match would attach another
property's reviews to a host, so the default threshold is conservative and a
low score means "no match" rather than a guess.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Optional

#: Below this, treat the candidate as a different property and drop it.
MATCH_THRESHOLD = 0.6


def _similar(a: str, b: str) -> float:
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _price_closeness(a: Optional[float], b: Optional[float]) -> float:
    if not a or not b:
        return 0.0
    lo, hi = min(a, b), max(a, b)
    if hi == 0:
        return 0.0
    return max(0.0, lo / hi)  # 1.0 when equal, decays as they diverge


def match_confidence(listing, candidate: dict) -> float:
    """Confidence in ``[0, 1]`` that ``candidate`` is ``listing`` on another portal.

    ``listing`` is our Airbnb ``Listing``; ``candidate`` is a normalized portal
    dict (title, location, guests, price_per_night). Weighs name and location
    most, with capacity and price as tie-breakers.
    """
    title = _similar(getattr(listing, "title", ""), candidate.get("title", ""))
    location = _similar(getattr(listing, "location", ""), candidate.get("location", ""))
    price = _price_closeness(
        getattr(listing, "price_per_night", 0) or 0, candidate.get("price_per_night")
    )
    l_guests = getattr(listing, "guests", 0) or 0
    c_guests = candidate.get("guests") or 0
    guests = 1.0 if l_guests and l_guests == c_guests else 0.0

    return round(
        0.5 * title + 0.3 * location + 0.1 * guests + 0.1 * price, 4
    )


def is_match(listing, candidate: dict, threshold: float = MATCH_THRESHOLD) -> bool:
    """True when the candidate clears the confidence bar."""
    return match_confidence(listing, candidate) >= threshold
