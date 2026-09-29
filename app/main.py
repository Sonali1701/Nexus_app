"""Nexus Resume Uploader — web app for pushing resumes into LaborEdge Nexus."""
import csv
import io
import json
import re
import threading
import time
import uuid
from pathlib import Path

import logging

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware

from . import auth, audit, config, csv_import, resume_extract
from .nexus_client import client, NexusError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

app = FastAPI(title="Nexus Resume Uploader")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Paths reachable without a login (the guard lets everything else require auth).
PUBLIC_PATHS = {"/login", "/logout", "/healthz", "/favicon.ico"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)
    if not request.session.get("user"):
        if path.startswith("/api/"):
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "Please log in.", "authRequired": True},
            )
        return RedirectResponse(url="/login", status_code=302)
    return await call_next(request)


# Added last so it wraps require_login — SessionMiddleware must populate
# request.session before the guard reads it.
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    max_age=config.SESSION_MAX_AGE,
    same_site="lax",
    https_only=config.COOKIE_SECURE,
)

if not config.SECRET_KEY_PROVIDED:
    log.warning("SECRET_KEY not set — using a random key; logins reset on restart.")

try:
    auth.seed()
except Exception as e:  # noqa: BLE001 - never block startup on seeding
    log.error("User seeding failed: %s", e)


def _current_user(request: Request):
    return request.session.get("user") or {}


def _require_admin(request: Request):
    """Return the admin user dict, or a JSONResponse to short-circuit with."""
    user = request.session.get("user") or {}
    if not user.get("is_admin"):
        return None
    return user


def _current_user_name(request: Request):
    return (_current_user(request).get("name")
            or _current_user(request).get("username") or "")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login")
def login_page():
    return FileResponse(TEMPLATES_DIR / "login.html")


@app.post("/login")
async def login_submit(request: Request,
                       username: str = Form(...), password: str = Form(...)):
    user = auth.authenticate(username, password)
    if not user:
        audit.record(username, "login", False, detail="invalid credentials")
        return JSONResponse(status_code=401,
                            content={"ok": False, "error": "Invalid username or password."})
    request.session["user"] = user
    audit.record(user["username"], "login", True)
    return {"ok": True, "user": user}


@app.get("/logout")
def logout(request: Request):
    user = request.session.get("user") or {}
    if user:
        audit.record(user.get("username"), "logout", True)
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


@app.get("/api/activity")
def activity():
    return {"ok": True, "entries": audit.recent()}


# --- admin: user management ------------------------------------------------

@app.get("/admin")
def admin_page(request: Request):
    if not (request.session.get("user") or {}).get("is_admin"):
        return RedirectResponse(url="/", status_code=302)
    return FileResponse(TEMPLATES_DIR / "admin.html")


def _admin_or_403(request: Request):
    if not _require_admin(request):
        return JSONResponse(status_code=403,
                            content={"ok": False, "error": "Admins only."})
    return None


@app.get("/api/admin/users")
def admin_list_users(request: Request):
    guard = _admin_or_403(request)
    if guard:
        return guard
    return {"ok": True, "users": auth.list_users(),
            "you": _current_user(request).get("username")}


@app.post("/api/admin/users")
def admin_add_user(request: Request,
                   username: str = Form(...), name: str = Form(""),
                   password: str = Form(...), is_admin: str = Form("false")):
    guard = _admin_or_403(request)
    if guard:
        return guard
    try:
        user = auth.add_user(username, name, password,
                             is_admin=is_admin.lower() == "true")
    except ValueError as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})
    audit.record(_current_user(request).get("username"), "admin_add_user", True,
                 name=username)
    return {"ok": True, "user": user}


@app.post("/api/admin/users/{username}/password")
def admin_reset_password(request: Request, username: str, password: str = Form(...)):
    guard = _admin_or_403(request)
    if guard:
        return guard
    try:
        auth.set_password(username, password)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})
    audit.record(_current_user(request).get("username"), "admin_reset_password", True,
                 name=username)
    return {"ok": True}


@app.post("/api/admin/users/{username}/role")
def admin_set_role(request: Request, username: str, is_admin: str = Form(...)):
    guard = _admin_or_403(request)
    if guard:
        return guard
    try:
        user = auth.set_admin(username, is_admin.lower() == "true")
    except ValueError as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})
    audit.record(_current_user(request).get("username"), "admin_set_role", True,
                 name=username, detail=("admin" if is_admin.lower() == "true" else "member"))
    return {"ok": True, "user": user}


