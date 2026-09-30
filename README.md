# Nexus Resume Uploader

A small web app for bulk-uploading resumes into **LaborEdge Nexus** so they are
picked up by the **Nexus Resume Parser**. Built against the
*"Nexus Resume Parser Guide for Resumes Uploaded via External APIs" (v1.0, Feb 2026)*.

Two upload modes, matching the two flows in the guide:

| Mode | Nexus API used | What happens |
|---|---|---|
| **New candidates** | `POST /candidate/webhook/create` | Drop resumes and the app **extracts name, email and phone from each resume automatically** to prefill the row — review the highlighted gaps, hit upload, and each resume creates a candidate whose document lands in *Documents → LEAP Resume*, queued for parsing. |
| **Existing candidate** | `POST /candidates/{id}/upload/documents` | Drop any number of resumes onto a candidate ID; files are uploaded under the **Candidate Resume** document type (id auto-discovered via `GET /master/documenttypes`) and queued for parsing. |

## Automatic field extraction

The webhook API requires `firstName`, `lastName`, `email`, `phone` per candidate,
so the app pulls them out of each resume on drop (`POST /api/extract`):

1. **Local heuristics (free, offline)** — text via PyMuPDF / python-docx, email
   and phone via regex, name from the resume's top lines (healthcare credential
   suffixes like *RN, BSN* are stripped), falling back to the email local-part
   or filename.
2. **Claude fallback (optional)** — when heuristics miss a field, or the PDF is
   a scanned image with no text layer, the resume is sent to Claude
   (`claude-opus-5`, structured JSON output). Controlled by `CLAUDE_EXTRACT`
   in `.env` (`auto` default / `always` / `never`) and requires Anthropic
   credentials (`ANTHROPIC_API_KEY`, or an `ant auth login` profile). Without
   credentials the app silently runs heuristics-only.

Extracted values prefill the table (never overwriting anything you typed);
fields that couldn't be found are highlighted in red for manual entry.

Extraction, Nexus profile normalization, and CSV safety rules are covered by four test suites
(no test framework needed):

```powershell
.\.venv\Scripts\python.exe tests\test_heuristics.py   # realistic resume layouts
.\.venv\Scripts\python.exe tests\test_merge.py        # heuristic/Claude merge rules
.\.venv\Scripts\python.exe tests\test_candidate_profile.py  # Nexus payload aliases/defaults
.\.venv\Scripts\python.exe tests\test_csv_import.py   # safe CSV filter/import workflow
```

Limits from the guide are enforced: 10 MB per file, and multi-document requests
are automatically split into batches under the 20 MB request cap.

## Setup

```powershell
# 1. install dependencies (from the project folder)
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. configure credentials
copy .env.example .env
# then edit .env — see below

# 3. run
.\.venv\Scripts\python.exe main.py
# open http://127.0.0.1:8020  → you'll be asked to sign in
```

## Login, admin & audit (who parsed what)

The app requires a login. There's an **admin role** that manages team logins
from an in-app **Admin** page — no editing config by hand, no self-service signup.

**User storage.** Users live in **MongoDB** when `MONGODB_URI` is set
(recommended for Render — a free MongoDB Atlas cluster persists them
permanently), otherwise in a local `users.json` file (fine for local dev or a
persistent disk). See "MongoDB setup" below.

**The bootstrap admin.** On startup the app seeds one admin from the env vars
`ADMIN_USERNAME` / `ADMIN_PASSWORD`, so there's always an admin who can sign in —
even on a brand-new database. Set at least:

```
SECRET_KEY=<any long random string>
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<a strong password>
```

**Adding users.** The admin signs in, clicks **Admin** (top-right), and adds
teammates with a username, name, password, and optional admin flag. From there
the admin can also reset passwords, promote/demote admins, and remove users.
Added users can log in immediately. Passwords are stored **hashed** (PBKDF2);
the last admin can't be removed or demoted.

> `tools/manage_users.py` still exists for the old file/`APP_USERS` workflow, but
> with the Admin page you generally won't need it. `APP_USERS` is now just an
> optional seed imported on first run.

### MongoDB setup (free, durable — recommended for Render)

1. Create a free cluster at mongodb.com/atlas (M0 tier, no card, no expiry).
2. **Database Access** → add a database user (username + password).
3. **Network Access** → allow `0.0.0.0/0` (Render's outbound IPs aren't fixed on
   the free tier), or Render's IPs if you have them.
