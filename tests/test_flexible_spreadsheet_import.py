import io
import unittest

from openpyxl import Workbook

from app import csv_import


class FlexibleSpreadsheetImportTests(unittest.TestCase):
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
