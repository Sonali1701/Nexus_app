"""User store: MongoDB when MONGODB_URI is set, else a local JSON file.

Both back the same small interface used by auth + the admin API. A user record
is {username, name, password (hash), is_admin}. Usernames are keyed lowercase.
"""
import json
import logging
import threading
import time

from . import config

log = logging.getLogger("store")


def _public(doc):
    if not doc:
        return None
    return {
        "username": doc.get("username"),
        "name": doc.get("name") or doc.get("username"),
        "is_admin": bool(doc.get("is_admin")),
        "createdAt": doc.get("createdAt"),
    }


class FileUserStore:
    """JSON file store. Durable only where the disk is (not Render free tier)."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def get(self, username):
        return self._load().get((username or "").strip().lower())

    def list(self):
        return sorted((_public(d) for d in self._load().values()),
                      key=lambda x: (x["username"] or "").lower())

    def upsert(self, username, fields):
        key = username.strip().lower()
        with self._lock:
            data = self._load()
            doc = data.get(key, {"username": username.strip(), "createdAt": _now()})
            doc.update(fields)
            doc["username"] = username.strip()
            data[key] = doc
            self._save(data)
        return _public(doc)

    def delete(self, username):
        key = (username or "").strip().lower()
        with self._lock:
            data = self._load()
            existed = data.pop(key, None) is not None
            if existed:
                self._save(data)
        return existed

    def admin_count(self):
        return sum(1 for d in self._load().values() if d.get("is_admin"))

    def count(self):
        return len(self._load())


class MongoUserStore:
    """Users keyed by a lowercase `username_lc` (unique index); the original-case
    name is kept in `username` so callers see the same shape as the file store."""

    def __init__(self, uri, db_name):
        from pymongo import MongoClient
        # Free Atlas clusters can be slow to wake, so allow a generous selection
        # window rather than falling back to the ephemeral file store on a blip.
        self._client = MongoClient(uri, serverSelectionTimeoutMS=30000,
                                   connectTimeoutMS=30000, retryWrites=True)
        self._col = self._client[db_name]["users"]
        self._col.create_index("username_lc", unique=True)

    def get(self, username):
        return self._col.find_one({"username_lc": (username or "").strip().lower()})

    def list(self):
        return sorted((_public(d) for d in self._col.find()),
                      key=lambda x: (x["username"] or "").lower())

    def upsert(self, username, fields):
        key = username.strip().lower()
        update = dict(fields)
        update["username"] = username.strip()
        self._col.update_one(
            {"username_lc": key},
            {"$set": update, "$setOnInsert": {"username_lc": key, "createdAt": _now()}},
            upsert=True,
        )
        return _public(self.get(username))

    def delete(self, username):
        res = self._col.delete_one({"username_lc": (username or "").strip().lower()})
        return res.deleted_count > 0

    def admin_count(self):
        return self._col.count_documents({"is_admin": True})

    def count(self):
        return self._col.count_documents({})


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_store():
    if config.MONGODB_URI:
        last = None
        for attempt in range(1, 4):  # retry: free clusters can be slow to wake
            try:
                s = MongoUserStore(config.MONGODB_URI, config.MONGODB_DB)
                s.count()  # force a connection check now
                log.info("User store: MongoDB (%s)", config.MONGODB_DB)
                return s
            except Exception as e:  # noqa: BLE001
                last = e
                log.warning("MongoDB connect attempt %d/3 failed: %s", attempt, e)
                time.sleep(2)
        log.error("MongoDB unreachable after retries (%s). Falling back to the "
                  "FILE store — added users will NOT persist across restarts. "
                  "Fix MONGODB_URI / Atlas Network Access.", last)
    log.info("User store: file (%s)", config.BASE_DIR / "users.json")
    return FileUserStore(str(config.BASE_DIR / "users.json"))


store = build_store()
