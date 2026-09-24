"""Lead repository: detail-page enrichment and collab-fit scoring.

A Listing is what the search page showed us. A Lead is that listing plus the
public context the Scribe needs to write something specific, plus the score
that decides whether it is worth one of our scarce sends.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from app.database import get_connection
from app.models import Lead

logger = logging.getLogger(__name__)


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _row_to_lead(row: sqlite3.Row) -> Lead:
    return Lead(
        id=row["id"],
        listing_id=row["listing_id"],
        campaign_id=row["campaign_id"],
        territory_id=row["territory_id"],
        search_id=row["search_id"],
        description=row["description"] or "",
        house_rules=row["house_rules"] or "",
        amenities=json.loads(row["amenities_json"] or "[]"),
        review_excerpts=json.loads(row["review_excerpts_json"] or "[]"),
        host_bio=row["host_bio"] or "",
        host_response_rate=row["host_response_rate"] or "",
        host_is_superhost=bool(row["host_is_superhost"]),
        listing_age_months=row["listing_age_months"],
        has_long_stay_discount=bool(row["has_long_stay_discount"]),
        instant_book=bool(row["instant_book"]),
        detail_scraped_at=_parse_ts(row["detail_scraped_at"]),
        collab_fit_score=row["collab_fit_score"],
        score_breakdown=json.loads(row["score_breakdown_json"] or "{}"),
        score_rationale=row["score_rationale"] or "",
        scored_at=_parse_ts(row["scored_at"]),
    )


def upsert_lead(
    listing_id: str,
    *,
    campaign_id: int = 0,
    territory_id: Optional[int] = None,
    search_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> int:
    """Get or create the lead row for a listing within a campaign."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM leads WHERE listing_id = ? AND campaign_id = ?",
            (listing_id, campaign_id),
        ).fetchone()
        if row:
            return int(row["id"])
        cursor = conn.execute(
            """INSERT INTO leads (listing_id, campaign_id, territory_id, search_id)
               VALUES (?, ?, ?, ?)""",
            (listing_id, campaign_id, territory_id, search_id),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def get_lead(lead_id: int, db_path: Optional[str] = None) -> Optional[Lead]:
    """Fetch one lead by id."""
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return _row_to_lead(row) if row else None
    finally:
        conn.close()


def save_enrichment(lead_id: int, detail: dict, db_path: Optional[str] = None) -> None:
    """Persist what the detail-page scrape found."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """UPDATE leads
                  SET description = ?, house_rules = ?, amenities_json = ?,
                      review_excerpts_json = ?, host_bio = ?, host_response_rate = ?,
                      host_is_superhost = ?, listing_age_months = ?,
                      has_long_stay_discount = ?, instant_book = ?,
                      detail_scraped_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?""",
            (
                detail.get("description", ""),
                detail.get("house_rules", ""),
                json.dumps(detail.get("amenities", [])),
                json.dumps(detail.get("review_excerpts", [])),
                detail.get("host_bio", ""),
                detail.get("host_response_rate", ""),
                1 if detail.get("host_is_superhost") else 0,
                detail.get("listing_age_months"),
                1 if detail.get("has_long_stay_discount") else 0,
                1 if detail.get("instant_book") else 0,
                datetime.now(timezone.utc).isoformat(),
                lead_id,
            ),
        )
        host_name = (detail.get("host_name") or "").strip()
        title = (detail.get("title") or "").strip()
        if host_name or title:
            conn.execute(
                """UPDATE listings SET host_name = COALESCE(NULLIF(?, ''), host_name),
                          title = COALESCE(NULLIF(?, ''), title)
                   WHERE id = (SELECT listing_id FROM leads WHERE id = ?)""",
                (host_name, title, lead_id),
            )
            conn.execute(
                """UPDATE deals SET host_name = COALESCE(NULLIF(?, ''), host_name),
                          place_name = COALESCE(NULLIF(?, ''), place_name), updated_at = CURRENT_TIMESTAMP
                   WHERE listing_id = (SELECT listing_id FROM leads WHERE id = ?)""",
                (host_name, title, lead_id),
            )
        conn.commit()
        logger.info(
            "[enriched lead #%s] host=%s | description=%d chars | amenities=%d | review excerpts=%d",
            lead_id, host_name or "not available", len(detail.get("description", "")),
            len(detail.get("amenities", [])), len(detail.get("review_excerpts", [])),
        )
    finally:
        conn.close()


def save_score(
    lead_id: int,
    score: float,
    breakdown: dict[str, float],
    rationale: str = "",
    db_path: Optional[str] = None,
) -> None:
    """Persist the Analyst's verdict."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """UPDATE leads
                  SET collab_fit_score = ?, score_breakdown_json = ?,
                      score_rationale = ?, scored_at = ?,
                      updated_at = CURRENT_TIMESTAMP
                WHERE id = ?""",
            (
                score,
                json.dumps(breakdown),
                rationale,
                datetime.now(timezone.utc).isoformat(),
                lead_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def top_unsent_leads(
    campaign_id: int = 0,
    limit: int = 10,
    min_score: float = 0.0,
    db_path: Optional[str] = None,
) -> list[Lead]:
    """Highest-scoring leads we have not yet messaged.

    This is the queue that decides where a scarce send is spent, so it excludes
    anything whose deal has already left the pre-contact states.
    """
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT l.* FROM leads l
                LEFT JOIN deals d
                       ON d.listing_id = l.listing_id AND d.campaign_id = l.campaign_id
                WHERE l.campaign_id = ?
                  AND l.collab_fit_score IS NOT NULL
                  AND l.collab_fit_score >= ?
                  AND (d.id IS NULL OR d.state IN ('discovered', 'qualified'))
                ORDER BY l.collab_fit_score DESC
                LIMIT ?""",
            (campaign_id, min_score, limit),
        ).fetchall()
        return [_row_to_lead(r) for r in rows]
    finally:
        conn.close()


