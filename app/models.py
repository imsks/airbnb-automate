"""Data models for Airbnb Automate."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class OutreachStatus(str, Enum):
    """Status of outreach to a host."""

    PENDING = "pending"
    SENT = "sent"
    FOLLOW_UP_SENT = "follow_up_sent"
    RESPONDED = "responded"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    NO_RESPONSE = "no_response"


class CampaignStatus(str, Enum):
    """Status of a campaign."""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"


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


class Campaign(BaseModel):
    """A search campaign targeting a specific location and date range."""

    id: Optional[int] = None
    name: str = ""
    location: str = ""
    checkin: str = ""
    checkout: str = ""
    guests: int = 2
    min_price: float = 0.0
    max_price: float = 500.0
    property_types: list[str] = Field(default_factory=list)
    amenities: list[str] = Field(default_factory=list)
    max_listings: int = 20
    status: CampaignStatus = CampaignStatus.ACTIVE
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Outreach(BaseModel):
    """An outreach record tracking communication with a host."""

    id: Optional[int] = None
    campaign_id: int = 0
    listing_id: str = ""
    host_name: str = ""
    listing_title: str = ""
    listing_url: str = ""
    message: str = ""
    status: OutreachStatus = OutreachStatus.PENDING
    sent_at: Optional[datetime] = None
    follow_up_count: int = 0
    last_follow_up_at: Optional[datetime] = None
    response: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