@app.delete("/api/admin/users/{username}")
def admin_delete_user(request: Request, username: str):
    guard = _admin_or_403(request)
    if guard:
        return guard
    if username.strip().lower() == (_current_user(request).get("username") or "").lower():
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "You cannot delete your own account."})
    try:
        auth.delete_user(username)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})
    audit.record(_current_user(request).get("username"), "admin_delete_user", True,
                 name=username)
    return {"ok": True}

_CSV_JOBS = {}
_CSV_JOB_LOCK = threading.Lock()


def _nexus_error_response(e: NexusError):
    return JSONResponse(
        status_code=502,
        content={
            "ok": False,
            "error": str(e),
            "nexusStatus": e.status_code,
            "nexusBody": e.body,
        },
    )


@app.get("/")
def index():
    return FileResponse(TEMPLATES_DIR / "index.html")


@app.get("/api/status")
def status(request: Request):
    """Config summary for the UI banner (no secrets)."""
    method = config.NEXUS_AUTH_METHOD
    configured = (
        (method == "static" and bool(config.NEXUS_STATIC_TOKEN))
        or (method == "password" and bool(config.NEXUS_TOKEN_URL and config.NEXUS_USERNAME))
        or (method == "client_credentials" and bool(config.NEXUS_TOKEN_URL and config.NEXUS_CLIENT_ID))
    )
    return {
        "baseUrl": config.NEXUS_BASE_URL,
        "authMethod": method,
        "configured": configured,
        "maxFileMb": config.MAX_FILE_BYTES // (1024 * 1024),
        "claudeExtract": config.CLAUDE_EXTRACT,
        "claudeAvailable": resume_extract.claude_available(),
        "profileDefaults": config.NEXUS_DEFAULT_PROFILE,
        "user": _current_user(request),
    }


@app.post("/api/extract")
async def extract(files: list[UploadFile] = File(...)):
    """Pull name/email/phone out of each resume for row prefill."""
    results = []
    for f in files:
        content = await f.read()
        if len(content) > config.MAX_FILE_BYTES:
            results.append({"filename": f.filename, "error": "over the 10 MB limit",
                            "firstName": "", "lastName": "", "email": "", "phone": "",
                            "missing": list(resume_extract.FIELDS), "source": "none"})
            continue
        results.append(await run_in_threadpool(resume_extract.extract_fields,
                                               f.filename, content))
    return {"results": results, "claudeEnabled": resume_extract.claude_available()}


def _active(rows):
    return [r for r in rows if r.get("active", True) is not False]


def _row_id(row):
    return row.get("id", row.get("value"))


def _default_master_id(name, predicate, description):
    """Resolve an agency-specific default from live Nexus master data."""
    rows = _active(client.get_master_cached(name))
    match = next((row for row in rows if predicate(row)), None)
    if not match or _row_id(match) is None:
        raise NexusError(
            f"Could not resolve the default {description} from Nexus master data. "
            f"Set it in NEXUS_DEFAULT_PROFILE in .env."
        )
    return int(_row_id(match))


