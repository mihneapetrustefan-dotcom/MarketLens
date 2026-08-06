"""
email_notifier.py
---------------------
Email Notifier module for MarketLens.

RESPONSIBILITY:
Send real-time alerts via SMTP email when something worth knowing
about NOW happens (an upgrade or downgrade) — the email equivalent of
telegram_notifier.py, for anyone who prefers email over Telegram.

WHY SMTP (not a paid email API): Python's built-in `smtplib` needs no
extra dependency and works with any SMTP provider (Gmail, Outlook,
etc.) — free. Most providers require an "app password" rather than
your normal account password for automated SMTP login, since plain
password login is usually blocked for security.

Configuration is read from environment variables (GitHub Actions
secrets in the automated deployment) — NEVER hardcoded:
  SMTP_HOST       e.g. "smtp.gmail.com"
  SMTP_PORT       e.g. "587"
  SMTP_USERNAME   the sending email address
  SMTP_PASSWORD   an app password (NOT the normal account password)
  ALERT_EMAIL_TO  address to send alerts to (can equal SMTP_USERNAME,
                  to send alerts to yourself)
"""

import os
import smtplib
import logging
from email.mime.text import MIMEText
from typing import Optional, Tuple, List, Dict, Any

logger = logging.getLogger("marketlens.email_notifier")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class EmailNotifier:
    """
    Sends alert emails via SMTP.
    """

    def __init__(
        self,
        smtp_host: Optional[str] = None,
        smtp_port: Optional[int] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        to_address: Optional[str] = None,
    ):
        """
        Any argument left as None falls back to the matching
        environment variable — never hardcode real credentials in
        source code.
        """
        self.smtp_host = smtp_host if smtp_host is not None else os.environ.get("SMTP_HOST")

        if smtp_port is not None:
            self.smtp_port = smtp_port
        else:
            env_port = os.environ.get("SMTP_PORT")
            self.smtp_port = int(env_port) if env_port else None

        self.username = username if username is not None else os.environ.get("SMTP_USERNAME")
        self.password = password if password is not None else os.environ.get("SMTP_PASSWORD")
        self.to_address = to_address if to_address is not None else os.environ.get("ALERT_EMAIL_TO")

    def is_configured(self) -> bool:
        """Whether every setting needed to send an email is present."""
        return bool(self.smtp_host and self.smtp_port and self.username and self.password and self.to_address)

    def _send_smtp(self, message: MIMEText) -> None:
        """
        Connect, authenticate, and send one message over SMTP with
        STARTTLS. Isolated as its own method — same pattern as every
        other network call in this project — so unit tests can mock it
        with no real SMTP connection.
        """
        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as server:
            server.starttls()
            server.login(self.username, self.password)
            server.sendmail(self.username, [self.to_address], message.as_string())

    def send_message(self, subject: str, body: str) -> bool:
        """
        Send one alert email.

        Returns:
            True if the email was sent successfully, False on any
            failure (missing configuration, SMTP/network error) —
            NEVER raises.
        """
        if not self.is_configured():
            logger.warning("Email notifier not configured (missing SMTP settings) — skipping send")
            return False

        message = MIMEText(body, "plain", "utf-8")
        message["Subject"] = subject
        message["From"] = self.username
        message["To"] = self.to_address

        try:
            self._send_smtp(message)
        except Exception as exc:  # noqa: BLE001 — a notification failure must never break the pipeline
            logger.error("Failed to send alert email: %s", exc)
            return False

        logger.info("Alert email sent successfully")
        return True


def build_alert_email(upgrade_downgrade_results: List[Dict[str, Any]]) -> Optional[Tuple[str, str]]:
    """
    Build a (subject, body) pair for an alert email from Upgrade/
    Downgrade Tracker results.

    Args:
        upgrade_downgrade_results: output of
            UpgradeDowngradeTracker.compare_batch().

    Returns:
        (subject, body), or None if there's nothing worth alerting
        about (no upgrades or downgrades this run) — the caller should
        skip sending in that case rather than emailing "nothing
        changed" every single day.
    """
    upgrades = [r for r in upgrade_downgrade_results if r["change"] == "upgrade"]
    downgrades = [r for r in upgrade_downgrade_results if r["change"] == "downgrade"]

    if not upgrades and not downgrades:
        return None

    subject = f"MarketLens: {len(upgrades)} upgrade(s), {len(downgrades)} downgrade(s)"

    lines = ["Schimbari noi in recomandarile MarketLens:", ""]
    for r in upgrades:
        lines.append(f"UPGRADE: {r['entity']}: {r['previous']} -> {r['current']}")
    for r in downgrades:
        lines.append(f"DOWNGRADE: {r['entity']}: {r['previous']} -> {r['current']}")
    body = "\n".join(lines)

    return subject, body