4. **Connect → Drivers** → copy the `mongodb+srv://…` URI, put your DB user's
   password in it, and set it as `MONGODB_URI` (locally in `.env`, on Render in
   the dashboard). `MONGODB_DB` defaults to `nexus_uploader`.

> Avoid Render's *own* free PostgreSQL for this — Render deletes free databases
> after ~30 days, which would wipe your logins. MongoDB Atlas M0 doesn't expire.

**Attribution.** Nexus authenticates every API call as the single API user, so
Nexus's own audit can't natively name an individual. The app bridges that two ways:

1. **Inside Nexus** — it stamps "Uploaded by *Name* via Bulk Parser" into the
   document **Notes** (existing-candidate uploads, shown in the Documents tab)
   and into a candidate note (new-candidate uploads, best-effort). Toggle with
   `UPLOADER_ATTRIBUTION`.
2. **In the app** — every login and upload is written to an **audit log** (the
   **Activity** button, top-right) with who, what file, the resulting candidate
   ID, and the result. It's also printed to stdout (persisted in Render's logs).

> On Render's free tier the on-disk `audit.log` is wiped on restart; the durable
> records are the Nexus notes and Render's captured stdout logs.

## Authentication

Every Nexus call needs `Authorization: Bearer <access_token>`. The token comes
from LaborEdge's OAuth endpoint, which the Resume Parser guide omits but their
Job Board API doc documents:

```
POST https://api-nexus.laboredge.com/auth/oauth2/token      (UAT: api-uat.laboredge.com)
Headers: Content-Type: application/x-www-form-urlencoded
         Authorization: Basic <base64 clientId:clientSecret>     <-- see below
Body:    grant_type=password, username, password, organizationCode
```

**Two separate credentials are required.** The API *user* (username / password /
organizationCode) is what LaborEdge emails you; the OAuth *client* sent as HTTP
Basic is a second, distinct credential. The production client from LaborEdge's
Job Board API v3.2 doc (`nexus:...`) is baked in as the default `NEXUS_TOKEN_BASIC`
and is confirmed working; the older UAT client was `vms:vmsSecret#$`
(`dm1zOnZtc1NlY3JldCMk`). Override `NEXUS_TOKEN_BASIC` (or set `NEXUS_CLIENT_ID` /
`NEXUS_CLIENT_SECRET`) only if LaborEdge rotates it.

Two quirks handled by the client, worth knowing if you debug it:

- The response field is spelled **`acess_token`** (LaborEdge's typo), not
  `access_token`. Both spellings are accepted.
- `organizationCode` is sent in the body, the query string, *and* a header,
  because the server has been observed demanding it "in request param" even
  when present in the body.

Tokens are cached and refreshed automatically (`refresh_token` + `tokenVerifier`,
single-use, with a full re-auth as fallback). Configure one of:

- **`NEXUS_AUTH_METHOD=static`** — paste a working JWT into `NEXUS_STATIC_TOKEN`.
  Quickest way to test; you must refresh it manually when it expires.
- **`NEXUS_AUTH_METHOD=password`** — set `NEXUS_TOKEN_URL`, `NEXUS_USERNAME`,
  `NEXUS_PASSWORD` (and client id/secret if your credentials sheet includes
  them). The app fetches and caches the token, and refreshes it automatically
  before expiry (reads the JWT `exp` claim / `expires_in`).
- **`NEXUS_AUTH_METHOD=client_credentials`** — set `NEXUS_TOKEN_URL`,
  `NEXUS_CLIENT_ID`, `NEXUS_CLIENT_SECRET`.

Use the **Test connection** button in the header to verify: it calls
`GET /master/documenttypes` and reports the discovered *Candidate Resume*
document type id. From the terminal, `python tools/check_auth.py` does the same
and additionally prints every master-data list (see below). If the token URL
ever changes, `python tools/find_token_url.py` probes for it.

## Finding your master-data IDs

`NEXUS_DEFAULT_PROFILE` needs your agency's numeric IDs. Rather than hunting
through the Nexus UI, run:

```powershell
.\.venv\Scripts\python.exe tools\check_auth.py
```

It authenticates and lists id/name pairs for professions, specialties, states,
referral sources, candidate types, shifts and document types — read off the IDs
you need and paste them into `.env`. (Underlying endpoints:
`GET /api/api-integration/v1/master/{professions|specialties|states|countries|referralsources|candidatetypes|candidatestatuses|recruiters|shifts|documenttypes}`.)

## Notes on the webhook fields

For the *New candidates* mode, the webhook requires `firstName`, `lastName`,
either `email` or `phone` (auto-extracted from the resume), `stateId`, `jobTypeIds`, and
either a `jobId` **or** `professionId` + `specialtyId`. Before upload, the server
also supplies Nexus's canonical Candidate API aliases (`primaryEmail`,
`professionIds`, `specialtyIds`, and `primarySpecialtyId`) and resolves the
agency's Prospect `statusId`, USA `countryId`, and sole referral source from
live master data when they are not configured explicitly.

