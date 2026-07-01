"""
send_vote_reminder.py
──────────────────────
Sends vote-reminder emails to users who haven't voted in all active categories.
Embeds exactly 3 approved image artworks (no videos) as single-column cards.

Run:
    python send_vote_reminder.py
    python send_vote_reminder.py --dry-run
    python send_vote_reminder.py --limit 10
"""

import argparse, os, random, sys, time, requests
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
GALLERY_URL     = "https://lenscape.jlug.club/gallery"

# Categories that actually have entries.
# A user must have voted in ALL of these to be excluded. motion-graphics ignored.
VOTE_REQUIRED_CATEGORIES = {"photography", "digital-art", "cinematography"}

ORIENTATION_PADDING = {
    "portrait":   "133.33%",
    "square":     "100%",
    "landscape":  "66.67%",
    "widescreen": "56.25%",
    "vertical":   "177.78%",
}
DEFAULT_PADDING = "66.67%"


# ── Cloudinary URL rewriter ───────────────────────────────────────────────────

def _cloudinary_email_url(raw_url: str) -> str:
    """
    Rewrite a Cloudinary URL to force a JPEG delivery that Gmail's image
    proxy will cache and display. Inserts f_jpg,q_80,w_520 before the
    version segment so the URL is stable and publicly accessible.
    """
    if not raw_url or "res.cloudinary.com" not in raw_url:
        return raw_url
    marker = "/image/upload/"
    idx = raw_url.find(marker)
    if idx == -1:
        return raw_url
    base = raw_url[: idx + len(marker)]
    rest = raw_url[idx + len(marker):]
    # Don't double-insert if transformations are already present
    if rest.startswith("f_") or rest.startswith("q_") or rest.startswith("w_"):
        return raw_url
    return base + "f_jpg,q_80,w_520/" + rest


# ── Artwork card ──────────────────────────────────────────────────────────────

def _artwork_card(artwork: dict) -> str:
    title       = artwork.get("title", "Untitled")
    artist_name = (artwork.get("artist") or {}).get("name", "Unknown Artist")
    college     = (artwork.get("artist") or {}).get("college", "")
    category    = artwork.get("category", "").replace("-", " ").title()
    raw_url     = artwork.get("imageUrl") or artwork.get("thumbnailUrl") or ""
    image_url   = _cloudinary_email_url(raw_url)
    orientation = artwork.get("orientation", "landscape").lower()
    padding_pct = ORIENTATION_PADDING.get(orientation, DEFAULT_PADDING)

    college_part = f"&nbsp;·&nbsp;{college}" if college else ""

    # Use a plain <img> with a fixed pixel height — the padding-bottom trick
    # is elegant in browsers but many email clients (Gmail Android, Outlook)
    # collapse position:absolute children. A fixed height is more reliable.
    HEIGHTS = {
        "portrait":   "320px",
        "square":     "260px",
        "landscape":  "220px",
        "widescreen": "190px",
        "vertical":   "360px",
    }
    img_height = HEIGHTS.get(orientation, "220px")

    img_block = (
        f'<img src="{image_url}" alt="{title}" width="100%" height="{img_height}"'
        f' style="display:block;width:100%;height:{img_height};object-fit:cover;border:0;" />'
        if image_url
        else f'<div style="width:100%;height:{img_height};background:#1a1a12;display:block;"></div>'
    )

    return f"""<tr>
  <td style="padding-bottom:20px;">
    <a href="{GALLERY_URL}"
       style="display:block;text-decoration:none;color:inherit;
              border:1px solid rgba(201,168,76,0.2);background:#0e0d0a;">
      {img_block}
      <div style="padding:12px 14px 14px;">
        <p style="margin:0 0 3px;font-family:Georgia,serif;font-size:14px;
                  font-weight:400;color:#e8dcc8;">{title}</p>
        <p style="margin:0 0 6px;font-family:monospace;font-size:9px;color:#777;">
          {artist_name}{college_part}
        </p>
        <p style="margin:0;font-family:monospace;font-size:8px;color:#C9A84C;
                  letter-spacing:0.22em;text-transform:uppercase;">{category}</p>
      </div>
    </a>
  </td>
</tr>"""


# ── Email HTML builder ────────────────────────────────────────────────────────

