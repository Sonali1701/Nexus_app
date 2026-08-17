"""Parse and safely filter supported candidate CSV files."""
import ast
import csv
import io
import re
import unicodedata
from collections import Counter


LICENSE_MIN_LIKELIHOOD = 5
INDEED_MIN_LIKELIHOOD = 3
# Kept for compatibility with callers/tests that refer to the original constant.
MIN_LIKELIHOOD = LICENSE_MIN_LIKELIHOOD
MAX_CSV_BYTES = 25 * 1024 * 1024
MAX_CSV_ROWS = 10_000
MAX_CSV_FILES = 100
MAX_TOTAL_CSV_BYTES = 100 * 1024 * 1024

LICENSE_REQUIRED_COLUMNS = {
    "input_name",
    "input_region",
    "input_preserved_License Type",
    "input_preserved_License #",
    "input_preserved_License Issue Date",
    "input_preserved_License Exp Date",
    "status",
    "likelihood",
    "full_name",
    "first_name",
    "last_name",
    "mobile_phone",
    "matched",
}
INDEED_REQUIRED_COLUMNS = {
    "Name",
    "Location",
    "Headline",
    "Email",
    "Phone",
    "PDL likelihood",
    "PDL matched inputs",
    "PDL job title",
    "PDL company",
    "Source URL",
}
# Kept as the original license schema for compatibility with existing callers.
REQUIRED_COLUMNS = LICENSE_REQUIRED_COLUMNS

PERSONAL_EMAIL_DOMAINS = {
    "aol.com",
    "fastmail.com",
    "gmail.com",
    "googlemail.com",
    "hotmail.com",
    "icloud.com",
    "live.com",
    "mail.com",
    "msn.com",
    "outlook.com",
    "proton.me",
    "protonmail.com",
    "yahoo.com",
    "ymail.com",
}

US_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "district of columbia": "DC", "florida": "FL",
    "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY",
    "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}
US_STATE_CODES = frozenset(US_STATE_NAMES.values())

# Ordered from specific credentials to broader profession terms. Headline is
# always checked first; the PDL job title is only a fallback when the headline
# has no confident match.
PROFESSION_RULES = (
    ("CRNA", re.compile(
        r"\bcrna\b|certified registered nurse anesthetist|nurse anesthetist|\banesthetist\b",
        re.I)),
    ("Nurse Practitioner", re.compile(
        r"nurse practitioner|\bcrnp\b|\bfnp(?:-bc|-c)?\b|\baprn\b|\bnp-c\b",
        re.I)),
    ("Physician Assistant", re.compile(r"physician assistant|\bpa-c\b", re.I)),
    ("LPN/LVN", re.compile(
        r"licensed practical nurse|licensed vocational nurse|\blpn\b|\blvn\b",
        re.I)),
    ("CNA", re.compile(r"certified nursing assistant|\bcna\b", re.I)),
    ("RN", re.compile(r"registered nurse|\brn\b", re.I)),
    ("Patient Care Technician", re.compile(
        r"patient care tech(?:nician)?|\bpct\b", re.I)),
    ("Medical Assistant", re.compile(r"medical assistant|\bcma\b", re.I)),
    ("Respiratory Therapy", re.compile(
        r"respiratory therap(?:ist|y)|\brrt\b", re.I)),
    ("Physical Therapy", re.compile(r"physical therap(?:ist|y)|\bdpt\b", re.I)),
    ("Occupational Therapy", re.compile(
        r"occupational therap(?:ist|y)|\botr\b|\bcota\b", re.I)),
    ("Social Services", re.compile(r"social worker|\blcsw\b|\bmsw\b", re.I)),
    ("Midwife", re.compile(r"certified nurse midwife|\bcnm\b|\bmidwife\b", re.I)),
)


class CSVImportError(ValueError):
    pass


def _text(value):
    return str(value or "").strip()