def _prepare_candidate_profile(profile):
    """Add Nexus's canonical candidate fields and validate before uploading.

    The resume webhook accepts the singular fields described by the parser
    guide, while candidate validation also expects the canonical fields used
    by the Candidate API. Keep both representations for compatibility.
    """
    profile = dict(profile)

    # Row-level webhook fields win over any stale values left in Advanced JSON.
    email = str(profile.get("email") or profile.get("primaryEmail") or "").strip()
    if email:
        profile["email"] = email
        profile["primaryEmail"] = email
    else:
        profile.pop("email", None)
        profile.pop("primaryEmail", None)
    phone = str(profile.get("phone") or "").strip()
    if phone:
        profile["phone"] = phone
    else:
        profile.pop("phone", None)

    profession_id = profile.get("professionId")
    profession_ids = profile.get("professionIds")
    if profession_id:
        profile["professionIds"] = [int(profession_id)]
    elif profession_ids and not profession_id:
        profile["professionId"] = int(profession_ids[0])

    specialty_id = profile.get("specialtyId") or profile.get("primarySpecialtyId")
    specialty_ids = profile.get("specialtyIds")
    if profile.get("specialtyId"):
        specialty_id = int(profile["specialtyId"])
        profile["specialtyId"] = specialty_id
        profile["primarySpecialtyId"] = specialty_id
        profile["specialtyIds"] = [specialty_id]
    elif specialty_id:
        specialty_id = int(specialty_id)
        profile["specialtyId"] = specialty_id
        profile["primarySpecialtyId"] = specialty_id
        profile["specialtyIds"] = [specialty_id]
    elif specialty_ids:
        profile["specialtyId"] = int(specialty_ids[0])
        profile.setdefault("primarySpecialtyId", int(specialty_ids[0]))

    # These IDs vary by agency, so discover safe defaults instead of baking in
    # the IDs from one Nexus tenant. Admin-provided .env values still win.
    if not profile.get("statusId"):
        profile["statusId"] = _default_master_id(
            "candidatestatuses",
            lambda row: str(row.get("code", "")).upper() == "PROSPECT"
            and str(row.get("module", "CANDIDATE")).upper() == "CANDIDATE",
            "Prospect candidate status",
        )
    if not profile.get("countryId"):
        profile["countryId"] = _default_master_id(
            "countries",
            lambda row: str(row.get("code", "")).upper() == "USA"
            or str(row.get("name", "")).strip().lower() == "united states",
            "United States country",
        )
    if not profile.get("referralSourceId"):
        sources = _active(client.get_master_cached("referralsources"))
        if len(sources) == 1 and _row_id(sources[0]) is not None:
            profile["referralSourceId"] = int(_row_id(sources[0]))

    errors = []
    for key, label in (("firstName", "first name"), ("lastName", "last name")):
        if not str(profile.get(key) or "").strip():
            errors.append(f"{label} is required")
    if not email and not phone:
        errors.append("email or phone is required")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        errors.append("email format is invalid")
    phone_digits = re.sub(r"\D", "", phone)
    if phone and not 10 <= len(phone_digits) <= 15:
        errors.append("phone must contain 10 to 15 digits")

    if not profile.get("jobId"):
        if not profile.get("professionId") or not profile.get("professionIds"):
            errors.append("profession is required when job ID is not supplied")
        if not profile.get("specialtyId") or not profile.get("specialtyIds") \
                or not profile.get("primarySpecialtyId"):
            errors.append("specialty is required when job ID is not supplied")
    for key, label in (("stateId", "state"), ("countryId", "country"),
                       ("statusId", "candidate status"),
                       ("referralSourceId", "referral source")):
        if not profile.get(key):
            errors.append(f"{label} is required")

    job_types = profile.get("jobTypeIds")
    allowed_job_types = {"LOCAL", "PERDIEM", "PERM", "TRAVEL"}
    if not isinstance(job_types, list) or not job_types:
        errors.append("at least one job type is required")
    elif any(str(value).upper() not in allowed_job_types for value in job_types):
        errors.append("job types may only be LOCAL, PERDIEM, PERM, or TRAVEL")

    if errors:
        raise ValueError("; ".join(errors))
    return profile


def _master_match_id(name, predicate, description):
    rows = _active(client.get_master_cached(name))
    match = next((row for row in rows if predicate(row)), None)
    value = None if match is None else match.get(
        "specialtyId", match.get("id", match.get("value")))
    if value is None:
        raise NexusError(f"Could not resolve {description} from Nexus master data")
    return int(value)


