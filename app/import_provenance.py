"""Verify a Nexus candidate came from the exact spreadsheet row being repaired."""

import re
from pathlib import Path

from .specialty_mapping import normalize


def matches_sheet_row(candidate, record):
    source_file = Path(str(record.get("sourceFile") or "")).name
    source_row = record.get("sourceRow")
    if not source_file or not source_row:
        return False
    marker = f"Spreadsheet import from {source_file} row {source_row}."
    highlights = str(candidate.get("candidateHighlights") or "")
    if not highlights.casefold().startswith(marker.casefold()):
        return False
    if normalize(candidate.get("firstName")) != normalize(record.get("firstName")):
        return False
    if normalize(candidate.get("lastName")) != normalize(record.get("lastName")):
        return False
    email = str(record.get("email") or "").strip().casefold()
    candidate_email = str(candidate.get("email") or candidate.get("primaryEmail") or "").strip().casefold()
    phone = re.sub(r"\D", "", str(record.get("phone") or ""))
    candidate_phones = {
        re.sub(r"\D", "", str(candidate.get(field) or ""))
        for field in ("phone", "cellPhone", "homePhone", "workPhone")
    }
    return bool((email and email == candidate_email)
                or (phone and phone in candidate_phones))