When only a phone number is available, the app creates the candidate through
the Candidate API and then uploads the resume as a Candidate Resume document.
This avoids the resume webhook's email requirement while keeping the document
in the Nexus parser queue.

**Profession, Specialty and State are chosen from live dropdowns** — the app
calls `GET /api/master` (cached) which proxies Nexus's `master/professions`,
`master/specialties`, and `master/states`, so users pick from real names
instead of typing ID numbers. The Specialty list cascades from the chosen
Profession and Offering (the sheet defines valid combinations). Selections are
remembered per browser. If master data can't be loaded (e.g. not connected),
the UI falls back to manual numeric ID entry.

The app also reads the populated `Old Profession`, `Old Offering`, `Old Sub Offering`,
and `Old Specialty` columns from `Adhoc-automation-data (5).csv`. It resolves
profession and specialty labels to IDs from Nexus's live master lists, filters
specialties by the supported offering, and requires an offering when a
profession has multiple offerings. The sheet is searched beside the app and in
Downloads by default. Set `NEXUS_TAXONOMY_CSV` in `.env` to use another path.

Agency-wide constants still come from `.env` and are merged server-side so
users never see them:

```
NEXUS_DEFAULT_PROFILE={"referralSourceId":5638,"jobTypeIds":["TRAVEL"]}
```

UI selections override `.env`. Anything else the webhook accepts (`zipCode`,
`licenseStateIds`, `travelStatus`, social links, …) can go in the
*Advanced → Extra profileData JSON* box.

## Safe CSV/Excel candidate import

The **CSV candidate import** tab accepts one or more `.csv`, `.xlsx`, or legacy
`.xls` files. Column names do not need to follow a fixed template. The app
groups files with identical layouts, automatically maps recognizable columns,
and displays a mapping panel for review. If a required field is unclear, choose
the correct source column and run Preview again. Different layouts and file
types can be selected in the same batch.

A custom layout needs these mapped field groups:

- full name, or both first name and last name;
- personal email or fallback email;
- mobile phone or fallback phone;
- combined city/state or a state column; and
- profession, professional headline, or job title.

Address, city, ZIP, company, confidence, profile/source URL, and license fields
are optional. Custom rows require a valid U.S. state, valid email and phone, and
a profession that can be resolved from the mapped profession/headline/title. A
supplied confidence must be at least 3; a missing confidence is allowed. Preview
is read-only and reports every excluded-row reason before import.

The two original layouts are still recognized automatically and retain their
stricter safety rules:

For a PDL Kentucky license export:

- enrichment status is 200 and the input name was a match signal;
- normalized input and enriched full names match exactly;
- People Data Labs confidence is at least 5;
- both a valid email and phone are present;
- the source row is a Kentucky RN license; and
- email, phone and license number are unique within the accepted subset.

For an Indeed candidate-match export:

- the location must contain a valid U.S. state;
- profession must be confidently detected from `Headline`, with `PDL job
  title` used only as a fallback; ambiguous or unmatched titles are excluded;
- a supplied PDL confidence must be at least 3 and include name as a match
  signal;
- a blank PDL confidence is accepted only for a complete direct contact that
  still meets the U.S. location check;
- both a valid email and phone are required; and
- email and phone are unique across all selected files.

For both formats, an explicit personal-email column is preferred over a generic
or work address, and an explicit mobile-phone column is preferred over a generic
phone. When those special columns are absent, the generic `Email` and `Phone`
values are used as fallbacks and labeled as such in Preview.

The approved mapping is resolved from live Nexus master data: each detected
profession, a generic specialty for that profession, every state present in
the accepted rows, Prospect status, USA country and the configured referral
source. If a confidently detected profession has no generic specialty in
Nexus, the candidate uses the Nexus Unknown profession/specialty and retains
the detected profession in Candidate Highlights. Each Indeed row's `Location`
chooses its Nexus state. All 50 states and Washington, D.C. are recognized by
abbreviation or full name. The Kentucky license format remains RN-specific and
retains its RN/Unknown mapping.
For `PERDIEM`, the importer also resolves the agency's Prospect PRN status.

