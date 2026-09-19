"""Records every LLM call to ``agent_runs``.

Without this a multi-agent system is undebuggable: you cannot tell which agent
burned the budget, which prompt version converts, or which call produced the
message a host actually received.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from app.database import get_connection

logger = logging.getLogger(__name__)

_PREVIEW_CHARS = 2000

#: USD per 1M tokens, (input, output). Unlisted models record zero cost rather
#: than guessing — a wrong number is worse than a missing one.
_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "sonar-pro": (3.00, 15.00),
}


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Dollar cost of a call, or 0.0 for models with no known price."""
    rates = _PRICING.get((model or "").lower())
    if not rates:
        return 0.0
    return (prompt_tokens * rates[0] + completion_tokens * rates[1]) / 1_000_000


def _extract_usage(response: Any) -> tuple[int, int]:
    """Pull (prompt, completion) token counts out of a LangChain response."""
    usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict):
        return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))

    metadata = getattr(response, "response_metadata", None) or {}
    token_usage = metadata.get("token_usage") or metadata.get("usage") or {}
    if isinstance(token_usage, dict):
        prompt = token_usage.get("prompt_tokens") or token_usage.get("input_tokens") or 0
        completion = (
            token_usage.get("completion_tokens") or token_usage.get("output_tokens") or 0
        )
        return int(prompt), int(completion)
    return 0, 0


def record_run(
    agent: str,
    *,
    job_id: Optional[int] = None,
    deal_id: Optional[int] = None,
    provider: str = "",
    model: str = "",
    prompt_version: str = "",
    input_preview: str = "",
    output_preview: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: int = 0,
    ok: bool = True,
    error: str = "",
    db_path: Optional[str] = None,
) -> int:
    """Write one ``agent_runs`` row and return its id."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO agent_runs
               (agent, job_id, deal_id, provider, model, prompt_version,
                input_preview, output_preview, prompt_tokens, completion_tokens,
                cost_usd, latency_ms, ok, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agent,
                job_id,
                deal_id,
                provider,
                model,
                prompt_version,
                input_preview[:_PREVIEW_CHARS],
                output_preview[:_PREVIEW_CHARS],
                prompt_tokens,
                completion_tokens,
                estimate_cost_usd(model, prompt_tokens, completion_tokens),
                latency_ms,
                1 if ok else 0,
                error[:_PREVIEW_CHARS],
            ),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def tracked_invoke(
    llm: Any,
    messages: list,
    *,
    agent: str,
    job_id: Optional[int] = None,
    deal_id: Optional[int] = None,
    prompt_version: str = "",
    db_path: Optional[str] = None,
) -> tuple[str, int]:
    """Invoke an LLM and record the call. Returns ``(text, agent_run_id)``.

    Failures are recorded before the exception is re-raised, so a crashed run
    still shows up in the cost ledger.
    """
    model = getattr(llm, "model_name", None) or getattr(llm, "model", "") or ""
    provider = type(llm).__name__
    input_preview = "\n\n".join(str(getattr(m, "content", m)) for m in messages)

    started = time.perf_counter()
    try:
        response = llm.invoke(messages)
    except Exception as exc:
        record_run(
            agent,
            job_id=job_id,
            deal_id=deal_id,
            provider=provider,
            model=str(model),
            prompt_version=prompt_version,
            input_preview=input_preview,
            latency_ms=int((time.perf_counter() - started) * 1000),
            ok=False,
            error=str(exc),
            db_path=db_path,
        )
        raise

    latency_ms = int((time.perf_counter() - started) * 1000)
    text = str(getattr(response, "content", response) or "").strip()
    prompt_tokens, completion_tokens = _extract_usage(response)

    run_id = record_run(
        agent,
        job_id=job_id,
        deal_id=deal_id,
        provider=provider,
        model=str(model),
        prompt_version=prompt_version,
        input_preview=input_preview,
        output_preview=text,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        db_path=db_path,
    )
    return text, run_id


def cost_by_agent(db_path: Optional[str] = None) -> list[dict[str, Any]]:
    """Spend and call volume per agent, for the dashboard."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT agent, COUNT(*) AS runs, SUM(cost_usd) AS cost_usd,
                      SUM(prompt_tokens + completion_tokens) AS tokens,
                      SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failures
                 FROM agent_runs GROUP BY agent ORDER BY cost_usd DESC"""
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def cost_for_deal(deal_id: int, db_path: Optional[str] = None) -> float:
    """Total LLM spend attributable to one deal."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM agent_runs WHERE deal_id = ?",
            (deal_id,),
        ).fetchone()
        return float(row["c"]) if row else 0.0
    finally:
        conn.close()