def _csv_candidate_defaults(
    job_type, state_codes=("KY",), profession_names=None,
):
    """Resolve row-level states and detected professions from Nexus masters."""
    job_type = str(job_type or "").strip().upper()
    if job_type not in {"LOCAL", "PERDIEM", "PERM", "TRAVEL"}:
        raise ValueError("Choose one job type: LOCAL, PERDIEM, PERM, or TRAVEL")

    # The license CSV remains RN-specific and these values are also the
    # backward-compatible fallback for internal callers.
    profession_id = _master_match_id(
        "professions",
        lambda row: str(row.get("name", "")).strip().upper() == "RN",
        "the RN profession",
    )
    specialty_id = _master_match_id(
        "specialties",
        lambda row: int(row.get("professionId") or 0) == profession_id
        and str(row.get("name", "")).strip().lower() == "unknown",
        "the Unknown RN specialty",
    )

    profession_mappings = {}
    requested_professions = sorted({
        str(name).strip() for name in (profession_names or ()) if name
    })
    if requested_professions:
        professions = _active(client.get_master_cached("professions"))
        specialties = _active(client.get_master_cached("specialties"))
        profession_by_name = {
            str(row.get("name", "")).strip().lower(): row for row in professions
        }
        unknown_profession = profession_by_name.get("unknown")
        if not unknown_profession:
            raise NexusError("Could not resolve the Unknown profession from Nexus")
        unknown_profession_id = int(_row_id(unknown_profession))

        def generic_specialty(target_profession_id):
            rows = [
                row for row in specialties
                if int(row.get("professionId") or 0) == target_profession_id
            ]
            for preferred_name in ("unknown", "other", "general"):
                match = next((
                    row for row in rows
                    if str(row.get("name", "")).strip().lower() == preferred_name
                ), None)
                if match:
                    return match
            return None

        unknown_specialty = generic_specialty(unknown_profession_id)
        if not unknown_specialty:
            raise NexusError("Could not resolve the Unknown specialty from Nexus")

        for detected_name in requested_professions:
            profession = profession_by_name.get(detected_name.lower())
            if not profession:
                raise NexusError(
                    f"Could not resolve detected profession {detected_name} from Nexus")
            detected_id = int(_row_id(profession))
            specialty = generic_specialty(detected_id)
            fallback = specialty is None
            mapped_profession = unknown_profession if fallback else profession
            mapped_specialty = unknown_specialty if fallback else specialty
            profession_mappings[detected_name] = {
                "professionId": int(_row_id(mapped_profession)),
                "specialtyId": int(mapped_specialty.get(
                    "specialtyId", _row_id(mapped_specialty))),
                "professionName": str(mapped_profession.get("name", "")).strip(),
                "specialtyName": str(mapped_specialty.get("name", "")).strip(),
                "usedUnknownFallback": fallback,
            }
    requested_states = sorted({str(code).strip().upper() for code in state_codes if code})
    if not requested_states:
        raise ValueError("No candidate states were found in the accepted CSV rows")
    available_states = {
        str(row.get("code", "")).strip().upper(): int(_row_id(row))
        for row in _active(client.get_master_cached("states"))
        if str(row.get("code", "")).strip() and _row_id(row) is not None
    }
    missing_states = [code for code in requested_states if code not in available_states]
    if missing_states:
        raise NexusError(
            "Could not resolve these candidate states from Nexus master data: "
            + ", ".join(missing_states)
        )
    state_ids = {code: available_states[code] for code in requested_states}
    status_id = _default_master_id(
        "candidatestatuses",
        lambda row: str(row.get("code", "")).upper() == "PROSPECT"
        and str(row.get("module", "CANDIDATE")).upper() == "CANDIDATE",
        "Prospect candidate status",
    )
    country_id = _default_master_id(
        "countries",
        lambda row: str(row.get("code", "")).upper() == "USA"
        or str(row.get("name", "")).strip().lower() == "united states",
        "United States country",
    )
    referral_id = config.NEXUS_DEFAULT_PROFILE.get("referralSourceId")
    if not referral_id:
        sources = _active(client.get_master_cached("referralsources"))
        if len(sources) == 1:
            referral_id = _row_id(sources[0])
    if not referral_id:
        raise NexusError(
            "Could not resolve a referral source; set referralSourceId in "
            "NEXUS_DEFAULT_PROFILE in .env"
        )
    defaults = {
        "professionId": profession_id,
        "specialtyId": specialty_id,
        "professionMappings": profession_mappings,
        "stateId": state_ids.get("KY", next(iter(state_ids.values()))),
        "stateIds": state_ids,
        "statusId": status_id,
        "countryId": country_id,
        "referralSourceId": int(referral_id),
        "jobTypeIds": [job_type],
    }
    if job_type == "PERDIEM":
        defaults["candidatePRNStatusId"] = _default_master_id(
            "candidatestatuses",
            lambda row: str(row.get("code", "")).upper() == "PROSPECT"
            and str(row.get("module", "")).upper() == "CANDIDATE_PRN",
            "Prospect PRN candidate status",
        )
    return defaults


def _job_snapshot(job_id):
    with _CSV_JOB_LOCK:
        job = _CSV_JOBS.get(job_id)
        if not job:
            return None
        snapshot = {key: value for key, value in job.items() if key != "results"}
        snapshot["recentResults"] = list(job["results"][-50:])
        return snapshot


