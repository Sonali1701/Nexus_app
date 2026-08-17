"""Audit trail: who uploaded/parsed what, when, and the result.

Every entry is logged to stdout (persisted in Render's log viewer) and appended
to AUDIT_LOG_PATH (best-effort; ephemeral on Render free tier). A rolling
in-memory buffer backs the in-app Activity view.
"""
import json
import logging
from collections import deque
from datetime import datetime, timezone

from . import config

log = logging.getLogger("audit")
_RECENT = deque(maxlen=500)


def record(user, action, ok, **fields):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user": user or "?",
        "action": action,
        "ok": bool(ok),
    }
    entry.update({k: v for k, v in fields.items() if v not in (None, "")})
    _RECENT.append(entry)
    log.info("AUDIT %s", json.dumps(entry, ensure_ascii=False))
    try:
        with open(config.AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return entry


def recent(limit=200):
    return list(_RECENT)[-limit:][::-1]
