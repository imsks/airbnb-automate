"""Campaign scheduler for Airbnb Automate."""

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import load_config, get_creator_profile
from app.database import (
    create_campaign,
    get_campaigns,
    save_listings,
    save_outreach,
)
from app.models import Campaign, CampaignStatus
from app.outreach import create_outreach_record
from app.scraper import scrape_listings_sync

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


def run_campaign(campaign: Campaign, creator_profile: dict) -> dict:
    """Execute a single campaign: search listings and generate outreach.

    Args:
        campaign: Campaign configuration to run
        creator_profile: Creator profile for message generation

    Returns:
        Dictionary with results summary
    """
    logger.info("Running campaign: %s (%s)", campaign.name, campaign.location)

    # Step 1: Scrape listings
    listings = scrape_listings_sync(
        location=campaign.location,
        checkin=campaign.checkin,
        checkout=campaign.checkout,
        guests=campaign.guests,
        min_price=campaign.min_price,
        max_price=campaign.max_price,
        max_listings=campaign.max_listings,
    )

    if not listings:
        logger.warning("No listings found for campaign: %s", campaign.name)
        return {"campaign": campaign.name, "listings_found": 0, "outreach_created": 0}

    # Step 2: Save listings to database
    campaign_id = campaign.id
    if campaign_id is None:
        campaign_id = create_campaign(campaign)

    saved = save_listings(listings, campaign_id)
    logger.info("Saved %d new listings for campaign %s", saved, campaign.name)

    # Step 3: Generate outreach for each listing
    outreach_count = 0
    for listing in listings:
        outreach = create_outreach_record(listing, campaign_id, creator_profile)
        save_outreach(outreach)
        outreach_count += 1

    logger.info(
        "Created %d outreach records for campaign %s",
        outreach_count,
        campaign.name,
    )

    return {
        "campaign": campaign.name,
        "listings_found": len(listings),
        "listings_saved": saved,
        "outreach_created": outreach_count,
    }


def run_all_active_campaigns() -> list[dict]:
    """Run all active campaigns from config."""
    config = load_config()
    creator = get_creator_profile(config)
    results = []

    campaigns = get_campaigns(status=CampaignStatus.ACTIVE)
    if not campaigns:
        logger.info("No active campaigns found in database. Loading from config...")
        for camp_cfg in config.get("campaigns", []):
            if not camp_cfg.get("enabled", True):
                continue

            campaign = Campaign(
                name=camp_cfg["name"],
                location=camp_cfg["location"],
                checkin=camp_cfg["checkin"],
                checkout=camp_cfg["checkout"],
                guests=camp_cfg.get("guests", 2),
                min_price=camp_cfg.get("min_price", 0),
                max_price=camp_cfg.get("max_price", 500),
                property_types=camp_cfg.get("property_types", []),
                amenities=camp_cfg.get("amenities", []),
                max_listings=camp_cfg.get("max_listings", 20),
            )
            campaign.id = create_campaign(campaign)
            campaigns.append(campaign)

    for campaign in campaigns:
        result = run_campaign(campaign, creator)
        results.append(result)

    return results


def _build_cron_trigger(schedule_config: dict) -> CronTrigger:
    """Build an APScheduler CronTrigger from schedule config.

    Args:
        schedule_config: Schedule configuration dict with frequency, day, time

    Returns:
        CronTrigger configured for the desired schedule
    """
    frequency = schedule_config.get("frequency", "weekly")
    time_str = schedule_config.get("time", "09:00")
    hour, minute = time_str.split(":")

    if frequency == "daily":
        return CronTrigger(hour=int(hour), minute=int(minute))
    elif frequency == "weekly":
        day = schedule_config.get("day", "monday")
        day_map = {
            "monday": "mon",
            "tuesday": "tue",
            "wednesday": "wed",
            "thursday": "thu",
            "friday": "fri",
            "saturday": "sat",
            "sunday": "sun",
        }
        return CronTrigger(
            day_of_week=day_map.get(day.lower(), "mon"),
            hour=int(hour),
            minute=int(minute),
        )
    elif frequency == "monthly":
        day_of_month = schedule_config.get("day_of_month", 1)
        return CronTrigger(
            day=day_of_month, hour=int(hour), minute=int(minute)
        )
    else:
        # Default to weekly on Monday
        return CronTrigger(day_of_week="mon", hour=int(hour), minute=int(minute))


def start_scheduler() -> BackgroundScheduler:
    """Start the campaign scheduler based on config."""
    config = load_config()
    schedule_config = config.get("schedule", {})

    trigger = _build_cron_trigger(schedule_config)

    scheduler.add_job(
        run_all_active_campaigns,
        trigger=trigger,
        id="campaign_runner",
        replace_existing=True,
        name="Run Active Campaigns",
    )

    scheduler.start()
    logger.info(
        "Scheduler started: %s at %s",
        schedule_config.get("frequency", "weekly"),
        schedule_config.get("time", "09:00"),
    )

    return scheduler


def stop_scheduler() -> None:
    """Stop the scheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