def _run_csv_import(job_id, records, defaults, uploader=""):
    with _CSV_JOB_LOCK:
        _CSV_JOBS[job_id]["status"] = "running"
        _CSV_JOBS[job_id]["startedAt"] = time.time()

    for record in records:
        with _CSV_JOB_LOCK:
            job = _CSV_JOBS[job_id]
            if job["cancelRequested"]:
                job["status"] = "cancelled"
                job["finishedAt"] = time.time()
                return
            job["current"] = f"{record['firstName']} {record['lastName']}"

        outcome = {"sourceFile": record.get("sourceFile", ""),
                   "sourceRow": record["sourceRow"],
                   "stateCode": record.get("stateCode", ""),
                   "name": f"{record['firstName']} {record['lastName']}"}
        try:
            # Search independently so a record with a changed email but the
            # same phone (or vice versa) is still treated as an existing candidate.
            matches = client.search_candidates(email=record["email"])
            if not matches:
                matches = client.search_candidates(phone=record["phone"])
            if matches:
                match = matches[0]
                outcome.update({
                    "status": "skipped",
                    "reason": "existing Nexus candidate matched email or phone",
                    "candidateId": match.get("candidateId", match.get("id")),
                })
                counter = "skipped"
            else:
                payload = csv_import.candidate_payload(record, defaults)
                if uploader and config.UPLOADER_ATTRIBUTION:
                    payload.setdefault(
                        "availabilityLogNotes",
                        f"Imported via Bulk Parser by {uploader}")
                response = client.create_candidate(payload)
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                new_id = body.get("id", body.get("Id")) if isinstance(body, dict) else None
                outcome.update({"status": "created", "candidateId": new_id})
                counter = "created"
                audit.record(uploader, "csv_create_candidate", True,
                             candidateId=new_id, name=outcome["name"],
                             sourceRow=record.get("sourceRow"))
        except NexusError as exc:
            detail = exc.body
            if not isinstance(detail, str):
                detail = json.dumps(detail)
            outcome.update({
                "status": "failed",
                "reason": (detail or str(exc))[:500],
            })
            counter = "failed"
        except Exception as exc:
            outcome.update({"status": "failed", "reason": str(exc)[:500]})
            counter = "failed"

        with _CSV_JOB_LOCK:
            job = _CSV_JOBS[job_id]
            job[counter] += 1
            job["processed"] += 1
            job["results"].append(outcome)

    with _CSV_JOB_LOCK:
        job = _CSV_JOBS[job_id]
        job["status"] = "completed"
        job["current"] = ""
        job["finishedAt"] = time.time()


