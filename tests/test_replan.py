"""The autonomous, self-healing side: propose places, retire dead ones, no form.

These cover the behaviours that let the office keep hunting deals without a human
pasting destinations or babysitting Warden blocks.
"""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import campaigns as campaign_repo
from app import jobs, leads as lead_repo, territories as territory_repo
from app.agent import planner, proposer
from app.database import get_connection, init_db
from app.jobs import JobType
from app.models import Campaign, CampaignStatus, TerritoryStatus
from app.worker import Worker

_REPOS = (
    "app.jobs",
    "app.deals",
    "app.leads",
    "app.territories",
    "app.campaigns",
    "app.policy",
    "app.agent.runs",
    "app.worker",
)


@pytest.fixture
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    for module in _REPOS:
        monkeypatch.setattr(
            f"{module}.get_connection", lambda p=None: get_connection(path), raising=False
        )
    yield path
    os.unlink(path)


def _mock_llm(text):
    response = MagicMock()
    response.content = text
    response.usage_metadata = {"input_tokens": 10, "output_tokens": 20}
    llm = MagicMock()
    llm.invoke.return_value = response
    llm.model_name = "gpt-4o-mini"
    return llm


# --- Proposer --------------------------------------------------------------


def test_parse_proposal_dedupes_and_trims():
    raw = '{"places": ["Ziro, Arunachal Pradesh", " Ziro, Arunachal Pradesh ", "Gokarna"]}'
    assert proposer.parse_proposal(raw) == ["Ziro, Arunachal Pradesh", "Gokarna"]


def test_proposer_adds_new_places_when_the_shortlist_is_thin(db):
    reply = '{"places": ["Gokarna, Karnataka", "Ziro, Arunachal Pradesh"]}'
    with patch("app.agent.proposer.get_llm", return_value=_mock_llm(reply)):
        result = proposer.propose_territories(min_live=12, db_path=db)

    assert result["proposed"] == 2
    names = territory_repo.all_territory_names(db)
    assert "Gokarna, Karnataka" in names
    assert "Ziro, Arunachal Pradesh" in names


def test_proposer_never_repeats_a_known_or_visited_place(db):
    territory_repo.upsert_territory("Gokarna, Karnataka", db_path=db)
    territory_repo.mark_visited("Maldives", db_path=db)

    reply = '{"places": ["Gokarna, Karnataka", "Maldives", "Bir, Himachal Pradesh"]}'
    with patch("app.agent.proposer.get_llm", return_value=_mock_llm(reply)):
        result = proposer.propose_territories(min_live=12, db_path=db)

    # Only the genuinely new place is added.
    assert result["places"] == ["Bir, Himachal Pradesh"]


def test_proposer_is_a_noop_when_there_is_enough_to_work_on(db):
    for i in range(3):
        territory_repo.upsert_territory(f"Place {i}", db_path=db)

    llm = MagicMock()
    with patch("app.agent.proposer.get_llm", return_value=llm):
        result = proposer.propose_territories(min_live=2, db_path=db)

    assert result["proposed"] == 0
    llm.invoke.assert_not_called()  # no place is worth an LLM call


# --- The standing office (no form) -----------------------------------------


def test_ensure_system_campaign_is_idempotent_and_seeds_visited(db):
    first = planner.ensure_system_campaign(db)
    second = planner.ensure_system_campaign(db)
    assert first == second

    active = [
        c for c in campaign_repo.list_campaigns(db) if c.status is CampaignStatus.ACTIVE
    ]
    assert len(active) == 1

    maldives = territory_repo.get_territory_by_name("Maldives", db)
    assert maldives.status is TerritoryStatus.VISITED


def test_only_the_standing_office_proposes_places(db):
    # A human-made campaign does not trigger proposing.
    campaign_repo.create_campaign(
        Campaign(name="Manual", status=CampaignStatus.ACTIVE), db
    )
    assert planner.plan_tick(db_path=db)["queued"]["propose"] == 0

    # The standing office does.
    planner.ensure_system_campaign(db)
    assert planner.plan_tick(db_path=db)["queued"]["propose"] == 1
    # ...but never more than one proposal in flight at a time.
    assert planner.plan_tick(db_path=db)["queued"]["propose"] == 0


# --- Self-healing: retire places that yield nothing ------------------------


def test_discovery_of_an_empty_place_retires_it(db):
    territory_id = territory_repo.upsert_territory("Nowhere, India", db_path=db)
    worker = Worker(db_path=db)
    job = MagicMock()
    job.payload = {"campaign_id": 0, "territory_id": territory_id}

    import asyncio

    with patch("app.scraper.scrape_listings", AsyncMock(return_value=[])):
        result = asyncio.run(worker._discover_leads(job))

    assert result["leads"] == 0
    assert territory_repo.get_territory(territory_id, db).status is TerritoryStatus.EXHAUSTED


def test_visited_and_exhausted_places_are_not_researched(db):
    territory_repo.mark_visited("Malaysia", db_path=db)
    good = territory_repo.upsert_territory("Coorg, Karnataka", db_path=db)
    exhausted = territory_repo.upsert_territory("Dud, India", db_path=db)
    territory_repo.set_status(exhausted, TerritoryStatus.EXHAUSTED, db_path=db)

    needing = {t.name for t in territory_repo.territories_needing_research(db_path=db)}
    assert "Coorg, Karnataka" in needing
    assert "Malaysia" not in needing
    assert "Dud, India" not in needing
