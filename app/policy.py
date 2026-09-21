"""Guardrail policy and the kill switch.

Policy lives in the database rather than only in env so it can be changed from
the dashboard and take effect on the next send without restarting the worker.
Env supplies the defaults used to seed an empty policy table.

Nothing here decides *whether a draft is acceptable* — that is the Warden's job
(:mod:`app.warden`). This module only answers "what are the limits right now?".
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import time
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.config import (
    get_default_allowed_deliverables,
    get_default_credential_facts,
    get_default_max_agent_replies_per_thread,
    get_default_price_ceiling_per_night,
)
from app.database import get_connection

logger = logging.getLogger(__name__)

KEY_SENDING_ENABLED = "sending_enabled"
KEY_PRICE_CEILING = "max_price_per_night"
KEY_ALLOW_SPECIFIC_DATES = "allow_specific_dates"
KEY_ALLOWED_DELIVERABLES = "allowed_deliverables"
KEY_MAX_AGENT_REPLIES = "max_agent_replies_per_thread"
KEY_CREDENTIAL_FACTS = "credential_facts"
KEY_ALLOW_OFF_PLATFORM = "allow_off_platform_contact"
KEY_SINGLE_SEND = "single_message_authorization"


class GuardrailPolicy(BaseModel):
    """The limits an autonomous agent is structurally unable to exceed."""

    sending_enabled: bool = True
    max_price_per_night: float = 0.0
    currency: str = "INR"
    #: Agents commit to availability windows, never to specific calendar dates.
    allow_specific_dates: bool = False
    allowed_deliverables: list[str] = Field(default_factory=list)
    max_agent_replies_per_thread: int = 4
    #: The only claims an agent may make about reach, credentials or identity.
    credential_facts: dict[str, str] = Field(default_factory=dict)
    allow_off_platform_contact: bool = False


def _get_raw(conn, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM policy WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_policy_value(key: str, value: Any, db_path: Optional[str] = None) -> None:
    """Upsert a single policy key. Values are stored as JSON."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """INSERT INTO policy (key, value, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE
                   SET value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
            (key, json.dumps(value)),
        )
        conn.commit()
    finally:
        conn.close()


def load_policy(db_path: Optional[str] = None) -> GuardrailPolicy:
    """Current policy, falling back to env defaults for unset keys."""
    conn = get_connection(db_path)
    try:
        stored: dict[str, Any] = {}
        for row in conn.execute("SELECT key, value FROM policy"):
            try:
                stored[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                stored[row["key"]] = row["value"]
    finally:
        conn.close()

    ceiling, currency = get_default_price_ceiling_per_night()
    return GuardrailPolicy(
        sending_enabled=bool(stored.get(KEY_SENDING_ENABLED, True)),
        max_price_per_night=float(stored.get(KEY_PRICE_CEILING, ceiling)),
        currency=currency,
        allow_specific_dates=bool(stored.get(KEY_ALLOW_SPECIFIC_DATES, False)),
        allowed_deliverables=list(
            stored.get(KEY_ALLOWED_DELIVERABLES, get_default_allowed_deliverables())
        ),
        max_agent_replies_per_thread=int(
            stored.get(KEY_MAX_AGENT_REPLIES, get_default_max_agent_replies_per_thread())
        ),
        credential_facts=dict(
            stored.get(KEY_CREDENTIAL_FACTS, get_default_credential_facts())
        ),
        allow_off_platform_contact=bool(stored.get(KEY_ALLOW_OFF_PLATFORM, False)),
    )


def sending_enabled(db_path: Optional[str] = None) -> bool:
    """The kill switch, read fresh. Call immediately before every send."""
    conn = get_connection(db_path)
    try:
        if _get_raw(conn, KEY_SINGLE_SEND) is not None:
            return False
        raw = _get_raw(conn, KEY_SENDING_ENABLED)
    finally:
        conn.close()
    if raw is None:
        return True
    try:
        return json.loads(raw) is True
    except json.JSONDecodeError:
        return False


def freeze_sending(reason: str = "", db_path: Optional[str] = None) -> None:
    """Stop all outbound messages immediately."""
    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO policy(key, value) VALUES (?, 'false') "
                "ON CONFLICT(key) DO UPDATE SET value = 'false', updated_at = CURRENT_TIMESTAMP",
                (KEY_SENDING_ENABLED,),
            )
            conn.execute("DELETE FROM policy WHERE key = ?", (KEY_SINGLE_SEND,))
    finally:
        conn.close()
    logger.warning("KILL SWITCH ENGAGED — all sending frozen. %s", reason)


