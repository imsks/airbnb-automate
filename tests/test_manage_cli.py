"""The control CLI wires roles through to the worker for the always-on split."""

from unittest.mock import patch

import manage


def test_worker_defaults_to_the_all_in_one_role():
    args = manage.build_parser().parse_args(["worker"])
    assert args.role == "all"


def test_worker_accepts_office_and_courier_roles():
    assert manage.build_parser().parse_args(["worker", "--role", "office"]).role == "office"
    assert manage.build_parser().parse_args(["worker", "--role", "courier"]).role == "courier"


def test_cmd_worker_passes_the_role_through():
    args = manage.build_parser().parse_args(["worker", "--role", "courier", "--once"])
    with patch("app.worker.main") as worker_main:
        manage.cmd_worker(args)
    _, kwargs = worker_main.call_args
    assert kwargs["role"] == "courier"
    assert kwargs["once"] is True


def test_reset_requires_explicit_confirmation():
    args = manage.build_parser().parse_args(["reset"])
    assert args.yes is False
    with patch("manage.reset_pipeline") as wipe:
        assert manage.cmd_reset(args) == 1
    wipe.assert_not_called()


def test_reset_with_yes_wipes_and_reseeds_the_office():
    args = manage.build_parser().parse_args(["reset", "--yes"])
    with patch("manage.reset_pipeline", return_value=["leads", "deals"]) as wipe, patch(
        "manage.planner.ensure_system_campaign", return_value=1
    ) as seed:
        assert manage.cmd_reset(args) == 0
    wipe.assert_called_once()
    seed.assert_called_once()
