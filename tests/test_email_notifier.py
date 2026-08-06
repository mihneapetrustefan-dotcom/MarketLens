"""
test_email_notifier.py
--------------------------
Unit tests for Email Notifier v1.

TESTING STRATEGY: _send_smtp() is mocked directly (exactly like every
other network call in this project), so tests are offline,
deterministic, and never touch a real SMTP server.
"""

import unittest
from unittest.mock import patch

from email_notifier import EmailNotifier, build_alert_email


def make_change(entity, change, previous="HOLD", current="BUY"):
    return {"entity": entity, "previous": previous, "current": current, "change": change}


def make_notifier():
    return EmailNotifier(
        smtp_host="smtp.example.com", smtp_port=587,
        username="bot@example.com", password="app-password",
        to_address="me@example.com",
    )


class TestIsConfigured(unittest.TestCase):
    def test_all_fields_present_is_configured(self):
        self.assertTrue(make_notifier().is_configured())

    def test_missing_host_is_not_configured(self):
        notifier = EmailNotifier(smtp_host=None, smtp_port=587, username="a", password="b", to_address="c")
        self.assertFalse(notifier.is_configured())

    def test_missing_password_is_not_configured(self):
        notifier = EmailNotifier(smtp_host="h", smtp_port=587, username="a", password=None, to_address="c")
        self.assertFalse(notifier.is_configured())

    def test_missing_to_address_is_not_configured(self):
        notifier = EmailNotifier(smtp_host="h", smtp_port=587, username="a", password="b", to_address=None)
        self.assertFalse(notifier.is_configured())


class TestSendMessage(unittest.TestCase):
    def setUp(self):
        self.notifier = make_notifier()

    def test_successful_send_returns_true(self):
        with patch.object(self.notifier, "_send_smtp", return_value=None):
            result = self.notifier.send_message("Subject", "Body")
        self.assertTrue(result)

    def test_not_configured_returns_false_without_network_call(self):
        notifier = EmailNotifier(smtp_host=None, smtp_port=None, username=None, password=None, to_address=None)
        with patch.object(notifier, "_send_smtp") as mock_send:
            result = notifier.send_message("Subject", "Body")
        self.assertFalse(result)
        mock_send.assert_not_called()

    def test_smtp_exception_returns_false_gracefully(self):
        with patch.object(self.notifier, "_send_smtp", side_effect=RuntimeError("smtp connection failed")):
            result = self.notifier.send_message("Subject", "Body")
        self.assertFalse(result)


class TestBuildAlertEmail(unittest.TestCase):
    def test_upgrade_appears_in_body(self):
        results = [make_change("Tesla", "upgrade", previous="HOLD", current="BUY")]
        subject, body = build_alert_email(results)
        self.assertIn("Tesla", body)
        self.assertIn("UPGRADE", body)
        self.assertIn("1 upgrade", subject)

    def test_downgrade_appears_in_body(self):
        results = [make_change("Bitcoin", "downgrade", previous="BUY", current="SELL")]
        subject, body = build_alert_email(results)
        self.assertIn("Bitcoin", body)
        self.assertIn("DOWNGRADE", body)
        self.assertIn("1 downgrade", subject)

    def test_no_changes_returns_none(self):
        results = [make_change("Tesla", "unchanged"), make_change("Apple", "new")]
        self.assertIsNone(build_alert_email(results))

    def test_empty_results_returns_none(self):
        self.assertIsNone(build_alert_email([]))

    def test_multiple_changes_all_included(self):
        results = [make_change("Tesla", "upgrade"), make_change("Bitcoin", "downgrade")]
        subject, body = build_alert_email(results)
        self.assertIn("Tesla", body)
        self.assertIn("Bitcoin", body)
        self.assertIn("1 upgrade(s), 1 downgrade(s)", subject)


if __name__ == "__main__":
    unittest.main(verbosity=2)
