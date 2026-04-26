"""Tests for the CLI module."""

import os
import sys
import tempfile

import pytest

# Ensure project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cli import build_parser, run_cycle, process_location, setup_logging


class TestBuildParser:
    """Tests for CLI argument parsing."""

    def test_locations_required(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_single_location(self):
        parser = build_parser()
        args = parser.parse_args(["--locations", "Goa, India"])
        assert args.locations == ["Goa, India"]

    def test_multiple_locations(self):
        parser = build_parser()
        args = parser.parse_args([
            "--locations", "Goa, India", "Bali, Indonesia", "Manali, India"
        ])
        assert args.locations == ["Goa, India", "Bali, Indonesia", "Manali, India"]

    def test_default_invites(self):
        parser = build_parser()
        args = parser.parse_args(["--locations", "Goa"])
        assert args.invites == 3

    def test_custom_invites(self):
        parser = build_parser()
        args = parser.parse_args(["--locations", "Goa", "--invites", "5"])
        assert args.invites == 5

    def test_schedule_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--locations", "Goa", "--schedule"])
        assert args.schedule is True

    def test_no_schedule_by_default(self):
        parser = build_parser()
        args = parser.parse_args(["--locations", "Goa"])
        assert args.schedule is False

    def test_interval_default(self):
        parser = build_parser()
        args = parser.parse_args(["--locations", "Goa"])
        assert args.interval == 4 * 60 * 60

    def test_custom_interval(self):
        parser = build_parser()
        args = parser.parse_args(["--locations", "Goa", "--interval", "3600"])
        assert args.interval == 3600

    def test_date_filters(self):
        parser = build_parser()
        args = parser.parse_args([
            "--locations", "Goa",
            "--checkin", "2026-07-01",
            "--checkout", "2026-07-07",
        ])
        assert args.checkin == "2026-07-01"
        assert args.checkout == "2026-07-07"

    def test_guest_filter(self):
        parser = build_parser()
        args = parser.parse_args(["--locations", "Goa", "--guests", "4"])
        assert args.guests == 4

    def test_default_guests(self):
        parser = build_parser()
        args = parser.parse_args(["--locations", "Goa"])
        assert args.guests == 2

    def test_price_filters(self):
        parser = build_parser()
        args = parser.parse_args([
            "--locations", "Goa",
            "--min-price", "25.5",
            "--max-price", "150",
        ])
        assert args.min_price == 25.5
        assert args.max_price == 150.0

    def test_dry_run_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--locations", "Goa", "--dry-run"])
        assert args.dry_run is True

    def test_no_headless_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--locations", "Goa", "--no-headless"])
        assert args.no_headless is True

    def test_verbose_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--locations", "Goa", "-v"])
        assert args.verbose is True

    def test_custom_message(self):
        parser = build_parser()
        args = parser.parse_args([
            "--locations", "Goa",
            "--message", "Hi {host_name}, love {place_name}!",
        ])
        assert args.message == "Hi {host_name}, love {place_name}!"

    def test_all_options_combined(self):
        parser = build_parser()
        args = parser.parse_args([
            "--locations", "Goa, India", "Bali, Indonesia",
            "--invites", "5",
            "--schedule",
            "--interval", "7200",
            "--checkin", "2026-08-01",
            "--checkout", "2026-08-07",
            "--guests", "3",
            "--min-price", "30",
            "--max-price", "200",
            "--dry-run",
            "--no-headless",
            "-v",
        ])
        assert args.locations == ["Goa, India", "Bali, Indonesia"]
        assert args.invites == 5
        assert args.schedule is True
        assert args.interval == 7200
        assert args.checkin == "2026-08-01"
        assert args.checkout == "2026-08-07"
        assert args.guests == 3
        assert args.min_price == 30.0
        assert args.max_price == 200.0
        assert args.dry_run is True
        assert args.no_headless is True
        assert args.verbose is True


class TestSetupLogging:
    """Tests for logging configuration."""

    def test_setup_logging_default(self):
        # Should not raise
        setup_logging(verbose=False)

    def test_setup_logging_verbose(self):
        # Should not raise
        setup_logging(verbose=True)
