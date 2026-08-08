#!/usr/bin/env python3
"""
test_email_setup.py
-----------------------
Standalone, one-off script to test the Email Notifier configuration in
isolation — sends a single test email using the SAME SMTP_* secrets
already configured for the daily pipeline, without touching any real
pipeline data (no RSS collection, no database, no Dashboard).

USAGE (via GitHub Actions): triggered manually through the
"Test Email Setup" workflow (workflow_dispatch) — see
.github/workflows/test_email.yml.

USAGE (locally, if you have Python installed):
    SMTP_HOST=... SMTP_PORT=... SMTP_USERNAME=... SMTP_PASSWORD=... ALERT_EMAIL_TO=... python3 test_email_setup.py
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from email_notifier import EmailNotifier


def main() -> int:
    notifier = EmailNotifier()

    if not notifier.is_configured():
        print("NOT CONFIGURED — one or more of SMTP_HOST, SMTP_PORT, SMTP_USERNAME, "
              "SMTP_PASSWORD, ALERT_EMAIL_TO is missing. Check the repository secrets.")
        return 1

    print(f"Sending a test email via {notifier.smtp_host}:{notifier.smtp_port} "
          f"from {notifier.username} to {notifier.to_address} ...")

    sent = notifier.send_message(
        subject="MarketLens — email de test",
        body="Dacă citești asta, configurarea SMTP funcționează corect. "
             "Acesta e doar un test — nu e o alertă reală de recomandare.",
    )

    if sent:
        print("SUCCESS — email trimis. Verifică inbox-ul (și Spam/Junk).")
        return 0
    else:
        print("FAILED — vezi mesajul de eroare de mai sus pentru detalii exacte.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