@app.get("/api/master")
def master():
    """Master data for the candidate-detail dropdowns (professions, specialties, states)."""
    try:
        professions = client.get_master_cached("professions")
        specialties = client.get_master_cached("specialties")
        states = client.get_master_cached("states")
    except NexusError as e:
        return _nexus_error_response(e)

    prof = sorted(
        ({"id": p.get("id"), "name": p.get("name")} for p in _active(professions) if p.get("id")),
        key=lambda x: (x["name"] or "").lower(),
    )
    spec = sorted(
        ({"id": s.get("specialtyId", s.get("id")), "name": s.get("name"),
          "professionId": s.get("professionId")}
         for s in _active(specialties) if s.get("specialtyId", s.get("id"))),
        key=lambda x: (x["name"] or "").lower(),
    )
    st = sorted(
        ({"id": s.get("id"), "name": s.get("name"), "code": s.get("code"),
          "countryId": s.get("countryId")}
         for s in _active(states) if s.get("id")),
        key=lambda x: (x["name"] or "").lower(),
    )

    # The support sheet's populated Old* columns define valid combinations.
    # Resolve its labels to the IDs from this agency's live Nexus masters.
    taxonomy = []
    taxonomy_error = None
    try:
        with config.NEXUS_TAXONOMY_CSV.open(
            "r", encoding="utf-8-sig", newline=""
        ) as sheet:
            reader = csv.DictReader(sheet)
            required = {"Old Profession", "Old Offering", "Old Specialty"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError("The support sheet is missing required Old* columns")

            profession_by_name = {
                str(row.get("name", "")).strip().casefold(): row
                for row in _active(professions)
                if row.get("id") and row.get("name")
            }
            specialty_by_profession_and_name = {
                (str(row.get("professionId", "")).strip(),
                 str(row.get("name", "")).strip().casefold()): row
                for row in _active(specialties)
                if row.get("name")
            }
            seen = set()
            for source in reader:
                profession_name = str(source.get("Old Profession") or "").strip()
                offering_name = str(source.get("Old Offering") or "").strip()
                sub_offering_name = str(source.get("Old Sub Offering") or "").strip()
                specialty_name = str(source.get("Old Specialty") or "").strip()
                profession = profession_by_name.get(profession_name.casefold())
                if not profession or not offering_name or not specialty_name:
                    continue
                profession_id = str(profession["id"])
                specialty = specialty_by_profession_and_name.get(
                    (profession_id, specialty_name.casefold())
                )
                if not specialty:
                    continue
                specialty_id = specialty.get("specialtyId", specialty.get("id"))
                if specialty_id is None:
                    continue
                key = (profession_id, offering_name.casefold(),
                       sub_offering_name.casefold(), str(specialty_id))
                if key in seen:
                    continue
                seen.add(key)
                taxonomy.append({
                    "professionId": int(profession_id),
                    "professionName": str(profession.get("name", "")).strip(),
                    "offeringName": offering_name,
                    "subOfferingName": sub_offering_name,
                    "specialtyId": int(specialty_id),
                    "specialtyName": str(specialty.get("name", "")).strip(),
                })
        if not taxonomy:
            taxonomy_error = (
                "No support-sheet combinations matched the active Nexus profession and specialty lists."
            )
    except (OSError, csv.Error, ValueError) as e:
        taxonomy_error = f"Could not load the Nexus support sheet: {e}"

    return {
        "ok": True, "professions": prof, "specialties": spec, "states": st,
        "taxonomy": taxonomy, "taxonomyError": taxonomy_error,
    }


@app.get("/api/document-types")
def document_types():
    """Proxy to Nexus master documenttypes + the resolved resume type id."""
    try:
        types = client.get_document_types()
        try:
            resume_id = client.resolve_resume_doc_type_id()
        except NexusError:
            resume_id = None
        return {"ok": True, "types": types, "resumeDocTypeId": resume_id}
    except NexusError as e:
        return _nexus_error_response(e)


def _parse_column_mappings(raw):
    try:
        mappings = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise csv_import.CSVImportError("Column mapping is not valid JSON") from exc
    if not isinstance(mappings, dict):
        raise csv_import.CSVImportError("Column mapping must be an object")
    return mappings


@app.post("/api/csv/inspect")
async def inspect_candidate_files(file: list[UploadFile] = File(...)):
    """Read spreadsheet headers and suggest canonical candidate mappings."""
    try:
        named_contents = [
            (item.filename or f"file-{index}", await item.read())
            for index, item in enumerate(file, start=1)
        ]
        inspection = csv_import.inspect_many(named_contents)
    except csv_import.CSVImportError as exc:
        return JSONResponse(
            status_code=400, content={"ok": False, "error": str(exc)})
    return {"ok": True, **inspection}


@app.post("/api/csv/preview")
async def preview_candidate_csv(
    file: list[UploadFile] = File(...),
    column_mapping: str = Form("{}"),
):
    """Validate mapped CSV/Excel files and report the safe import subset."""
    try:
        named_contents = [
            (item.filename or f"file-{index}", await item.read())
            for index, item in enumerate(file, start=1)
        ]
        mappings = _parse_column_mappings(column_mapping)
        summary, records = csv_import.parse_and_filter_many(
            named_contents, mappings=mappings)
    except csv_import.CSVImportError as exc:
        return JSONResponse(
            status_code=400, content={"ok": False, "error": str(exc)})
    sample_keys = ("sourceType", "sourceFile", "sourceRow", "stateCode",
                   "professionName", "professionSource", "firstName", "lastName",
                   "email", "emailType", "phone", "phoneSource", "licenseNumber",
                   "headline", "likelihood")
    return {
        "ok": True,
        "summary": summary,
        "sample": [{key: row.get(key) for key in sample_keys} for row in records[:25]],
    }


@app.post("/api/csv/import")
async def start_candidate_csv_import(
    request: Request,
    background_tasks: BackgroundTasks,
    file: list[UploadFile] = File(...),
    job_type: str = Form(...),
    confirmed: str = Form("false"),
    column_mapping: str = Form("{}"),
):
    """Start a confirmed, de-duplicated candidate import in the background."""
    if confirmed.lower() != "true":
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Confirm the bulk import first"},
    )
    try:
        named_contents = [
            (item.filename or f"file-{index}", await item.read())
            for index, item in enumerate(file, start=1)
        ]
        mappings = _parse_column_mappings(column_mapping)
        summary, records = csv_import.parse_and_filter_many(
            named_contents, mappings=mappings)
        if not records:
            raise csv_import.CSVImportError("No rows passed the safe-import filter")
        detected_professions = {
            record.get("professionName") for record in records
            if record.get("professionName")
        }
        defaults = _csv_candidate_defaults(
            job_type, {record["stateCode"] for record in records},
            detected_professions or None,
        )
    except csv_import.CSVImportError as exc:
        return JSONResponse(
            status_code=400, content={"ok": False, "error": str(exc)})
    except ValueError as exc:
        return JSONResponse(
            status_code=400, content={"ok": False, "error": str(exc)})
    except NexusError as exc:
        return _nexus_error_response(exc)
    job_id = uuid.uuid4().hex
    with _CSV_JOB_LOCK:
        _CSV_JOBS[job_id] = {
            "jobId": job_id,
            "status": "queued",
            "total": len(records),
            "processed": 0,
            "created": 0,
            "skipped": 0,
            "failed": 0,
            "current": "",
            "cancelRequested": False,
            "createdAt": time.time(),
            "summary": summary,
            "mapping": defaults,
            "results": [],
        }
    background_tasks.add_task(_run_csv_import, job_id, records, defaults,
                              _current_user_name(request))
    return {"ok": True, "job": _job_snapshot(job_id)}


