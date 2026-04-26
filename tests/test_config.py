"""Tests for app.config helpers."""

from pathlib import Path

import pytest

from app import config
from app.config import (
    get_browser_user_agent,
    get_browser_user_data_dir,
    get_playwright_channel,
)


def test_get_playwright_channel_default(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_CHANNEL", raising=False)
    assert get_playwright_channel() is None


@pytest.mark.parametrize(
    "value, expected",
    [
        ("", None),
        ("  ", None),
        ("CHROMIUM", None),
        ("chromium", None),
        ("chrome", "chrome"),
        ("Chrome", "chrome"),
        ("msedge", "msedge"),
    ],
)
def test_get_playwright_channel_env(monkeypatch, value, expected):
    if value == "" and expected is None:
        monkeypatch.delenv("PLAYWRIGHT_CHANNEL", raising=False)
    else:
        monkeypatch.setenv("PLAYWRIGHT_CHANNEL", value)
    assert get_playwright_channel() == expected


def test_get_browser_user_agent_unset(monkeypatch):
    monkeypatch.delenv("BROWSER_USER_AGENT", raising=False)
    assert get_browser_user_agent() is None


def test_get_browser_user_agent_set(monkeypatch):
    monkeypatch.setenv("BROWSER_USER_AGENT", "Mozilla/5.0 (Custom)")
    assert get_browser_user_agent() == "Mozilla/5.0 (Custom)"


def test_get_browser_user_data_dir_default(monkeypatch):
    monkeypatch.delenv("BROWSER_USER_DATA_DIR", raising=False)
    result = get_browser_user_data_dir()
    # Now defaults to data/airbnb_browser_profile under BASE_DIR
    assert result != ""
    assert result.endswith("airbnb_browser_profile")
    assert Path(result).is_absolute()


def test_get_browser_user_data_dir_disabled(monkeypatch):
    monkeypatch.setenv("BROWSER_USER_DATA_DIR", "none")
    assert get_browser_user_data_dir() == ""


def test_get_browser_user_data_dir_relative(monkeypatch):
    monkeypatch.setenv("BROWSER_USER_DATA_DIR", "data/persist_test")
    result = get_browser_user_data_dir()
    assert result is not None
    assert result.endswith("persist_test")
    assert Path(result).is_absolute()
    assert str(result).startswith(str(config.BASE_DIR))


def test_get_browser_user_data_dir_absolute(monkeypatch, tmp_path):
    p = tmp_path / "ud"
    monkeypatch.setenv("BROWSER_USER_DATA_DIR", str(p))
    assert get_browser_user_data_dir() == str(p.resolve())