def list_leads_with_listing(
    campaign_id: int = 0, limit: int = 300, db_path: Optional[str] = None
) -> list[dict]:
    """Every lead joined to its listing and deal, for the Leads dashboard page.

    Returns plain dicts (not ``Lead`` models) because the page also wants listing
    columns and the deal state, and it renders read-only. Ordered best-first:
    scored leads by score desc, then the freshest unscored ones.
    """
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT l.id AS lead_id, l.listing_id, l.campaign_id,
                      l.collab_fit_score, l.detail_scraped_at,
                      li.title, li.location, li.price_per_night, li.currency,
                      li.rating, li.review_count, li.host_name, li.url,
                      d.id AS deal_id, d.state AS deal_state,
                      (SELECT status FROM messages m
                        WHERE m.deal_id = d.id AND m.direction = 'outbound'
                        ORDER BY m.id DESC LIMIT 1) AS last_message_status
                 FROM leads l
                 LEFT JOIN listings li ON li.id = l.listing_id
                 LEFT JOIN deals d
                        ON d.listing_id = l.listing_id AND d.campaign_id = l.campaign_id
                WHERE (? = 0 OR l.campaign_id = ?)
                ORDER BY (l.collab_fit_score IS NULL), l.collab_fit_score DESC, l.id DESC
                LIMIT ?""",
            (campaign_id, campaign_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def leads_needing_enrichment(
    campaign_id: int = 0, limit: int = 25, db_path: Optional[str] = None
) -> list[Lead]:
    """Leads whose detail page has never been scraped."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT * FROM leads
                WHERE campaign_id = ? AND detail_scraped_at IS NULL
                ORDER BY id ASC LIMIT ?""",
            (campaign_id, limit),
        ).fetchall()
        return [_row_to_lead(r) for r in rows]
    finally:
        conn.close()


def leads_needing_scoring(
    campaign_id: int = 0, limit: int = 25, db_path: Optional[str] = None
) -> list[Lead]:
    """Enriched leads that have not been scored yet."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT * FROM leads
                WHERE campaign_id = ?
                  AND detail_scraped_at IS NOT NULL
                  AND collab_fit_score IS NULL
                ORDER BY id ASC LIMIT ?""",
            (campaign_id, limit),
        ).fetchall()
        return [_row_to_lead(r) for r in rows]
    finally:
        conn.close()
