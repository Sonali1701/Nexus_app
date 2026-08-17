"""Find the LaborEdge OAuth token endpoint for your credentials.

The credentials email from LaborEdge gives username / password /
organizationCode / grant_type but not the token URL. This tries the plausible
endpoints (reading credentials from .env — nothing is hardcoded) and prints
the one that works, ready to paste into NEXUS_TOKEN_URL.

Run: python tools/find_token_url.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from app import config

CANDIDATE_PATHS = [
    "/api/api-integration/v1/oauth/token",
    "/api/api-integration/v1/token",
    "/api/api-integration/oauth/token",
    "/api/api-integration/v1/authenticate",
    "/api/api-integration/v1/login",
]

TOKEN_KEYS = ("access_token", "accessToken", "token", "jwt", "id_token")


def token_from(data):
    if not isinstance(data, dict):
        return None
    for key in TOKEN_KEYS:
        if data.get(key):
            return key
    inner = data.get("data")
    if isinstance(inner, dict):
        for key in TOKEN_KEYS:
            if inner.get(key):
                return f"data.{key}"
    return None


def main():
    if not config.NEXUS_USERNAME or not config.NEXUS_PASSWORD:
        print("Set NEXUS_USERNAME and NEXUS_PASSWORD in .env first.")
        return 1

    payload = {
        "grant_type": "password",
        "username": config.NEXUS_USERNAME,
        "password": config.NEXUS_PASSWORD,
    }
    headers = {}
    if config.NEXUS_ORG_CODE:
        payload["organizationCode"] = config.NEXUS_ORG_CODE
        headers["organizationCode"] = config.NEXUS_ORG_CODE

    print(f"user={config.NEXUS_USERNAME}  org={config.NEXUS_ORG_CODE or '(none)'}")
    print(f"base={config.NEXUS_BASE_URL}\n")

    # Any URL already configured is tried first.
    urls = ([config.NEXUS_TOKEN_URL] if config.NEXUS_TOKEN_URL else []) + \
           [config.NEXUS_BASE_URL + p for p in CANDIDATE_PATHS]

    with httpx.Client(timeout=30.0) as http:
        for url in urls:
            for style in ("form", "json"):
                kwargs = {"json": payload} if style == "json" else {"data": payload}
                try:
                    r = http.post(url, headers=headers, **kwargs)
                except httpx.HTTPError as e:
                    print(f"  ERR   {url} [{style}]: {type(e).__name__}")
                    continue

                note = ""
                if r.status_code < 400:
                    try:
                        key = token_from(r.json())
                    except ValueError:
                        key = None
                    if key:
                        print(f"  {r.status_code}  {url} [{style}]  <-- TOKEN FOUND ({key})")
                        print("\n" + "=" * 70)
                        print("Add these two lines to .env:\n")
                        print(f"NEXUS_TOKEN_URL={url}")
                        print(f"NEXUS_TOKEN_PAYLOAD_STYLE={style}")
                        print("=" * 70)
                        return 0
                    note = "  (2xx but no token field)"
                print(f"  {r.status_code}  {url} [{style}]{note}")
                # Don't keep hammering a route that clearly isn't the endpoint
                if r.status_code in (404, 405):
                    break

    print("\nNone of the candidates worked. Ask LaborEdge support for the exact")
    print("OAuth token endpoint URL, then set NEXUS_TOKEN_URL in .env.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
