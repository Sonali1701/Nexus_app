"""Thin client for the LaborEdge Nexus API-Integration endpoints.

Handles OAuth token acquisition/caching and the three upload-related
endpoints from the "Nexus Resume Parser Guide for External APIs":

  POST /api/api-integration/v1/candidate/webhook/create
  GET  /api/api-integration/v1/master/documenttypes
  POST /api/api-integration/v1/candidates/{id}/upload/documents
"""
import base64
import json
import time
import threading

import httpx

from . import config


class NexusError(Exception):
    """Raised when Nexus returns an error or auth is misconfigured."""

    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _jwt_exp(token: str):
    """Return the exp claim of a JWT, or None if it can't be decoded."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return int(payload["exp"])
    except Exception:
        return None


class NexusClient:
    def __init__(self):
        self._token = None
        self._token_expires_at = 0
        self._refresh_token = None
        self._token_verifier = None
        self._lock = threading.Lock()
        self._resume_doc_type_id = None
        self._master_cache = {}  # name -> (expires_at, data)
        self._http = httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0))

    # --- auth -------------------------------------------------------------

    def _fetch_token(self) -> str:
        method = config.NEXUS_AUTH_METHOD
        if method == "static":
            if not config.NEXUS_STATIC_TOKEN:
                raise NexusError(
                    "NEXUS_AUTH_METHOD is 'static' but NEXUS_STATIC_TOKEN is empty. "
                    "Paste your OAuth API access token into .env."
                )
            return config.NEXUS_STATIC_TOKEN

        if not config.NEXUS_TOKEN_URL:
            raise NexusError(
                "NEXUS_TOKEN_URL is not set. Add your OAuth token endpoint to .env "
                "(from your LaborEdge API credentials sheet), or switch to "
                "NEXUS_AUTH_METHOD=static and paste a token."
            )

        payload = {}
        auth = None
        if method == "password":
            payload = {
                "grant_type": "password",
                "username": config.NEXUS_USERNAME,
                "password": config.NEXUS_PASSWORD,
            }
            if config.NEXUS_CLIENT_ID and config.NEXUS_CLIENT_SECRET:
                auth = (config.NEXUS_CLIENT_ID, config.NEXUS_CLIENT_SECRET)
        elif method == "client_credentials":
            payload = {"grant_type": "client_credentials"}
            auth = (config.NEXUS_CLIENT_ID, config.NEXUS_CLIENT_SECRET)
        else:
            raise NexusError(f"Unknown NEXUS_AUTH_METHOD '{method}'.")

        if config.NEXUS_ORG_CODE:
            payload["organizationCode"] = config.NEXUS_ORG_CODE
        return self._post_token(payload)

    def _post_token(self, payload: dict) -> str:
        """POST the token endpoint and cache the token/refresh material."""
        headers = {}
        auth = None
        if config.NEXUS_CLIENT_ID and config.NEXUS_CLIENT_SECRET:
            auth = (config.NEXUS_CLIENT_ID, config.NEXUS_CLIENT_SECRET)
        elif config.NEXUS_TOKEN_BASIC:
            # LaborEdge publishes a fixed OAuth client for this endpoint
            headers["Authorization"] = f"Basic {config.NEXUS_TOKEN_BASIC}"
        # LaborEdge's token server is inconsistent about where organizationCode
        # belongs — UAT answers "Organization Code must be present in request
        # param" even when it is in the form body — so send it as a query
        # parameter and a header as well as in the body. Harmless duplication.
        params = {}
        if config.NEXUS_ORG_CODE:
            params["organizationCode"] = config.NEXUS_ORG_CODE
            headers["organizationCode"] = config.NEXUS_ORG_CODE

        try:
            if config.NEXUS_TOKEN_PAYLOAD_STYLE == "json":
                resp = self._http.post(config.NEXUS_TOKEN_URL, json=payload,
                                       params=params, auth=auth, headers=headers)
            else:
                resp = self._http.post(config.NEXUS_TOKEN_URL, data=payload,
                                       params=params, auth=auth, headers=headers)
        except httpx.HTTPError as e:
            raise NexusError(f"Could not reach the token endpoint: {e}")

        if resp.status_code >= 400:
            hint = ""
            if "invalid_client" in resp.text:
                hint = (" — the OAuth *client* (HTTP Basic) was rejected. This is "
                        "separate from your API username/password: ask LaborEdge for "
                        "the production client id/secret (or the 'Basic ...' header "
                        "value) and set NEXUS_CLIENT_ID/NEXUS_CLIENT_SECRET in .env.")
            elif "Organization Code" in resp.text:
                hint = (" — check NEXUS_ORG_CODE in .env matches the organizationCode "
                        "LaborEdge gave you, and that it exists in this environment.")
            elif "invalid_grant" in resp.text or "Bad credentials" in resp.text:
                hint = " — NEXUS_USERNAME / NEXUS_PASSWORD were rejected."
            raise NexusError(
                f"Token endpoint returned {resp.status_code}{hint}",
                status_code=resp.status_code,
                body=resp.text[:2000],
            )

        try:
            data = resp.json()
        except ValueError:
            raise NexusError("Token endpoint did not return JSON", body=resp.text[:2000])

        token = None
        # NOTE: LaborEdge's response spells it "acess_token" (their typo) — accept
        # both spellings so this keeps working if they ever fix it.
        for key in ("acess_token", "access_token", "accessToken", "token", "jwt"):
            if isinstance(data, dict) and data.get(key):
                token = data[key]
                break
        if token is None and isinstance(data, dict) and isinstance(data.get("data"), dict):
            inner = data["data"]
            token = inner.get("acess_token") or inner.get("access_token") \
                or inner.get("accessToken")
        if not token:
            raise NexusError(
                "Could not find an access token in the token endpoint response",
                body=json.dumps(data)[:2000],
            )

        if isinstance(data, dict):
            self._refresh_token = data.get("refresh_token")
            self._token_verifier = data.get("tokenVerifier")

        expires_in = data.get("expires_in") if isinstance(data, dict) else None
        if expires_in:
            self._token_expires_at = time.time() + int(expires_in)
        else:
            exp = _jwt_exp(token)
            self._token_expires_at = exp if exp else time.time() + 1500
        return token

    def _refresh(self) -> str | None:
        """Use the one-shot refresh grant; returns None if it isn't possible."""
        if not (self._refresh_token and config.NEXUS_AUTH_METHOD == "password"):
            return None
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        }
        if self._token_verifier:
            payload["tokenVerifier"] = self._token_verifier
        if config.NEXUS_ORG_CODE:
            payload["organizationCode"] = config.NEXUS_ORG_CODE
        try:
            return self._post_token(payload)
        except NexusError:
            # Refresh tokens are single-use; fall back to a full re-auth.
            self._refresh_token = None
            return None

    def _get_token(self, force_refresh=False) -> str:
        with self._lock:
            if force_refresh or not self._token or time.time() > self._token_expires_at - 60:
                # Try the cheap refresh grant before a full re-authentication
                self._token = (self._refresh() if self._token else None) or self._fetch_token()
                if config.NEXUS_AUTH_METHOD == "static":
                    exp = _jwt_exp(self._token)
                    self._token_expires_at = exp if exp else time.time() + 3600
            return self._token

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = config.NEXUS_BASE_URL + path
        token = self._get_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        try:
            resp = self._http.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as e:
            raise NexusError(f"Could not reach Nexus at {url}: {e}")

        # One retry with a fresh token on auth failure (skip for static tokens —
        # refetching would return the same expired token).
        if resp.status_code in (401, 403) and config.NEXUS_AUTH_METHOD != "static":
            headers["Authorization"] = f"Bearer {self._get_token(force_refresh=True)}"
            try:
                resp = self._http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as e:
                raise NexusError(f"Could not reach Nexus at {url}: {e}")
        return resp

    # --- endpoints ---------------------------------------------------------

    # Master data lists (per LaborEdge's Job Board API doc). Each returns
    # [{"id": .., "name": .., "active": ..}] except documenttypes, which uses
    # {"value": .., "label": ..}.
    MASTER_LISTS = {
        "professions": "professions",
        "specialties": "specialties",
        "states": "states",
        "countries": "countries",
        "referralsources": "referralsources",
        "candidatetypes": "candidatetypes",
        "candidatestatuses": "candidatestatuses",
        "recruiters": "recruiters",
        "shifts": "shifts",
        "documenttypes": "documenttypes",
    }

    def get_master(self, name: str):
        """Fetch one master-data list by name (see MASTER_LISTS)."""
        if name not in self.MASTER_LISTS:
            raise NexusError(f"Unknown master list '{name}'")
        resp = self._request(
            "GET", f"/api/api-integration/v1/master/{self.MASTER_LISTS[name]}")
        if resp.status_code >= 400:
            raise NexusError(
                f"GET master/{name} failed with {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text[:2000],
            )
        return resp.json()

    def get_master_cached(self, name: str, ttl: int = 600):
        """get_master with an in-memory TTL cache (master data rarely changes)."""
        now = time.time()
        hit = self._master_cache.get(name)
        if hit and hit[0] > now:
            return hit[1]
        data = self.get_master(name)
        self._master_cache[name] = (now + ttl, data)
        return data

    def get_document_types(self):
        resp = self._request("GET", "/api/api-integration/v1/master/documenttypes")
        if resp.status_code >= 400:
            raise NexusError(
                f"GET documenttypes failed with {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text[:2000],
            )
        return resp.json()

    def resolve_resume_doc_type_id(self):
        """Return the 'Candidate Resume' document type id (cached)."""
        if config.NEXUS_RESUME_DOC_TYPE_ID:
            return int(config.NEXUS_RESUME_DOC_TYPE_ID)
        if self._resume_doc_type_id:
            return self._resume_doc_type_id
        types = self.get_document_types()
        match = None
        for t in types:
            label = str(t.get("label", "")).strip().lower()
            if label == "candidate resume":
                match = t
                break
            if "resume" in label and match is None:
                match = t
        if not match:
            raise NexusError(
                "No 'Candidate Resume' document type found in this agency's ATS. "
                "Is the Nexus Resume Parser enabled? You can also pin an id via "
                "NEXUS_RESUME_DOC_TYPE_ID in .env.",
                body=json.dumps(types)[:2000],
            )
        self._resume_doc_type_id = int(match["value"])
        return self._resume_doc_type_id

    def create_candidate_with_resume(self, profile_data: dict, filename: str,
                                     content: bytes, content_type: str):
        """Webhook Profile Data: create a candidate and queue the resume for parsing."""
        resp = self._request(
            "POST",
            "/api/api-integration/v1/candidate/webhook/create",
            data={"profileData": json.dumps(profile_data)},
            files=[("profile", (filename, content, content_type))],
        )
        return resp

    def search_candidates(self, email: str = "", phone: str = ""):
        """Find an existing candidate by exact email or phone (read-only)."""
        payload = {
            "pagingSortingDetails": {"start": 0, "maxRowsToFetch": 10},
        }
        if email:
            payload["email"] = email
        if phone:
            payload["phone"] = phone
        resp = self._request(
            "POST", "/api/api-integration/v1/candidates/search", json=payload)
        if resp.status_code >= 400:
            raise NexusError(
                f"Candidate duplicate check failed with {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text[:2000],
            )
        try:
            body = resp.json()
        except ValueError:
            raise NexusError("Candidate duplicate check did not return JSON",
                             body=resp.text[:2000])
        return (body.get("records") or []) if isinstance(body, dict) else []

    def create_candidate(self, profile_data: dict):
        """Create one candidate with the documented Candidate API."""
        resp = self._request(
            "POST", "/api/api-integration/v1/candidates", json=profile_data)
        if resp.status_code >= 400:
            raise NexusError(
                f"Candidate creation failed with {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text[:2000],
            )
        return resp

    def patch_candidate(self, candidate_id: int, fields: dict):
        """PATCH a candidate (all fields optional). Used to stamp an uploader note.

        Returns the httpx.Response; caller decides how to treat failures.
        """
        return self._request(
            "PATCH", f"/api/api-integration/v1/candidates/{candidate_id}", json=fields)

    def upload_documents(self, candidate_id: int, docs: list):
        """Upload Multiple Candidate Documents.

        docs: list of dicts {doc_type_id, filename, content, content_type, notes}
        """
        data = {}
        files = []
        for i, d in enumerate(docs):
            data[f"uploadedDocuments[{i}].documentTypeId"] = str(d["doc_type_id"])
            if d.get("notes"):
                data[f"uploadedDocuments[{i}].notes"] = d["notes"]
            files.append((
                f"uploadedDocuments[{i}].document",
                (d["filename"], d["content"], d["content_type"]),
            ))
        resp = self._request(
            "POST",
            f"/api/api-integration/v1/candidates/{candidate_id}/upload/documents",
            data=data,
            files=files,
        )
        return resp


client = NexusClient()
