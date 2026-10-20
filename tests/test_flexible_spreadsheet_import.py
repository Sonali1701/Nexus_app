import io
import csv
import io
import asyncio
import json
import unittest
from unittest.mock import MagicMock, call, patch

from openpyxl import Workbook
from starlette.datastructures import UploadFile

from app import csv_import


class FlexibleSpreadsheetImportTests(unittest.TestCase):
    def test_generic_import_accepts_either_contact_and_omits_missing_payload_fields(self):
        content = (
            "Name,Email,Phone,State,Profession\n"
            "Jane Doe,jane@example.com,,KY,RN\n"
            "John Smith,,5025551212,KY,RN\n"
            "Jill Jones,jill@example.com,,KY,RN\n"
            "No Contact,,,KY,RN\n"
        ).encode()
        summary, records = csv_import.parse_and_filter_many([("contacts.csv", content)])
        self.assertEqual(summary["safeRows"], 3)
        self.assertEqual(summary["rejectedByReason"], {"missing or invalid email and phone": 1})
        self.assertEqual([(row["email"], row["phone"]) for row in records], [
            ("jane@example.com", ""), ("", "(502) 555-1212"),
            ("jill@example.com", ""),
        ])
        defaults = {
            "stateIds": {"KY": 1}, "professionId": 2, "specialtyId": 3,
            "statusId": 4, "referralSourceId": 5638,
            "jobTypeIds": ["TRAVEL"], "countryId": 5,
        }
        email_only = csv_import.candidate_payload(records[0], defaults)
        phone_only = csv_import.candidate_payload(records[1], defaults)
        self.assertEqual(email_only["primaryEmail"], "jane@example.com")
        self.assertNotIn("phone", email_only)
        self.assertNotIn("cellPhone", email_only)
        self.assertEqual(phone_only["phone"], "(502) 555-1212")
        self.assertNotIn("primaryEmail", phone_only)

    def test_mapping_accepts_file_with_only_one_contact_column(self):
        phone_csv = b"Name,Phone,State,Profession\nJane Doe,5025551212,KY,RN\n"
        email_csv = b"Name,Email,State,Profession\nJohn Smith,john@example.com,KY,RN\n"
        for filename, content in (("phone.csv", phone_csv), ("email.csv", email_csv)):
            with self.subTest(filename=filename):
                inspection = csv_import.inspect_many([(filename, content)])
                self.assertTrue(inspection["canAutoMap"])
                summary, records = csv_import.parse_and_filter_many([(filename, content)])
                self.assertEqual(summary["safeRows"], 1)
                self.assertTrue(records[0]["email"] or records[0]["phone"])

    def test_valid_fallback_contact_is_used_when_preferred_value_is_invalid(self):
        self.assertEqual(csv_import._best_indeed_email({
            "Personal Email": "invalid", "Email": "jane@example.com",
        })[0], "jane@example.com")
        self.assertEqual(csv_import._best_indeed_phone({
            "Mobile Phone": "bad", "Phone": "5025551212",
        })[0], "(502) 555-1212")
        self.assertEqual(csv_import._best_email({
            "recommended_personal_email": "invalid",
            "work_email": "jane@example.com",
        }), "jane@example.com")

    def test_indeed_import_accepts_either_contact(self):
        headers = sorted(csv_import.INDEED_REQUIRED_COLUMNS)
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=headers)
        writer.writeheader()
        for name, email, phone in (
            ("Jane Doe", "jane@example.com", ""),
            ("John Smith", "", "5025551212"),
        ):
            writer.writerow({
                "Name": name, "Location": "Louisville, KY",
                "Headline": "Registered Nurse", "Email": email, "Phone": phone,
                "PDL likelihood": "5", "PDL matched inputs": "name",
                "PDL job title": "RN", "PDL company": "Hospital",
            })
        summary, records = csv_import.parse_and_filter(output.getvalue().encode())
        self.assertEqual(summary["safeRows"], 2)
        self.assertEqual(records[0]["phone"], "")
        self.assertEqual(records[1]["email"], "")

    def test_license_import_accepts_either_contact(self):
        headers = sorted(csv_import.LICENSE_REQUIRED_COLUMNS | {"recommended_personal_email"})
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=headers)
        writer.writeheader()
        for name, first, last, email, phone, license_number in (
            ("Jane Doe", "Jane", "Doe", "jane@example.com", "", "RN-1"),
            ("John Smith", "John", "Smith", "", "5025551212", "RN-2"),
        ):
            writer.writerow({
                "input_name": name, "full_name": name, "first_name": first,
                "last_name": last, "recommended_personal_email": email,
                "mobile_phone": phone, "input_region": "KY",
                "input_preserved_License Type": "RN",
                "input_preserved_License #": license_number,
                "input_preserved_License Issue Date": "2024-01-01",
                "input_preserved_License Exp Date": "2026-01-01",
                "status": "200", "likelihood": "7", "matched": "['name']",
            })
        summary, records = csv_import.parse_and_filter(output.getvalue().encode())
        self.assertEqual(summary["safeRows"], 2)
        self.assertEqual(records[0]["phone"], "")
        self.assertEqual(records[1]["email"], "")

    def test_import_searches_only_supplied_contact(self):
        from app import main

        records = [
            {"sourceRow": 2, "stateCode": "KY", "firstName": "Jane",
             "lastName": "Doe", "email": "jane@example.com", "phone": ""},
            {"sourceRow": 3, "stateCode": "KY", "firstName": "John",
             "lastName": "Smith", "email": "", "phone": "(502) 555-1212"},
        ]
        job_id = "contact-only-test"
        main._CSV_JOBS[job_id] = {
            "status": "queued", "processed": 0, "created": 0,
            "skipped": 0, "failed": 0, "cancelRequested": False,
            "results": [], "current": "",
        }
        response = MagicMock(status_code=200)
        response.json.return_value = {"id": 123}
        try:
            with patch.object(main.client, "search_candidates", return_value=[]) as search, \
                    patch.object(main.client, "create_candidate", return_value=response), \
                    patch.object(main.csv_import, "candidate_payload", return_value={}), \
                    patch.object(main.audit, "record"):
                main._run_csv_import(job_id, records, {}, uploader="")
            self.assertEqual(search.call_args_list, [
                call(email="jane@example.com"), call(phone="(502) 555-1212"),
            ])
            self.assertEqual(main._CSV_JOBS[job_id]["created"], 2)
        finally:
            main._CSV_JOBS.pop(job_id, None)

    def test_single_resume_upload_uses_candidate_api_for_either_contact(self):
        from app import main

        response = MagicMock(status_code=200)
        response.json.return_value = {"id": 123}
        document_response = MagicMock(status_code=200)
        for profile in (
            {"firstName": "Jane", "lastName": "Doe", "primaryEmail": "jane@example.com"},
            {"firstName": "John", "lastName": "Smith", "phone": "(502) 555-1212"},
        ):
            with self.subTest(profile=profile), \
                    patch.object(main, "_current_user_name", return_value=""), \
                    patch.object(main, "_prepare_candidate_profile", side_effect=lambda value: value), \
                    patch.object(main.client, "create_candidate", return_value=response) as create, \
                    patch.object(main.client, "create_candidate_with_resume") as webhook, \
                    patch.object(main.client, "resolve_resume_doc_type_id", return_value=5), \
                    patch.object(main.client, "upload_documents", return_value=document_response) as upload, \
                    patch.object(main.audit, "record"):
                file = UploadFile(file=io.BytesIO(b"%PDF-1.4 test"), filename="resume.pdf")
                result = asyncio.run(main.upload_new_candidate(
                    None, json.dumps(profile), file,
                ))
            self.assertTrue(result["ok"])
            self.assertEqual(result["candidateId"], 123)
            create.assert_called_once()
            upload.assert_called_once()
            webhook.assert_not_called()

    def test_auto_maps_custom_csv_and_applies_threshold(self):
        content = (
            "Candidate Full Name,Personal Email Address,Cell Phone,Current Location,"
            "Professional Headline,Match Confidence\n"
            'Jane Doe,jane@gmail.com,5025551212,"Louisville, Kentucky",'
            "Registered Nurse,5\n"
            'John Smith,john@yahoo.com,6145551212,"Columbus, Ohio",RN,2\n'
        ).encode()

        inspection = csv_import.inspect_many([("custom.csv", content)])
        self.assertTrue(inspection["canAutoMap"])
        mapping = inspection["groups"][0]["suggestedMapping"]
        self.assertEqual(mapping["personalEmail"], "Personal Email Address")
        self.assertEqual(mapping["mobilePhone"], "Cell Phone")

        summary, records = csv_import.parse_and_filter_many([("custom.csv", content)])
        self.assertEqual(summary["safeRows"], 1)
        self.assertEqual(summary["rejectedByReason"], {"confidence below 3": 1})
        self.assertEqual(records[0]["stateCode"], "KY")
        self.assertEqual(records[0]["professionName"], "RN")

    def test_manual_mapping_handles_unrecognized_columns(self):
        content = (
            "Col A,Col B,Col C,Col D,Col E\n"
            "Alice Jones,alice@gmail.com,5025550000,KY,Registered Nurse\n"
        ).encode()
        inspection = csv_import.inspect_many([("unknown.csv", content)])
        self.assertFalse(inspection["canAutoMap"])
        group_id = inspection["groups"][0]["id"]
        mappings = {group_id: {
            "fullName": "Col A",
            "personalEmail": "Col B",
            "mobilePhone": "Col C",
            "state": "Col D",
            "headline": "Col E",
        }}

        summary, records = csv_import.parse_and_filter_many(
            [("unknown.csv", content)], mappings=mappings)
        self.assertEqual(summary["safeRows"], 1)
        self.assertEqual(records[0]["firstName"], "Alice")
        self.assertEqual(records[0]["phoneSource"], "mobile")

    def test_reads_xlsx_and_prefers_personal_mobile_fields(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Candidates"
        sheet.append([
            "First", "Last", "Business Email", "Private Email", "Main Phone",
            "Mobile", "State Code", "Occupation",
        ])
        sheet.append([
            "Robert", "Brown", "robert@hospital.example", "rbrown@gmail.com",
            "5025550101", "5025550102", "KY", "RN",
        ])
        output = io.BytesIO()
        workbook.save(output)

        inspection = csv_import.inspect_many([("candidates.xlsx", output.getvalue())])
        self.assertTrue(inspection["canAutoMap"])
        summary, records = csv_import.parse_and_filter_many(
            [("candidates.xlsx", output.getvalue())])
        self.assertEqual(summary["safeRows"], 1)
        self.assertEqual(records[0]["email"], "rbrown@gmail.com")
        self.assertEqual(records[0]["phone"], "(502) 555-0102")
        self.assertEqual(records[0]["sourceRow"], 2)

    def test_groups_identical_layouts_and_separates_different_layouts(self):
        first = b"Name,Email,Phone,State,Profession\nA Person,a@x.com,5025551111,KY,RN\n"
        second = b"Name,Email,Phone,State,Profession\nB Person,b@x.com,5025552222,OH,RN\n"
        third = b"Full Name,Personal Email,Mobile Phone,Location,Headline\nC Person,c@x.com,5025553333,OH,RN\n"
        inspection = csv_import.inspect_many([
            ("first.csv", first), ("second.csv", second), ("third.csv", third),
        ])
        self.assertEqual(len(inspection["groups"]), 2)
        group_sizes = sorted(len(group["files"]) for group in inspection["groups"])
        self.assertEqual(group_sizes, [1, 2])


if __name__ == "__main__":
    unittest.main()
