import os
import time
import random
import threading
import logging
import jwt as pyjwt
import bcrypt
import requests as http_requests

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(name)s] - %(message)s'
)
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, g
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv

# Import database collections and setup
from database import init_db, users_col, artworks_col, categories_col, banned_users_col, admins_col, fb_auth
from auth import require_auth, admin_only
from video_upload import (
    allowed_video_file, 
    allowed_image_file,
    upload_video_to_cloudinary,
    upload_cover_image_to_cloudinary,
    delete_video_from_cloudinary,
    delete_image_from_cloudinary
)
from google_drive_backup import backup_artwork_to_drive

# Load environment variables
load_dotenv()

app = Flask(__name__)

# CORS — configurable allowed origins (comma-separated), defaults to all in dev
_cors_origins = os.getenv("CORS_ORIGINS", "*")
_origins = [o.strip() for o in _cors_origins.split(",")] if _cors_origins != "*" else "*"
CORS(app, resources={r"/api/*": {"origins": _origins}})

# SMTP2GO email config
SMTP2GO_API_KEY  = os.getenv("SMTP2GO_API_KEY")
SENDER_EMAIL     = os.getenv("SENDER_EMAIL", "lenscape@jlug.club")
SMTP2GO_API_URL  = "https://api.smtp2go.com/v3/email/send"

# OTP signing secret — used to create tamper-proof signed OTP tokens
OTP_SECRET = os.getenv("OTP_SECRET", "otp-secret-change-me")
otp_serializer = URLSafeTimedSerializer(OTP_SECRET)

# User session JWT
USER_JWT_SECRET = os.getenv("USER_JWT_SECRET", "user-jwt-secret-change-me")
USER_SESSION_HOURS = 24 * 7  # 7 days

# Rate Limiter setup using local in-memory storage to prevent crash spikes
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["200 per minute", "10000 per day"],
    storage_uri="memory://"
)

# Configure Cloudinary — primary account
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")

# Fallback Cloudinary account (used when primary runs out of credits)
CLOUDINARY_FALLBACK_CLOUD_NAME = os.getenv("CLOUDINARY_FALLBACK_CLOUD_NAME")
CLOUDINARY_FALLBACK_API_KEY = os.getenv("CLOUDINARY_FALLBACK_API_KEY")
CLOUDINARY_FALLBACK_API_SECRET = os.getenv("CLOUDINARY_FALLBACK_API_SECRET")

# Track which account is currently active
_using_fallback = False

if CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET:
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True
    )
    print("Cloudinary configured successfully (primary).")
elif CLOUDINARY_FALLBACK_CLOUD_NAME and CLOUDINARY_FALLBACK_API_KEY and CLOUDINARY_FALLBACK_API_SECRET:
    # Primary missing/incomplete — use fallback from the start
    CLOUDINARY_CLOUD_NAME = CLOUDINARY_FALLBACK_CLOUD_NAME
    CLOUDINARY_API_KEY = CLOUDINARY_FALLBACK_API_KEY
    CLOUDINARY_API_SECRET = CLOUDINARY_FALLBACK_API_SECRET
    cloudinary.config(
        cloud_name=CLOUDINARY_FALLBACK_CLOUD_NAME,
        api_key=CLOUDINARY_FALLBACK_API_KEY,
        api_secret=CLOUDINARY_FALLBACK_API_SECRET,
        secure=True
    )
    _using_fallback = True
    print("[Cloudinary] ⚠ Primary credentials missing — using FALLBACK account (dpnbjyxsc).")
else:
    print("Warning: No Cloudinary credentials configured. File uploads will not work.")

def switch_to_fallback_cloudinary():
    """Switch Cloudinary config to the fallback account."""
    global _using_fallback, CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET
    if _using_fallback or not CLOUDINARY_FALLBACK_CLOUD_NAME:
        return False
    CLOUDINARY_CLOUD_NAME = CLOUDINARY_FALLBACK_CLOUD_NAME
    CLOUDINARY_API_KEY = CLOUDINARY_FALLBACK_API_KEY
    CLOUDINARY_API_SECRET = CLOUDINARY_FALLBACK_API_SECRET
    cloudinary.config(
        cloud_name=CLOUDINARY_FALLBACK_CLOUD_NAME,
        api_key=CLOUDINARY_FALLBACK_API_KEY,
        api_secret=CLOUDINARY_FALLBACK_API_SECRET,
        secure=True
    )
    _using_fallback = True
    print("⚠ Switched to fallback Cloudinary account.")
    return True

def _is_credit_error(e):
    """Check if a Cloudinary error is due to credit/quota exhaustion."""
    msg = str(e).lower()
    return any(kw in msg for kw in [
        "quota", "limit", "credit", "exceeded", "usage", "plan",
        "rate limit", "too many", "402", "429"
    ])

def cloudinary_upload_with_fallback(file, **kwargs):
    """
    Upload to Cloudinary with automatic fallback.
    If the primary account fails due to credits/quota, switches to fallback and retries.
    """
    if _using_fallback:
        print(f"[Cloudinary] ⚠ Using FALLBACK account (dpnbjyxsc) for upload: {kwargs.get('folder', 'unknown')}")
    try:
        result = cloudinary.uploader.upload(file, **kwargs)
        if _using_fallback:
            print(f"[Cloudinary] ✓ Fallback upload succeeded: {result.get('public_id', '')}")
        return result
    except Exception as e:
        if not _using_fallback and switch_to_fallback_cloudinary():
            print(f"[Cloudinary] ✗ Primary failed ({e}), retrying with fallback...")
            result = cloudinary.uploader.upload(file, **kwargs)
            print(f"[Cloudinary] ✓ Fallback upload succeeded: {result.get('public_id', '')}")
            return result
        raise

# Initialize Firestore + seed default categories
init_db()

# --- Helper Functions ---

def serialize_doc(doc):
    """
    Helper to convert MongoDB objects and dates to JSON-friendly format.
    """
    if doc is None:
        return None
    if isinstance(doc, list):
        return [serialize_doc(item) for item in doc]
    
    serialized = dict(doc)
    if "_id" in serialized:
        serialized["id"] = str(serialized["_id"])
        del serialized["_id"]
        
    for k, v in serialized.items():
        if isinstance(v, datetime):
            serialized[k] = v.isoformat() + "Z"
        elif isinstance(v, dict):
            serialized[k] = serialize_doc(v)
        elif isinstance(v, list):
            serialized[k] = [serialize_doc(item) if isinstance(item, dict) else item for item in v]
            
    return serialized

