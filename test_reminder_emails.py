"""
test_reminder_emails.py
────────────────────────
Sends both reminder emails directly to a test address.
No Firestore user queries — bypasses all filters.

Run:
    python test_reminder_emails.py
    python test_reminder_emails.py --to you@example.com --name Priya
"""

import argparse, os, sys, requests
from dotenv import load_dotenv

load_dotenv()

TEST_EMAIL = "garvitdayal17@gmail.com"
TEST_NAME  = "Garvit"

from send_submission_reminder import build_html as build_submission_html
from send_vote_reminder import build_html as build_vote_html, get_showcase

SMTP2GO_API_KEY = os.getenv("SMTP2GO_API_KEY")
SENDER_EMAIL    = os.getenv("SENDER_EMAIL", "lenscape@jlug.club")
SMTP2GO_API_URL = "https://api.smtp2go.com/v3/email/send"


def _send(to: str, subject: str, html: str, label: str) -> bool:
    payload = {
        "api_key": SMTP2GO_API_KEY,
        "to": [to],
        "sender": f"Lenscape <{SENDER_EMAIL}>",
        "subject": subject,
        "html_body": html,
    }
    try:
        r = requests.post(SMTP2GO_API_URL, json=payload, timeout=15)
        r.raise_for_status()
        res = r.json()
        if res.get("request_id") or res.get("data", {}).get("succeeded"):
            print(f"  ✓ [{label}] sent to {to}")
            return True
        print(f"  ✗ [{label}] SMTP2GO rejected: {res}")
        return False
    except Exception as e:
        print(f"  ✗ [{label}] {e}")
        return False


def run(to: str, name: str, max_cards: int):
    if not SMTP2GO_API_KEY:
        print("ERROR: SMTP2GO_API_KEY not set."); sys.exit(1)

    print(f"\nTest email target : {to} ({name})\n")

    # 1 — Submission reminder
    print("Sending submission reminder…")
    _send(
        to=to,
        subject="[TEST] Your artwork is missing from the Lenscape gallery",
        html=build_submission_html(name),
        label="submission-reminder",
    )

    # 2 — Vote reminder (fetch real artwork cards)
    print("\nFetching artworks for vote reminder…")
    showcase = get_showcase(max_cards)
    print(f"  {len(showcase)} card(s) selected")
    _send(
        to=to,
        subject="[TEST] Your vote is missing from the Lenscape gallery",
        html=build_vote_html(name, showcase),
        label="vote-reminder",
    )

    print("\nDone.\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--to",        default=TEST_EMAIL)
    p.add_argument("--name",      default=TEST_NAME)
    p.add_argument("--max-cards", type=int, default=3)
    a = p.parse_args()
    run(to=a.to, name=a.name, max_cards=a.max_cards)