def build_html(user_name: str, artworks: list) -> str:
    first = user_name.split()[0] if user_name else "Visitor"

    art_section = ""
    if artworks:
        cards = "".join(_artwork_card(a) for a in artworks)
        art_section = f"""
      <!-- section label -->
      <tr>
        <td style="padding:0 28px 10px;">
          <p style="margin:0 0 1px;font-family:monospace;font-size:8px;color:#C9A84C;
                    letter-spacing:0.3em;text-transform:uppercase;white-space:nowrap;">From the Gallery</p>
          <p style="margin:0;font-family:monospace;font-size:8px;color:#555;
                    letter-spacing:0.3em;text-transform:uppercase;white-space:nowrap;">Tap to Vote</p>
        </td>
      </tr>
      <tr>
        <td style="padding:0 28px 28px;">
          <table width="100%" cellpadding="0" cellspacing="0">
            {cards}
          </table>
        </td>
      </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <meta name="x-apple-disable-message-reformatting"/>
  <title>Lenscape — Cast Your Vote</title>
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
        <td style="padding:36px 28px 16px;">
          <h1 style="margin:0 0 6px;font-family:Georgia,serif;font-size:28px;
                     font-weight:300;color:#e8dcc8;line-height:1.25;">
            Your vote is still<br/>uncounted, {first}.
          </h1>
          <p style="margin:0 0 16px;font-family:monospace;font-size:12px;color:#888;line-height:1.8;">
            One vote per category &mdash; photography, digital art, cinematography.<br/>
            Pick the work that deserves to win.
          </p>
        </td>
      </tr>

      {art_section}

      <!-- CTA -->
      <tr>
        <td style="padding:0 28px 36px;">
          <table cellpadding="0" cellspacing="0" role="presentation">
            <tr>
              <td style="background:#C9A84C;">
                <a href="{GALLERY_URL}"
                   style="display:inline-block;padding:14px 28px;
                          font-family:monospace;color:#0c0c0c;font-size:11px;font-weight:bold;
                          letter-spacing:0.2em;text-transform:uppercase;text-decoration:none;
                          white-space:nowrap;">
                  Open the Gallery &rarr;
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
            You registered on Lenscape but haven&apos;t voted yet.
            This is a one-time reminder from the Lenscape team.
          </p>
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""


# ── Data helpers ──────────────────────────────────────────────────────────────

def get_showcase(max_cards: int = 3) -> list:
    """
    Pick exactly max_cards approved image-only artworks.
    Strategy: one per category first, then fill remaining slots from the
    largest category so we always return max_cards (or all available if fewer).
    """
    approved = [
        a for a in artworks_col.find({"status": "approved"})
        if not a.get("videoUrl") and (a.get("imageUrl") or a.get("thumbnailUrl"))
    ]
    if not approved:
        return []

    by_cat: dict = {}
    for a in approved:
        by_cat.setdefault(a.get("category", "other"), []).append(a)

    # One random pick per category (sorted busiest first)
    picks = []
    used_ids = set()
    for _cat, arts in sorted(by_cat.items(), key=lambda x: len(x[1]), reverse=True):
        if len(picks) >= max_cards:
            break
        choice = random.choice(arts)
        picks.append(choice)
        used_ids.add(choice.get("_id"))

    # Fill remaining slots from the largest category (photography typically)
    if len(picks) < max_cards:
        largest_cat = sorted(by_cat.items(), key=lambda x: len(x[1]), reverse=True)[0][1]
        pool = [a for a in largest_cat if a.get("_id") not in used_ids]
        random.shuffle(pool)
        for a in pool:
            if len(picks) >= max_cards:
                break
            picks.append(a)

    return picks[:max_cards]


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
        voted = set(user.get("votedCategories") or [])
        if not VOTE_REQUIRED_CATEGORIES.issubset(voted):
            targets.append(user)
    return targets


# ── Send ──────────────────────────────────────────────────────────────────────

def send_email(to: str, name: str, artworks: list, dry_run: bool) -> bool:
    if dry_run:
        print(f"  [dry-run] → {to} ({name}) — {len(artworks)} card(s)")
        return True
    payload = {
        "api_key": SMTP2GO_API_KEY,
        "to": [to],
        "sender": f"Lenscape <{SENDER_EMAIL}>",
        "subject": "Your vote is missing from the Lenscape gallery",
        "html_body": build_html(name, artworks),
        "text_body": (
            f"Hi {name.split()[0] if name else 'there'},\n\n"
            f"The Lenscape gallery is live and your vote hasn't been cast yet.\n"
            f"One vote per category — photography, digital art, cinematography.\n\n"
            f"Visit the gallery: {GALLERY_URL}\n\n"
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


# ── Runner ────────────────────────────────────────────────────────────────────

def run(dry_run=False, limit=None, max_cards=3):
    if not SMTP2GO_API_KEY:
        print("ERROR: SMTP2GO_API_KEY not set."); sys.exit(1)

    print(f"\n{'DRY RUN  ' if dry_run else 'LIVE     '} Vote Reminder — {datetime.utcnow():%Y-%m-%d %H:%M} UTC\n")

    showcase = get_showcase(max_cards)
    print(f"Artwork cards selected: {len(showcase)}")
    for a in showcase:
        print(f"  · {a.get('category')} — {a.get('title')} — {_cloudinary_email_url(a.get('imageUrl',''))[:80]}")

    targets = get_targets()
    print(f"\nUsers to notify: {len(targets)}")
    if not targets:
        print("Nothing to do."); return
    if limit:
        targets = targets[:limit]

    sent = failed = 0
    for u in targets:
        ok = send_email(u["email"], u.get("name") or "Visitor", showcase, dry_run)
        if ok: sent += 1
        else:  failed += 1
        if not dry_run:
            time.sleep(SEND_DELAY_SECONDS)

    print(f"\nSent {sent}  Failed {failed}  Total {sent+failed}\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--max-cards", type=int, default=3)
    a = p.parse_args()
    run(dry_run=a.dry_run, limit=a.limit, max_cards=a.max_cards)
