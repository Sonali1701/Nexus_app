"""Login + user management on top of the pluggable user store.

Passwords are hashed with stdlib PBKDF2. Accounts live in MongoDB (or a local
file) via app.store. A bootstrap admin and any APP_USERS seed are imported on
startup so there is always someone who can log in and manage users.
"""
import base64
import hashlib
import hmac
import json
import logging
import os

from . import config
from .store import store

log = logging.getLogger("auth")

_ITERATIONS = 200_000


def hash_password(password: str, iterations: int = _ITERATIONS) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, iters, salt_b64, hash_b64 = stored.split("$")
            dk = hashlib.pbkdf2_hmac(
                "sha256", password.encode(), base64.b64decode(salt_b64), int(iters))
            return hmac.compare_digest(dk, base64.b64decode(hash_b64))
        except Exception:
            return False
    return hmac.compare_digest(password.encode(), stored.encode())  # plaintext fallback


# --- session identity ------------------------------------------------------

def authenticate(username: str, password: str):
    """Return {username, name, is_admin} on success, else None."""
    doc = store.get(username)
    if doc and doc.get("password") and verify_password(password, doc["password"]):
        return {
            "username": doc.get("username"),
            "name": doc.get("name") or doc.get("username"),
            "is_admin": bool(doc.get("is_admin")),
        }
    return None


# --- management (used by the admin API) ------------------------------------

def add_user(username, name, password, is_admin=False):
    username = (username or "").strip()
    if not username:
        raise ValueError("Username is required")
    if not password:
        raise ValueError("Password is required")
    if store.get(username):
        raise ValueError(f"User '{username}' already exists")
    return store.upsert(username, {
        "name": (name or username).strip(),
        "password": hash_password(password),
        "is_admin": bool(is_admin),
    })


def set_password(username, password):
    if not store.get(username):
        raise ValueError(f"User '{username}' not found")
    if not password:
        raise ValueError("Password is required")
    return store.upsert(username, {"password": hash_password(password)})


def set_admin(username, is_admin):
    doc = store.get(username)
    if not doc:
        raise ValueError(f"User '{username}' not found")
    if doc.get("is_admin") and not is_admin and store.admin_count() <= 1:
        raise ValueError("Cannot remove the last admin")
    return store.upsert(username, {"is_admin": bool(is_admin)})


def delete_user(username):
    doc = store.get(username)
    if not doc:
        raise ValueError(f"User '{username}' not found")
    if doc.get("is_admin") and store.admin_count() <= 1:
        raise ValueError("Cannot delete the last admin")
    return store.delete(username)


def list_users():
    return store.list()


def any_users_configured() -> bool:
    return store.count() > 0


# --- startup seeding -------------------------------------------------------

def seed():
    """Ensure a bootstrap admin and import any APP_USERS seed (idempotent)."""
    # 1. bootstrap admin from env
    if config.ADMIN_PASSWORD:
        existing = store.get(config.ADMIN_USERNAME)
        if not existing:
            store.upsert(config.ADMIN_USERNAME, {
                "name": config.ADMIN_NAME,
                "password": hash_password(config.ADMIN_PASSWORD),
                "is_admin": True,
            })
            log.info("Seeded bootstrap admin '%s'", config.ADMIN_USERNAME)
        elif not existing.get("is_admin"):
            store.upsert(config.ADMIN_USERNAME, {"is_admin": True})

    # 2. import APP_USERS seed (only users not already present)
    if config.APP_USERS_RAW:
        try:
            data = json.loads(config.APP_USERS_RAW)
        except ValueError:
            log.warning("APP_USERS is not valid JSON — skipping seed import")
            data = {}
        if isinstance(data, dict):
            for username, info in data.items():
                if isinstance(info, str):
                    info = {"password": info}
                if not isinstance(info, dict) or store.get(username):
                    continue
                pw = info.get("password", "")
                # store the hash; hash plaintext seeds on the way in
                pw_hash = pw if pw.startswith("pbkdf2_sha256$") else hash_password(pw) if pw else ""
                if not pw_hash:
                    continue
                store.upsert(username, {
                    "name": (info.get("name") or username).strip(),
                    "password": pw_hash,
                    "is_admin": bool(info.get("admin") or info.get("is_admin")),
                })

    if store.count() == 0:
        log.warning("No users exist. Set ADMIN_USERNAME/ADMIN_PASSWORD (or APP_USERS) "
                    "so someone can log in.")
    elif store.admin_count() == 0:
        log.warning("No admin user exists. Set ADMIN_PASSWORD to seed one.")
