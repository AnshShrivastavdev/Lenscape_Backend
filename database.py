"""
Firestore-backed data layer for Lenscape.

Replaces MongoDB/PyMongo. Exposes collection objects that mimic the small
subset of the PyMongo API the app uses (find_one, find, insert_one,
update_one, delete_one, count_documents, insert_many, create_index) so the
rest of the codebase needs almost no changes.

Simple equality queries (and lookups by document id) are pushed down to
Firestore as server-side `.where()` / `.count()` queries so we are billed
only for the documents that actually match — not the whole collection.
Every query the app issues filters on a single field (status, email,
userId, artist.id, name) or by _id, so no composite indexes are required.
Only the rare queries the translator can't express server-side (operator
dicts like {"$ne": ...}, or matching inside arrays of objects) fall back to
streaming the collection in Python.
"""

import os
import json
import firebase_admin
from firebase_admin import credentials, firestore, auth as firebase_auth
from google.cloud.firestore_v1.base_query import FieldFilter
from dotenv import load_dotenv

load_dotenv()

# ── Initialize Firebase Admin ────────────────────────────────────────────────
# Two ways to provide credentials:
#  1. FIREBASE_SERVICE_ACCOUNT_JSON  → full JSON string (best for cloud hosts)
#  2. FIREBASE_SERVICE_ACCOUNT       → path to the JSON file (best for local dev)
SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
SERVICE_ACCOUNT_PATH = os.getenv(
    "FIREBASE_SERVICE_ACCOUNT",
    "lenscape-25955-firebase-adminsdk-fbsvc-2e02180c2c.json",
)

if not firebase_admin._apps:
    if SERVICE_ACCOUNT_JSON:
        cred = credentials.Certificate(json.loads(SERVICE_ACCOUNT_JSON))
    else:
        cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
    firebase_admin.initialize_app(cred)

_db = firestore.client()

# Expose firebase auth for verifying Google ID tokens
fb_auth = firebase_auth


# ── Result objects (mimic PyMongo return shapes) ─────────────────────────────
class _InsertResult:
    def __init__(self, inserted_id):
        self.inserted_id = inserted_id


class _UpdateResult:
    def __init__(self, matched_count):
        self.matched_count = matched_count


class _DeleteResult:
    def __init__(self, deleted_count):
        self.deleted_count = deleted_count


class _Cursor:
    """Iterable wrapper supporting .sort() chaining like a PyMongo cursor."""
    def __init__(self, docs):
        self._docs = docs

    def sort(self, field, direction=1):
        self._docs.sort(
            key=lambda d: (d.get(field) is None, d.get(field)),
            reverse=(direction == -1),
        )
        return self

    def __iter__(self):
        return iter(self._docs)

    def __len__(self):
        return len(self._docs)


