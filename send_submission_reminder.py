"""
send_submission_reminder.py
────────────────────────────
Sends submission reminder emails to users who have NOT uploaded any artwork.

Run:
    python send_submission_reminder.py
    python send_submission_reminder.py --dry-run
    python send_submission_reminder.py --limit 10
"""

import argparse, os, sys, time, requests
from datetime import datetime
from dotenv import load_dotenv
from database import users_col, artworks_col

# SMTP2GO free tier: 1 000 emails/month, burst ~10/s.
# Send at max 2 emails/second to stay well inside limits.
SEND_DELAY_SECONDS = 0.5

load_dotenv()

SMTP2GO_API_KEY = os.getenv("SMTP2GO_API_KEY")
SENDER_EMAIL    = os.getenv("SENDER_EMAIL", "lenscape@jlug.club")
SMTP2GO_API_URL = "https://api.smtp2go.com/v3/email/send"
SUBMIT_URL      = "https://lenscape.jlug.club/submit"
GALLERY_URL     = "https://lenscape.jlug.club/gallery"

CATEGORIES = [
    ("Photography",    "01"),
    ("Digital Art",    "02"),
    ("Cinematography", "03"),
    ("Motion Graphics","04"),
]


def build_html(user_name: str) -> str:
    first = user_name.split()[0] if user_name else "Artist"

    cat_chips = "".join(
        f"""<tr>
              <td style="padding:5px 0;">
                <table cellpadding="0" cellspacing="0">
                  <tr>
                    <td style="padding:1px 8px;border:1px solid rgba(201,168,76,0.25);
                               font-family:monospace;font-size:8px;color:#555;
                               letter-spacing:0.2em;text-transform:uppercase;
                               white-space:nowrap;">Room {room}</td>
                    <td style="padding:1px 0 1px 10px;font-family:monospace;font-size:10px;
                               color:#C9A84C;letter-spacing:0.1em;text-transform:uppercase;
                               white-space:nowrap;">{name}</td>
                  </tr>
                </table>
              </td>
            </tr>"""
        for name, room in CATEGORIES
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <meta name="x-apple-disable-message-reformatting"/>
  <title>Lenscape — Submit Your Artwork</title>
</head>
<body style="margin:0;padding:0;background:#0c0c0c;-webkit-text-size-adjust:100%;mso-line-height-rule:exactly;">
<table width="100%" cellpadding="0" cellspacing="0" role="presentation"
       style="background:#0c0c0c;padding:32px 12px;">
  <tr><td align="center">
    <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
           style="max-width:520px;background:#0c0c0c;border:1px solid rgba(201,168,76,0.3);">

      <!-- top bar -->
      <tr>
        <td style="padding:16px 28px;border-bottom:1px solid rgba(201,168,76,0.15);">
          <p style="margin:0 0 2px;font-family:monospace;font-size:8px;color:#C9A84C;
                    letter-spacing:0.35em;text-transform:uppercase;white-space:nowrap;">Lenscape</p>
          <p style="margin:0;font-family:monospace;font-size:8px;color:#555;
                    letter-spacing:0.25em;text-transform:uppercase;white-space:nowrap;">Digital Exhibition</p>
        </td>
      </tr>

      <!-- headline -->
      <tr>
        <td style="padding:36px 28px 20px;">
          <h1 style="margin:0 0 6px;font-family:Georgia,serif;font-size:28px;
                     font-weight:300;color:#e8dcc8;line-height:1.25;">
            The gallery is missing<br/>your work, {first}.
          </h1>
          <p style="margin:0 0 0;font-family:monospace;font-size:12px;color:#888;line-height:1.8;">
            You registered on Lenscape but haven&apos;t submitted anything yet.<br/>
            Submissions are reviewed and, once approved, displayed to the entire community.
          </p>
        </td>
      </tr>

      <!-- open categories -->
      <tr>
        <td style="padding:24px 28px 28px;">
          <p style="margin:0 0 12px;font-family:monospace;font-size:8px;
                    color:#C9A84C;letter-spacing:0.3em;text-transform:uppercase;">
            Open Categories
          </p>
          <table cellpadding="0" cellspacing="0">{cat_chips}</table>
        </td>
      </tr>

      <!-- CTA -->
      <tr>
        <td style="padding:0 28px 36px;">
          <table cellpadding="0" cellspacing="0" role="presentation">
            <tr>
              <td style="background:#C9A84C;">
                <a href="{SUBMIT_URL}"
                   style="display:inline-block;padding:14px 28px;
                          font-family:monospace;color:#0c0c0c;font-size:11px;font-weight:bold;
                          letter-spacing:0.2em;text-transform:uppercase;text-decoration:none;
                          white-space:nowrap;">
                  Submit Your Artwork &rarr;
                </a>
              </td>
            </tr>
          </table>
        </td>
      </tr>

      <!-- footer -->
      <tr>
        <td style="padding:18px 28px;border-top:1px solid rgba(255,255,255,0.07);">
          <p style="margin:0 0 3px;font-family:monospace;font-size:11px;color:#666;white-space:nowrap;">Lenscape</p>
          <p style="margin:0 0 3px;font-family:monospace;font-size:11px;color:#555;white-space:nowrap;">JLUG Digital Exhibition</p>
          <p style="margin:0 0 8px;">
            <a href="{GALLERY_URL}"
               style="font-family:monospace;font-size:11px;color:#C9A84C;
                      text-decoration:underline;white-space:nowrap;">lenscape.jlug.club</a>
          </p>
          <p style="margin:0;font-family:monospace;font-size:10px;color:#555;line-height:1.6;">
            You registered on Lenscape but haven&apos;t submitted yet.
            This is a one-time reminder from the Lenscape team.
          </p>
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""


def send_email(to: str, name: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"  [dry-run] → {to} ({name})")
        return True
    first = name.split()[0] if name else "there"
    payload = {
        "api_key": SMTP2GO_API_KEY,
        "to": [to],
        "sender": f"Lenscape <{SENDER_EMAIL}>",
        "subject": "Your artwork is missing from the Lenscape gallery",
        "html_body": build_html(name),
        "text_body": (
            f"Hi {first},\n\n"
            f"You registered on Lenscape but haven't submitted any artwork yet.\n"
            f"The gallery is open — photography, digital art, cinematography, motion graphics.\n\n"
            f"Submit your work: {SUBMIT_URL}\n\n"
            f"— The Lenscape Team\n"
            f"JLUG Digital Exhibition · lenscape.jlug.club\n"
        ),
    }
    try:
        r = requests.post(SMTP2GO_API_URL, json=payload, timeout=15)
        r.raise_for_status()
        res = r.json()
        if res.get("request_id") or res.get("data", {}).get("succeeded"):
            print(f"  ✓ {to}")
            return True
        print(f"  ✗ SMTP2GO: {res}")
        return False
    except Exception as e:
        print(f"  ✗ {to} — {e}")
        return False


def get_targets() -> list:
    print("Fetching users…")
    targets = []
    for user in users_col.find({}):
        uid = user.get("_id")
        if not uid or user.get("isAdmin") or user.get("isBanned"):
            continue
        email = (user.get("email") or "").strip()
        if not email or "@" not in email:
            continue
        if artworks_col.count_documents({"artist.id": uid}) == 0:
            targets.append(user)
    return targets


def run(dry_run=False, limit=None):
    if not SMTP2GO_API_KEY:
        print("ERROR: SMTP2GO_API_KEY not set."); sys.exit(1)

    print(f"\n{'DRY RUN  ' if dry_run else 'LIVE     '} Submission Reminder — {datetime.utcnow():%Y-%m-%d %H:%M} UTC\n")

    targets = get_targets()
    print(f"Users without any submission: {len(targets)}")
    if not targets:
        print("Nothing to do."); return
    if limit:
        targets = targets[:limit]

    sent = failed = 0
    for u in targets:
        ok = send_email(u["email"], u.get("name") or "Artist", dry_run)
        if ok: sent += 1
        else:  failed += 1
        if not dry_run:
            time.sleep(SEND_DELAY_SECONDS)

    print(f"\nSent {sent}  Failed {failed}  Total {sent+failed}\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    a = p.parse_args()
    run(dry_run=a.dry_run, limit=a.limit)
