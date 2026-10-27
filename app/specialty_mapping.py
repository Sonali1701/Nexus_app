"""Match spreadsheet roles and specialties to this Nexus tenant's master IDs."""

import re
import unicodedata
from collections import defaultdict


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = value.encode("ascii", "ignore").decode().casefold()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


PROFESSION_PATTERNS = (
    ("CRNA", r"\bcrna\b|nurse anesthetist"),
    ("Nurse Practitioner", r"nurse practitioner|\baprn\b|\bfnp\b"),
    ("Physician Assistant", r"physician assistant|\bpa c\b"),
    ("LPN/LVN", r"\blpn\b|\blvn\b|licensed practical nurse|licensed vocational nurse"),
    ("CNA", r"\bcna\b|certified nurs(?:e|ing) assistant"),
    ("RN", r"\brn\b|registered nurse"),
    ("Physical Therapy", r"physical therap|physical therapist|\bpta\b"),
    ("Occupational Therapy", r"occupational therap|occupational therapist|\bcota\b"),
    ("Respiratory Therapy", r"respiratory therap|\brrt\b|\bcrt\b"),
    ("Speech Therapy", r"speech.language patholog|\bslp\b|\bslpa\b"),
    ("Medical Assistant", r"medical assistant|\bcma\b"),
    ("Laboratory", r"phlebotom|medical lab|clinical lab|medical technologist|"
                   r"histo|cytotech|microbiolog|pathology assistant"),
    ("Pharmacy", r"pharmacist|pharmacy"),
    ("Surgical Services", r"sterile processing|surg(?:ical)? tech|"
                          r"anesthesia tech|endo tech|gi tech|ob tech"),
    ("Radiology/Imaging", r"radiolog|\brad tech\b|\bx ray\b|\bct tech\b|"
                          r"\bmri\b|ultrasound|sonograph|\becho tech\b|"
                          r"mammograph|nuclear medicine|radiation therap|dosimetrist|"
                          r"vascular tech"),
    ("Social Services", r"social work|\blcsw\b|\blmsw\b|"
                         r"professional counsel|marriage.*counsel"),
    ("Behavioral Health", r"behavioral health tech|psychiatric tech"),
    ("EMT", r"emergency medical technician|\bemt\b"),
    ("Paramedic", r"paramedic"),
    ("Dialysis Tech", r"dialysis tech"),
    ("Patient Care Technician", r"patient care tech|\bpct\b"),
    ("Audiologist", r"audiolog"),
)

ROLE_SPECIALTY_PATTERNS = (
    (r"certified.*surg.*tech|\bcst\b", "Certified OR Tech / Surgical Tech (CST)"),
    (r"surg(?:ical)? tech|surgical technologist", "OR Tech / Surgical Tech"),
    (r"sterile processing", "Sterile Processing Tech (SPT)"),
    (r"anesthesia tech", "Anesthesia Technician"),
    (r"endo tech|gi tech", "GI / Endo Tech"),
    (r"\bob tech\b", "OB Tech"),
    (r"phlebotom", "Phlebotomist"),
    (r"medical lab(?:oratory)? tech", "Medical Lab Technician"),
    (r"medical technologist", "Medical Technologist"),
    (r"clinical lab scientist", "Clinical Lab Scientist"),
    (r"histo", "Histology Tech"),
    (r"microbiolog", "Microbiology Tech"),
    (r"physical therap(?:ist|y) assistant|\bpta\b", "Physical Therapist Assistant (PTA)"),
    (r"physical therapist|\bpt\b", "Physical Therapist (PT)"),
    (r"occupational therap(?:ist|y) assistant|\bcota\b", "Certified Occupational Therapist Assistant (COTA)"),
    (r"occupational therapist|\bot\b", "Occupational Therapist (OT)"),
    (r"registered respiratory therapist|\brrt\b", "Registered Respiratory Therapist"),
    (r"speech.language patholog(?:y|ist) assistant|\bslpa\b", "Speech Language Pathologist Assistant (SLPA)"),
    (r"speech.language patholog|\bslp\b", "Speech Language Pathologist (SLP)"),
    (r"licensed clinical social work|\blcsw\b", "Licensed Clinical Social Worker (LCSW)"),
    (r"licensed professional clinical counsel", "Licensed Professional Clinical Counselor"),
    (r"licensed professional counsel|\blpc\b", "Licensed Professional Counselor"),
    (r"medical social work", "Medical Social Worker"),
    (r"certified medical assistant|\bcma\b", "Certified Medical Assistant"),
    (r"emergency medical technician|\bemt\b", "EMT"),
    (r"\bct tech\b|ct technologist", "CT Tech"),
    (r"\bmri\b", "MRI Tech"),
    (r"\bx ray\b", "X-Ray Tech"),
    (r"mammograph", "Mammography Tech"),
    (r"nuclear medicine", "Nuclear Medicine Tech"),
    (r"radiation therap", "Radiation Therapist"),
    (r"dosimetrist", "Dosimetrist"),
    (r"interventional radiolog", "Interventional Radiology Tech"),
    (r"\becho tech\b|echocardiograph", "Echo Tech/Cardiac Sonographer"),
    (r"ultrasound|sonograph", "Ultrasound Tech - General"),
    (r"dialysis tech", "Dialysis Tech"),
    (r"patient care tech|\bpct\b", "Patient Care Technician"),
    (r"behavioral health tech", "Behavioral Health Tech"),
    (r"audiolog", "Audiologist"),
)

