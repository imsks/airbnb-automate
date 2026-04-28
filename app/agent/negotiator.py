"""LangGraph negotiation workflow.

Workflow nodes:
  1. **fetch_chats**   — pull inbox threads via browser automation
  2. **classify**      — for each thread, determine if a reply is needed
  3. **generate_reply** — craft a negotiation reply using the LLM
  4. **send_reply**    — (optional) send the reply via browser automation

The graph can be run end-to-end or step-by-step for human-in-the-loop review.
"""

from __future__ import annotations

import json
import logging
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


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class NegotiationState(TypedDict, total=False):
    """Shared state flowing through the negotiation graph."""

    threads: list[dict]  # serialised ChatThread data
    threads_needing_reply: list[dict]
    generated_replies: list[dict]  # [{thread_id, host_name, reply, ...}]
    send_results: list[dict]
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
        "conversation_text": t.conversation_text,
        "messages": [
            {"sender": m.sender, "text": m.text, "timestamp": m.timestamp}
            for m in t.messages
        ],
    }


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


def fetch_chats_node(state: NegotiationState) -> dict[str, Any]:
    """Node 1: fetch all inbox threads."""
    headless = state.get("headless", True)
    max_threads = state.get("max_threads", 20)
    logger.info("📥 Fetching inbox chats (headless=%s, max=%d)…", headless, max_threads)
    threads = fetch_inbox_chats_sync(max_threads=max_threads, headless=headless)
    logger.info("   Found %d threads", len(threads))
    return {"threads": [_thread_to_dict(t) for t in threads]}


def classify_node(state: NegotiationState) -> dict[str, Any]:
    """Node 2: classify which threads need a reply."""
    llm = get_llm()
    threads = state.get("threads", [])
    needs_reply: list[dict] = []

    for t in threads:
        conv = t.get("conversation_text", "")
        if not conv:
            continue

        prompt = CLASSIFIER_HUMAN.format(conversation=conv)
        response = llm.invoke([
            SystemMessage(content=CLASSIFIER_SYSTEM),
            HumanMessage(content=prompt),
        ])
        text = response.content.strip()

        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON from the response
            import re
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                result = json.loads(match.group())
            else:
                logger.warning("Could not parse classifier output: %s", text)
                result = {"needs_reply": False, "reason": "parse error"}

        if result.get("needs_reply"):
            t["classify_reason"] = result.get("reason", "")
            needs_reply.append(t)

    logger.info("   %d / %d threads need a reply", len(needs_reply), len(threads))
    return {"threads_needing_reply": needs_reply}


def generate_replies_node(state: NegotiationState) -> dict[str, Any]:
    """Node 3: generate negotiation replies."""
    llm = get_llm()
    threads = state.get("threads_needing_reply", [])
    replies: list[dict] = []

    for t in threads:
        prompt = NEGOTIATION_HUMAN.format(
            place_name=t.get("listing_title", "your place"),
            host_name=t.get("host_name", "Host"),
            location="",
            price_per_night="N/A",
            currency="",
            rating="N/A",
            review_count="N/A",
            conversation=t.get("conversation_text", ""),
        )
        response = llm.invoke([
            SystemMessage(content=NEGOTIATION_SYSTEM),
            HumanMessage(content=prompt),
        ])
        reply_text = response.content.strip()
        replies.append({
            "thread_id": t.get("thread_id"),
            "host_name": t.get("host_name"),
            "reply": reply_text,
            "classify_reason": t.get("classify_reason", ""),
        })
        logger.info("   ✍️  Generated reply for %s", t.get("host_name"))

    return {"generated_replies": replies}


def send_replies_node(state: NegotiationState) -> dict[str, Any]:
    """Node 4: send replies (placeholder — logs for now; browser send TBD)."""
    auto_send = state.get("auto_send", False)
    replies = state.get("generated_replies", [])
    results: list[dict] = []

    for r in replies:
        if auto_send:
            # TODO: implement browser-based reply sending
            logger.info(
                "🚀 [auto-send] Would send reply to %s (thread %s)",
                r.get("host_name"),
                r.get("thread_id"),
            )
            results.append({**r, "status": "pending_send", "note": "auto-send not yet wired"})
        else:
            logger.info(
                "📝 [review] Reply for %s:\n%s",
                r.get("host_name"),
                r.get("reply"),
            )
            results.append({**r, "status": "review"})

    return {"send_results": results}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def _should_generate(state: NegotiationState) -> str:
    """Route: skip generation if nothing needs a reply."""
    if state.get("threads_needing_reply"):
        return "generate_replies"
    return "done"


# ---------------------------------------------------------------------------
# Build the graph
# ---------------------------------------------------------------------------


def build_negotiation_graph() -> StateGraph:
    """Return a compiled LangGraph for the negotiation workflow."""
    graph = StateGraph(NegotiationState)

    graph.add_node("fetch_chats", fetch_chats_node)
    graph.add_node("classify", classify_node)
    graph.add_node("generate_replies", generate_replies_node)
    graph.add_node("send_replies", send_replies_node)

    graph.set_entry_point("fetch_chats")
    graph.add_edge("fetch_chats", "classify")
    graph.add_conditional_edges("classify", _should_generate, {
        "generate_replies": "generate_replies",
        "done": END,
    })
    graph.add_edge("generate_replies", "send_replies")
    graph.add_edge("send_replies", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_negotiation(
    *,
    headless: bool = True,
    auto_send: bool = False,
    max_threads: int = 20,
) -> list[dict]:
    """Run the full negotiation workflow and return generated replies.

    Args:
        headless: run the browser in headless mode.
        auto_send: if True, attempt to send replies automatically.
        max_threads: max inbox threads to process.

    Returns:
        List of dicts with thread_id, host_name, reply, and status.
    """
    graph = build_negotiation_graph()
    result = graph.invoke({
        "headless": headless,
        "auto_send": auto_send,
        "max_threads": max_threads,
    })
    return result.get("send_results") or result.get("generated_replies") or []
