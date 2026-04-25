"""Flask web dashboard for Airbnb Automate."""

import os
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

from app.config import load_config, get_creator_profile
from app.database import (
    init_db,
    create_campaign,
    get_campaigns,
    get_campaign,
    get_listings,
    get_outreach_for_campaign,
    get_outreach_stats,
    update_campaign_status,
    update_outreach_status,
)
from app.models import Campaign, CampaignStatus, OutreachStatus
from app.scheduler import run_campaign, start_scheduler, stop_scheduler


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
    def dashboard():
        """Main dashboard showing overview stats and campaigns."""
        campaigns = get_campaigns()
        stats = get_outreach_stats()
        return render_template(
            "dashboard.html",
            campaigns=campaigns,
            stats=stats,
            now=datetime.utcnow(),
        )

    @app.route("/campaigns")
    def campaigns_list():
        """List all campaigns."""
        status_filter = request.args.get("status")
        if status_filter and status_filter != "all":
            campaigns = get_campaigns(status=CampaignStatus(status_filter))
        else:
            campaigns = get_campaigns()
        return render_template("campaigns.html", campaigns=campaigns)

    @app.route("/campaigns/new", methods=["GET", "POST"])
    def new_campaign():
        """Create a new campaign."""
        if request.method == "POST":
            campaign = Campaign(
                name=request.form["name"],
                location=request.form["location"],
                checkin=request.form["checkin"],
                checkout=request.form["checkout"],
                guests=int(request.form.get("guests", 2)),
                min_price=float(request.form.get("min_price", 0)),
                max_price=float(request.form.get("max_price", 500)),
                max_listings=int(request.form.get("max_listings", 20)),
            )
            campaign_id = create_campaign(campaign)
            flash(f"Campaign '{campaign.name}' created!", "success")
            return redirect(url_for("campaign_detail", campaign_id=campaign_id))

        return render_template("new_campaign.html")

    @app.route("/campaigns/<int:campaign_id>")
    def campaign_detail(campaign_id):
        """View campaign details with listings and outreach."""
        campaign = get_campaign(campaign_id)
        if not campaign:
            flash("Campaign not found", "error")
            return redirect(url_for("campaigns_list"))

        listings = get_listings(campaign_id)
        outreach = get_outreach_for_campaign(campaign_id)

        return render_template(
            "campaign_detail.html",
            campaign=campaign,
            listings=listings,
            outreach=outreach,
        )

    @app.route("/campaigns/<int:campaign_id>/run", methods=["POST"])
    def run_campaign_route(campaign_id):
        """Trigger a campaign run manually."""
        campaign = get_campaign(campaign_id)
        if not campaign:
            flash("Campaign not found", "error")
            return redirect(url_for("campaigns_list"))

        config = load_config()
        creator = get_creator_profile(config)
        result = run_campaign(campaign, creator)

        flash(
            f"Campaign completed! Found {result['listings_found']} listings, "
            f"created {result['outreach_created']} outreach messages.",
            "success",
        )
        return redirect(url_for("campaign_detail", campaign_id=campaign_id))

    @app.route("/campaigns/<int:campaign_id>/status", methods=["POST"])
    def update_status(campaign_id):
        """Update campaign status."""
        new_status = CampaignStatus(request.form["status"])
        update_campaign_status(campaign_id, new_status)
        flash(f"Campaign status updated to {new_status.value}", "success")
        return redirect(url_for("campaign_detail", campaign_id=campaign_id))

    @app.route("/outreach/<int:outreach_id>/status", methods=["POST"])
    def update_outreach(outreach_id):
        """Update outreach status."""
        new_status = OutreachStatus(request.form["status"])
        update_outreach_status(outreach_id, new_status)
        campaign_id = request.form.get("campaign_id", 1)
        return redirect(url_for("campaign_detail", campaign_id=campaign_id))

    @app.route("/api/stats")
    def api_stats():
        """API endpoint for outreach statistics."""
        return jsonify(get_outreach_stats())

    @app.route("/api/campaigns")
    def api_campaigns():
        """API endpoint listing all campaigns."""
        campaigns = get_campaigns()
        return jsonify([c.model_dump() for c in campaigns])

    return app
