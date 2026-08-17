"""Verify Nexus authentication and show your agency's master-data IDs.

Reads credentials from .env. Read-only — makes no changes in Nexus.

Run: python tools/check_auth.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.nexus_client import client, NexusError


def main():
    print("Authenticating against", os.environ.get("NEXUS_TOKEN_URL", "") or "(token URL from .env)")
    try:
        token = client._get_token()
    except NexusError as e:
        print("\nAUTH FAILED:", e)
        if e.status_code:
            print("  HTTP", e.status_code)
        if e.body:
            print("  body:", e.body[:500])
        return 1

    print(f"OK — got a {len(token)}-char access token\n")

    for name in ("professions", "specialties", "states", "referralsources",
                 "candidatetypes", "shifts", "documenttypes"):
        try:
            rows = client.get_master(name)
        except NexusError as e:
            print(f"{name}: FAILED ({e})")
            continue
        if not isinstance(rows, list):
            print(f"{name}: unexpected response {str(rows)[:120]}")
            continue
        print(f"--- {name} ({len(rows)} entries) ---")
        for row in rows[:15]:
            rid = row.get("id", row.get("value"))
            label = row.get("name", row.get("label"))
            active = row.get("active")
            suffix = "" if active in (None, True) else "  (inactive)"
            print(f"  {rid:>8}  {label}{suffix}")
        if len(rows) > 15:
            print(f"  ... and {len(rows) - 15} more")
        print()

    try:
        print("Candidate Resume document type id:",
              client.resolve_resume_doc_type_id())
    except NexusError as e:
        print("Could not resolve the Candidate Resume document type:", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
