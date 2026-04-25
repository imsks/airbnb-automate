"""Tests for the outreach module."""

import os

from app.models import Listing
from app.outreach import generate_pitch_message, generate_follow_up_message, format_message_for_airbnb


CREATOR_PROFILE = {
    "name": "Alex Creator",
    "platform": "Instagram",
    "handle": "@alexcreator",
    "followers": 15000,
    "email": "alex@example.com",
    "portfolio_url": "https://alexcreator.com",
    "content_types": [
        "Professional photography",
        "Short-form video (Reels/TikTok)",
    ],
}


def test_generate_pitch_message():
    """Test pitch message generation."""
    listing = Listing(
        id="123",
        title="Beautiful Beach Villa",
        host_name="Maria",
        location="Goa, India",
        rating=4.9,
        superhost=True,
        url="https://airbnb.com/rooms/123",
    )

    message = generate_pitch_message(listing, CREATOR_PROFILE)

    assert "Maria" in message
    assert "Beautiful Beach Villa" in message
    assert "Alex Creator" in message
    assert "@alexcreator" in message
    assert "Instagram" in message
    assert "Professional photography" in message
    assert "Superhost" in message


def test_generate_pitch_message_no_host():
    """Test pitch with unknown host defaults to 'Host'."""
    listing = Listing(
        id="456",
        title="Cozy Apartment",
        host_name="",
        location="Paris, France",
    )

    message = generate_pitch_message(listing, CREATOR_PROFILE)
    assert "Host" in message


def test_generate_follow_up_message():
    """Test follow-up message generation."""
    listing = Listing(
        id="123",
        title="Beach Villa",
        host_name="Maria",
    )

    message = generate_follow_up_message(listing, CREATOR_PROFILE)
    assert "Maria" in message
    assert "Beach Villa" in message
    assert "Alex Creator" in message
    assert "follow up" in message.lower() or "followed up" in message.lower() or "follow" in message.lower()


def test_format_message_for_airbnb():
    """Test message formatting for Airbnb."""
    # Short message stays the same
    short = "Hello! This is a test message."
    assert format_message_for_airbnb(short) == short.strip()

    # Long message gets truncated
    long_msg = "A" * 3000
    formatted = format_message_for_airbnb(long_msg)
    assert len(formatted) <= 2000
    assert formatted.endswith("...")
