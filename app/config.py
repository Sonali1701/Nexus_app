"""Configuration loaded from .env at the project root."""
import json
import logging
import os
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv

# Source runs keep using the project-root .env.  A PyInstaller executable is
# extracted to a temporary directory at runtime, so secrets must instead be
# loaded from the folder containing NexusUploader.exe.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent

ENV_FILE = BASE_DIR / ".env"
load_dotenv(ENV_FILE)

# Nexus API base (no trailing slash)
NEXUS_BASE_URL = os.getenv("NEXUS_BASE_URL", "https://api-nexus.laboredge.com").rstrip("/")

# --- Authentication -------------------------------------------------------
# NEXUS_AUTH_METHOD: "password" | "client_credentials" | "static"
#   password           -> POST NEXUS_TOKEN_URL with username/password (+ client creds if set)
#   client_credentials -> POST NEXUS_TOKEN_URL with client id/secret only
#   static             -> use NEXUS_STATIC_TOKEN as-is (paste a JWT you obtained elsewhere)
NEXUS_AUTH_METHOD = os.getenv("NEXUS_AUTH_METHOD", "static").strip().lower()
NEXUS_TOKEN_URL = os.getenv("NEXUS_TOKEN_URL", "").strip()
# "form" (application/x-www-form-urlencoded) or "json" body for the token request
NEXUS_TOKEN_PAYLOAD_STYLE = os.getenv("NEXUS_TOKEN_PAYLOAD_STYLE", "form").strip().lower()

NEXUS_CLIENT_ID = os.getenv("NEXUS_CLIENT_ID", "").strip()
NEXUS_CLIENT_SECRET = os.getenv("NEXUS_CLIENT_SECRET", "").strip()
NEXUS_USERNAME = os.getenv("NEXUS_USERNAME", "").strip()
NEXUS_PASSWORD = os.getenv("NEXUS_PASSWORD", "").strip()
NEXUS_STATIC_TOKEN = os.getenv("NEXUS_STATIC_TOKEN", "").strip()
# LaborEdge issues an organizationCode alongside the API user; sent with the
# token request (as a body field, and as a header for gateways that want it there).
NEXUS_ORG_CODE = os.getenv("NEXUS_ORG_CODE", "").strip()

# The token endpoint expects a fixed OAuth client via HTTP Basic auth. Default
# is the PRODUCTION client from LaborEdge's Job Board API v3.2 doc
# (base64 of "nexus:..."). The older UAT client was "dm1zOnZtc1NlY3JldCMk"
# (vms:vmsSecret#$). Override via NEXUS_TOKEN_BASIC if LaborEdge rotates it.
NEXUS_TOKEN_BASIC = os.getenv("NEXUS_TOKEN_BASIC", "bmV4dXM6NXM6Nn5EcEhaelcmVFoj").strip()

# Optional: pin the "Candidate Resume" document type id instead of auto-discovering
# it via GET /master/documenttypes.
NEXUS_RESUME_DOC_TYPE_ID = os.getenv("NEXUS_RESUME_DOC_TYPE_ID", "").strip()

# Nexus's supported profession/offering/specialty combinations. The source
# sheet can sit beside the app or in the user's Downloads folder; deployments
# can point NEXUS_TAXONOMY_CSV at their own copy.
_taxonomy_name = "Adhoc-automation-data (5).csv"
_taxonomy_override = os.getenv("NEXUS_TAXONOMY_CSV", "").strip()
if _taxonomy_override:
    NEXUS_TAXONOMY_CSV = Path(_taxonomy_override).expanduser()
else:
    _taxonomy_candidates = (
        BASE_DIR / _taxonomy_name,
        Path.home() / "Downloads" / _taxonomy_name,
    )
    NEXUS_TAXONOMY_CSV = next(
        (path for path in _taxonomy_candidates if path.is_file()),
        _taxonomy_candidates[0],
    )

# --- Admin-set candidate defaults -----------------------------------------
# JSON merged into every webhook profileData so end users don't have to know
# Nexus master-data IDs. Example:
#   NEXUS_DEFAULT_PROFILE={"professionId":123,"specialtyId":456,"stateId":1,
#                          "referralSourceId":2,"jobTypeIds":["TRAVEL"]}
# Anything the user sets in the UI overrides these per batch.
try:
    NEXUS_DEFAULT_PROFILE = json.loads(os.getenv("NEXUS_DEFAULT_PROFILE", "") or "{}")
    if not isinstance(NEXUS_DEFAULT_PROFILE, dict):
        raise ValueError("must be a JSON object")
except ValueError as e:
    logging.getLogger("config").warning("Ignoring invalid NEXUS_DEFAULT_PROFILE: %s", e)
    NEXUS_DEFAULT_PROFILE = {}

# --- Resume field extraction ----------------------------------------------
# CLAUDE_EXTRACT: "auto" (default — call Claude only when local heuristics
# can't fill every field, or the PDF is a scanned image), "always", "never".
# Claude needs Anthropic credentials (ANTHROPIC_API_KEY env var or an
# `ant auth login` profile); without them extraction is heuristics-only.
CLAUDE_EXTRACT = os.getenv("CLAUDE_EXTRACT", "auto").strip().lower()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5").strip()

# --- Upload limits (from the Nexus guide) ---------------------------------
MAX_FILE_BYTES = 10 * 1024 * 1024        # 10 MB per resume (webhook limit)
MAX_REQUEST_BYTES = 18 * 1024 * 1024     # keep batches under the 20 MB request cap

ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx", ".rtf", ".txt"}

APP_PORT = int(os.getenv("APP_PORT", "8020"))

# --- Login / sessions -----------------------------------------------------
# SECRET_KEY signs the session cookie. Set a fixed value in production, or
# every restart invalidates logins. A random one is used if unset.
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
SECRET_KEY_PROVIDED = bool(SECRET_KEY)
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)

# --- User store -----------------------------------------------------------
# If MONGODB_URI is set, users live in MongoDB (durable — recommended for
# Render, e.g. a free MongoDB Atlas cluster). Otherwise they live in a local
# users.json file (fine for local dev / a persistent disk).
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_DB = os.getenv("MONGODB_DB", "nexus_uploader").strip()

# Bootstrap admin: seeded into the store on startup if that username is absent,
# so there is always an admin who can add users — even on a fresh database.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ADMIN_NAME = os.getenv("ADMIN_NAME", "Administrator").strip()

# Optional legacy/seed logins as a JSON object; imported into the store on
# startup if missing. Values may be a password string or {"name","password"}.
# Passwords may be plaintext or a pbkdf2 hash.
#   APP_USERS={"alice":{"name":"Alice Smith","password":"pbkdf2_sha256$..."}}
APP_USERS_RAW = os.getenv("APP_USERS", "").strip()

# Session lifetime in seconds (default 12 hours).
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE", str(12 * 3600)))

# Mark the session cookie Secure (HTTPS-only). Leave false for local http;
# set COOKIE_SECURE=true in production (Render serves over HTTPS).
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").strip().lower() == "true"

# --- Audit log ------------------------------------------------------------
# Who uploaded what. Always logged to stdout; also appended here (best-effort;
# on Render's free tier the file is ephemeral, but stdout logs persist).
AUDIT_LOG_PATH = os.getenv("AUDIT_LOG_PATH", str(BASE_DIR / "audit.log")).strip()

# Stamp the uploader's name into Nexus (document notes + candidate note).
UPLOADER_ATTRIBUTION = os.getenv("UPLOADER_ATTRIBUTION", "true").strip().lower() != "false"
