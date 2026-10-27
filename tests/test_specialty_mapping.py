import unittest
from unittest.mock import MagicMock

from app.nexus_client import NexusError
from app.specialty_mapping import SpecialtyResolver
from tools.import_contact_workbooks import process_contact


class SpecialtyMappingTests(unittest.TestCase):
    def setUp(self):
        self.resolver = SpecialtyResolver(
            [{"id": 1, "name": "RN"}, {"id": 2, "name": "Laboratory"}],
            [{"specialtyId": 10, "professionId": 1, "name": "MedSurg"},
             {"specialtyId": 11, "professionId": 1, "name": "Unknown"},
             {"specialtyId": 20, "professionId": 2, "name": "Phlebotomist"}],
        )

    def test_original_specialty_takes_precedence_over_generic_label(self):
        mapping = self.resolver.resolve("RN", "Nursing", "Med/Surg")
        self.assertEqual(mapping["specialtyId"], 10)
        self.assertEqual(mapping["matchSource"], "original specialty")

    def test_role_can_select_validated_profession_specialty(self):
        mapping = self.resolver.resolve("Phlebotomist")
        self.assertEqual((mapping["professionId"], mapping["specialtyId"]), (2, 20))

    def test_unknown_and_ambiguous_labels_are_not_uploaded(self):
        self.assertIsNone(self.resolver.resolve("RN", "Unknown"))
        self.assertIsNone(self.resolver.resolve("RN", "Nursing"))

    def test_unsearchable_nexus_duplicate_is_quarantined(self):
        client = MagicMock()
        client.search_candidates.return_value = []
        client.create_candidate.side_effect = NexusError(
            "duplicate", status_code=400,
            body='{"errorCode":"duplicate.candidate.found"}')
        record = {
            "email": "jane@example.com", "phone": "", "firstName": "Jane",
            "lastName": "Doe", "nexusMapping": {"professionId": 1,
                                                   "specialtyId": 10},
        }
        csv_import = MagicMock()
        csv_import.candidate_payload.return_value = {"firstName": "Jane"}
        status, candidate_id, reason = process_contact(
            record, {}, client, lambda value: str(value).lower(), csv_import, {11})
        self.assertEqual(status, "review")
        self.assertIsNone(candidate_id)
        self.assertIn("duplicate", reason)

    def test_lookup_account_repairs_only_unknown_specialty(self):
        client = MagicMock()
        lookup_client = MagicMock()
        lookup_client.search_candidates.side_effect = [[], [{
            "candidateId": 123, "firstName": "Jane", "lastName": "Doe",
            "specialtyIds": [11], "primarySpecialtyId": 11,
        }]]
        lookup_client.patch_candidate.return_value.status_code = 200
        record = {
            "email": "jane@example.com", "phone": "5025551212",
            "firstName": "Jane", "lastName": "Doe",
            "nexusMapping": {"professionId": 1, "specialtyId": 10},
        }
        status, candidate_id, _ = process_contact(
            record, {}, client, lambda value: str(value).lower(),
            MagicMock(), {11}, lookup_client)
        self.assertEqual((status, candidate_id), ("updated", 123))
        lookup_client.patch_candidate.assert_called_once_with(123, {
            "professionIds": [1], "specialtyIds": [10], "primarySpecialtyId": 10,
        })
        client.create_candidate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
