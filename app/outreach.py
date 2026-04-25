"""Outreach message generator and sender for Airbnb Automate."""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from app.models import Listing, Outreach, OutreachStatus

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _get_template_env() -> Environment:
    """Get Jinja2 template environment."""
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=False,
    )


def generate_pitch_message(listing: Listing, creator_profile: dict) -> str:
    """Generate a personalized pitch message for a host.

    Uses the pitch_message.txt template with listing and creator details
    to create a compelling outreach message.

    Args:
        listing: The Airbnb listing to pitch for
        creator_profile: Creator's profile information

    Returns:
        Rendered pitch message string
    """
    env = _get_template_env()
    template = env.get_template("pitch_message.txt")

    return template.render(
        host_name=listing.host_name or "Host",
        listing_title=listing.title,
        listing_url=listing.url,
        location=listing.location,
        creator_name=creator_profile.get("name", "Content Creator"),
        platform=creator_profile.get("platform", "Instagram"),
        handle=creator_profile.get("handle", "@creator"),
        followers=creator_profile.get("followers", "10,000+"),
        email=creator_profile.get("email", ""),
        portfolio_url=creator_profile.get("portfolio_url", ""),
        content_types=creator_profile.get("content_types", []),
        rating=listing.rating,
        superhost=listing.superhost,
    )


def generate_follow_up_message(listing: Listing, creator_profile: dict) -> str:
    """Generate a follow-up message for non-responsive hosts.

    Args:
        listing: The Airbnb listing
        creator_profile: Creator's profile information

    Returns:
        Rendered follow-up message string
    """
    env = _get_template_env()
    template = env.get_template("follow_up_message.txt")

    return template.render(
        host_name=listing.host_name or "Host",
        listing_title=listing.title,
        creator_name=creator_profile.get("name", "Content Creator"),
        platform=creator_profile.get("platform", "Instagram"),
        handle=creator_profile.get("handle", "@creator"),
    )


def create_outreach_record(
    listing: Listing,
    campaign_id: int,
    creator_profile: dict,
) -> Outreach:
    """Create an outreach record with a generated pitch message.

    Args:
        listing: The target listing
        campaign_id: ID of the campaign this outreach belongs to
        creator_profile: Creator's profile for message generation

    Returns:
        Outreach object ready to be saved and sent
    """
    message = generate_pitch_message(listing, creator_profile)

    return Outreach(
        campaign_id=campaign_id,
        listing_id=listing.id,
        host_name=listing.host_name,
        listing_title=listing.title,
        listing_url=listing.url,
        message=message,
        status=OutreachStatus.PENDING,
    )


def send_email(
    to_email: str,
    subject: str,
    body: str,
) -> bool:
    """Send an outreach email via SMTP.

    Uses SMTP settings from environment variables.

    Args:
        to_email: Recipient email address
        subject: Email subject line
        body: Email body text

    Returns:
        True if sent successfully, False otherwise
    """
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")

    if not smtp_user or not smtp_password:
        logger.warning("SMTP credentials not configured. Email not sent.")
        return False

    try:
        msg = MIMEMultipart()
        msg["From"] = smtp_user
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, to_email, msg.as_string())

        logger.info("Email sent to %s", to_email)
        return True
    except Exception as e:
        logger.error("Failed to send email to %s: %s", to_email, e)
        return False


def format_message_for_airbnb(message: str) -> str:
    """Format a message suitable for pasting into Airbnb's messaging system.

    Cleans up the message and ensures it's within Airbnb's character limits.

    Args:
        message: Raw message text

    Returns:
        Formatted message string
    """
    # Airbnb messages have a ~500 char soft limit for first message
    # Keep it concise but impactful
    lines = [line.strip() for line in message.strip().split("\n")]
    cleaned = "\n".join(lines)

    if len(cleaned) > 2000:
        cleaned = cleaned[:1997] + "..."

    return cleaned
