"""Read-only context pulled from other portals (Booking.com, ...).

Portals corroborate an Airbnb-discovered listing with extra signal (reviews,
amenities, a price band). They are never a discovery or messaging channel — the
office still finds and messages hosts only on Airbnb. This package owns the
storage; :mod:`app.portals.booking` owns one connector.
"""

from __future__ import annotations

import json
from typing import Optional

from app.database import get_connection


def save_context(
    listing_id: str,
    portal: str,
    payload: dict,
    *,
    external_url: str = "",
    match_confidence: float = 0.0,
    db_path: Optional[str] = None,
) -> None:
    """Upsert one portal's normalized context for a listing (one row per portal)."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """INSERT INTO portal_context
                   (listing_id, portal, external_url, match_confidence,
                    payload_json, fetched_at)
               VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(listing_id, portal) DO UPDATE SET
                   external_url = excluded.external_url,
                   match_confidence = excluded.match_confidence,
                   payload_json = excluded.payload_json,
                   fetched_at = CURRENT_TIMESTAMP""",
            (
                listing_id,
                portal,
                external_url,
                float(match_confidence),
                json.dumps(payload or {}),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_context_for_listing(listing_id: str, db_path: Optional[str] = None) -> list[dict]:
    """Every portal's context for a listing, richest match first."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT portal, external_url, match_confidence, payload_json, fetched_at
                 FROM portal_context WHERE listing_id = ?
                ORDER BY match_confidence DESC""",
            (listing_id,),
        ).fetchall()
    finally:
        conn.close()
    out: list[dict] = []
    for row in rows:
        record = dict(row)
        record["payload"] = json.loads(record.pop("payload_json") or "{}")
        out.append(record)
    return out


def listings_missing_context(
    campaign_id: int = 0,
    portal: str = "booking",
    limit: int = 25,
    db_path: Optional[str] = None,
) -> list[str]:
    """Enriched listings that have no context for ``portal`` yet.

    Drives the Planner: only enriched leads (we have a real detail page to match
    against) that we have not already pulled this portal for.
    """
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT DISTINCT l.listing_id
                 FROM leads l
                 LEFT JOIN portal_context pc
                   ON pc.listing_id = l.listing_id AND pc.portal = ?
                WHERE l.detail_scraped_at IS NOT NULL
                  AND pc.id IS NULL
                  AND (? = 0 OR l.campaign_id = ?)
                LIMIT ?""",
            (portal, campaign_id, campaign_id, limit),
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()
