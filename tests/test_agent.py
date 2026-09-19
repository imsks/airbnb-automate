"""Tests for the agent package — LLM abstraction, chat reader, v2 prompts."""

from __future__ import annotations

import os
from unittest.mock import patch

from app.agent.chat_reader import ChatMessage, ChatThread
from app.agent.prompts_v2 import (
    build_closer_prompt,
    build_scribe_prompt,
    guardrail_block,
)
from app.models import Lead, Listing
from app.policy import GuardrailPolicy


# --- LLM abstraction -------------------------------------------------------


def test_get_llm_openai_provider():
    from app.agent.llm import get_llm

    get_llm.cache_clear()
    with patch.dict(os.environ, {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-test"}):
        assert hasattr(get_llm(), "invoke")
    get_llm.cache_clear()


def test_get_llm_gemini_provider():
    from app.agent.llm import get_llm

    get_llm.cache_clear()
    with patch.dict(os.environ, {"LLM_PROVIDER": "gemini", "GOOGLE_API_KEY": "test-key"}):
        assert hasattr(get_llm(), "invoke")
    get_llm.cache_clear()


def test_get_llm_perplexity_provider():
    from app.agent.llm import get_llm

    get_llm.cache_clear()
    with patch.dict(
        os.environ, {"LLM_PROVIDER": "perplexity", "PERPLEXITY_API_KEY": "pplx-test"}
    ):
        assert hasattr(get_llm(), "invoke")
    get_llm.cache_clear()


# --- Chat reader -----------------------------------------------------------


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
        thread_id="1", host_name="Bob", messages=[ChatMessage(sender="host", text="Hey")]
    )
    assert thread.last_message.text == "Hey"


def test_chat_thread_empty():
    thread = ChatThread(thread_id="2", host_name="Empty")
    assert thread.last_message is None
    assert thread.conversation_text == ""


# --- v2 prompts ------------------------------------------------------------


def _policy(**kwargs):
    base = dict(
        max_price_per_night=0.0,
        currency="INR",
        allowed_deliverables=["2 Instagram reels", "10 edited photos"],
        max_agent_replies_per_thread=4,
        credential_facts={
            "name": "Sachin Shukla",
            "role": "founder of The Boring Education",
            "followers": "150k+ combined",
            "handles": "@theboringfounder",
        },
    )
    base.update(kwargs)
    return GuardrailPolicy(**base)


def test_guardrail_block_states_a_free_only_rule():
    text = guardrail_block(_policy())
    assert "Never agree to pay anything" in text
    assert "2 Instagram reels" in text
    assert "@theboringfounder" in text
    assert "150k+ combined" in text


def test_guardrail_block_states_a_ceiling_when_one_is_set():
    text = guardrail_block(_policy(max_price_per_night=2500.0))
    assert "2500 INR" in text
    assert "Never agree to pay anything" not in text


def test_scribe_prompt_carries_the_listing_context():
    """The whole point of the v2 Scribe: write from what the host actually said."""
    listing = Listing(id="L1", title="Sea Villa", host_name="Asha", location="Goa, India")
    lead = Lead(
        listing_id="L1",
        description="A sunlit villa above the beach.",
        host_bio="I grew up here.",
        review_excerpts=["The sunrise deck is unreal"],
        amenities=["Wifi", "Dedicated workspace"],
    )
    system, human = build_scribe_prompt(listing, lead, _policy(), "Known for beach shacks")

    assert "Sachin Shukla" in system
    assert "sunlit villa" in human
    assert "sunrise deck" in human
    assert "Dedicated workspace" in human
    assert "beach shacks" in human
    assert "Asha" in human


def test_scribe_prompt_degrades_without_enrichment():
    """A lead whose detail page failed to scrape must still produce a prompt."""
    listing = Listing(id="L1", title="Sea Villa", host_name="Asha")
    system, human = build_scribe_prompt(listing, None, _policy())
    assert "(not available)" in human
    assert "(no reviews yet)" in human
    assert system


def test_closer_prompt_includes_the_round_number():
    system, human = build_closer_prompt(
        place_name="Sea Villa",
        host_name="Asha",
        location="Goa, India",
        booking_status="invited to book",
        conversation="Host: Tell me more.",
        round_number=3,
        policy=_policy(),
    )
    assert "round 3" in system
    assert "Tell me more" in human
    assert "Sea Villa" in human
