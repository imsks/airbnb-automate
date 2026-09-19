"""Campaign repository: goals and the itineraries the Router produces."""

from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from app.database import get_connection
from app.models import Campaign, CampaignStatus, CampaignStop

logger = logging.getLogger(__name__)


def _row_to_campaign(row: sqlite3.Row) -> Campaign:
    return Campaign(
        id=row["id"],
        name=row["name"],
        goal=row["goal"] or "",
        window_start=row["window_start"] or "",
        window_end=row["window_end"] or "",
        origin=row["origin"] or "",
        guests=row["guests"],
        stay_nights=row["stay_nights"],
        max_price_per_night=row["max_price_per_night"],
        status=CampaignStatus(row["status"]),
    )


def _row_to_stop(row: sqlite3.Row) -> CampaignStop:
    return CampaignStop(
        id=row["id"],
        campaign_id=row["campaign_id"],
        territory_id=row["territory_id"],
        seq=row["seq"],
        target_month=row["target_month"] or "",
        score=row["score"],
        rationale=row["rationale"] or "",
        status=row["status"],
    )


def create_campaign(campaign: Campaign, db_path: Optional[str] = None) -> int:
    """Insert a campaign and return its id."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO campaigns
               (name, goal, window_start, window_end, origin, guests, stay_nights,
                max_price_per_night, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                campaign.name,
                campaign.goal,
                campaign.window_start,
                campaign.window_end,
                campaign.origin,
                campaign.guests,
                campaign.stay_nights,
                campaign.max_price_per_night,
                campaign.status.value,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def get_campaign(campaign_id: int, db_path: Optional[str] = None) -> Optional[Campaign]:
    """Fetch one campaign by id."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()
        return _row_to_campaign(row) if row else None
    finally:
        conn.close()


def list_campaigns(db_path: Optional[str] = None) -> list[Campaign]:
    """Every campaign, newest first."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()
        return [_row_to_campaign(r) for r in rows]
    finally:
        conn.close()


def set_status(
    campaign_id: int, status: CampaignStatus, db_path: Optional[str] = None
) -> None:
    """Move a campaign between draft, active, paused and completed."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE campaigns SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status.value, campaign_id),
        )
        conn.commit()
    finally:
        conn.close()


def save_stops(campaign_id: int, stops: list, db_path: Optional[str] = None) -> int:
    """Replace a campaign's itinerary with a freshly planned one."""
    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM campaign_stops WHERE campaign_id = ?", (campaign_id,))
        for stop in stops:
            conn.execute(
                """INSERT INTO campaign_stops
                   (campaign_id, territory_id, seq, target_month, score, rationale)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    campaign_id,
                    stop.territory_id,
                    stop.seq,
                    stop.target_month,
                    stop.score,
                    stop.rationale,
                ),
            )
        conn.commit()
        return len(stops)
    finally:
        conn.close()


def get_stops(campaign_id: int, db_path: Optional[str] = None) -> list[CampaignStop]:
    """The itinerary for a campaign, in travel order."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM campaign_stops WHERE campaign_id = ? ORDER BY seq",
            (campaign_id,),
        ).fetchall()
        return [_row_to_stop(r) for r in rows]
    finally:
        conn.close()


def set_stop_status(
    stop_id: int, status: str, db_path: Optional[str] = None
) -> None:
    """Advance one stop through planned → discovering → outreach → won."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE campaign_stops SET status = ? WHERE id = ?", (status, stop_id)
        )
        conn.commit()
    finally:
        conn.close()
