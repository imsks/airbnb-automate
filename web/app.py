"""Flask web app for Airbnb Automate."""

import logging
import os
import threading
from typing import Optional

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

from app.config import get_flex_trip_months_count, get_outreach_message_template
from app.locations_md import project_locations_md, read_locations_md
from app.database import (
    init_db,
    create_search,
    get_search,
    get_searches,
    get_listings,
    save_listings,
    update_search_status,
    create_outreach_messages,
    get_outreach_messages,
)
from app.models import Search, SearchStatus
from app.outreach import login_to_airbnb_sync, run_outreach_sync
from app.scraper import scrape_listings_sync

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Track running background processes
_outreach_threads: dict[int, threading.Thread] = {}
_login_thread: dict[str, threading.Thread] = {}
_login_result: dict[str, Optional[bool]] = {"status": None}


def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "static"),
    )
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")

    # Initialize database
    init_db()

    # --- Login Routes ---

    @app.route("/login/airbnb", methods=["POST"])
    def airbnb_login():
        """Open a browser for the user to log in to Airbnb."""
        # Check if already running
        if "login" in _login_thread and _login_thread["login"].is_alive():
            flash("Login browser is already open. Complete the login there.", "warning")
            return redirect(url_for("home"))

        _login_result["status"] = None

        def _run():
            try:
                result = login_to_airbnb_sync()
                _login_result["status"] = result
            except Exception as e:
                logger.error("Login failed: %s", e)
                _login_result["status"] = False

        thread = threading.Thread(target=_run, daemon=True)
        _login_thread["login"] = thread
        thread.start()

        flash(
            "🔐 Browser opened — log in to Airbnb there. "
            "The session will be saved automatically.",
            "success",
        )
        return redirect(url_for("home"))

    @app.route("/api/login/status")
    def api_login_status():
        """API endpoint for login status (used by polling UI)."""
        running = "login" in _login_thread and _login_thread["login"].is_alive()
        return jsonify({
            "running": running,
            "logged_in": _login_result.get("status"),
        })

    # --- Search Routes ---

    @app.route("/")
    def home():
        """Landing page with search form and past searches."""
        searches = get_searches()
        login_running = (
            "login" in _login_thread and _login_thread["login"].is_alive()
        )
        loc_md = project_locations_md(_PROJECT_ROOT)
        file_locations = read_locations_md(loc_md)
        return render_template(
            "home.html",
            searches=searches,
            login_running=login_running,
            login_status=_login_result.get("status"),
            file_locations=file_locations,
            flex_months_default=get_flex_trip_months_count(),
        )

    @app.route("/search", methods=["POST"])
    def search():
        """Run a new Airbnb search."""
        location = request.form.get("location", "").strip()
        if not location:
            flash("Location is required.", "error")
            return redirect(url_for("home"))

        date_mode = (request.form.get("date_mode") or "flexible").strip().lower()
        if date_mode not in ("flexible", "fixed"):
            date_mode = "flexible"

        checkin = request.form.get("checkin", "").strip()
        checkout = request.form.get("checkout", "").strip()
        flex_duration_raw = request.form.get("flex_duration", "1") or "1"
        flex_unit_raw = (request.form.get("flex_duration_unit") or "week").strip().lower()

        try:
            flex_duration = max(1, int(flex_duration_raw))
        except ValueError:
            flex_duration = 1
        if flex_unit_raw not in ("day", "week", "month", "weekend"):
            flex_unit_raw = "week"
        if flex_unit_raw == "weekend":
            flex_duration = 1

        guests = int(request.form.get("guests", 2) or 2)
        min_price = request.form.get("min_price", "")
        max_price = request.form.get("max_price", "")
        flex_months_raw = request.form.get("flex_trip_months", "").strip()
        try:
            flex_trip_months_count = (
                max(1, min(12, int(flex_months_raw)))
                if flex_months_raw
                else get_flex_trip_months_count()
            )
        except ValueError:
            flex_trip_months_count = get_flex_trip_months_count()

        if date_mode == "fixed":
            search_record = Search(
                location=location,
                checkin=checkin,
                checkout=checkout,
                guests=guests,
                min_price=float(min_price) if min_price else None,
                max_price=float(max_price) if max_price else None,
                date_mode="fixed",
                flex_duration=flex_duration,
                flex_duration_unit=flex_unit_raw,
            )
        else:
            search_record = Search(
                location=location,
                checkin="",
                checkout="",
                guests=guests,
                min_price=float(min_price) if min_price else None,
                max_price=float(max_price) if max_price else None,
                date_mode="flexible",
                flex_duration=flex_duration,
                flex_duration_unit=flex_unit_raw,
            )
        search_id = create_search(search_record)

        # Run the scraper
        try:
            headless = os.getenv("HEADLESS", "true").lower() == "true"
            listings = scrape_listings_sync(
                location=location,
                checkin=checkin if date_mode == "fixed" and checkin else None,
                checkout=checkout if date_mode == "fixed" and checkout else None,
                guests=guests,
                min_price=float(min_price) if min_price else None,
                max_price=float(max_price) if max_price else None,
                headless=headless,
                date_mode=date_mode,
                flex_duration=flex_duration,
                flex_duration_unit=flex_unit_raw,
                flex_trip_months_count=flex_trip_months_count,
            )

            saved = save_listings(listings, search_id)
            update_search_status(search_id, SearchStatus.COMPLETED, len(listings))
            flash(f"Found {len(listings)} listings for {location}!", "success")
        except Exception as e:
            logger.error("Search failed for %s: %s", location, e)
            update_search_status(search_id, SearchStatus.FAILED, 0)
            flash(f"Search failed: {e}", "error")

        return redirect(url_for("search_results", search_id=search_id))

    @app.route("/search/<int:search_id>")
    def search_results(search_id):
        """View search results with outreach controls."""
        search_record = get_search(search_id)
        if not search_record:
            flash("Search not found.", "error")
            return redirect(url_for("home"))

        listings = get_listings(search_id)
        outreach_messages = get_outreach_messages(search_id)
        message_template = get_outreach_message_template()

        # Build a map of listing_id → outreach status
        outreach_map = {m.listing_id: m for m in outreach_messages}

        # Check if outreach is running
        outreach_running = (
            search_id in _outreach_threads
            and _outreach_threads[search_id].is_alive()
        )

        login_running = (
            "login" in _login_thread and _login_thread["login"].is_alive()
        )

        return render_template(
            "results.html",
            search=search_record,
            listings=listings,
            outreach_map=outreach_map,
            outreach_messages=outreach_messages,
            outreach_running=outreach_running,
            message_template=message_template,
            login_running=login_running,
            login_status=_login_result.get("status"),
        )

    @app.route("/search/<int:search_id>/outreach", methods=["POST"])
    def start_outreach(search_id):
        """Start the outreach process for a search."""
        search_record = get_search(search_id)
        if not search_record:
            flash("Search not found.", "error")
            return redirect(url_for("home"))

        # Check if already running
        if search_id in _outreach_threads and _outreach_threads[search_id].is_alive():
            flash("Outreach is already running for this search.", "warning")
            return redirect(url_for("search_results", search_id=search_id))

        # Get custom message or use default
        custom_message = request.form.get("message_template", "").strip()
        message_template = custom_message if custom_message else None

        # Prepare outreach messages in the DB
        listings = get_listings(search_id)
        if not listings:
            flash("No listings to outreach to.", "error")
            return redirect(url_for("search_results", search_id=search_id))

        template = message_template or get_outreach_message_template()
        create_outreach_messages(search_id, listings, template)

        # Run outreach in a background thread
        def _run():
            try:
                run_outreach_sync(search_id, message_template)
            except Exception as e:
                logger.error("Outreach failed for search %d: %s", search_id, e)

        thread = threading.Thread(target=_run, daemon=True)
        _outreach_threads[search_id] = thread
        thread.start()

        flash("🚀 Outreach started! Messages will be sent using your saved Airbnb session.", "success")
        return redirect(url_for("outreach_status", search_id=search_id))

    @app.route("/search/<int:search_id>/outreach/status")
    def outreach_status(search_id):
        """View outreach progress for a search."""
        search_record = get_search(search_id)
        if not search_record:
            flash("Search not found.", "error")
            return redirect(url_for("home"))

        outreach_messages = get_outreach_messages(search_id)
        outreach_running = (
            search_id in _outreach_threads
            and _outreach_threads[search_id].is_alive()
        )

        return render_template(
            "outreach.html",
            search=search_record,
            messages=outreach_messages,
            outreach_running=outreach_running,
        )

    @app.route("/api/searches")
    def api_searches():
        """API endpoint listing all searches."""
        searches = get_searches()
        return jsonify([s.model_dump() for s in searches])

    @app.route("/api/search/<int:search_id>/outreach")
    def api_outreach_status(search_id):
        """API endpoint for outreach status (used by polling UI)."""
        messages = get_outreach_messages(search_id)
        outreach_running = (
            search_id in _outreach_threads
            and _outreach_threads[search_id].is_alive()
        )
        return jsonify({
            "running": outreach_running,
            "messages": [m.model_dump() for m in messages],
            "summary": {
                "total": len(messages),
                "sent": sum(1 for m in messages if m.status.value == "sent"),
                "failed": sum(1 for m in messages if m.status.value == "failed"),
                "pending": sum(1 for m in messages if m.status.value in ("pending", "sending")),
                "skipped": sum(1 for m in messages if m.status.value == "skipped"),
            },
        })

    return app
