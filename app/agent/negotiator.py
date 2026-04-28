"""LangGraph negotiation workflow — single-thread focused.

Flow:
  1. **fetch_chats**   — pull first N inbox threads via browser automation
  2. **pre_filter**    — locally skip threads that are awaiting (last msg = user)
                         or have dead booking statuses (expired, unavailable)
  3. **classify**      — LLM decides: is there a real *chance* to negotiate?
                         straight "no" / declined → skip.  only "chance" → proceed.
  4. **pick_one**      — select the single best candidate thread
  5. **generate_reply** — craft a negotiation reply using the LLM
  6. **present**       — print the reply for human review (or auto-send)

The graph can be run end-to-end or step-by-step for human-in-the-loop review.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from app.agent.chat_reader import ChatThread, fetch_inbox_chats_sync
from app.agent.llm import get_llm
from app.agent.prompts import (
    CLASSIFIER_HUMAN,
    CLASSIFIER_SYSTEM,
    NEGOTIATION_HUMAN,
    NEGOTIATION_SYSTEM,
)

logger = logging.getLogger(__name__)

# Booking statuses that indicate the thread is dead — no point replying
_DEAD_STATUSES = {
    "invite expired",
    "dates are not available",
    "declined",
    "cancelled",
    "withdrawn",
}

DEFAULT_MAX_THREADS = 5


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class NegotiationState(TypedDict, total=False):
    """Shared state flowing through the negotiation graph."""

    threads: list[dict]  # serialised ChatThread data
    candidates: list[dict]  # threads that passed pre-filter
    picked_thread: dict  # single thread selected for reply
    generated_reply: dict  # {thread_id, host_name, reply, …}
    result: dict  # final output
    headless: bool
    auto_send: bool
    max_threads: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _thread_to_dict(t: ChatThread) -> dict:
    return {
        "thread_id": t.thread_id,
        "host_name": t.host_name,
        "listing_title": t.listing_title,
        "listing_url": t.listing_url,
        "booking_status": t.booking_status,
        "location": t.location,
        "conversation_text": t.conversation_text,
        "messages": [
            {"sender": m.sender, "text": m.text, "timestamp": m.timestamp}
            for m in t.messages
        ],
    }


def _last_sender(t: dict) -> str:
    msgs = t.get("messages", [])
    return msgs[-1]["sender"] if msgs else "unknown"


# ---------------------------------------------------------------------------
# Node 1 — Fetch
# ---------------------------------------------------------------------------


def fetch_chats_node(state: NegotiationState) -> dict[str, Any]:
    """Pull first N inbox threads."""
    headless = state.get("headless", True)
    max_threads = state.get("max_threads", DEFAULT_MAX_THREADS)
    logger.info("📥 Fetching first %d inbox thread(s) (headless=%s)…", max_threads, headless)

    threads = fetch_inbox_chats_sync(max_threads=max_threads, headless=headless)

    logger.info("   Fetched %d thread(s)", len(threads))
    for t in threads:
        msg_count = len(t.messages)
        last = t.messages[-1].sender if t.messages else "n/a"
        logger.info(
            "   📌 %s (id=%s): %d msg(s), last_sender=%s, status=%s, loc=%s",
            t.host_name, t.thread_id, msg_count, last,
            t.booking_status or "(none)", t.location or "(none)",
        )

    return {"threads": [_thread_to_dict(t) for t in threads]}


# ---------------------------------------------------------------------------
# Node 2 — Pre-filter (local, no LLM)
# ---------------------------------------------------------------------------


def pre_filter_node(state: NegotiationState) -> dict[str, Any]:
    """Skip threads we can rule out without an LLM call.

    Skip when:
      - last message is from user (we're already awaiting a host reply)
      - booking status is dead (expired, unavailable, declined, etc.)
      - conversation is empty
    """
    threads = state.get("threads", [])
    candidates: list[dict] = []

    logger.info("🔎 Pre-filtering %d thread(s)…", len(threads))

    for t in threads:
        host = t.get("host_name", "?")
        tid = t.get("thread_id", "?")
        status = (t.get("booking_status") or "").strip().lower()
        msgs = t.get("messages", [])
        conv = t.get("conversation_text", "")

        # Skip: empty conversation
        if not conv or not msgs:
            logger.info("   ⏭️  %s (#%s): SKIP — empty conversation", host, tid)
            continue

        # Skip: last message is from user → we're awaiting host reply
        last = msgs[-1]["sender"]
        if last == "user":
            logger.info("   ⏭️  %s (#%s): SKIP — awaiting host reply (last msg is ours)", host, tid)
            continue

        # Skip: dead booking status
        if status in _DEAD_STATUSES:
            logger.info("   ⏭️  %s (#%s): SKIP — dead status '%s'", host, tid, status)
            continue

        logger.info("   ✅ %s (#%s): candidate (last_sender=host, status='%s')", host, tid, status)
        candidates.append(t)

    logger.info("   %d / %d thread(s) passed pre-filter", len(candidates), len(threads))
    return {"candidates": candidates}


# ---------------------------------------------------------------------------
# Node 3 — Classify (LLM) — is there a real chance?
# ---------------------------------------------------------------------------


def classify_node(state: NegotiationState) -> dict[str, Any]:
    """LLM classifies each candidate: 'chance' or 'no_chance'."""
    candidates = state.get("candidates", [])
    if not candidates:
        logger.info("🔍 No candidates to classify")
        return {"candidates": []}

    llm = get_llm()
    worth_replying: list[dict] = []

    logger.info("🔍 Classifying %d candidate(s) with LLM…", len(candidates))

    for t in candidates:
        host = t.get("host_name", "?")
        conv = t.get("conversation_text", "")

        logger.info("   Classifying %s…", host)
        prompt = CLASSIFIER_HUMAN.format(conversation=conv)
        response = llm.invoke([
            SystemMessage(content=CLASSIFIER_SYSTEM),
            HumanMessage(content=prompt),
        ])
        raw = response.content.strip()
        logger.debug("   Classifier raw for %s: %s", host, raw[:300])

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                result = json.loads(match.group())
            else:
                logger.warning("   Could not parse classifier output for %s — skipping", host)
                continue

        needs = result.get("needs_reply", False)
        reason = result.get("reason", "")

        if needs:
            t["classify_reason"] = reason
            worth_replying.append(t)
            logger.info("   ✅ %s → CHANCE: %s", host, reason)
        else:
            logger.info("   ❌ %s → NO CHANCE: %s", host, reason)

    logger.info("   %d / %d candidate(s) worth replying to", len(worth_replying), len(candidates))
    return {"candidates": worth_replying}


# ---------------------------------------------------------------------------
# Node 4 — Pick the single best thread
# ---------------------------------------------------------------------------


def pick_one_node(state: NegotiationState) -> dict[str, Any]:
    """Select the single best thread to reply to.

    Priority: most recent thread with the fewest messages (freshest
    conversation, host still engaged).
    """
    candidates = state.get("candidates", [])
    if not candidates:
        logger.info("🎯 No thread to pick — nothing worth replying to")
        return {"picked_thread": {}}

    # Sort: fewest messages first (freshest negotiation), break ties by index
    picked = min(candidates, key=lambda t: len(t.get("messages", [])))
    host = picked.get("host_name", "?")
    logger.info(
        "🎯 Picked thread: %s (#%s) — %d msg(s), reason: %s",
        host, picked.get("thread_id"), len(picked.get("messages", [])),
        picked.get("classify_reason", ""),
    )
    return {"picked_thread": picked}


# ---------------------------------------------------------------------------
# Node 5 — Generate reply
# ---------------------------------------------------------------------------


def generate_reply_node(state: NegotiationState) -> dict[str, Any]:
    """Craft a negotiation reply for the picked thread."""
    picked = state.get("picked_thread", {})
    if not picked:
        return {"generated_reply": {}}

    llm = get_llm()
    host = picked.get("host_name", "Host")
    logger.info("✍️  Generating reply for %s…", host)

    prompt = NEGOTIATION_HUMAN.format(
        place_name=picked.get("listing_title") or "your place",
        host_name=host,
        location=picked.get("location", ""),
        price_per_night="N/A",
        currency="",
        rating="N/A",
        review_count="N/A",
        conversation=picked.get("conversation_text", ""),
    )
    response = llm.invoke([
        SystemMessage(content=NEGOTIATION_SYSTEM),
        HumanMessage(content=prompt),
    ])
    reply_text = response.content.strip()

    reply = {
        "thread_id": picked.get("thread_id"),
        "host_name": host,
        "location": picked.get("location", ""),
        "booking_status": picked.get("booking_status", ""),
        "classify_reason": picked.get("classify_reason", ""),
        "reply": reply_text,
    }
    logger.info("   ✅ Reply generated for %s (%d chars)", host, len(reply_text))
    logger.debug("   Reply: %s", reply_text)
    return {"generated_reply": reply}


# ---------------------------------------------------------------------------
# Node 6 — Present / send
# ---------------------------------------------------------------------------


def present_node(state: NegotiationState) -> dict[str, Any]:
    """Print the reply for review or auto-send."""
    auto_send = state.get("auto_send", False)
    reply = state.get("generated_reply", {})

    if not reply or not reply.get("reply"):
        logger.info("📭 Nothing to present — no reply was generated")
        return {"result": {"status": "no_action"}}

    host = reply.get("host_name", "?")

    if auto_send:
        # TODO: wire browser-based reply sending
        logger.info("🚀 [auto-send] Would send reply to %s (thread %s)", host, reply.get("thread_id"))
        return {"result": {**reply, "status": "pending_send", "note": "auto-send not yet wired"}}

    logger.info("📝 [review] Reply for %s:\n%s", host, reply.get("reply"))
    return {"result": {**reply, "status": "review"}}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def _has_candidates(state: NegotiationState) -> str:
    if state.get("candidates"):
        return "classify"
    return "done"


def _has_picked(state: NegotiationState) -> str:
    if state.get("picked_thread"):
        return "generate_reply"
    return "done"


# ---------------------------------------------------------------------------
# Build the graph
# ---------------------------------------------------------------------------


def build_negotiation_graph() -> StateGraph:
    """Return a compiled LangGraph for the negotiation workflow."""
    graph = StateGraph(NegotiationState)

    graph.add_node("fetch_chats", fetch_chats_node)
    graph.add_node("pre_filter", pre_filter_node)
    graph.add_node("classify", classify_node)
    graph.add_node("pick_one", pick_one_node)
    graph.add_node("generate_reply", generate_reply_node)
    graph.add_node("present", present_node)

    graph.set_entry_point("fetch_chats")
    graph.add_edge("fetch_chats", "pre_filter")
    graph.add_conditional_edges("pre_filter", _has_candidates, {
        "classify": "classify",
        "done": END,
    })
    graph.add_edge("classify", "pick_one")
    graph.add_conditional_edges("pick_one", _has_picked, {
        "generate_reply": "generate_reply",
        "done": END,
    })
    graph.add_edge("generate_reply", "present")
    graph.add_edge("present", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_negotiation(
    *,
    headless: bool = True,
    auto_send: bool = False,
    max_threads: int = DEFAULT_MAX_THREADS,
) -> list[dict]:
    """Run the negotiation workflow and return the generated reply (if any).

    Args:
        headless: run the browser in headless mode.
        auto_send: if True, attempt to send the reply automatically.
        max_threads: max inbox threads to fetch (default 5).

    Returns:
        List with a single dict (thread_id, host_name, reply, status)
        or empty list if no action was taken.
    """
    graph = build_negotiation_graph()
    final = graph.invoke({
        "headless": headless,
        "auto_send": auto_send,
        "max_threads": max_threads,
    })
    result = final.get("result", {})
    if result and result.get("status") != "no_action":
        return [result]
    return []
