"""Dump every master-data list to master_data.txt for picking default IDs.

Read-only. Run: python tools/dump_master_data.py
Then open master_data.txt and copy the IDs you need into NEXUS_DEFAULT_PROFILE.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.nexus_client import client, NexusError

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "master_data.txt")

# name -> (id field, label field, extra fields to show)
LISTS = {
    "professions": ("id", "name", []),
    "specialties": ("specialtyId", "name", ["professionId"]),
    "states": ("id", "name", ["code"]),
    "countries": ("id", "name", ["code"]),
    "referralsources": ("id", "name", []),
    "candidatetypes": ("value", "label", []),
    "candidatestatuses": ("id", "name", ["module", "code"]),
    "shifts": ("id", "name", []),
    "documenttypes": ("value", "label", []),
}


def main():
    try:
        client._get_token()
    except NexusError as e:
        print("Auth failed:", e)
        return 1

    lines = []
    for name, (id_f, label_f, extras) in LISTS.items():
        try:
            rows = client.get_master(name)
        except NexusError as e:
            lines.append(f"## {name}: FAILED ({e})\n")
            continue
        if not isinstance(rows, list):
            lines.append(f"## {name}: unexpected response\n")
            continue
        lines.append(f"## {name} — {len(rows)} entries")
        for row in rows:
            rid = row.get(id_f, row.get("id", row.get("value")))
            label = row.get(label_f, row.get("name", row.get("label")))
            extra = "  ".join(f"{k}={row.get(k)}" for k in extras if row.get(k) is not None)
            active = row.get("active")
            flag = "" if active in (None, True) else " [inactive]"
            lines.append(f"  {rid}\t{label}{('   ' + extra) if extra else ''}{flag}")
        lines.append("")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Wrote {OUT}")
    print("Open it, pick your profession/specialty/state IDs, and put them in")
    print("NEXUS_DEFAULT_PROFILE in .env. Note: a specialty's professionId must")
    print("match the profession you choose.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