def check_and_unlock_achievements(user_id):
    """
    Scans database to unlock creator badges dynamically based on user interactions.
    """
    if not user_id:
        return []

    user = users_col.find_one({"_id": user_id})
    if not user:
        return []
        
    user_achievements = user.get("achievements") or []
    unlocked_ids = [ach.get("id") for ach in user_achievements if isinstance(ach, dict)]
    new_achievements = list(user_achievements)
    
    # 1. Creative Pioneer: First submission
    user_submissions_count = artworks_col.count_documents({"artist.id": user_id})
    if user_submissions_count > 0 and "ach1" not in unlocked_ids:
        new_achievements.append({
            "id": "ach1",
            "title": "Creative Pioneer",
            "description": "Submitted your first artwork to the gallery",
            "icon": "🚀",
            "unlockedAt": datetime.utcnow()
        })
        
    # 2. Art Critic: Left a comment
    # Read from the user's own commentedArtworks list instead of scanning every
    # artwork's comments array (which can't be queried server-side).
    comment_count = len(user.get("commentedArtworks") or [])
    if comment_count > 0 and "ach2" not in unlocked_ids:
        new_achievements.append({
            "id": "ach2",
            "title": "Art Critic",
            "description": "Left a thoughtful comment on another student's artwork",
            "icon": "💬",
            "unlockedAt": datetime.utcnow()
        })
        
    # 3. Grand Patron: Voted in at least 3 distinct categories
    voted_cats = user.get("votedCategories") or []
    if len(voted_cats) >= 3 and "ach3" not in unlocked_ids:
        new_achievements.append({
            "id": "ach3",
            "title": "Grand Patron",
            "description": "Voted in at least 3 distinct categories",
            "icon": "👑",
            "unlockedAt": datetime.utcnow()
        })
        
    # 4. Polymath: Voted in all available categories
    all_cats = [cat["name"] for cat in categories_col.find({"name": {"$ne": "other"}})]
    voted_main_cats = [c for c in voted_cats if c != "other"]
    if len(all_cats) > 0 and len(voted_main_cats) >= len(all_cats) and "ach4" not in unlocked_ids:
        new_achievements.append({
            "id": "ach4",
            "title": "Polymath",
            "description": "Voted in all available artwork categories",
            "icon": "🔮",
            "unlockedAt": datetime.utcnow()
        })
        
    if len(new_achievements) != len(user.get("achievements", [])):
        users_col.update_one({"_id": user_id}, {"$set": {"achievements": new_achievements}})

# --- Lightweight in-memory cache ─────────────────────────────────────────────
# The public gallery and category list are identical for every anonymous
# visitor and change rarely, but each request used to re-read Firestore. A short
# TTL cache collapses a traffic spike into a handful of reads per worker.
_cache = {}
_CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "30"))

def _cache_get(key):
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < _CACHE_TTL:
        return entry[1]
    return None

def _cache_set(key, value):
    _cache[key] = (time.time(), value)
    return value

def _cache_invalidate(*keys):
    for k in keys:
        _cache.pop(k, None)

# --- API Endpoints ---

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "service": "lenscape-api"}), 200

# 1. Categories Endpoints
@app.route("/api/categories", methods=["GET"])
def get_categories():
    cached = _cache_get("categories")
    if cached is None:
        cached = _cache_set("categories", [cat["name"] for cat in categories_col.find()])
    return jsonify(cached), 200

@app.route("/api/categories", methods=["POST"])
@admin_only
def add_category():
    data = request.get_json() or {}
    name = data.get("category", "").strip().lower().replace(" ", "-")
    if not name:
        return jsonify({"error": "Category name is required"}), 400
        
    if categories_col.find_one({"name": name}):
        return jsonify({"error": "Category already exists"}), 400
        
    categories_col.insert_one({"name": name})
    _cache_invalidate("categories")
    return jsonify({"success": True, "category": name}), 201

@app.route("/api/categories/<name>", methods=["DELETE"])
@admin_only
def delete_category(name):
    result = categories_col.delete_one({"name": name.strip().lower()})
    if result.deleted_count == 0:
        return jsonify({"error": "Category not found"}), 404
    _cache_invalidate("categories")
    return jsonify({"success": True}), 200

# 2. Users / Profile Endpoints
@app.route("/api/users/submissions", methods=["GET"])
@require_auth
def get_user_submissions():
    """Return all artworks submitted by the authenticated user (all statuses). Votes hidden."""
    submissions = list(artworks_col.find({"artist.id": g.user_id}).sort("createdAt", -1))
    serialized = serialize_doc(submissions)
    for art in serialized:
        art.pop("votes", None)
        art.pop("voters", None)
    return jsonify(serialized), 200


@app.route("/api/users/profile", methods=["GET"])
@require_auth
def get_profile():
    user = users_col.find_one({"_id": g.user_id})
    if not user:
        return jsonify({"registered": False, "message": "Profile not created yet"}), 404
        
    serialized_user = serialize_doc(user)
    # Append user's submissions
    submissions = list(artworks_col.find({"artist.id": g.user_id}))
    serialized_user["submissions"] = serialize_doc(submissions)
    
    return jsonify(serialized_user), 200

@app.route("/api/users/profile", methods=["POST"])
@require_auth
def update_profile():
    # Supports JSON or Multipart form (for avatar upload)
    data = {}
    avatar_url = None
    
    if request.is_json:
        data = request.get_json() or {}
        avatar_url = data.get("avatar")
    else:
        # Form-data
        data = request.form.to_dict()
        avatar_file = request.files.get("avatarFile")
        if avatar_file and CLOUDINARY_CLOUD_NAME:
            try:
                upload_result = cloudinary_upload_with_fallback(avatar_file, folder="lenscape/avatars")
                avatar_url = upload_result.get("secure_url")
            except Exception as e:
                return jsonify({"error": f"Failed to upload avatar: {str(e)}"}), 500
        else:
            avatar_url = data.get("avatar")

    name = data.get("name", "").strip()
    college = data.get("college", "").strip()
    branch = data.get("branch", "").strip()
    year = data.get("year", "1st Year").strip()
    bio = data.get("bio", "").strip()
    
    if not name or not college or not branch:
        return jsonify({"error": "Name, college, and branch are required fields"}), 400

    existing_user = users_col.find_one({"_id": g.user_id})
    
    profile_data = {
        "name": name,
        "email": g.user_email if g.user_email else data.get("email", "").strip().lower(),
        "college": college,
        "branch": branch,
        "year": year,
        "bio": bio,
        "avatar": avatar_url or (existing_user.get("avatar") if existing_user else None)
    }

    if not existing_user:
        # Create profile
        profile_data.update({
            "_id": g.user_id,
            "votedCategories": [],
            "commentedArtworks": [],
            "achievements": [],
            "joinedDate": datetime.utcnow(),
            "isBanned": False,
            "isAdmin": g.is_admin
        })
        users_col.insert_one(profile_data)
    else:
        # Update existing profile
        users_col.update_one({"_id": g.user_id}, {"$set": profile_data})
        
    # Refresh user
    user = users_col.find_one({"_id": g.user_id})
    return jsonify(serialize_doc(user)), 200