@app.get("/api/csv/import/{job_id}")
def candidate_csv_import_status(job_id: str):
    snapshot = _job_snapshot(job_id)
    if not snapshot:
        return JSONResponse(
            status_code=404, content={"ok": False, "error": "Import job not found"})
    return {"ok": True, "job": snapshot}


@app.get("/api/csv/import/{job_id}/report")
def candidate_csv_import_report(job_id: str):
    with _CSV_JOB_LOCK:
        job = _CSV_JOBS.get(job_id)
        if not job:
            return JSONResponse(
                status_code=404, content={"ok": False, "error": "Import job not found"})
        results = list(job["results"])
    output = io.StringIO(newline="")
    fields = ("sourceFile", "sourceRow", "stateCode", "name", "status",
              "candidateId", "reason")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(results)
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="nexus-import-{job_id[:8]}.csv"'},
    )


@app.post("/api/csv/import/{job_id}/cancel")
def cancel_candidate_csv_import(job_id: str):
    with _CSV_JOB_LOCK:
        job = _CSV_JOBS.get(job_id)
        if not job:
            return JSONResponse(
                status_code=404, content={"ok": False, "error": "Import job not found"})
        if job["status"] in {"queued", "running"}:
            job["cancelRequested"] = True
    return {"ok": True}


@app.post("/api/upload/new-candidate")
async def upload_new_candidate(
    request: Request,
    profile_data: str = Form(...),
    file: UploadFile = File(...),
):
    """Create a candidate via the webhook API with the resume attached."""
    uploader = _current_user_name(request)
    try:
        profile = json.loads(profile_data)
    except ValueError:
        return JSONResponse(status_code=400, content={"ok": False, "error": "profile_data is not valid JSON"})

    # Admin defaults fill anything the UI did not provide. Then add the
    # canonical Candidate API aliases/required defaults used by Nexus's
    # candidate-profile validator.
    try:
        profile = _prepare_candidate_profile({**config.NEXUS_DEFAULT_PROFILE, **profile})
    except NexusError as e:
        return _nexus_error_response(e)
    except (TypeError, ValueError, IndexError) as e:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": f"Candidate profile is incomplete: {e}"},
        )

    content = await file.read()
    if len(content) > config.MAX_FILE_BYTES:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": f"{file.filename} is over the 10 MB Nexus limit"},
        )

    try:
        resp = client.create_candidate_with_resume(
            profile, file.filename, content,
            file.content_type or "application/octet-stream",
        )
    except NexusError as e:
        audit.record(uploader, "create_candidate", False,
                     filename=file.filename, detail=str(e))
        return _nexus_error_response(e)

    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:2000]

    ok = resp.status_code < 400
    candidate_id = body.get("id", body.get("Id")) if isinstance(body, dict) else None

    # Best-effort: stamp who uploaded into a Nexus-visible candidate note.
    # Never let this break the successful create — it's supplementary to the
    # authoritative app audit log below.
    if ok and candidate_id and uploader and config.UPLOADER_ATTRIBUTION:
        note = f"Resume uploaded via Bulk Parser by {uploader}"
        try:
            pr = client.patch_candidate(candidate_id, {"availabilityLogNotes": note})
            if pr.status_code >= 400:
                log.info("uploader note PATCH returned %s for candidate %s",
                         pr.status_code, candidate_id)
        except Exception as e:  # noqa: BLE001 - supplementary, must not fail upload
            log.info("uploader note PATCH failed for candidate %s: %s", candidate_id, e)

    audit.record(uploader, "create_candidate", ok,
                 filename=file.filename, candidateId=candidate_id,
                 detail=None if ok else (json.dumps(body)[:300] if not isinstance(body, str) else body[:300]))

    return {
        "ok": ok,
        "nexusStatus": resp.status_code,
        "nexusBody": body,
        "candidateId": candidate_id,
    }


