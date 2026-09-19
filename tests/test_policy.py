"""Tests for guardrail policy, the kill switch, and agent run accounting."""

import os
import tempfile

import pytest

from app import policy
from app.agent import runs
from app.database import get_connection, init_db


@pytest.fixture
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    monkeypatch.setattr("app.policy.get_connection", lambda p=None: get_connection(path))
    monkeypatch.setattr(
        "app.agent.runs.get_connection", lambda p=None: get_connection(path)
    )
    yield path
    os.unlink(path)


# --- Policy ----------------------------------------------------------------


def test_empty_policy_falls_back_to_env_defaults(db):
    loaded = policy.load_policy()
    assert loaded.sending_enabled is True
    assert loaded.allow_specific_dates is False
    assert loaded.allow_off_platform_contact is False
    assert loaded.allowed_deliverables
    assert loaded.credential_facts["name"]


def test_policy_values_round_trip(db):
    policy.set_policy_value(policy.KEY_PRICE_CEILING, 2500.0)
    policy.set_policy_value(policy.KEY_MAX_AGENT_REPLIES, 2)
    policy.set_policy_value(policy.KEY_ALLOWED_DELIVERABLES, ["1 reel"])

    loaded = policy.load_policy()
    assert loaded.max_price_per_night == 2500.0
    assert loaded.max_agent_replies_per_thread == 2
    assert loaded.allowed_deliverables == ["1 reel"]


def test_setting_a_policy_key_twice_updates_it(db):
    policy.set_policy_value(policy.KEY_PRICE_CEILING, 1000.0)
    policy.set_policy_value(policy.KEY_PRICE_CEILING, 2000.0)
    assert policy.load_policy().max_price_per_night == 2000.0


# --- Kill switch -----------------------------------------------------------


def test_sending_is_enabled_by_default(db):
    assert policy.sending_enabled() is True


def test_freeze_stops_sending_immediately(db):
    policy.freeze_sending("account looks flagged")
    assert policy.sending_enabled() is False
    assert policy.load_policy().sending_enabled is False


def test_resume_re_enables_sending(db):
    policy.freeze_sending()
    policy.resume_sending()
    assert policy.sending_enabled() is True


def test_unparseable_kill_switch_value_fails_closed(db):
    """A corrupt policy row must stop sending, not permit it."""
    conn = get_connection(db)
    try:
        conn.execute(
            "INSERT INTO policy (key, value) VALUES (?, ?)",
            (policy.KEY_SENDING_ENABLED, "not-json"),
        )
        conn.commit()
    finally:
        conn.close()
    assert policy.sending_enabled() is False


# --- Agent runs ------------------------------------------------------------


class _FakeResponse:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage_metadata = usage


class _FakeLLM:
    model_name = "gpt-4o-mini"

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    def invoke(self, messages):
        if self._error:
            raise self._error
        return self._response


def test_tracked_invoke_records_tokens_and_cost(db):
    llm = _FakeLLM(
        _FakeResponse("Hi Asha!", {"input_tokens": 1000, "output_tokens": 500})
    )
    text, run_id = runs.tracked_invoke(
        llm, ["prompt"], agent="scribe", prompt_version="v1", deal_id=7
    )

    assert text == "Hi Asha!"
    assert run_id
    by_agent = {r["agent"]: r for r in runs.cost_by_agent()}
    assert by_agent["scribe"]["tokens"] == 1500
    assert by_agent["scribe"]["runs"] == 1
    assert runs.cost_for_deal(7) > 0


def test_tracked_invoke_records_failures_before_reraising(db):
    llm = _FakeLLM(error=RuntimeError("rate limited"))
    with pytest.raises(RuntimeError, match="rate limited"):
        runs.tracked_invoke(llm, ["prompt"], agent="closer")

    (row,) = runs.cost_by_agent()
    assert row["agent"] == "closer"
    assert row["failures"] == 1


def test_unknown_model_records_zero_cost_rather_than_guessing(db):
    assert runs.estimate_cost_usd("some-new-model", 1000, 1000) == 0.0
    assert runs.estimate_cost_usd("gpt-4o-mini", 1_000_000, 0) == pytest.approx(0.15)


def test_response_without_usage_metadata_still_records(db):
    llm = _FakeLLM(_FakeResponse("ok"))
    text, run_id = runs.tracked_invoke(llm, ["p"], agent="scout")
    assert text == "ok"
    assert runs.cost_by_agent()[0]["tokens"] == 0
