"""Data models for Airbnb Automate."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class SearchStatus(str, Enum):
    """Status of a search."""

    SEARCHING = "searching"
    COMPLETED = "completed"
    FAILED = "failed"


class OutreachStatus(str, Enum):
    """Status of an outreach message."""

    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class Listing(BaseModel):
    """An Airbnb listing."""

    id: str = ""
    url: str = ""
    title: str = ""
    host_name: str = ""
    location: str = ""
    price_per_night: float = 0.0
    currency: str = "USD"
    rating: float = 0.0
    review_count: int = 0
    property_type: str = ""
    guests: int = 0
    bedrooms: int = 0
    bathrooms: float = 0.0
    amenities: list[str] = Field(default_factory=list)
    photo_url: str = ""
    superhost: bool = False
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Search(BaseModel):
    """A search request for Airbnb listings."""

    id: Optional[int] = None
    location: str = ""
    checkin: str = ""
    checkout: str = ""
    guests: int = 2
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    date_mode: str = "flexible"
    flex_duration: int = 1
    flex_duration_unit: str = "week"
    status: SearchStatus = SearchStatus.SEARCHING
    listings_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def date_summary(self) -> str:
        """Human-readable dates / flexible trip for UI."""
        mode = (self.date_mode or "flexible").lower()
        if mode == "fixed" and self.checkin:
            if self.checkout:
                return f"{self.checkin} → {self.checkout}"
            return self.checkin
        raw_unit = (self.flex_duration_unit or "week").lower()
        if raw_unit == "weekend":
            return "Flexible · weekend trip"
        unit = raw_unit.rstrip("s")
        n = self.flex_duration or 1
        label = {"day": "night", "week": "week", "month": "month"}.get(unit, "week")
        if unit == "day":
            return f"Flexible · {n} night{'s' if n != 1 else ''}"
        return f"Flexible · {n} {label}{'s' if n != 1 else ''}"


class OutreachMessage(BaseModel):
    """A message sent to an Airbnb host."""

    id: Optional[int] = None
    search_id: int = 0
    listing_id: str = ""
    host_name: str = ""
    place_name: str = ""
    location: str = ""
    message: str = ""
    status: OutreachStatus = OutreachStatus.PENDING
    error: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    sent_at: Optional[datetime] = None
