"""Tests for global outreach send sliding-window quota (DB layer)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import (
    init_db,
    outreach_send_log_count_in_window,
    outreach_send_log_oldest_in_window,
    outreach_send_log_record,
)


@pytest.fixture
def db_path():
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    os.unlink(path)


def test_outreach_send_log_sliding_window(db_path):
    now = 1_700_000_000.0
    outreach_send_log_record(db_path, sent_at=now - 100)
    outreach_send_log_record(db_path, sent_at=now - 50)
    assert outreach_send_log_count_in_window(db_path, 3600, now=now) == 2
    assert outreach_send_log_count_in_window(db_path, 60, now=now) == 1


def test_outreach_send_log_oldest_in_window(db_path):
    now = 100_000.0
    outreach_send_log_record(db_path, sent_at=now - 100)
    outreach_send_log_record(db_path, sent_at=now - 50)
    oldest = outreach_send_log_oldest_in_window(db_path, 200, now=now)
    assert oldest == pytest.approx(now - 100)
