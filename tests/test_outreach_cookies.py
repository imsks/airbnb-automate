"""Tests for Airbnb session heuristics in app.outreach."""

import pytest

from app.outreach import _is_target_disconnected_error, cookies_indicate_airbnb_session


@pytest.mark.parametrize(
    "cookies, expected",
    [
        (
            [
                {
                    "name": "bev",
                    "domain": ".airbnb.com",
                },
            ],
            False,
        ),
        (
            [
                {
                    "name": "_unknown_session_xyz",
                    "domain": "www.airbnb.com",
                },
            ],
            True,
        ),
        (
            [
                {
                    "name": "_aat",
                    "domain": "www.airbnb.com",
                },
            ],
            True,
        ),
        (
            [
                {
                    "name": "aaj",
                    "domain": "www.airbnb.com",
                },
            ],
            True,
        ),
        ([], False),
    ],
)
def test_cookies_indicate_airbnb_session(cookies, expected):
    assert cookies_indicate_airbnb_session(cookies) is expected


@pytest.mark.parametrize(
    "message, is_closed",
    [
        ("Page.wait_for_timeout: Target page, context or browser has been closed", True),
        ("Something else", False),
        ("Browser has been closed", True),
    ],
)
def test_is_target_disconnected_error(message, is_closed):
    assert _is_target_disconnected_error(RuntimeError(message)) is is_closed
