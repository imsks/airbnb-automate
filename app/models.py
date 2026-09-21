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


# --- v2: the deal pipeline -------------------------------------------------


class DealState(str, Enum):
    """Lifecycle of one host relationship, from discovery to delivered content."""

    DISCOVERED = "discovered"
    QUALIFIED = "qualified"
    CONTACTED = "contacted"
    HOST_REPLIED = "host_replied"
    NEGOTIATING = "negotiating"
    TERMS_AGREED = "terms_agreed"
    READY_TO_BOOK = "ready_to_book"
    BOOKED = "booked"
    STAYED = "stayed"
    CONTENT_DELIVERED = "content_delivered"
    # Exits
    DISQUALIFIED = "disqualified"
    REJECTED = "rejected"
    STALE = "stale"
    NEEDS_HUMAN = "needs_human"


TERMINAL_DEAL_STATES: frozenset[DealState] = frozenset(
    {
        DealState.CONTENT_DELIVERED,
        DealState.DISQUALIFIED,
        DealState.REJECTED,
        DealState.STALE,
    }
)

#: Deals here have crossed the payment wall the agents cannot cross.
HUMAN_ACTION_STATES: frozenset[DealState] = frozenset(
    {DealState.READY_TO_BOOK, DealState.NEEDS_HUMAN}
)

_HAPPY_PATH: dict[DealState, set[DealState]] = {
    DealState.DISCOVERED: {DealState.QUALIFIED, DealState.DISQUALIFIED},
    DealState.QUALIFIED: {DealState.CONTACTED, DealState.DISQUALIFIED},
    DealState.CONTACTED: {DealState.HOST_REPLIED, DealState.REJECTED, DealState.STALE},
    DealState.HOST_REPLIED: {
        DealState.NEGOTIATING,
        DealState.TERMS_AGREED,
        DealState.REJECTED,
        DealState.STALE,
    },
    DealState.NEGOTIATING: {
        DealState.NEGOTIATING,
        DealState.TERMS_AGREED,
        DealState.HOST_REPLIED,
        DealState.REJECTED,
        DealState.STALE,
    },
    DealState.TERMS_AGREED: {DealState.READY_TO_BOOK, DealState.REJECTED},
    DealState.READY_TO_BOOK: {DealState.BOOKED, DealState.REJECTED, DealState.STALE},
    DealState.BOOKED: {DealState.STAYED, DealState.REJECTED},
    DealState.STAYED: {DealState.CONTENT_DELIVERED},
    DealState.CONTENT_DELIVERED: set(),
    DealState.DISQUALIFIED: set(),
    DealState.REJECTED: set(),
    DealState.STALE: {DealState.HOST_REPLIED},
    DealState.NEEDS_HUMAN: {
        # A human who verifies the outreach did land puts the deal back on the
        # happy path; without this an unconfirmed-but-delivered send is stuck.
        DealState.CONTACTED,
        DealState.NEGOTIATING,
        DealState.TERMS_AGREED,
        DealState.READY_TO_BOOK,
        DealState.REJECTED,
        DealState.DISQUALIFIED,
    },
}


def allowed_deal_transitions(state: DealState) -> frozenset[DealState]:
    """States reachable from ``state``.

    Any non-terminal state may escalate to NEEDS_HUMAN — that is the Warden's
    only escape hatch and it must never be blocked.
    """
    allowed = set(_HAPPY_PATH.get(state, set()))
    if state not in TERMINAL_DEAL_STATES and state != DealState.NEEDS_HUMAN:
        allowed.add(DealState.NEEDS_HUMAN)
    return frozenset(allowed)


class TerritoryStatus(str, Enum):
    """How far a place has progressed through research and prospecting."""

    CANDIDATE = "candidate"
    RESEARCHED = "researched"
    ACTIVE = "active"
    EXHAUSTED = "exhausted"
    BLOCKED = "blocked"


class CampaignStatus(str, Enum):
    """Lifecycle of a travel campaign."""

    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"


class MessageDirection(str, Enum):
    """Who sent a message."""

    OUTBOUND = "outbound"
    INBOUND = "inbound"


class MessageKind(str, Enum):
    """What a message is for."""

    OUTREACH = "outreach"
    NEGOTIATION = "negotiation"
    FOLLOW_UP = "follow_up"
    HOST = "host"


class MessageStatus(str, Enum):
    """Delivery state of a message. ``BLOCKED`` means the Warden refused it."""

    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    BLOCKED = "blocked"
    RECEIVED = "received"


class JobStatus(str, Enum):
    """Lifecycle of a queued unit of work."""

    PENDING = "pending"
    LEASED = "leased"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Territory(BaseModel):
    """A place we might stay, plus saturation counters."""

    id: Optional[int] = None
    name: str = ""
    country: str = "India"
    region: str = ""
    status: TerritoryStatus = TerritoryStatus.CANDIDATE
    leads_discovered: int = 0
    messages_sent: int = 0
    deals_won: int = 0
    last_discovered_at: Optional[datetime] = None


