"""The Scout: researches whether a place is worth going to, and when.

India makes seasonality non-negotiable. A naive system sends you to Manali in
January when the roads are shut, or Goa in July in the middle of the monsoon.
So the Scout's primary output is not a single score but a month-by-month
verdict, which the Router then matches against the campaign window.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.llm import get_llm
from app.agent.runs import tracked_invoke
from app.models import TerritoryProfile
from app.territories import save_profile

logger = logging.getLogger(__name__)

PROMPT_VERSION = "scout-v1"
PROFILE_TTL_DAYS = 90

MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)

SCOUT_SYSTEM = """You research destinations for a remote-working travel creator in India.

He works full time on video calls while travelling, and makes short-form content
about each place. He stays about a week per location and moves overland.

Judge a destination on:
- Seasonality: month by month, is this place actually good to be in? Account for
  monsoon, extreme heat, snow, road/pass closures and peak-crowd periods.
- Connectivity: realistic mobile data and broadband quality for video calls.
- Content potential: how distinctive this place looks and what stories it offers.
- Cost: typical nightly stay cost relative to the rest of India.
- Events: festivals or seasonal happenings worth timing a stay around.
- Caveats: anything that would make a week here genuinely unpleasant.

Be honest and specific. A place that is wonderful in October and miserable in
July must score accordingly. Do not flatter destinations.

Reply with JSON only, no markdown fence. `seasonality` MUST contain all twelve
months as lowercase keys — a month you omit is treated as unvisitable:
{
  "summary": "<two sentences>",
  "seasonality": {"january": 0.0, "february": 0.0, "march": 0.0, "april": 0.0,
                  "may": 0.0, "june": 0.0, "july": 0.0, "august": 0.0,
                  "september": 0.0, "october": 0.0, "november": 0.0,
                  "december": 0.0},
  "connectivity_score": 0.0-1.0,
  "connectivity_note": "<one sentence>",
  "content_score": 0.0-1.0,
  "content_angles": ["<angle>", ...],
  "cost_band": "low|mid|high",
  "cost_score": 0.0-1.0,
  "events": ["<month: event>", ...],
  "caveats": ["<caveat>", ...]
}
cost_score is 1.0 when cheap and 0.0 when expensive."""

SCOUT_HUMAN = """Destination: {name}
Country: {country}

Research this destination for a week-long workation stay."""


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _coerce_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def parse_scout_response(raw: str) -> dict:
    """Turn the Scout's JSON into a validated profile payload.

    Missing months default to 0.0 rather than a neutral 0.5: an unresearched
    month should never look like a viable one.
    """
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        raise ValueError("Scout returned no JSON object")
    payload = json.loads(match.group())

    raw_seasonality = payload.get("seasonality") or {}
    seasonality = {
        month: _coerce_float(raw_seasonality.get(month), 0.0) for month in MONTHS
    }

    cost_band = str(payload.get("cost_band", "")).strip().lower()
    if cost_band not in ("low", "mid", "high"):
        cost_band = ""

    return {
        "summary": str(payload.get("summary", "")).strip(),
        "seasonality": seasonality,
        "connectivity_score": _coerce_float(payload.get("connectivity_score")),
        "connectivity_note": str(payload.get("connectivity_note", "")).strip(),
        "content_score": _coerce_float(payload.get("content_score")),
        "content_angles": _coerce_list(payload.get("content_angles")),
        "cost_band": cost_band,
        "cost_score": _coerce_float(payload.get("cost_score")),
        "events": _coerce_list(payload.get("events")),
        "caveats": _coerce_list(payload.get("caveats")),
    }


def research_territory(
    territory_id: int,
    name: str,
    *,
    country: str = "India",
    job_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> TerritoryProfile:
    """Research a place and persist the resulting profile as current."""
    logger.info("🔭 Scouting %s…", name)
    raw, _ = tracked_invoke(
        get_llm(),
        [
            SystemMessage(content=SCOUT_SYSTEM),
            HumanMessage(content=SCOUT_HUMAN.format(name=name, country=country)),
        ],
        agent="scout",
        job_id=job_id,
        prompt_version=PROMPT_VERSION,
        db_path=db_path,
    )

    payload = parse_scout_response(raw)
    profile = TerritoryProfile(
        territory_id=territory_id,
        prompt_version=PROMPT_VERSION,
        **payload,
    )
    save_profile(profile, ttl_days=PROFILE_TTL_DAYS, db_path=db_path)

    seasonality = payload["seasonality"]
    covered = sum(1 for v in seasonality.values() if v > 0)
    if covered < 6:
        logger.warning(
            "🔭 %s: only %d/12 months scored — the Router will skip the rest",
            name,
            covered,
        )

    top = sorted(seasonality.items(), key=lambda kv: -kv[1])[:3]
    logger.info(
        "🔭 %s · best %s · wifi %.0f%% · content %.0f%% · cost %s",
        name,
        ", ".join(f"{m[:3]} {v:.2f}" for m, v in top),
        payload["connectivity_score"] * 100,
        payload["content_score"] * 100,
        payload["cost_band"] or "?",
    )
    return profile