# 3. Artwork Endpoints
@app.route("/api/artworks", methods=["GET"])
def get_artworks():
    # Public feed is the same for every visitor — serve it from a short TTL cache
    # so a burst of traffic doesn't re-scan the approved artworks each request.
    cached = _cache_get("artworks_public")
    if cached is not None:
        return jsonify(cached), 200

    # Only return approved artworks for public feed — votes are excluded for regular users
    artworks = list(artworks_col.find({"status": "approved"}).sort("createdAt", -1))
    serialized = serialize_doc(artworks)

    # Strip vote counts from public response — only admins can see votes
    for art in serialized:
        art.pop("votes", None)
        art.pop("voters", None)

    _cache_set("artworks_public", serialized)
    return jsonify(serialized), 200

@app.route("/api/admin/artworks", methods=["GET"])
@admin_only
def get_admin_artworks():
    """Admin endpoint to get approved artworks with voter details"""
    artworks = list(artworks_col.find({"status": "approved"}).sort("createdAt", -1))
    serialized = serialize_doc(artworks)
    
    # Enrich with voter names
    for art in serialized:
        voters = art.get("voters", [])
        voter_details = []
        
        if voters:
            for voter_id in voters:
                user = users_col.find_one({"_id": voter_id})
                if user:
                    voter_details.append({
                        "id": voter_id,
                        "name": user.get("name", "Unknown"),
                        "email": user.get("email", ""),
                        "college": user.get("college", "")
                    })
        
        art["voterDetails"] = voter_details
    
    return jsonify(serialized), 200

@app.route("/api/artworks/pending", methods=["GET"])
@admin_only
def get_pending_artworks():
    artworks = list(artworks_col.find({"status": "pending"}).sort("createdAt", -1))
    return jsonify(serialize_doc(artworks)), 200

@app.route("/api/artworks/rejected", methods=["GET"])
@admin_only
def get_rejected_artworks():
    artworks = list(artworks_col.find({"status": "rejected"}).sort("createdAt", -1))
    return jsonify(serialize_doc(artworks)), 200

# 3. Cloudinary Signed Upload Endpoint
@app.route("/api/cloudinary/signature", methods=["GET"])
@require_auth
@limiter.limit("10 per minute")
def get_cloudinary_signature():
    if not CLOUDINARY_API_SECRET:
        return jsonify({"error": "Cloudinary credentials not configured on backend"}), 500
        
    timestamp = int(time.time())
    params = {
        "timestamp": timestamp,
        "folder": "lenscape/gallery"
    }
    try:
        signature = cloudinary.utils.api_sign_request(
            params,
            CLOUDINARY_API_SECRET
        )
        return jsonify({
            "signature": signature,
            "timestamp": timestamp,
            "folder": "lenscape/gallery",
            "cloud_name": CLOUDINARY_CLOUD_NAME,
            "api_key": CLOUDINARY_API_KEY
        }), 200
    except Exception as e:
        return jsonify({"error": f"Failed generating signature: {str(e)}"}), 500

# 4. Artwork Endpoints
@app.route("/api/artworks", methods=["POST"])
@require_auth
@limiter.limit("3 per minute")
def submit_artwork():
    # Check if user is registered in db
    user = users_col.find_one({"_id": g.user_id})
    if not user:
        return jsonify({"error": "Complete your profile signature first before submitting art"}), 400
        
    data = request.get_json() or {}
    image_url = data.get("imageUrl")
    video_url = data.get("videoUrl")
    title = data.get("title", "").strip()
    description = data.get("description", "").strip()
    category = data.get("category", "").strip().lower()
    subcategory = data.get("subcategory", "").strip()
    orientation = data.get("orientation", "landscape").strip()

    if not title or not description or not category:
        return jsonify({"error": "Title, description, and category are required"}), 400
    if not image_url and not video_url:
        return jsonify({"error": "A cover image or video URL is required"}), 400

    artwork_doc = {
        "title": title,
        "description": description,
        "category": category,
        "subcategory": subcategory or None,
        "orientation": orientation,
        "imageUrl": image_url or None,
        "thumbnailUrl": image_url or None,
        "videoUrl": video_url or None,
        "artist": {
            "id": user["_id"],
            "name": user["name"],
            "email": user["email"],
            "college": user["college"],
            "branch": user["branch"],
            "year": user["year"],
            "avatar": user["avatar"],
            "bio": user["bio"],
            "joinedDate": user["joinedDate"]
        },
        "votes": 0,
        "voters": [],
        "comments": [],
        "createdAt": datetime.utcnow(),
        "status": "approved" if g.is_admin else "pending"  # Admin uploads auto-approved
    }

    result = artworks_col.insert_one(artwork_doc)
    artwork_doc["id"] = str(result.inserted_id)

    # Admin uploads are auto-approved and appear in the public feed immediately
    if artwork_doc["status"] == "approved":
        _cache_invalidate("artworks_public")

    # Check achievements for submitter
    check_and_unlock_achievements(g.user_id)
    
    if image_url:
        ext = image_url.split('.')[-1].lower() if '.' in image_url else 'jpg'
        mime = 'image/png' if ext == 'png' else 'image/jpeg'
        def backup_task():
            backup_artwork_to_drive(image_url, user["name"], title, 'photo', mime)
        threading.Thread(target=backup_task, daemon=True).start()
    
    return jsonify(serialize_doc(artwork_doc)), 201