class TerritoryProfile(BaseModel):
    """Scout research output for one territory."""

    id: Optional[int] = None
    territory_id: int = 0
    summary: str = ""
    seasonality: dict[str, float] = Field(default_factory=dict)
    connectivity_score: float = 0.0
    connectivity_note: str = ""
    content_score: float = 0.0
    content_angles: list[str] = Field(default_factory=list)
    cost_band: str = ""
    cost_score: float = 0.0
    events: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    model: str = ""
    prompt_version: str = ""
    is_current: bool = True
    researched_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    def month_score(self, month: str) -> float:
        """Seasonality score for a lowercase month name; 0.0 when unresearched."""
        return float(self.seasonality.get(month.lower(), 0.0))


class Campaign(BaseModel):
    """A travel goal the Planner decomposes into work."""

    id: Optional[int] = None
    name: str = ""
    goal: str = ""
    window_start: str = ""
    window_end: str = ""
    origin: str = ""
    guests: int = 2
    stay_nights: int = 7
    max_price_per_night: Optional[float] = None
    status: CampaignStatus = CampaignStatus.DRAFT


class CampaignStop(BaseModel):
    """One ordered stop on a campaign itinerary."""

    id: Optional[int] = None
    campaign_id: int = 0
    territory_id: int = 0
    seq: int = 0
    target_month: str = ""
    score: float = 0.0
    rationale: str = ""
    status: str = "planned"


class Lead(BaseModel):
    """A listing enriched with detail-page context and a collab fit score."""

    id: Optional[int] = None
    listing_id: str = ""
    campaign_id: int = 0
    territory_id: Optional[int] = None
    search_id: Optional[int] = None
    description: str = ""
    house_rules: str = ""
    amenities: list[str] = Field(default_factory=list)
    review_excerpts: list[str] = Field(default_factory=list)
    host_bio: str = ""
    host_response_rate: str = ""
    host_is_superhost: bool = False
    listing_age_months: Optional[int] = None
    has_long_stay_discount: bool = False
    instant_book: bool = False
    detail_scraped_at: Optional[datetime] = None
    collab_fit_score: Optional[float] = None
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    score_rationale: str = ""
    scored_at: Optional[datetime] = None

    @property
    def is_enriched(self) -> bool:
        """True once the detail page has been scraped, which Scribe requires."""
        return self.detail_scraped_at is not None


class Deal(BaseModel):
    """One host relationship. Owns the thread link and the agreed terms."""

    id: Optional[int] = None
    lead_id: Optional[int] = None
    listing_id: str = ""
    campaign_id: int = 0
    territory_id: Optional[int] = None
    host_name: str = ""
    place_name: str = ""
    location: str = ""
    listing_url: str = ""
    thread_id: Optional[str] = None
    thread_url: str = ""
    thread_linked_via: str = ""
    state: DealState = DealState.DISCOVERED
    state_reason: str = ""
    agent_reply_count: int = 0
    follow_up_count: int = 0
    last_inbound_at: Optional[datetime] = None
    last_outbound_at: Optional[datetime] = None
    agreed_price_per_night: Optional[float] = None
    agreed_currency: str = ""
    agreed_discount_pct: Optional[float] = None
    agreed_window_start: str = ""
    agreed_window_end: str = ""
    agreed_nights: Optional[int] = None
    agreed_deliverables: list[str] = Field(default_factory=list)
    terms_confidence: Optional[float] = None
    booking_url: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_linked(self) -> bool:
        """True when we know which inbox thread this deal became."""
        return bool(self.thread_id)


class DealEvent(BaseModel):
    """An append-only record of one deal state transition."""

    id: Optional[int] = None
    deal_id: int = 0
    from_state: str = ""
    to_state: str = ""
    reason: str = ""
    actor: str = ""
    metadata: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Message(BaseModel):
    """A message on a deal's thread, in either direction."""

    id: Optional[int] = None
    deal_id: int = 0
    direction: MessageDirection = MessageDirection.OUTBOUND
    kind: MessageKind = MessageKind.OUTREACH
    body: str = ""
    status: MessageStatus = MessageStatus.PENDING
    error: str = ""
    blocked_reason: str = ""
    agent: str = ""
    prompt_version: str = ""
    agent_run_id: Optional[int] = None
    idempotency_key: Optional[str] = None
    external_ts: str = ""
    legacy_outreach_id: Optional[int] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    sent_at: Optional[datetime] = None


class Job(BaseModel):
    """A durable unit of work. Enqueueing jobs is how the office grows itself."""

    id: Optional[int] = None
    type: str = ""
    payload: dict = Field(default_factory=dict)
    status: JobStatus = JobStatus.PENDING
    priority: int = 100
    run_after: float = 0.0
    attempts: int = 0
    max_attempts: int = 3
    idempotency_key: Optional[str] = None
    lease_until: Optional[float] = None
    lease_owner: str = ""
    last_error: str = ""
    result: dict = Field(default_factory=dict)
    campaign_id: Optional[int] = None
    deal_id: Optional[int] = None


class AgentRun(BaseModel):
    """One LLM call, for cost and quality attribution."""

    id: Optional[int] = None
    agent: str = ""
    job_id: Optional[int] = None
    deal_id: Optional[int] = None
    provider: str = ""
    model: str = ""
    prompt_version: str = ""
    input_preview: str = ""
    output_preview: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    ok: bool = True
    error: str = ""