def resume_sending(db_path: Optional[str] = None) -> None:
    """Re-enable outbound messages."""
    conn = get_connection(db_path)
    try:
        if _get_raw(conn, KEY_SINGLE_SEND) is not None:
            raise ValueError("A single-message test is active; the bulk queue cannot be resumed.")
    finally:
        conn.close()
    set_policy_value(KEY_SENDING_ENABLED, True, db_path)
    logger.warning("Sending resumed")


def authorize_single_message(message_id: int, db_path: Optional[str] = None) -> str:
    """Permit one submission of one stored message while bulk sending stays frozen."""
    token = secrets.token_urlsafe(32)
    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status, direction FROM messages WHERE id = ?", (message_id,)).fetchone()
            if row is None or row["direction"] != "outbound":
                raise ValueError("Select an existing outbound message.")
            if row["status"] in ("sending", "sent"):
                raise ValueError("This message may already have been submitted; inspect its thread first.")
            if _get_raw(conn, KEY_SINGLE_SEND) is not None:
                raise ValueError("Another single-message test is active.")
            grant = {"message_id": message_id, "token": token, "claimed": False, "expires_at": time.time() + 600}
            conn.execute(
                "INSERT INTO policy(key,value) VALUES (?, 'false') "
                "ON CONFLICT(key) DO UPDATE SET value = 'false', updated_at = CURRENT_TIMESTAMP",
                (KEY_SENDING_ENABLED,),
            )
            conn.execute("INSERT INTO policy(key,value) VALUES (?, ?)", (KEY_SINGLE_SEND, json.dumps(grant)))
    finally:
        conn.close()
    logger.info("[authorization] Only message #%s may be submitted once; bulk sending stays frozen.", message_id)
    return token


def _single_message_grant(conn: sqlite3.Connection, message_id: Optional[int], token: Optional[str]) -> Optional[dict]:
    if message_id is None or not token:
        return None
    try:
        grant = json.loads(_get_raw(conn, KEY_SINGLE_SEND) or "null")
        if (
            isinstance(grant, dict)
            and grant.get("message_id") == message_id
            and grant.get("claimed") is False
            and grant.get("expires_at", 0) > time.time()
            and secrets.compare_digest(grant.get("token", ""), token)
        ):
            return grant
    except (ValueError, TypeError):
        pass
    return None


def single_message_authorized(message_id: Optional[int], token: Optional[str], db_path: Optional[str] = None) -> bool:
    conn = get_connection(db_path)
    try:
        return _single_message_grant(conn, message_id, token) is not None
    finally:
        conn.close()


def claim_send_permission(conn: sqlite3.Connection, message_id: int, token: Optional[str]) -> None:
    """Check permission in the same transaction that records the submission attempt."""
    if token:
        grant = _single_message_grant(conn, message_id, token)
        if grant is None:
            raise PermissionError("Single-message authorization expired, was revoked, or was already used.")
        grant["claimed"] = True
        conn.execute("UPDATE policy SET value = ? WHERE key = ?", (json.dumps(grant), KEY_SINGLE_SEND))
        return
    if _get_raw(conn, KEY_SINGLE_SEND) is not None:
        raise PermissionError("Bulk sending is paused for a single-message test.")
    if json.loads(_get_raw(conn, KEY_SENDING_ENABLED) or "true") is not True:
        raise PermissionError("Sending was frozen before the final Send action.")
