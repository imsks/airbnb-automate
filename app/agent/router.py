"""The Router: orders researched territories into an itinerary.

Scoring places independently produces twelve disconnected pins. A campaign is a
journey, so the Router assigns each stop a month and penalises hops that make no
geographic sense, on top of seasonality, cost, saturation and content value.

Distances are approximate and table-driven rather than geocoded: at the
resolution of "is Kerala a sensible hop from Himachal in December", regional
adjacency is enough and it keeps the planner offline and deterministic.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

from app.agent.scout import MONTHS
from app.models import Campaign, Territory, TerritoryProfile
from app.territories import saturation

logger = logging.getLogger(__name__)

WEIGHTS = {
    "seasonality": 0.40,
    "content": 0.20,
    "connectivity": 0.15,
    "cost": 0.10,
    "freshness": 0.10,
    "route": 0.05,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "Router weights must sum to 1.0"

#: Coarse regional clusters. Hops within a cluster are cheap; across the country
#: they are not.
_REGIONS: dict[str, tuple[str, ...]] = {
    "north": ("himachal", "uttarakhand", "punjab", "haryana", "delhi", "ladakh",
              "kashmir", "jammu", "rishikesh", "manali", "shimla", "dharamshala"),
    "west": ("goa", "maharashtra", "gujarat", "rajasthan", "mumbai", "pune", "udaipur"),
    "south": ("kerala", "karnataka", "tamil nadu", "andhra", "telangana", "goa",
              "bangalore", "bengaluru", "chennai", "pondicherry", "puducherry",
              "coorg", "wayanad", "munnar", "hampi", "gokarna"),
    "east": ("west bengal", "odisha", "bihar", "jharkhand", "kolkata", "darjeeling"),
    "northeast": ("assam", "meghalaya", "sikkim", "arunachal", "nagaland", "manipur",
                  "mizoram", "tripura", "shillong", "gangtok"),
}

#: 1.0 means a natural next hop, 0.0 means crossing the country.
_ADJACENCY: dict[tuple[str, str], float] = {
    ("north", "north"): 1.0,
    ("north", "west"): 0.6,
    ("north", "east"): 0.4,
    ("north", "northeast"): 0.2,
    ("north", "south"): 0.1,
    ("west", "west"): 1.0,
    ("west", "south"): 0.6,
    ("west", "east"): 0.3,
    ("west", "northeast"): 0.1,
    ("south", "south"): 1.0,
    ("south", "east"): 0.4,
    ("south", "northeast"): 0.2,
    ("east", "east"): 1.0,
    ("east", "northeast"): 0.8,
    ("northeast", "northeast"): 1.0,
}


@dataclass
class Stop:
    """One planned stop: a territory, a month, and why."""

    territory_id: int
    territory_name: str
    seq: int
    target_month: str
    score: float
    rationale: str


def region_of(place: str) -> str:
    """Coarse region for a place name, or ``""`` when unrecognised."""
    lowered = (place or "").lower()
    for region, markers in _REGIONS.items():
        if any(marker in lowered for marker in markers):
            return region
    return ""


def route_affinity(origin: str, destination: str) -> float:
    """How natural a hop is, 0.0–1.0. Unknown places get a neutral 0.5."""
    a, b = region_of(origin), region_of(destination)
    if not a or not b:
        return 0.5
    return _ADJACENCY.get((a, b)) or _ADJACENCY.get((b, a)) or 0.5


def campaign_months(campaign: Campaign, limit: int = 12) -> list[str]:
    """The ``YYYY-MM`` months the campaign window covers."""
    start = _parse_month(campaign.window_start) or _today_month()
    end = _parse_month(campaign.window_end)

    months: list[str] = []
    year, month = start
    while len(months) < limit:
        months.append(f"{year:04d}-{month:02d}")
        if end and (year, month) >= end:
            break
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return months


def _today_month() -> tuple[int, int]:
    today = date.today()
    return today.year, today.month


def _parse_month(value: str) -> Optional[tuple[int, int]]:
    match = re.match(r"(\d{4})-(\d{1,2})", (value or "").strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def month_name(year_month: str) -> str:
    """Lowercase month name for a ``YYYY-MM`` string, matching Scout keys."""
    parsed = _parse_month(year_month)
    return MONTHS[parsed[1] - 1] if parsed else ""


def score_stop(
    territory: Territory,
    profile: Optional[TerritoryProfile],
    year_month: str,
    previous_place: str,
) -> tuple[float, dict[str, float]]:
    """How good this territory is in this month, coming from ``previous_place``."""
    name = month_name(year_month)
    components = {
        "seasonality": profile.month_score(name) if profile else 0.0,
        "content": profile.content_score if profile else 0.0,
        "connectivity": profile.connectivity_score if profile else 0.0,
        "cost": profile.cost_score if profile else 0.0,
        "freshness": 1.0 - saturation(territory),
        "route": route_affinity(previous_place, territory.name),
    }
    score = sum(WEIGHTS[key] * value for key, value in components.items())
    return round(score, 4), components


def plan_route(
    campaign: Campaign,
    territories: list[Territory],
    profiles: dict[int, Optional[TerritoryProfile]],
    *,
    min_seasonality: float = 0.35,
    max_stops: int = 12,
) -> list[Stop]:
    """Greedily assign the best available territory to each month in the window.

    ``min_seasonality`` is a hard floor, not a weight: no amount of cheapness or
    content potential makes a snowed-in pass worth visiting. A month with no
    acceptable territory is left empty rather than filled with a bad stop.
    """
    months = campaign_months(campaign, limit=max_stops)
    remaining = {t.id: t for t in territories if t.id is not None}
    previous = campaign.origin or ""
    stops: list[Stop] = []

    for year_month in months:
        name = month_name(year_month)
        best: Optional[tuple[float, dict[str, float], Territory]] = None

        for territory in remaining.values():
            profile = profiles.get(territory.id)
            if profile and profile.month_score(name) < min_seasonality:
                continue
            score, components = score_stop(territory, profile, year_month, previous)
            if best is None or score > best[0]:
                best = (score, components, territory)

        if best is None:
            logger.info("No suitable territory for %s — leaving the month open", year_month)
            continue

        score, components, territory = best
        drivers = sorted(components.items(), key=lambda kv: WEIGHTS[kv[0]] * kv[1], reverse=True)
        stops.append(
            Stop(
                territory_id=territory.id,
                territory_name=territory.name,
                seq=len(stops) + 1,
                target_month=year_month,
                score=score,
                rationale="; ".join(f"{k}={v:.2f}" for k, v in drivers[:3]),
            )
        )
        remaining.pop(territory.id, None)
        previous = territory.name

    return stops