def _normalized_name(value):
    value = unicodedata.normalize("NFKD", _text(value)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _list(value):
    try:
        parsed = ast.literal_eval(value) if value else []
        return parsed if isinstance(parsed, list) else []
    except (SyntaxError, ValueError):
        return []


def _structured_emails(row):
    return [
        item for item in _list(row.get("emails"))
        if isinstance(item, dict) and _text(item.get("address"))
    ]


def _best_email(row):
    """Prefer every available personal-email source before a work address."""
    recommended = _text(row.get("recommended_personal_email")).lower()
    if recommended:
        return recommended
    for item in _list(row.get("personal_emails")):
        if isinstance(item, str) and _text(item):
            return _text(item).lower()
    structured = _structured_emails(row)
    for item in structured:
        if _text(item.get("type")).lower() == "personal":
            return _text(item["address"]).lower()
    work = _text(row.get("work_email")).lower()
    if work:
        return work
    if structured:
        return _text(structured[0]["address"]).lower()
    return ""


def _first_value(row, keys):
    for key in keys:
        value = _text(row.get(key))
        if value:
            return value
    return ""


def _email_type(email):
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    return "personal" if domain in PERSONAL_EMAIL_DOMAINS else "provided fallback"


def _best_indeed_email(row):
    personal = _first_value(row, (
        "Personal Email", "Personal email", "personal_email",
        "recommended_personal_email", "PDL personal email",
    )).lower()
    if personal:
        return personal, "personal"
    email = _text(row.get("Email")).lower()
    return email, _email_type(email)


def _phone(value):
    digits = re.sub(r"\D", "", _text(value))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if 10 <= len(digits) <= 15:
        return "+" + digits
    return ""


def _best_indeed_phone(row):
    mobile = _first_value(row, (
        "Mobile Phone", "Mobile phone", "mobile_phone", "PDL mobile phone",
    ))
    if mobile:
        return _phone(mobile), "mobile"
    return _phone(row.get("Phone")), "provided fallback"


def _name_case(value):
    return _text(value).title()


def _decode(content):
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CSVImportError("CSV encoding is not supported; save it as UTF-8 CSV")


def _split_name(value):
    parts = _text(value).split()
    if len(parts) < 2:
        return "", "", ""
    return _name_case(parts[0]), _name_case(" ".join(parts[1:-1])), _name_case(parts[-1])


def _parse_indeed_likelihood(value):
    raw = _text(value)
    if not raw:
        return None
    match = re.search(r"\d+", raw)
    return int(match.group()) if match else -1


def _parse_us_location(value):
    """Return ``(city, state_code)`` for a U.S. city/state location."""
    location = _text(value)
    if "," not in location:
        return "", ""
    city, state = (part.strip() for part in location.rsplit(",", 1))
    state = re.sub(r"\s+\d{5}(?:-\d{4})?$", "", state).strip()
    normalized_state = re.sub(r"\s+", " ", state.replace(".", "")).lower()
    state_code = (
        state.upper() if state.upper() in US_STATE_CODES
        else US_STATE_NAMES.get(normalized_state, "")
    )
    return (city, state_code) if city and state_code else ("", "")


def _detect_profession(headline, pdl_job_title):
    for source, value in (("headline", headline), ("PDL job title", pdl_job_title)):
        text_value = _text(value)
        if not text_value:
            continue
        for profession_name, pattern in PROFESSION_RULES:
            if pattern.search(text_value):
                return profession_name, source
    return "", ""


def _parse_indeed_rows(reader):
    safe = []
    rejected = Counter()
    seen_email = set()
    seen_phone = set()
    total = 0

    for source_row, row in enumerate(reader, start=2):
        total += 1
        if total > MAX_CSV_ROWS:
            raise CSVImportError(f"CSV has more than the {MAX_CSV_ROWS:,}-row limit")

        likelihood = _parse_indeed_likelihood(row.get("PDL likelihood"))
        if likelihood == -1:
            rejected["invalid PDL confidence"] += 1
            continue
        if likelihood is not None and likelihood < INDEED_MIN_LIKELIHOOD:
            rejected[f"confidence below {INDEED_MIN_LIKELIHOOD}"] += 1
            continue
        matched = {
            part.strip().lower()
            for part in _text(row.get("PDL matched inputs")).split(",")
            if part.strip()
        }
        if likelihood is not None and "name" not in matched:
            rejected["name was not a PDL match signal"] += 1
            continue

        city, state_code = _parse_us_location(row.get("Location"))
        if not city or not state_code:
            rejected["location is not a supported U.S. state"] += 1
            continue
        headline = _text(row.get("Headline"))

        first_name, middle_name, last_name = _split_name(row.get("Name"))
        if not first_name or not last_name:
            rejected["missing parsed first or last name"] += 1
            continue
        email, email_type = _best_indeed_email(row)
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            rejected["missing or invalid email"] += 1
            continue
        phone, phone_source = _best_indeed_phone(row)
        if not phone:
            rejected["missing or invalid phone"] += 1
            continue

        profession_name, profession_source = _detect_profession(
            headline, row.get("PDL job title"))
        if not profession_name:
            rejected["profession could not be confidently inferred"] += 1
            continue

        phone_key = re.sub(r"\D", "", phone)
        if email in seen_email or phone_key in seen_phone:
            rejected["duplicate email or phone inside CSV"] += 1
            continue
        seen_email.add(email)
        seen_phone.add(phone_key)

        safe.append({
            "sourceType": "indeed_matches",
            "sourceRow": source_row,
            "firstName": first_name,
            "middleName": middle_name,
            "lastName": last_name,
            "email": email,
            "emailType": email_type,
            "phone": phone,
            "phoneSource": phone_source,
            "addressLine1": "",
            "city": _name_case(city),
            "stateCode": state_code,
            "zip": "",
            "licenseType": "",
            "licenseNumber": "",
            "licenseIssueDate": "",
            "licenseExpirationDate": "",
            "linkedin": "",
            "likelihood": likelihood,
            "headline": headline,
            "pdlJobTitle": _text(row.get("PDL job title")),
            "pdlCompany": _text(row.get("PDL company")),
            "sourceUrl": _text(row.get("Source URL")),
            "professionName": profession_name,
            "professionSource": profession_source,
        })

    return ({
        "schema": "indeed_matches",
        "schemaLabel": "Indeed candidate matches",
        "totalRows": total,
        "safeRows": len(safe),
        "rejectedRows": total - len(safe),
        "minimumLikelihood": INDEED_MIN_LIKELIHOOD,
        "blankLikelihoodPolicy": (
            "allowed only for complete U.S. contacts"
        ),
        "safeByState": dict(Counter(row["stateCode"] for row in safe)),
        "safeByProfession": dict(Counter(row["professionName"] for row in safe)),
        "rejectedByReason": dict(rejected),
    }, safe)


def parse_and_filter(content):
    """Return ``(summary, safe_records)`` for either supported CSV schema."""
    if not content:
        raise CSVImportError("CSV file is empty")
    if len(content) > MAX_CSV_BYTES:
        raise CSVImportError("CSV file is over the 25 MB import limit")

    reader = csv.DictReader(io.StringIO(_decode(content)))
    headers = set(reader.fieldnames or [])
    if INDEED_REQUIRED_COLUMNS <= headers:
        return _parse_indeed_rows(reader)

    missing = sorted(LICENSE_REQUIRED_COLUMNS - headers)
    if missing:
        raise CSVImportError(
            "CSV format is not supported. For the Kentucky-license format, missing: "
            + ", ".join(missing)
        )

    safe = []
    rejected = Counter()
    seen_email = set()
    seen_phone = set()
    seen_license = set()
    total = 0

    for source_row, row in enumerate(reader, start=2):
        total += 1
        if total > MAX_CSV_ROWS:
            raise CSVImportError(f"CSV has more than the {MAX_CSV_ROWS:,}-row limit")

        if _text(row.get("status")) != "200":
            rejected["no enrichment match"] += 1
            continue
        if _normalized_name(row.get("input_name")) != _normalized_name(row.get("full_name")):
            rejected["enriched name does not exactly match input name"] += 1
            continue
        if "name" not in _list(row.get("matched")):
            rejected["input name was not an enrichment match signal"] += 1
            continue
        try:
            likelihood = int(_text(row.get("likelihood")) or 0)
        except ValueError:
            likelihood = 0
        if likelihood < LICENSE_MIN_LIKELIHOOD:
            rejected[f"confidence below {LICENSE_MIN_LIKELIHOOD}"] += 1
            continue
        if _text(row.get("input_region")).upper() != "KY":
            rejected["not a Kentucky record"] += 1
            continue
        if _text(row.get("input_preserved_License Type")).upper() != "RN":
            rejected["not an RN license"] += 1
            continue

        email = _best_email(row)
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            rejected["missing or invalid email"] += 1
            continue
        phone = _phone(row.get("mobile_phone"))
        if not phone:
            rejected["missing or invalid phone"] += 1
            continue

        license_number = _text(row.get("input_preserved_License #"))
        issue_date = _text(row.get("input_preserved_License Issue Date"))
        expiration_date = _text(row.get("input_preserved_License Exp Date"))
        if not license_number or not issue_date or not expiration_date:
            rejected["incomplete license details"] += 1
            continue
        phone_key = re.sub(r"\D", "", phone)
        if email in seen_email or phone_key in seen_phone or license_number in seen_license:
            rejected["duplicate contact or license inside CSV"] += 1
            continue
        seen_email.add(email)
        seen_phone.add(phone_key)
        seen_license.add(license_number)

        first_name = _name_case(row.get("first_name"))
        last_name = _name_case(row.get("last_name"))
        if not first_name or not last_name:
            rejected["missing parsed first or last name"] += 1
            continue

        linkedin = _text(row.get("linkedin_url"))
        if linkedin and not linkedin.startswith(("http://", "https://")):
            linkedin = "https://" + linkedin

        safe.append({
            "sourceType": "pdl_license",
            "sourceRow": source_row,
            "firstName": first_name,
            "middleName": _name_case(row.get("middle_name")),
            "lastName": last_name,
            "email": email,
            "emailType": _email_type(email),
            "phone": phone,
            "phoneSource": "mobile",
            "addressLine1": _text(row.get("input_street_address")),
            "city": _name_case(row.get("input_locality")),
            "stateCode": _text(row.get("input_region")).upper(),
            "zip": _text(row.get("input_postal_code")),
            "licenseType": _text(row.get("input_preserved_License Type")).upper(),
            "licenseNumber": license_number,
            "licenseIssueDate": issue_date,
            "licenseExpirationDate": expiration_date,
            "linkedin": linkedin,
            "likelihood": likelihood,
        })

    summary = {
        "schema": "pdl_license",
        "schemaLabel": "PDL-enriched Kentucky RN licenses",
        "totalRows": total,
        "safeRows": len(safe),
        "rejectedRows": total - len(safe),
        "minimumLikelihood": LICENSE_MIN_LIKELIHOOD,
        "safeByState": dict(Counter(row["stateCode"] for row in safe)),
        "rejectedByReason": dict(rejected),
    }
    return summary, safe


def parse_and_filter_many(named_contents):
    """Parse multiple same-schema CSVs and deduplicate their accepted rows."""
    files = list(named_contents)
    if not files:
        raise CSVImportError("Choose at least one CSV file")
    if len(files) > MAX_CSV_FILES:
        raise CSVImportError(f"Choose no more than {MAX_CSV_FILES} CSV files at once")
    if sum(len(content) for _, content in files) > MAX_TOTAL_CSV_BYTES:
        raise CSVImportError("Selected CSV files exceed the 100 MB combined limit")

    summaries = []
    parsed_summaries = []
    records = []
    schemas = set()
    for filename, content in files:
        summary, file_records = parse_and_filter(content)
        schemas.add(summary["schema"])
        parsed_summaries.append(summary)
        summaries.append({
            "filename": filename,
            "totalRows": summary["totalRows"],
            "safeRowsBeforeCrossFileDeduplication": summary["safeRows"],
            "rejectedRows": summary["rejectedRows"],
        })
        for record in file_records:
            records.append({**record, "sourceFile": filename})

    if len(schemas) != 1:
        raise CSVImportError(
            "Select files from one export format at a time; do not mix license and Indeed CSVs"
        )

    # Keep the strongest occurrence when the same email, phone, or license is
    # present in multiple selected files. Personal/mobile contact sources and a
    # higher PDL score win; original file/row order breaks ties.
    ranked = sorted(
        enumerate(records),
        key=lambda item: (
            item[1].get("emailType") == "personal",
            item[1].get("phoneSource") == "mobile",
            item[1].get("likelihood") if item[1].get("likelihood") is not None else -1,
            -item[0],
        ),
        reverse=True,
    )
    kept = []
    seen_email = set()
    seen_phone = set()
    seen_license = set()
    cross_file_duplicates = 0
    for original_index, record in ranked:
        email = record["email"].lower()
        phone = re.sub(r"\D", "", record["phone"])
        license_number = _text(record.get("licenseNumber"))
        if (email in seen_email or phone in seen_phone
                or (license_number and license_number in seen_license)):
            cross_file_duplicates += 1
            continue
        seen_email.add(email)
        seen_phone.add(phone)
        if license_number:
            seen_license.add(license_number)
        kept.append((original_index, record))
    records = [record for _, record in sorted(kept)]

    rejected = Counter()
    for summary in parsed_summaries:
        rejected.update(summary["rejectedByReason"])
    if cross_file_duplicates:
        rejected["duplicate contact across selected CSVs"] += cross_file_duplicates

    schema = next(iter(schemas))
    first_summary = parsed_summaries[0]
    total_rows = sum(item["totalRows"] for item in summaries)
    summary = {
        "schema": schema,
        "schemaLabel": first_summary["schemaLabel"],
        "fileCount": len(files),
        "totalRows": total_rows,
        "safeRows": len(records),
        "rejectedRows": total_rows - len(records),
        "minimumLikelihood": first_summary["minimumLikelihood"],
        "safeByState": dict(Counter(row["stateCode"] for row in records)),
        "safeByProfession": dict(Counter(
            row.get("professionName", "RN") for row in records)),
        "rejectedByReason": dict(rejected),
        "files": summaries,
    }
    if first_summary.get("blankLikelihoodPolicy"):
        summary["blankLikelihoodPolicy"] = first_summary["blankLikelihoodPolicy"]
    return summary, records


def candidate_payload(record, defaults):
    """Map one filtered CSV record to the documented Nexus Candidate API."""
    is_license_record = bool(record.get("licenseNumber"))
    state_code = _text(record.get("stateCode")).upper()
    state_id = defaults.get("stateIds", {}).get(state_code)
    if state_id is None and state_code == "KY":
        # Backward-compatible fallback for older callers and license tests.
        state_id = defaults.get("stateId")
    if state_id is None:
        raise ValueError(f"No Nexus state mapping is available for {state_code or 'this row'}")

    profession_mapping = defaults.get("professionMappings", {}).get(
        record.get("professionName"), {})
    profession_id = profession_mapping.get("professionId", defaults["professionId"])
    specialty_id = profession_mapping.get("specialtyId", defaults["specialtyId"])

    if is_license_record:
        highlights = (
            f"{state_code} {record['licenseType']} license #{record['licenseNumber']}; "
            f"issued {record['licenseIssueDate']}; expires {record['licenseExpirationDate']}. "
            f"Source: PDL-enriched KY license list "
            f"(confidence {record['likelihood']}/10)."
        )
    else:
        confidence = (
            f"PDL confidence {record['likelihood']}/10"
            if record.get("likelihood") is not None
            else "no PDL confidence supplied"
        )
        role = record.get("pdlJobTitle") or record.get("headline") or "not supplied"
        company = record.get("pdlCompany")
        employment = f" at {company}" if company else ""
        highlights = (
            f"Indeed candidate match: {record.get('headline') or 'headline not supplied'}. "
            f"PDL role: {role}{employment}; {confidence}. "
            f"Detected profession: {record.get('professionName') or 'not supplied'} "
            f"from {record.get('professionSource') or 'CSV'}; "
            f"Nexus mapping: {profession_mapping.get('professionName', 'batch default')} / "
            f"{profession_mapping.get('specialtyName', 'batch default')}. "
            f"Email: {record.get('emailType', 'provided fallback')}; "
            f"phone: {record.get('phoneSource', 'provided fallback')}."
        )

    payload = {
        "firstName": record["firstName"],
        "lastName": record["lastName"],
        "primaryEmail": record["email"],
        "phone": record["phone"],
        "cellPhone": record["phone"],
        "statusId": defaults["statusId"],
        "referralSourceId": defaults["referralSourceId"],
        "professionIds": [profession_id],
        "specialtyIds": [specialty_id],
        "primarySpecialtyId": specialty_id,
        "jobTypeIds": defaults["jobTypeIds"],
        "stateId": state_id,
        "countryId": defaults["countryId"],
        "addressLine1": record["addressLine1"] or None,
        "city": record["city"] or None,
        "zip": record["zip"] or None,
        "candidateHighlights": highlights,
        "sendMassEmails": False,
        "sendMassSms": False,
    }
    if is_license_record:
        payload["licensedStateIds"] = [state_id]
    if record.get("middleName"):
        payload["middleName"] = record["middleName"]
    if record.get("linkedin"):
        payload["linkedin"] = record["linkedin"]
    if defaults.get("candidatePRNStatusId"):
        payload["candidatePRNStatusId"] = defaults["candidatePRNStatusId"]
    return payload
