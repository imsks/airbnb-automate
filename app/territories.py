"""Territory repository: places, their research profiles, and saturation.

Saturation matters more than it looks. Messaging the same place repeatedly
burns a scarce send budget on a market that has already seen us, so these
counters feed directly into where the Planner sends work next.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.database import get_connection
from app.models import Territory, TerritoryProfile, TerritoryStatus

logger = logging.getLogger(__name__)


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _row_to_territory(row: sqlite3.Row) -> Territory:
    return Territory(
        id=row["id"],
        name=row["name"],
        country=row["country"] or "India",
        region=row["region"] or "",
        status=TerritoryStatus(row["status"]),
        leads_discovered=row["leads_discovered"],
        messages_sent=row["messages_sent"],
        deals_won=row["deals_won"],
        last_discovered_at=_parse_ts(row["last_discovered_at"]),
    )


def _row_to_profile(row: sqlite3.Row) -> TerritoryProfile:
    return TerritoryProfile(
        id=row["id"],
        territory_id=row["territory_id"],
        summary=row["summary"] or "",
        seasonality=json.loads(row["seasonality_json"] or "{}"),
        connectivity_score=row["connectivity_score"],
        connectivity_note=row["connectivity_note"] or "",
        content_score=row["content_score"],
        content_angles=json.loads(row["content_angles_json"] or "[]"),
        cost_band=row["cost_band"] or "",
        cost_score=row["cost_score"],
        events=json.loads(row["events_json"] or "[]"),
        caveats=json.loads(row["caveats_json"] or "[]"),
        sources=json.loads(row["sources_json"] or "[]"),
        model=row["model"] or "",
        prompt_version=row["prompt_version"] or "",
        is_current=bool(row["is_current"]),
        researched_at=_parse_ts(row["researched_at"]),
        expires_at=_parse_ts(row["expires_at"]),
    )


def upsert_territory(
    name: str,
    *,
    country: str = "India",
    region: str = "",
    db_path: Optional[str] = None,
) -> int:
    """Get or create a territory by name."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM territories WHERE name = ?", (name,)
        ).fetchone()
        if row:
            return int(row["id"])
        cursor = conn.execute(
            "INSERT INTO territories (name, country, region) VALUES (?, ?, ?)",
            (name, country, region),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def get_territory(territory_id: int, db_path: Optional[str] = None) -> Optional[Territory]:
    """Fetch one territory by id."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM territories WHERE id = ?", (territory_id,)
        ).fetchone()
        return _row_to_territory(row) if row else None
    finally:
        conn.close()


def get_territory_by_name(name: str, db_path: Optional[str] = None) -> Optional[Territory]:
    """Fetch one territory by its canonical name."""
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM territories WHERE name = ?", (name,)).fetchone()
        return _row_to_territory(row) if row else None
    finally:
        conn.close()


def set_status(
    territory_id: int, status: TerritoryStatus, db_path: Optional[str] = None
) -> None:
    """Move a territory through candidate → researched → active → exhausted."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE territories SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status.value, territory_id),
        )
        conn.commit()
    finally:
        conn.close()


def save_profile(
    profile: TerritoryProfile,
    *,
    ttl_days: int = 90,
    db_path: Optional[str] = None,
) -> int:
    """Store a research profile as current, retiring the previous one.

    Old profiles are kept rather than overwritten so a change in the Scout's
    verdict stays auditable.
    """
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE territory_profiles SET is_current = 0 WHERE territory_id = ?",
            (profile.territory_id,),
        )
        now = datetime.now(timezone.utc)
        cursor = conn.execute(
            """INSERT INTO territory_profiles
               (territory_id, summary, seasonality_json, connectivity_score,
                connectivity_note, content_score, content_angles_json, cost_band,
                cost_score, events_json, caveats_json, sources_json, model,
                prompt_version, is_current, researched_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                profile.territory_id,
                profile.summary,
                json.dumps(profile.seasonality),
                profile.connectivity_score,
                profile.connectivity_note,
                profile.content_score,
                json.dumps(profile.content_angles),
                profile.cost_band,
                profile.cost_score,
                json.dumps(profile.events),
                json.dumps(profile.caveats),
                json.dumps(profile.sources),
                profile.model,
                profile.prompt_version,
                now.isoformat(),
                (now + timedelta(days=ttl_days)).isoformat(),
            ),
        )
        conn.execute(
            "UPDATE territories SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (TerritoryStatus.RESEARCHED.value, profile.territory_id),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def get_current_profile(
    territory_id: int, db_path: Optional[str] = None
) -> Optional[TerritoryProfile]:
    """The live research profile for a territory, if one exists."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            """SELECT * FROM territory_profiles
                WHERE territory_id = ? AND is_current = 1
                ORDER BY id DESC LIMIT 1""",
            (territory_id,),
        ).fetchone()
        return _row_to_profile(row) if row else None
    finally:
        conn.close()


def territories_needing_research(
    limit: int = 10, db_path: Optional[str] = None
) -> list[Territory]:
    """Places with no current profile, or whose profile has expired."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT t.* FROM territories t
                LEFT JOIN territory_profiles p
                       ON p.territory_id = t.id AND p.is_current = 1
                WHERE t.status != ?
                  AND (p.id IS NULL OR p.expires_at IS NULL OR p.expires_at < ?)
                ORDER BY t.id ASC LIMIT ?""",
            (
                TerritoryStatus.BLOCKED.value,
                datetime.now(timezone.utc).isoformat(),
                limit,
            ),
        ).fetchall()
        return [_row_to_territory(r) for r in rows]
    finally:
        conn.close()


def researched_territories(db_path: Optional[str] = None) -> list[Territory]:
    """Every territory that has a current research profile."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT DISTINCT t.* FROM territories t
                JOIN territory_profiles p ON p.territory_id = t.id AND p.is_current = 1
                WHERE t.status != ?
                ORDER BY t.name""",
            (TerritoryStatus.BLOCKED.value,),
        ).fetchall()
        return [_row_to_territory(r) for r in rows]
    finally:
        conn.close()


def record_discovery(
    territory_id: int, leads_found: int, db_path: Optional[str] = None
) -> None:
    """Bump discovery counters after a prospecting run."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """UPDATE territories
                  SET leads_discovered = leads_discovered + ?,
                      last_discovered_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?""",
            (leads_found, datetime.now(timezone.utc).isoformat(), territory_id),
        )
        conn.commit()
    finally:
        conn.close()


def record_send(territory_id: int, db_path: Optional[str] = None) -> None:
    """Increment the saturation counter after messaging a host here."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """UPDATE territories
                  SET messages_sent = messages_sent + 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?""",
            (territory_id,),
        )
        conn.commit()
    finally:
        conn.close()


def saturation(territory: Territory, cap: int = 25) -> float:
    """0.0 for a fresh market, 1.0 once we have messaged it ``cap`` times."""
    return min(1.0, territory.messages_sent / cap) if cap else 0.0
