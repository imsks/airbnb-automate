"""Tests for app.browser_session."""

from app.browser_session import _launch_basics, _viewport


def test_launch_basics_excludes_enable_automation_from_playwright() -> None:
    """``ignore`` is applied at launch_persistent_context / launch, not in this dict."""
    o = _launch_basics(headless=True)
    assert o["headless"] is True
    assert "ignore_default_args" not in o
    assert "args" in o


def test_viewport() -> None:
    v = _viewport()
    assert v["width"] == 1920