@app.route("/api/artworks/<artwork_id>/vote", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def vote_artwork(artwork_id):
    artwork = artworks_col.find_one({"_id": artwork_id, "status": "approved"})
    if not artwork:
        return jsonify({"error": "Approved artwork not found"}), 404

    # Enforce voting rules: 1 vote per category per user
    user = users_col.find_one({"_id": g.user_id})
    if not user:
        return jsonify({"error": "Please complete user signature first"}), 400

    category = artwork["category"]
    voted_categories = user.get("votedCategories", [])
    
    if category in voted_categories:
        return jsonify({"error": f"You have already voted in the '{category.replace('-', ' ').title()}' category."}), 400

    # Register Vote
    artworks_col.update_one(
        {"_id": artwork_id},
        {
            "$inc": {"votes": 1},
            "$push": {"voters": g.user_id}
        }
    )
    users_col.update_one(
        {"_id": g.user_id},
        {"$push": {"votedCategories": category}}
    )

    # Check and trigger achievements
    check_and_unlock_achievements(g.user_id)

    return jsonify({"success": True, "category": category}), 200

@app.route("/api/artworks/<artwork_id>/comment", methods=["POST"])
@require_auth
@limiter.limit("5 per minute")
def comment_artwork(artwork_id):
    artwork = artworks_col.find_one({"_id": artwork_id, "status": "approved"})
    if not artwork:
        return jsonify({"error": "Approved artwork not found"}), 404

    user = users_col.find_one({"_id": g.user_id})
    if not user:
        return jsonify({"error": "Please complete user signature first"}), 400

    # Rule: 1 comment per artwork per user
    already_commented = any(c["userId"] == g.user_id for c in artwork.get("comments", []))
    if already_commented:
        return jsonify({"error": "You have already commented on this artwork. Only 1 comment is allowed."}), 400

    data = request.get_json() or {}
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"error": "Comment content cannot be empty"}), 400

    comment_doc = {
        "id": "comment_" + str(int(datetime.utcnow().timestamp() * 1000)),
        "artworkId": artwork_id,
        "userId": g.user_id,
        "userName": user["name"],
        "content": content,
        "createdAt": datetime.utcnow()
    }

    # Add comment to artwork
    artworks_col.update_one(
        {"_id": artwork_id},
        {"$push": {"comments": comment_doc}}
    )
    # Track on the user so the "Art Critic" achievement check stays O(1)
    users_col.update_one(
        {"_id": g.user_id},
        {"$push": {"commentedArtworks": artwork_id}}
    )

    # Check achievements
    check_and_unlock_achievements(g.user_id)

    return jsonify(serialize_doc(comment_doc)), 201

# 4. Admin Curation Actions
@app.route("/api/artworks/<artwork_id>/approve", methods=["POST"])
@admin_only
def approve_artwork(artwork_id):
    # Get admin name from the JWT token
    token = request.headers.get("Authorization", "").split(" ")[1] if request.headers.get("Authorization") else ""
    try:
        payload = pyjwt.decode(token, ADMIN_JWT_SECRET, algorithms=["HS256"])
        admin_name = payload.get("name", "Unknown")
    except:
        admin_name = "Unknown"
    
    result = artworks_col.update_one(
        {"_id": artwork_id},
        {"$set": {
            "status": "approved",
            "approvedBy": admin_name,
            "approvedAt": datetime.utcnow()
        }}
    )
    if result.matched_count == 0:
        return jsonify({"error": "Artwork not found"}), 404

    # New artwork is now public — drop the cached feed
    _cache_invalidate("artworks_public")

    # Trigger achievement checklist updates for the artist of the approved artwork
    artwork = artworks_col.find_one({"_id": artwork_id})
    if artwork and "artist" in artwork:
        check_and_unlock_achievements(artwork["artist"]["id"])

        # Send approval notification email to the artist
        try:
            send_approval_email(
                artist_email=artwork["artist"]["email"],
                artist_name=artwork["artist"]["name"],
                artwork_title=artwork["title"],
                category=artwork.get("category", "")
            )
        except Exception as e:
            app.logger.warning(f"Approval email failed for artwork {artwork_id}: {e}")

    return jsonify({"success": True}), 200


@app.route("/api/artworks/<artwork_id>/reject", methods=["POST"])
@admin_only
def reject_artwork(artwork_id):
    data = request.get_json() or {}
    reason = data.get("reason", "").strip()

    update = {"status": "rejected"}
    if reason:
        update["rejectionReason"] = reason

    result = artworks_col.update_one({"_id": artwork_id}, {"$set": update})
    if result.matched_count == 0:
        return jsonify({"error": "Artwork not found"}), 404

    # A rejected artwork may have previously been approved — refresh the feed
    _cache_invalidate("artworks_public")

    # Send rejection notification email to the artist
    artwork = artworks_col.find_one({"_id": artwork_id})
    if artwork and "artist" in artwork:
        try:
            send_rejection_email(
                artist_email=artwork["artist"]["email"],
                artist_name=artwork["artist"]["name"],
                artwork_title=artwork["title"],
                category=artwork.get("category", ""),
                reason=reason
            )
        except Exception as e:
            app.logger.warning(f"Rejection email failed for artwork {artwork_id}: {e}")

    return jsonify({"success": True}), 200

# 5. User Ban Management
@app.route("/api/admin/users", methods=["GET"])
@admin_only
def get_all_users():
    users = list(users_col.find())
    return jsonify(serialize_doc(users)), 200

@app.route("/api/admin/users/<user_id>/ban", methods=["POST"])
@admin_only
def ban_user(user_id):
    if user_id == "user_admin":
        return jsonify({"error": "Cannot ban administrator account"}), 400

    # Add to banned_users list
    banned_users_col.update_one(
        {"userId": user_id},
        {"$set": {"userId": user_id, "bannedAt": datetime.utcnow()}},
        upsert=True
    )
    # Set status flag
    users_col.update_one({"_id": user_id}, {"$set": {"isBanned": True}})
    
    return jsonify({"success": True}), 200

@app.route("/api/admin/users/<user_id>/unban", methods=["POST"])
@admin_only
def unban_user(user_id):
    banned_users_col.delete_one({"userId": user_id})
    users_col.update_one({"_id": user_id}, {"$set": {"isBanned": False}})
    return jsonify({"success": True}), 200