Before each create, the importer independently searches Nexus by email and by
phone. Existing matches are skipped. New records have mass email/SMS disabled.
License details or Indeed source/job details are preserved in Candidate
Highlights. An Indeed location is not treated as proof of a professional
license, so only the license-export format sets a licensed state in Nexus.

The import runs in the background with progress and cancellation. Keep the app
running until it finishes, then download the full result report. If an import is
interrupted, preview and run it again; the duplicate checks prevent already
created email/phone matches from being recreated.

## Deploying to Render (free tier)

Use a **Web Service** (Render's Blueprint / `render.yaml` path now needs a paid
plan). The included `render.yaml` is kept only as a reference for the settings.

**1. Push this repo to GitHub** (`.env` is gitignored — secrets go in Render's
dashboard, never in git):

```powershell
git add -A
git commit -m "Nexus Resume Uploader with login + audit"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

**2. Create the service** — Render → **New → Web Service** → connect the repo, then:

| Setting | Value |
|---|---|
| Language / Runtime | **Python 3** (version pinned by `.python-version` = 3.11.8) |
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Instance type | **Free** |
| Health check path | `/healthz` (optional but recommended) |

**3. Add environment variables** (Environment tab). Required:

| Key | Value |
|---|---|
| `SECRET_KEY` | click **Generate** (or any long random string) |
| `COOKIE_SECURE` | `true` |
| `MONGODB_URI` | your Atlas `mongodb+srv://…` URI (durable user storage) |
| `ADMIN_USERNAME` | `admin` |
| `ADMIN_PASSWORD` | a strong password for the first admin |
| `NEXUS_BASE_URL` | `https://api-nexus.laboredge.com` |
| `NEXUS_TOKEN_URL` | `https://api-nexus.laboredge.com/auth/oauth2/token` |
| `NEXUS_AUTH_METHOD` | `password` |
| `NEXUS_USERNAME` | `api_radix_website` |
| `NEXUS_PASSWORD` | *(your API password)* |
| `NEXUS_ORG_CODE` | `Radix` |
| `NEXUS_DEFAULT_PROFILE` | `{"referralSourceId":5638,"jobTypeIds":["TRAVEL"]}` |

Optional: `CLAUDE_EXTRACT=auto` + `ANTHROPIC_API_KEY=<key>` for AI extraction of
scanned resumes. (`NEXUS_TOKEN_BASIC` is baked into the code — no need to set it.)

**4. Create Web Service**, wait for the build, then open
`https://<your-service>.onrender.com` and sign in.

To update later: `git push` — Render auto-deploys the new commit.

**Free-tier caveats:** the service sleeps after ~15 min idle and cold-starts in
~30–60s on the next request; the disk is ephemeral (so `audit.log` resets on
redeploy — rely on Nexus notes + Render's log viewer for durable history); and
the token/master-data caches warm up again after each cold start (one slightly
slower first request). The `NEXUS_TOKEN_BASIC` production client is baked into
the code as a default, so you don't need to set it on Render.

The Nexus API IP-allowlist (if any) is per-agency — if uploads fail from Render
with an auth/403 that works locally, ask LaborEdge whether Render's outbound IPs
need allowlisting.

## Project layout

```
app/
  main.py            FastAPI routes, auth guard, login, admin API, uploads, CSV jobs
  auth.py            login + user management (PBKDF2 hashing, roles, startup seed)
  store.py           user store: MongoDB (durable) or local JSON file
  audit.py           who-did-what audit log (stdout + file + in-memory)
  nexus_client.py    OAuth token cache + Nexus API calls
  csv_import.py      CSV/Excel readers, column mapper, safety filters + payload mapping
  resume_extract.py  resume text extraction + heuristic/Claude field extraction
  config.py          .env-driven settings, login/session/db, upload limits
  templates/
    index.html       single-page uploader (drag & drop, auto-prefill, activity)
    login.html       sign-in page
    admin.html       admin: add/remove users, reset passwords, set roles
tools/
  manage_users.py    (legacy) build an APP_USERS seed with hashed passwords
  check_auth.py      verify Nexus auth + list master data (read-only)
  dump_master_data.py  full master lists → master_data.txt
main.py              uvicorn entry point (python main.py)
render.yaml          reference settings for the manual Render web service
```