SPECIALTY_ALIASES = {
    "RN": {
        "med surg": "MedSurg", "ms": "MedSurg", "ms tele": "MedSurg / Tele",
        "med surg tele": "MedSurg / Tele", "er trauma": "ER",
        "icu critical care": "ICU", "long term care": "Long Term Care (LTC)",
        "ltac": "Long Term Acute Care (LTAC)", "labor delivery": "L&D",
        "or": "Operating Room", "pacu pre post op": "PACU",
        "pcu": "PCU/IMC/Stepdown", "stepdown": "PCU/IMC/Stepdown",
        "imc": "PCU/IMC/Stepdown", "postpartum": "Post-Partum",
        "case management": "Case Manager", "homecare": "Home Health",
        "rn dialysis": "Dialysis", "wound ostomy": "Wound Care",
        "nurse manager": "Manager",
        "orthopedics": "Ortho", "endoscopy": "GI / Endoscopy",
        "correctional health": "Corrections",
    },
    "LPN/LVN": {
        "med surg": "MedSurg", "ms": "MedSurg", "ms tele": "MedSurg / Tele",
        "med surg tele": "MedSurg / Tele", "er trauma": "Emergency Room",
        "long term care": "Long Term Care", "ltac": "Long Term Acute Care (LTAC)",
        "or": "Operating Room", "occupational": "Occupational Health",
        "correctional health": "Corrections",
    },
    "CNA": {
        "long term care": "Long Term Care (LTC)",
        "ltac": "Long Term Acute Care (LTAC)",
        "geriatrics snf": "Geriatrics", "psych": "PSYCH",
    },
    "Respiratory Therapy": {
        "rrt": "Registered Respiratory Therapist",
    },
}


class SpecialtyResolver:
    def __init__(self, professions, specialties):
        self.professions = {normalize(row.get("name")): row for row in professions}
        self.specialties = defaultdict(dict)
        for row in specialties:
            profession_id = row.get("professionId")
            specialty_id = row.get("specialtyId", row.get("id"))
            if profession_id is not None and specialty_id is not None:
                self.specialties[int(profession_id)][normalize(row.get("name"))] = row

    def resolve(self, role, specialty="", original_specialty=""):
        normalized_role = normalize(role)
        profession = self.professions.get(normalized_role)
        if profession is None:
            for name, pattern in PROFESSION_PATTERNS:
                if re.search(pattern, normalized_role):
                    profession = self.professions.get(normalize(name))
                    break
        if profession is None and normalize(specialty) in (
                "rrt", "registered respiratory therapist"):
            profession = self.professions.get(normalize("Respiratory Therapy"))
        if profession is None:
            return None
        profession_id = int(profession["id"])
        available = self.specialties[profession_id]
        profession_name = str(profession["name"])
        for source, label in (("original specialty", original_specialty),
                              ("specialty", specialty)):
            normalized_label = normalize(label)
            if not normalized_label:
                continue
            target = SPECIALTY_ALIASES.get(profession_name, {}).get(
                normalized_label, label)
            match = available.get(normalize(target))
            if match and normalize(match.get("name")) != "unknown":
                return self._mapping(profession, match, source)
        match = available.get(normalized_role)
        if match and normalize(match.get("name")) != "unknown":
            return self._mapping(profession, match, "profession title")
        for pattern, target in ROLE_SPECIALTY_PATTERNS:
            if re.search(pattern, normalized_role):
                match = available.get(normalize(target))
                if match:
                    return self._mapping(profession, match, "profession title")
        return None

    @staticmethod
    def _mapping(profession, specialty, source):
        return {
            "professionId": int(profession["id"]),
            "professionName": str(profession["name"]),
            "specialtyId": int(specialty.get("specialtyId", specialty.get("id"))),
            "specialtyName": str(specialty["name"]),
            "matchSource": source,
        }