# ── Collection wrapper ───────────────────────────────────────────────────────
class FirestoreCollection:
    def __init__(self, name):
        self.name = name
        self.col = _db.collection(name)

    # -- helpers --
    def _doc_to_dict(self, snap):
        data = snap.to_dict() or {}
        data["_id"] = snap.id
        return data

    def _all(self):
        return [self._doc_to_dict(s) for s in self.col.stream()]

    @staticmethod
    def _server_filters(query):
        """
        Split a query into (server_side_equality_filters, can_push_down).

        Returns a list of (field_path, value) equality conditions that Firestore
        can evaluate with `.where()`, and a bool that is False when the query
        contains something we can't express server-side (operator dicts, or the
        special comments.userId array match) — in which case the caller must fall
        back to streaming + Python matching.
        """
        filters = []
        for k, v in query.items():
            if k == "_id":
                # handled separately via direct document lookup
                continue
            # operator dicts (e.g. {"$ne": ...}) or array-of-object matches
            # can't be pushed down — bail out to the Python fallback.
            if isinstance(v, dict) or k == "comments.userId":
                return filters, False
            filters.append((k, v))
        return filters, True

    def _server_query(self, filters):
        """Build a Firestore query from a list of (field_path, value) equalities."""
        q = self.col
        for field, value in filters:
            q = q.where(filter=FieldFilter(field, "==", value))
        return q

    @staticmethod
    def _get_field(doc, key):
        cur = doc
        for part in key.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return None
        return cur

    @staticmethod
    def _set_field(doc, key, value):
        parts = key.split(".")
        cur = doc
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value

    def _match(self, doc, query):
        for k, v in query.items():
            # operator dicts e.g. {"$ne": "other"}
            if isinstance(v, dict):
                if "$ne" in v:
                    if self._get_field(doc, k) == v["$ne"]:
                        return False
                    continue
            # special case: array of comment objects
            if k == "comments.userId":
                comments = doc.get("comments", []) or []
                if not any(c.get("userId") == v for c in comments):
                    return False
                continue
            if self._get_field(doc, k) != v:
                return False
        return True

    # -- read --
    def find_one(self, query=None):
        query = query or {}
        # fast path: lookup by document id (1 read)
        if "_id" in query and isinstance(query["_id"], str):
            snap = self.col.document(query["_id"]).get()
            if not snap.exists:
                return None
            doc = self._doc_to_dict(snap)
            rest = {k: v for k, v in query.items() if k != "_id"}
            return doc if self._match(doc, rest) else None

        filters, pushdown = self._server_filters(query)
        if pushdown and filters:
            # server-side query — billed only for matching docs
            for snap in self._server_query(filters).limit(1).stream():
                return self._doc_to_dict(snap)
            return None

        # fallback: stream + match in Python (e.g. operator dicts)
        for doc in self._all():
            if self._match(doc, query):
                return doc
        return None

    def find(self, query=None):
        query = query or {}
        filters, pushdown = self._server_filters(query)
        if pushdown and filters:
            docs = [self._doc_to_dict(s) for s in self._server_query(filters).stream()]
            return _Cursor(docs)
        return _Cursor([d for d in self._all() if self._match(d, query)])

    def count_documents(self, query=None):
        query = query or {}
        filters, pushdown = self._server_filters(query)
        if pushdown:
            # aggregation count — billed at roughly 1 read per 1000 matched docs
            q = self._server_query(filters) if filters else self.col
            result = q.count().get()
            return int(result[0][0].value)
        # fallback for queries we can't express server-side
        return len([d for d in self._all() if self._match(d, query)])

    # -- write --
    def insert_one(self, doc):
        doc = dict(doc)
        _id = doc.pop("_id", None)
        if _id is not None:
            self.col.document(str(_id)).set(doc)
            return _InsertResult(str(_id))
        ref = self.col.document()
        ref.set(doc)
        return _InsertResult(ref.id)

    def insert_many(self, docs):
        for d in docs:
            self.insert_one(d)

    def _apply_update(self, doc, update):
        doc = dict(doc)
        for op, fields in update.items():
            if op == "$set":
                for k, v in fields.items():
                    self._set_field(doc, k, v)
            elif op == "$inc":
                for k, v in fields.items():
                    self._set_field(doc, k, (self._get_field(doc, k) or 0) + v)
            elif op == "$push":
                for k, v in fields.items():
                    arr = list(self._get_field(doc, k) or [])
                    arr.append(v)
                    self._set_field(doc, k, arr)
        return doc

    def update_one(self, query, update, upsert=False):
        target = self.find_one(query)
        if not target:
            if upsert:
                base = {k: v for k, v in query.items() if not isinstance(v, dict)}
                base = self._apply_update(base, update)
                self.insert_one(base)
                return _UpdateResult(0)
            return _UpdateResult(0)
        doc_id = target["_id"]
        new_doc = self._apply_update(target, update)
        new_doc.pop("_id", None)
        self.col.document(doc_id).set(new_doc)
        return _UpdateResult(1)

    def delete_one(self, query):
        target = self.find_one(query)
        if not target:
            return _DeleteResult(0)
        self.col.document(target["_id"]).delete()
        return _DeleteResult(1)

    # -- compatibility no-op --
    def create_index(self, *args, **kwargs):
        return None


# ── Collections ──────────────────────────────────────────────────────────────
users_col = FirestoreCollection("users")
artworks_col = FirestoreCollection("artworks")
categories_col = FirestoreCollection("categories")
banned_users_col = FirestoreCollection("banned_users")
admins_col = FirestoreCollection("admins")


def init_db():
    """Seed default categories if the collection is empty."""
    try:
        if categories_col.count_documents({}) == 0:
            default_categories = [
                {"name": "photography"},
                {"name": "filmmaking"},
                {"name": "animation"},
                {"name": "digital-art"},
                {"name": "illustration"},
                {"name": "motion-graphics"},
                {"name": "other"},
            ]
            categories_col.insert_many(default_categories)
            print("Successfully initialized default art categories in Firestore.")
        else:
            print("Firestore connected. Categories already initialized.")
    except Exception as e:
        print(f"Error initializing database: {e}")