# ── User Auth Helpers ────────────────────────────────────────────────────────

def issue_user_jwt(user_id, email, name, profile_complete):
    payload = {
        "sub": user_id,
        "email": email,
        "name": name,
        "profileComplete": profile_complete,
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(hours=USER_SESSION_HOURS),
    }
    return pyjwt.encode(payload, USER_JWT_SECRET, algorithm="HS256")


def send_otp_email(email, otp):
    payload = {
        "api_key": SMTP2GO_API_KEY,
        "to": [email],
        "sender": f"Lenscape <{SENDER_EMAIL}>",
        "subject": "Lenscape — Your Verification Code",
        "html_body": f"""
        <div style="font-family:monospace;background:#0c0c0c;color:#e8dcc8;padding:32px;max-width:480px;margin:auto;border:1px solid rgba(201,168,76,0.3)">
          <p style="font-size:10px;color:#C9A84C;letter-spacing:0.3em;text-transform:uppercase;margin-bottom:8px">Lenscape · Digital Exhibition</p>
          <h2 style="font-size:28px;font-weight:300;margin:0 0 24px">Verification Code</h2>
          <div style="font-size:36px;font-weight:bold;letter-spacing:0.2em;color:#C9A84C;border:1px solid rgba(201,168,76,0.3);padding:16px;text-align:center;margin-bottom:24px">{otp}</div>
          <p style="font-size:11px;color:#666;line-height:1.6">
            Enter this code on the Lenscape sign-in page. It expires in <strong style="color:#e8dcc8">10 minutes</strong>.<br>
            If you didn't request this, ignore this email.
          </p>
        </div>
        """
    }
    try:
        resp = http_requests.post(SMTP2GO_API_URL, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        app.logger.info(f"SMTP2GO response: {result}")
        
        # Check if the API call was successful
        if result.get("request_id"):  # SMTP2GO returns request_id on success
            return True
        elif not result.get("data", {}).get("succeeded"):
            raise RuntimeError(f"SMTP2GO send failed: {result}")
        return True
    except http_requests.exceptions.RequestException as e:
        app.logger.error(f"SMTP2GO request failed: {e}")
        raise RuntimeError(f"Email service error: {str(e)}")


def send_approval_email(artist_email, artist_name, artwork_title, category):
    payload = {
        "api_key": SMTP2GO_API_KEY,
        "to": [artist_email],
        "sender": f"Lenscape <{SENDER_EMAIL}>",
        "subject": "Lenscape — Your Artwork Has Been Approved 🎉",
        "html_body": f"""
        <div style="font-family:monospace;background:#0c0c0c;color:#e8dcc8;padding:32px;max-width:520px;margin:auto;border:1px solid rgba(201,168,76,0.3)">
          <p style="font-size:10px;color:#C9A84C;letter-spacing:0.3em;text-transform:uppercase;margin-bottom:8px">Lenscape · Digital Exhibition</p>
          <h2 style="font-size:26px;font-weight:300;margin:0 0 8px">Your artwork is now live.</h2>
          <p style="font-size:12px;color:#888;margin:0 0 28px">Congratulations, {artist_name}.</p>
          <div style="border:1px solid rgba(201,168,76,0.25);padding:20px;margin-bottom:28px;background:#0e0d0a">
            <p style="font-size:10px;color:#C9A84C;letter-spacing:0.2em;text-transform:uppercase;margin:0 0 6px">Accepted Submission</p>
            <p style="font-size:18px;font-weight:300;margin:0 0 6px;color:#e8dcc8">{artwork_title}</p>
            <p style="font-size:10px;color:#666;text-transform:uppercase;letter-spacing:0.15em;margin:0">{category.replace('-', ' ')}</p>
          </div>
          <p style="font-size:11px;color:#666;line-height:1.8">
            Your artwork has been reviewed and <strong style="color:#C9A84C">approved</strong> for the Lenscape exhibition.<br>
            It is now visible in the gallery and eligible to receive votes from the community.
          </p>
          <div style="margin-top:28px;padding-top:20px;border-top:1px solid rgba(255,255,255,0.05)">
            <p style="font-size:10px;color:#444;margin:0">Lenscape · JLUG Digital Exhibition · <a href="https://lenscape.jlug.club" style="color:#C9A84C;text-decoration:none">lenscape.jlug.club</a></p>
          </div>
        </div>
        """
    }
    try:
        resp = http_requests.post(SMTP2GO_API_URL, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        app.logger.info(f"SMTP2GO approval email response: {result}")
        
        if result.get("request_id"):
            return True
        elif not result.get("data", {}).get("succeeded"):
            raise RuntimeError(f"SMTP2GO send failed: {result}")
        return True
    except http_requests.exceptions.RequestException as e:
        app.logger.error(f"SMTP2GO approval email failed: {e}")
        raise RuntimeError(f"Email service error: {str(e)}")


def send_rejection_email(artist_email, artist_name, artwork_title, category, reason=""):
    reason_block = f"""
          <div style="border-left:2px solid rgba(201,168,76,0.4);padding:12px 16px;margin:20px 0;background:#0e0d0a">
            <p style="font-size:10px;color:#C9A84C;letter-spacing:0.2em;text-transform:uppercase;margin:0 0 6px">Reviewer's Note</p>
            <p style="font-size:11px;color:#aaa;margin:0;line-height:1.7">{reason}</p>
          </div>
    """ if reason else ""

    payload = {
        "api_key": SMTP2GO_API_KEY,
        "to": [artist_email],
        "sender": f"Lenscape <{SENDER_EMAIL}>",
        "subject": "Lenscape — Update on Your Submission",
        "html_body": f"""
        <div style="font-family:monospace;background:#0c0c0c;color:#e8dcc8;padding:32px;max-width:520px;margin:auto;border:1px solid rgba(201,168,76,0.3)">
          <p style="font-size:10px;color:#C9A84C;letter-spacing:0.3em;text-transform:uppercase;margin-bottom:8px">Lenscape · Digital Exhibition</p>
          <h2 style="font-size:26px;font-weight:300;margin:0 0 8px">Submission not accepted.</h2>
          <p style="font-size:12px;color:#888;margin:0 0 28px">Hi {artist_name}, thank you for participating.</p>
          <div style="border:1px solid rgba(255,255,255,0.08);padding:20px;margin-bottom:20px;background:#0a0a0a">
            <p style="font-size:10px;color:#666;letter-spacing:0.2em;text-transform:uppercase;margin:0 0 6px">Submission</p>
            <p style="font-size:18px;font-weight:300;margin:0 0 6px;color:#e8dcc8">{artwork_title}</p>
            <p style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:0.15em;margin:0">{category.replace('-', ' ')}</p>
          </div>
          {reason_block}
          <p style="font-size:11px;color:#666;line-height:1.8">
            After review, your submission was <strong style="color:#e8dcc8">not selected</strong> for the current exhibition.<br>
            We encourage you to keep creating and submit again in future exhibitions.
          </p>
          <div style="margin-top:28px;padding-top:20px;border-top:1px solid rgba(255,255,255,0.05)">
            <p style="font-size:10px;color:#444;margin:0">Lenscape · JLUG Digital Exhibition · <a href="https://lenscape.jlug.club" style="color:#C9A84C;text-decoration:none">lenscape.jlug.club</a></p>
          </div>
        </div>
        """
    }
    try:
        resp = http_requests.post(SMTP2GO_API_URL, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        app.logger.info(f"SMTP2GO rejection email response: {result}")
        
        if result.get("request_id"):
            return True
        elif not result.get("data", {}).get("succeeded"):
            raise RuntimeError(f"SMTP2GO send failed: {result}")
        return True
    except http_requests.exceptions.RequestException as e:
        app.logger.error(f"SMTP2GO rejection email failed: {e}")
        raise RuntimeError(f"Email service error: {str(e)}")

# ── User Auth Routes ─────────────────────────────────────────────────────────

@app.route("/api/auth/google", methods=["POST"])
@limiter.limit("20 per minute")
def auth_google():
    """
    Google OAuth: frontend signs in with Firebase, sends the Firebase ID token.
    We verify it, then find/create the user and issue our session JWT.
    """
    data = request.get_json() or {}
    id_token = data.get("idToken", "")
    if not id_token:
        return jsonify({"error": "Missing Google ID token"}), 400

    try:
        decoded = fb_auth.verify_id_token(id_token)
    except Exception as e:
        return jsonify({"error": f"Invalid Google token: {str(e)}"}), 401

    email = (decoded.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "Google account has no email"}), 400
    if not decoded.get("email_verified", False):
        return jsonify({"error": "Google email is not verified"}), 403

    google_name = decoded.get("name", "")
    google_avatar = decoded.get("picture", "")

    existing = users_col.find_one({"email": email})
    if existing:
        user_id = str(existing["_id"])
        if banned_users_col.find_one({"userId": user_id}):
            return jsonify({"error": "This account is banned due to a code of conduct violation."}), 403
        profile_complete = bool(existing.get("profileComplete", True))
        token = issue_user_jwt(user_id, email, existing.get("name", ""), profile_complete)
        return jsonify({
            "token": token, "userId": user_id, "name": existing.get("name", ""),
            "email": email, "profileComplete": profile_complete,
        }), 200

    # New user — create a stub, mark profile incomplete
    stub = {
        "name": google_name,
        "email": email,
        "college": "", "branch": "", "year": "", "bio": "",
        "avatar": google_avatar or f"https://api.dicebear.com/7.x/bottts/svg?seed={email}",
        "authProvider": "google",
        "votedCategories": [], "commentedArtworks": [], "achievements": [],
        "joinedDate": datetime.utcnow(), "isBanned": False, "isAdmin": False,
        "profileComplete": False,
    }
    result = users_col.insert_one(stub)
    user_id = str(result.inserted_id)
    token = issue_user_jwt(user_id, email, google_name, False)
    return jsonify({
        "token": token, "userId": user_id, "name": google_name,
        "email": email, "profileComplete": False,
    }), 200


@app.route("/api/auth/send-otp", methods=["POST"])
@limiter.limit("3 per minute")
def send_otp():
    """
    Email/Password signup step 1 — verify the email is real by sending an OTP.
    Requires email + password. Password is hashed and stashed in the signed token
    so we only create the account after OTP confirmation.
    """
    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not email or "@" not in email:
        return jsonify({"error": "A valid email is required"}), 400
    if not password or len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    existing = users_col.find_one({"email": email})
    if existing and existing.get("passwordHash"):
        return jsonify({"error": "An account with this email already exists. Please log in."}), 400

    otp = str(random.randint(100000, 999999))
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    # Sign email + otp + password hash into a short-lived token
    token = otp_serializer.dumps({"email": email, "otp": otp, "ph": password_hash})

    try:
        send_otp_email(email, otp)
    except Exception as e:
        app.logger.error(f"Mail send failed: {e}")
        return jsonify({"error": f"Failed to send verification email: {str(e)}"}), 500

    return jsonify({"token": token, "message": "OTP sent"}), 200


@app.route("/api/auth/verify-otp", methods=["POST"])
@limiter.limit("5 per minute")
def verify_otp():
    """
    Email/Password signup step 2 — verify OTP, create the account, issue JWT.
    New accounts always start with profileComplete = False.
    """
    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    otp_input = data.get("otp", "").strip()
    token = data.get("token", "")

    if not email or not otp_input or not token:
        return jsonify({"error": "email, otp, and token are required"}), 400

    try:
        payload = otp_serializer.loads(token, max_age=600)
    except SignatureExpired:
        return jsonify({"error": "OTP has expired. Please request a new one."}), 400
    except BadSignature:
        return jsonify({"error": "Invalid token. Please request a new OTP."}), 400

    if payload.get("email") != email:
        return jsonify({"error": "Email mismatch."}), 400
    if payload.get("otp") != otp_input:
        return jsonify({"error": "Incorrect OTP. Please try again."}), 400

    existing = users_col.find_one({"email": email})
    if existing and existing.get("passwordHash"):
        return jsonify({"error": "Account already exists. Please log in."}), 400

    new_user = {
        "name": "",
        "email": email,
        "passwordHash": payload.get("ph"),
        "authProvider": "password",
        "college": "", "branch": "", "year": "", "bio": "",
        "avatar": f"https://api.dicebear.com/7.x/bottts/svg?seed={email}",
        "votedCategories": [], "commentedArtworks": [], "achievements": [],
        "joinedDate": datetime.utcnow(), "isBanned": False, "isAdmin": False,
        "profileComplete": False,
    }
    result = users_col.insert_one(new_user)
    user_id = str(result.inserted_id)
    token_jwt = issue_user_jwt(user_id, email, "", False)
    return jsonify({
        "verified": True, "token": token_jwt, "userId": user_id,
        "email": email, "name": "", "profileComplete": False,
    }), 200


@app.route("/api/auth/login", methods=["POST"])
@limiter.limit("10 per minute")
def login_password():
    """Email/Password login for existing accounts."""
    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    user = users_col.find_one({"email": email})
    if not user or not user.get("passwordHash"):
        return jsonify({"error": "Invalid credentials"}), 401
    if not bcrypt.checkpw(password.encode(), user["passwordHash"].encode()):
        return jsonify({"error": "Invalid credentials"}), 401

    user_id = str(user["_id"])
    if banned_users_col.find_one({"userId": user_id}):
        return jsonify({"error": "This account is banned due to a code of conduct violation."}), 403

    profile_complete = bool(user.get("profileComplete", True))
    token = issue_user_jwt(user_id, email, user.get("name", ""), profile_complete)
    return jsonify({
        "token": token, "userId": user_id, "name": user.get("name", ""),
        "email": email, "profileComplete": profile_complete,
    }), 200


@app.route("/api/auth/complete-profile", methods=["POST"])
@require_auth
def complete_profile():
    """
    After Google or email signup, the user lands here to fill in
    name/college/branch/year/bio. Marks profileComplete = True and re-issues JWT.
    """
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    college = data.get("college", "").strip()
    branch = data.get("branch", "").strip()
    year = data.get("year", "1st Year").strip()
    bio = data.get("bio", "").strip()
    avatar = data.get("avatar", "")

    if not name or not college or not branch:
        return jsonify({"error": "Name, college and branch are required"}), 400

    existing = users_col.find_one({"_id": g.user_id})
    if not existing:
        return jsonify({"error": "User not found"}), 404

    update = {
        "name": name, "college": college, "branch": branch,
        "year": year, "bio": bio, "profileComplete": True,
    }
    if avatar:
        update["avatar"] = avatar

    users_col.update_one({"_id": g.user_id}, {"$set": update})

    token = issue_user_jwt(g.user_id, g.user_email, name, True)
    user = users_col.find_one({"_id": g.user_id})
    return jsonify({
        "token": token, "user": serialize_doc(user), "profileComplete": True,
    }), 200


@app.route("/api/auth/verify-token", methods=["GET"])
def verify_user_token():
    """Validate a stored user session."""
    auth_header = request.headers.get("Authorization", "")
    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return jsonify({"valid": False}), 401
    try:
        payload = pyjwt.decode(parts[1], USER_JWT_SECRET, algorithms=["HS256"])
        user = users_col.find_one({"_id": payload["sub"]})
        if not user:
            return jsonify({"valid": False}), 401
        if banned_users_col.find_one({"userId": payload["sub"]}):
            return jsonify({"valid": False, "banned": True}), 403
        return jsonify({
            "valid": True, "userId": payload["sub"], "name": payload.get("name"),
            "email": payload.get("email"), "profileComplete": bool(user.get("profileComplete", True)),
        }), 200
    except pyjwt.ExpiredSignatureError:
        return jsonify({"valid": False, "error": "Session expired"}), 401
    except pyjwt.InvalidTokenError:
        return jsonify({"valid": False}), 401


# ── Admin Auth Routes ────────────────────────────────────────────────────────────

ADMIN_JWT_SECRET = os.getenv("ADMIN_JWT_SECRET", "change-me-in-production")
ADMIN_SESSION_HOURS = 12
# Master secret key — anyone who knows it can register/login as an admin
ADMIN_SECRET_KEY = os.getenv("ADMIN_SECRET_KEY", "lenscape-master-key")


@app.route("/api/admin/login", methods=["POST"])
@limiter.limit("5 per minute")
def admin_login():
    """
    Admin login and signup endpoint.
    - Login: email, password (no name provided)
    - Signup: name, email, password, secretKey
    """
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    secret_key = data.get("secretKey", "")

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    admin = admins_col.find_one({"email": email})

    if name:
        # SIGNUP FLOW — create new admin or update existing
        if not secret_key:
            return jsonify({"error": "Secret key is required for registration"}), 400
        
        if secret_key != ADMIN_SECRET_KEY:
            return jsonify({"error": "Invalid secret key"}), 401

        if admin:
            # Update existing admin password
            password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            admins_col.update_one({"email": email}, {"$set": {"passwordHash": password_hash, "name": name}})
            admin_id = str(admin["_id"])
            admin_name = name
        else:
            # Create new admin
            password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            new_admin = {
                "name": name,
                "email": email,
                "passwordHash": password_hash,
                "createdAt": datetime.utcnow(),
            }
            result = admins_col.insert_one(new_admin)
            admin_id = str(result.inserted_id)
            admin_name = name
    else:
        # LOGIN FLOW — verify existing admin
        if not admin:
            return jsonify({"error": "Invalid credentials"}), 401
        
        if not bcrypt.checkpw(password.encode(), admin["passwordHash"].encode()):
            return jsonify({"error": "Invalid credentials"}), 401
        
        admin_id = str(admin["_id"])
        admin_name = admin.get("name", "Curator")

    payload = {
        "sub": admin_id,
        "email": email,
        "name": admin_name,
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(hours=ADMIN_SESSION_HOURS),
    }
    token = pyjwt.encode(payload, ADMIN_JWT_SECRET, algorithm="HS256")
    return jsonify({"token": token, "name": admin_name}), 200


@app.route("/api/admin/verify", methods=["GET"])
def admin_verify():
    """Lightweight token check used by frontend on page load."""
    auth_header = request.headers.get("Authorization", "")
    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return jsonify({"valid": False}), 401
    try:
        payload = pyjwt.decode(parts[1], ADMIN_JWT_SECRET, algorithms=["HS256"])
        admin = admins_col.find_one({"email": payload.get("email")})
        if not admin:
            return jsonify({"valid": False}), 401
        return jsonify({"valid": True, "name": payload.get("name")}), 200
    except pyjwt.ExpiredSignatureError:
        return jsonify({"valid": False, "error": "Token expired"}), 401
    except pyjwt.InvalidTokenError:
        return jsonify({"valid": False, "error": "Invalid token"}), 401


# 5. Video Artwork Upload Endpoint (for Cinematography & Motion Graphics)
@app.route("/api/artworks/submit-video", methods=["POST"])
@require_auth
@limiter.limit("3 per minute")
def submit_video_artwork():
    """
    Handle video artwork submission with cover image for cinematography and motion graphics
    
    Form Data:
        - video: Video file (MP4/MKV, max 500MB)
        - cover: Cover image (JPG/PNG/WebP, max 10MB)
        - title: Artwork title
        - description: Description
        - category: Main category (cinematography/motion-graphics)
        - subCategory: Subcategory (optional)
    """
    # Check if user is registered
    user = users_col.find_one({"_id": g.user_id})
    if not user:
        return jsonify({"error": "Complete your profile signature first before submitting art"}), 400
    
    # Validate files are present
    if 'video' not in request.files or 'cover' not in request.files:
        return jsonify({"error": "Both video and cover image are required"}), 400
    
    video_file = request.files['video']
    cover_file = request.files['cover']
    
    # Validate video file
    if not video_file.filename or not allowed_video_file(video_file.filename):
        return jsonify({"error": "Invalid video format. Please upload MP4 or MKV"}), 400
    
    # Validate cover image
    if not cover_file.filename or not allowed_image_file(cover_file.filename):
        return jsonify({"error": "Invalid image format. Please upload JPG, PNG or WebP"}), 400
    
    # Get form data
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    category = request.form.get('category', '').strip().lower()
    sub_category = request.form.get('subCategory', '').strip()
    orientation = request.form.get('orientation', 'widescreen').strip()
    
    # Validate required fields
    if not title or not description or not category:
        return jsonify({"error": "Title, description, and category are required"}), 400
    
    # Validate category is video-compatible
    valid_video_categories = ['cinematography', 'motion-graphics']
    if category not in valid_video_categories:
        return jsonify({"error": f"Invalid category for video. Must be one of: {', '.join(valid_video_categories)}"}), 400
    
    # Generate unique artwork ID
    artwork_id = "art_" + str(int(datetime.utcnow().timestamp() * 1000))
    
    try:
        # Upload video to Cloudinary
        print(f"Uploading video for artwork {artwork_id}...")
        video_result = upload_video_to_cloudinary(video_file, artwork_id)
        
        if not video_result.get('success'):
            error_msg = video_result.get('error', 'Unknown error')
            return jsonify({"error": f"Video upload failed: {error_msg}"}), 500
        
        # Upload cover image to Cloudinary
        print(f"Uploading cover image for artwork {artwork_id}...")
        cover_result = upload_cover_image_to_cloudinary(cover_file, artwork_id)
        
        if not cover_result.get('success'):
            # Rollback: Delete uploaded video
            print(f"Cover upload failed. Rolling back video upload...")
            delete_video_from_cloudinary(video_result.get('public_id'))
            error_msg = cover_result.get('error', 'Unknown error')
            return jsonify({"error": f"Cover image upload failed: {error_msg}"}), 500
        
        # Create artwork document
        artwork_doc = {
            "_id": artwork_id,
            "title": title,
            "description": description,
            "category": category,
            "subcategory": sub_category or None,
            "orientation": orientation,
            "imageUrl": cover_result['image_url'],
            "thumbnailUrl": cover_result['thumbnail_url'],
            "videoUrl": video_result['video_url'],
            "videoDuration": video_result.get('duration'),
            "videoFormat": video_result.get('format'),
            "cloudinaryVideoId": video_result['public_id'],
            "cloudinaryCoverId": cover_result['public_id'],
            "artist": {
                "id": user["_id"],
                "name": user["name"],
                "email": user["email"],
                "college": user["college"],
                "branch": user["branch"],
                "year": user["year"],
                "avatar": user["avatar"],
                "bio": user["bio"],
                "joinedDate": user["joinedDate"]
            },
            "votes": 0,
            "voters": [],
            "comments": [],
            "createdAt": datetime.utcnow(),
            "status": "approved" if g.is_admin else "pending"
        }
        
        # Save to database
        artworks_col.insert_one(artwork_doc)

        # Admin uploads are auto-approved and appear in the public feed immediately
        if artwork_doc["status"] == "approved":
            _cache_invalidate("artworks_public")

        # Check achievements
        check_and_unlock_achievements(g.user_id)
        
        video_url_for_backup = video_result['video_url']
        cover_url_for_backup = cover_result['image_url']
        
        def backup_video_task():
            video_ext = video_url_for_backup.split('.')[-1].lower() if '.' in video_url_for_backup else 'mp4'
            video_mime = 'video/x-matroska' if video_ext == 'mkv' else 'video/mp4'
            backup_artwork_to_drive(video_url_for_backup, user["name"], title, 'video', video_mime, False)
            
            cover_ext = cover_url_for_backup.split('.')[-1].lower() if '.' in cover_url_for_backup else 'jpg'
            cover_mime = 'image/png' if cover_ext == 'png' else 'image/jpeg'
            backup_artwork_to_drive(cover_url_for_backup, user["name"], title, 'video', cover_mime, True)
            
        threading.Thread(target=backup_video_task, daemon=True).start()
        
        print(f"Video artwork {artwork_id} submitted successfully!")
        
        return jsonify({
            "success": True,
            "artwork": serialize_doc(artwork_doc),
            "message": "Video artwork submitted successfully"
        }), 201
        
    except Exception as e:
        app.logger.error(f"Video upload error: {str(e)}")
        return jsonify({"error": f"Upload failed: {str(e)}"}), 500


# 6. Global Error Handlers
@app.route("/", methods=["GET", "HEAD"])
def root_health():
    """Simple root endpoint for Render's automated health checks."""
    return jsonify({"status": "healthy", "service": "lenscape-backend"}), 200

from werkzeug.exceptions import HTTPException

@app.errorhandler(Exception)
def handle_exception(e):
    """
    Global catch-all error handler returning structured JSON error details.
    """
    # Pass through standard HTTP errors (like 404) correctly
    if isinstance(e, HTTPException):
        return jsonify({
            "error": e.name,
            "details": e.description
        }), e.code

    app.logger.error(f"Unhandled server error: {str(e)}")
    return jsonify({
        "error": "An unexpected server error occurred",
        "details": str(e)
    }), 500

if __name__ == "__main__":
    port = int(os.getenv("FLASK_PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=(os.getenv("FLASK_ENV") == "development"))
