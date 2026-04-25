"""Flask web app for Airbnb Automate."""

import logging
import os

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

from app.database import (
    init_db,
    create_search,
    get_search,
    get_searches,
    get_listings,
    save_listings,
    update_search_status,
)
from app.models import Search, SearchStatus
from app.scraper import scrape_listings_sync

logger = logging.getLogger(__name__)


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

    @app.route("/")
    def home():
        """Landing page with search form and past searches."""
        searches = get_searches()
        return render_template("home.html", searches=searches)

    @app.route("/search", methods=["POST"])
    def search():
        """Run a new Airbnb search."""
        location = request.form.get("location", "").strip()
        if not location:
            flash("Location is required.", "error")
            return redirect(url_for("home"))

        checkin = request.form.get("checkin", "")
        checkout = request.form.get("checkout", "")
        guests = int(request.form.get("guests", 2) or 2)
        min_price = request.form.get("min_price", "")
        max_price = request.form.get("max_price", "")

        search_record = Search(
            location=location,
            checkin=checkin,
            checkout=checkout,
            guests=guests,
            min_price=float(min_price) if min_price else None,
            max_price=float(max_price) if max_price else None,
        )
        search_id = create_search(search_record)

        # Run the scraper
        try:
            headless = os.getenv("HEADLESS", "true").lower() == "true"
            listings = scrape_listings_sync(
                location=location,
                checkin=checkin if checkin else None,
                checkout=checkout if checkout else None,
                guests=guests,
                min_price=float(min_price) if min_price else None,
                max_price=float(max_price) if max_price else None,
                headless=headless,
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
        """View search results."""
        search_record = get_search(search_id)
        if not search_record:
            flash("Search not found.", "error")
            return redirect(url_for("home"))

        listings = get_listings(search_id)
        return render_template(
            "results.html",
            search=search_record,
            listings=listings,
        )

    @app.route("/api/searches")
    def api_searches():
        """API endpoint listing all searches."""
        searches = get_searches()
        return jsonify([s.model_dump() for s in searches])

    return app