@app.post("/api/upload/existing/{candidate_id}")
async def upload_existing_candidate(
    request: Request,
    candidate_id: int,
    files: list[UploadFile] = File(...),
    notes: str = Form(""),
    doc_type_id: str = Form(""),
):
    """Upload one or more resumes to an existing candidate.

    Files are batched so each Nexus request stays under the 20 MB cap.
    """
    uploader = _current_user_name(request)
    # Stamp who uploaded into the Nexus document Notes (shows in Documents tab).
    if uploader and config.UPLOADER_ATTRIBUTION:
        stamp = f"Uploaded by {uploader} via Bulk Parser"
        notes = f"{notes} — {stamp}" if notes.strip() else stamp
    try:
        type_id = int(doc_type_id) if doc_type_id.strip() else client.resolve_resume_doc_type_id()
    except NexusError as e:
        return _nexus_error_response(e)
    except ValueError:
        return JSONResponse(status_code=400, content={"ok": False, "error": "doc_type_id must be a number"})

    docs, results = [], []
    for f in files:
        content = await f.read()
        if len(content) > config.MAX_FILE_BYTES:
            results.append({"filename": f.filename, "ok": False,
                            "error": "over the 10 MB per-file limit"})
            continue
        docs.append({
            "doc_type_id": type_id,
            "filename": f.filename,
            "content": content,
            "content_type": f.content_type or "application/octet-stream",
            "notes": notes,
        })

    # batch under the request-size cap
    batches, current, current_size = [], [], 0
    for d in docs:
        if current and current_size + len(d["content"]) > config.MAX_REQUEST_BYTES:
            batches.append(current)
            current, current_size = [], 0
        current.append(d)
        current_size += len(d["content"])
    if current:
        batches.append(current)

    raw_responses = []
    for batch in batches:
        try:
            resp = client.upload_documents(candidate_id, batch)
        except NexusError as e:
            for d in batch:
                results.append({"filename": d["filename"], "ok": False, "error": str(e)})
            continue

        try:
            body = resp.json()
        except ValueError:
            body = resp.text[:2000]
        raw_responses.append({"status": resp.status_code, "body": body})

        if resp.status_code >= 400:
            msg = body if isinstance(body, str) else json.dumps(body)[:300]
            for d in batch:
                results.append({"filename": d["filename"], "ok": False,
                                "error": f"Nexus returned {resp.status_code}: {msg}"})
            continue

        uploaded = {u.get("documentName"): u for u in (body.get("uploadedDocuments") or [])} \
            if isinstance(body, dict) else {}
        failed = set((body.get("uploadFailedDocumentNames") or []) if isinstance(body, dict) else [])
        for d in batch:
            name = d["filename"]
            if name in failed:
                results.append({"filename": name, "ok": False, "error": "Nexus reported upload failed"})
            else:
                doc = uploaded.get(name, {})
                results.append({"filename": name, "ok": True, "documentId": doc.get("id"),
                                "documentType": doc.get("documentType")})

    for r in results:
        audit.record(uploader, "upload_document", r.get("ok", False),
                     filename=r.get("filename"), candidateId=candidate_id,
                     documentId=r.get("documentId"), detail=r.get("error"))

    return {"ok": all(r["ok"] for r in results) if results else False,
            "docTypeId": type_id, "results": results, "raw": raw_responses}
