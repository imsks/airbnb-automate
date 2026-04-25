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
    status: SearchStatus = SearchStatus.SEARCHING
    listings_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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
