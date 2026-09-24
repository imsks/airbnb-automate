"""The Proposer: keeps the office supplied with places to chase.

The old flow made a human paste a list of destinations into a campaign form.
This removes that step. When the shortlist of live places runs thin, the
Proposer invents new candidates — India-weighted, skewed toward stays a creator
can pitch — and never re-suggests somewhere already known or already visited.
The Scout then researches whatever the Proposer adds.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app import territories as territory_repo
from app.agent.llm import get_llm
from app.agent.runs import tracked_invoke

logger = logging.getLogger(__name__)

PROMPT_VERSION = "proposer-v1"

#: Keep at least this many live places on hand. Below it, the office asks for more.
MIN_LIVE_TERRITORIES = 12
#: How many new places to ask for in one proposal.
PROPOSE_BATCH = 8

PROPOSER_SYSTEM = """You keep a remote-working travel creator supplied with places to stay.

He works full time on video calls while travelling and makes short-form content
about each stay. He wants a deal with a host: a free or discounted stay in
exchange for content. Trips range from a long weekend to a multi-month base, and
dates are always flexible and negotiated later — so do NOT worry about seasons.

Weight your suggestions:
- Mostly India: hill stations, beaches, backwaters, deserts, heritage towns,
  offbeat valleys — anywhere with distinctive stays and a story to film.
- Some international: nearby, visa-easy, creator-friendly destinations.

Favour places with a healthy supply of independent hosts and boutique stays
(where a content-for-stay collaboration is realistic), not just big hotels.

You are given a list of places to AVOID — already known, exhausted, or already
visited. Never suggest anything on it, and never repeat yourself.

Reply with JSON only, no markdown fence:
{"places": ["City, Region, Country", "City, Country", ...]}
Each entry is a single concrete destination a host actually lists on Airbnb."""

PROPOSER_HUMAN = """Suggest {count} new destinations.

Avoid these ({avoid_count}):
{avoid}"""


def parse_proposal(raw: str) -> list[str]:
    """Pull a clean, de-duplicated list of place names from the model's JSON."""
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        raise ValueError("Proposer returned no JSON object")
    payload = json.loads(match.group())
    places = payload.get("places") or []
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in places:
        name = str(item).strip()
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            cleaned.append(name)
    return cleaned


def propose_territories(
    *,
    min_live: int = MIN_LIVE_TERRITORIES,
    batch: int = PROPOSE_BATCH,
    job_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> dict:
    """Top the shortlist up with new candidate places when it runs low.

    Returns a summary; ``{"proposed": 0, "reason": "shortlist healthy"}`` when
    there is already enough to work on.
    """
    live = territory_repo.live_territory_count(db_path)
    if live >= min_live:
        return {"proposed": 0, "live": live, "reason": "shortlist healthy"}

    known = territory_repo.all_territory_names(db_path)
    known_lower = {name.lower() for name in known}
    avoid = "\n".join(f"- {name}" for name in known) or "(nothing yet)"

    raw, _ = tracked_invoke(
        get_llm(),
        [
            SystemMessage(content=PROPOSER_SYSTEM),
            HumanMessage(
                content=PROPOSER_HUMAN.format(
                    count=batch, avoid_count=len(known), avoid=avoid
                )
            ),
        ],
        agent="proposer",
        job_id=job_id,
        prompt_version=PROMPT_VERSION,
        db_path=db_path,
    )

    suggestions = parse_proposal(raw)
    added: list[str] = []
    for name in suggestions:
        if name.lower() in known_lower:
            continue
        territory_repo.upsert_territory(name, db_path=db_path)
        known_lower.add(name.lower())
        added.append(name)

    logger.info(
        "🧩 Proposer added %d place(s) (%d live before): %s",
        len(added), live, ", ".join(added) or "(none new)",
    )
    return {"proposed": len(added), "live": live, "places": added}
