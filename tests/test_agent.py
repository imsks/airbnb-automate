"""Tests for the agent package — LLM abstraction, prompts, graphs, and helpers."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Prompts — basic sanity checks
# ---------------------------------------------------------------------------

from app.agent.prompts import (
    CLASSIFIER_HUMAN,
    CLASSIFIER_SYSTEM,
    NEGOTIATION_HUMAN,
    NEGOTIATION_SYSTEM,
    OUTREACH_HUMAN,
    OUTREACH_SYSTEM,
)


def test_negotiation_prompts_have_placeholders():
    assert "{place_name}" in NEGOTIATION_HUMAN
    assert "{host_name}" in NEGOTIATION_HUMAN
    assert "{conversation}" in NEGOTIATION_HUMAN


def test_outreach_prompts_have_placeholders():
    assert "{place_name}" in OUTREACH_HUMAN
    assert "{host_name}" in OUTREACH_HUMAN
    assert "{amenities}" in OUTREACH_HUMAN


def test_classifier_prompts_have_placeholders():
    assert "{conversation}" in CLASSIFIER_HUMAN


def test_system_prompts_non_empty():
    assert len(NEGOTIATION_SYSTEM) > 100
    assert len(OUTREACH_SYSTEM) > 100
    assert len(CLASSIFIER_SYSTEM) > 100


# ---------------------------------------------------------------------------
# LLM abstraction
# ---------------------------------------------------------------------------


def test_get_llm_returns_openai_by_default():
    """With OPENAI_API_KEY set, get_llm should return a ChatOpenAI instance."""
    from app.agent.llm import get_llm

    # Clear the lru_cache before this test
    get_llm.cache_clear()

    with patch.dict(os.environ, {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-test"}):
        llm = get_llm()
        assert llm is not None
        # Should be a ChatOpenAI (or compatible)
        assert hasattr(llm, "invoke")

    get_llm.cache_clear()


def test_get_llm_gemini_provider():
    from app.agent.llm import get_llm

    get_llm.cache_clear()

    with patch.dict(os.environ, {"LLM_PROVIDER": "gemini", "GOOGLE_API_KEY": "test-key"}):
        llm = get_llm()
        assert llm is not None
        assert hasattr(llm, "invoke")

    get_llm.cache_clear()


def test_get_llm_perplexity_provider():
    from app.agent.llm import get_llm

    get_llm.cache_clear()

    with patch.dict(os.environ, {"LLM_PROVIDER": "perplexity", "PERPLEXITY_API_KEY": "pplx-test"}):
        llm = get_llm()
        assert llm is not None
        assert hasattr(llm, "invoke")

    get_llm.cache_clear()


# ---------------------------------------------------------------------------
# Chat reader data classes
# ---------------------------------------------------------------------------

from app.agent.chat_reader import ChatMessage, ChatThread


def test_chat_thread_conversation_text():
    thread = ChatThread(
        thread_id="123",
        host_name="Alice",
        messages=[
            ChatMessage(sender="host", text="Hello!"),
            ChatMessage(sender="user", text="Hi Alice!"),
            ChatMessage(sender="host", text="Interested in a collab?"),
        ],
    )
    text = thread.conversation_text
    assert "**Host**: Hello!" in text
    assert "**You**: Hi Alice!" in text
    assert "Interested in a collab?" in text


def test_chat_thread_last_message():
    thread = ChatThread(
        thread_id="1",
        host_name="Bob",
        messages=[ChatMessage(sender="host", text="Hey")],
    )
    assert thread.last_message is not None
    assert thread.last_message.text == "Hey"


def test_chat_thread_empty():
    thread = ChatThread(thread_id="2", host_name="Empty")
    assert thread.last_message is None
    assert thread.conversation_text == ""


# ---------------------------------------------------------------------------
# Outreach agent — listing_to_dict
# ---------------------------------------------------------------------------

from app.agent.outreach_agent import listing_to_dict
from app.models import Listing


def test_listing_to_dict():
    listing = Listing(
        id="room123",
        title="Beach Villa",
        host_name="Carlos",
        location="Goa, India",
        price_per_night=50.0,
        currency="USD",
        rating=4.8,
        review_count=42,
        property_type="Villa",
        guests=4,
        bedrooms=2,
        bathrooms=1.5,
        superhost=True,
        amenities=["WiFi", "Pool", "Kitchen"],
    )
    d = listing_to_dict(listing)
    assert d["place_name"] == "Beach Villa"
    assert d["host_name"] == "Carlos"
    assert d["superhost"] == "Yes"
    assert "WiFi" in d["amenities"]


# ---------------------------------------------------------------------------
# Negotiation graph — structure
# ---------------------------------------------------------------------------


def test_negotiation_graph_builds():
    from app.agent.negotiator import build_negotiation_graph

    graph = build_negotiation_graph()
    assert graph is not None


def test_outreach_graph_builds():
    from app.agent.outreach_agent import build_outreach_graph

    graph = build_outreach_graph()
    assert graph is not None


# ---------------------------------------------------------------------------
# Scheduler config
# ---------------------------------------------------------------------------


def test_schedule_interval_default():
    from app.agent.scheduler import get_schedule_interval_seconds

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("AGENT_SCHEDULE_HOURS", None)
        assert get_schedule_interval_seconds() == 5 * 3600


def test_schedule_interval_custom():
    from app.agent.scheduler import get_schedule_interval_seconds

    with patch.dict(os.environ, {"AGENT_SCHEDULE_HOURS": "3"}):
        assert get_schedule_interval_seconds() == 3 * 3600


# ---------------------------------------------------------------------------
# Classify node (mocked LLM)
# ---------------------------------------------------------------------------


def test_classify_node_with_mock_llm():
    from app.agent.negotiator import classify_node

    mock_response = MagicMock()
    mock_response.content = json.dumps({"needs_reply": True, "reason": "host asked a question"})

    mock_llm = MagicMock()
    mock_llm.invoke.return_value = mock_response

    with patch("app.agent.negotiator.get_llm", return_value=mock_llm):
        state = {
            "threads": [
                {
                    "thread_id": "1",
                    "host_name": "Host A",
                    "conversation_text": "**Host**: Are you available next week?",
                    "messages": [],
                }
            ]
        }
        result = classify_node(state)
        assert len(result["threads_needing_reply"]) == 1
        assert result["threads_needing_reply"][0]["thread_id"] == "1"


def test_classify_node_no_reply_needed():
    from app.agent.negotiator import classify_node

    mock_response = MagicMock()
    mock_response.content = json.dumps({"needs_reply": False, "reason": "user already replied"})

    mock_llm = MagicMock()
    mock_llm.invoke.return_value = mock_response

    with patch("app.agent.negotiator.get_llm", return_value=mock_llm):
        state = {
            "threads": [
                {
                    "thread_id": "2",
                    "host_name": "Host B",
                    "conversation_text": "**You**: Thanks for the offer!",
                    "messages": [],
                }
            ]
        }
        result = classify_node(state)
        assert len(result["threads_needing_reply"]) == 0


# ---------------------------------------------------------------------------
# Generate replies node (mocked LLM)
# ---------------------------------------------------------------------------


def test_generate_replies_node_with_mock_llm():
    from app.agent.negotiator import generate_replies_node

    mock_response = MagicMock()
    mock_response.content = "Hi! I'd love to discuss a collab. Would a content exchange work?"

    mock_llm = MagicMock()
    mock_llm.invoke.return_value = mock_response

    with patch("app.agent.negotiator.get_llm", return_value=mock_llm):
        state = {
            "threads_needing_reply": [
                {
                    "thread_id": "1",
                    "host_name": "Alice",
                    "listing_title": "Beachfront Villa",
                    "conversation_text": "**Host**: Are you interested?",
                    "classify_reason": "host asked question",
                }
            ]
        }
        result = generate_replies_node(state)
        replies = result["generated_replies"]
        assert len(replies) == 1
        assert replies[0]["host_name"] == "Alice"
        assert "collab" in replies[0]["reply"].lower()


# ---------------------------------------------------------------------------
# Outreach generation (mocked LLM)
# ---------------------------------------------------------------------------


def test_generate_outreach_message_with_mock_llm():
    from app.agent.outreach_agent import generate_outreach_message

    mock_response = MagicMock()
    mock_response.content = "Hey Carlos! Your Beach Villa in Goa looks incredible..."

    mock_llm = MagicMock()
    mock_llm.invoke.return_value = mock_response

    listing = Listing(
        id="room123",
        title="Beach Villa",
        host_name="Carlos",
        location="Goa, India",
    )

    with patch("app.agent.outreach_agent.get_llm", return_value=mock_llm):
        msg = generate_outreach_message(listing)
        assert "Carlos" in msg
        assert "Beach Villa" in msg
