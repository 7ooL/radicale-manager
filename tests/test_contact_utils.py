import unittest
import vobject

from app.contact_utils import (
    build_vcard_from_fields,
    duplicate_vcard,
    extract_address,
    extract_emails,
    extract_note,
    extract_organization,
    extract_phones,
    merge_unknown_fields_into_vcard,
    vcard_to_dict,
)


class ContactUtilsTest(unittest.TestCase):
    def test_vcard_to_dict_parses_real_world_card(self):
        vcard_text = (
            "BEGIN:VCARD\n"
            "VERSION:3.0\n"
            "FN:John Doe\n"
            "N:Doe;John;;;\n"
            "EMAIL;TYPE=INTERNET:john@example.com\n"
            "TEL;TYPE=CELL:12345\n"
            "ORG:Example Inc.\n"
            "ADR;TYPE=WORK:;;123 Main St;City;State;Postal;Country\n"
            "NOTE:Test note\n"
            "END:VCARD\n"
        )

        result = vcard_to_dict(vcard_text)

        self.assertEqual(result["full_name"], "John Doe")
        self.assertEqual(result["first_name"], "John")
        self.assertEqual(result["last_name"], "Doe")
        self.assertEqual(result["emails"], ["john@example.com"])
        self.assertEqual(result["phones"], ["12345"])
        self.assertEqual(result["organization"], "Example Inc.")
        self.assertEqual(result["address"], "123 Main St, City, State, Postal, Country")
        self.assertEqual(result["note"], "Test note")
        self.assertEqual(result["uid"], "")
        self.assertIn("raw", result)

    def test_vcard_to_dict_handles_malformed_vcard(self):
        vcard_text = "BEGIN:VCARD\nINVALID_LINE\nEND:VCARD\n"

        with self.assertLogs("app.contact_utils", level="WARNING") as captured:
            result = vcard_to_dict(vcard_text)

        self.assertIn("Unable to parse vCard for vcard_to_dict", captured.output[0])
        self.assertEqual(result["uid"], "")
        self.assertEqual(result["full_name"], "")
        self.assertEqual(result["emails"], [])
        self.assertEqual(result["phones"], [])
        self.assertEqual(result["organization"], "")
        self.assertEqual(result["address"], "")
        self.assertEqual(result["note"], "")
        self.assertEqual(result["raw"], vcard_text)

    def test_helper_extract_functions(self):
        vcard_text = (
            "BEGIN:VCARD\n"
            "VERSION:3.0\n"
            "FN:Jane Smith\n"
            "N:Smith;Jane;;;\n"
            "EMAIL:jane@example.com\n"
            "EMAIL:jane.smith@example.org\n"
            "TEL:111-222-3333\n"
            "TEL:444-555-6666\n"
            "ORG:Acme Corp\n"
            "NOTE:Example note\n"
            "ADR;TYPE=WORK:;;456 Oak St;Bigcity;State;99999;Country\n"
            "END:VCARD\n"
        )

        parsed_card = vobject.readOne(vcard_text)

        self.assertEqual(extract_emails(parsed_card), ["jane@example.com", "jane.smith@example.org"])
        self.assertEqual(extract_phones(parsed_card), ["111-222-3333", "444-555-6666"])
        self.assertEqual(extract_organization(parsed_card), "Acme Corp")
        self.assertEqual(extract_note(parsed_card), "Example note")
        self.assertEqual(extract_address(parsed_card), "456 Oak St, Bigcity, State, 99999, Country")

        card = vcard_to_dict(vcard_text)
        self.assertEqual(card["emails"], ["jane@example.com", "jane.smith@example.org"])
        self.assertEqual(card["phones"], ["111-222-3333", "444-555-6666"])
        self.assertEqual(card["organization"], "Acme Corp")
        self.assertEqual(card["note"], "Example note")
        self.assertEqual(card["address"], "456 Oak St, Bigcity, State, 99999, Country")

    def test_build_vcard_preserves_editable_summary_fields(self):
        vcard_text = build_vcard_from_fields(
            {
                "uid": "contact-1",
                "full_name": "Taylor Example",
                "first_name": "Taylor",
                "last_name": "Example",
                "nickname": "Tay",
                "organization": "Example Co",
                "job_title": "Director",
                "birthday": "1990-01-02",
                "emails": ["taylor@example.com"],
                "phones": ["555-0100"],
                "address": "789 Pine St",
                "urls": ["https://example.com"],
                "categories": ["Friends", "Work"],
                "note": "Editable note",
            }
        )

        result = vcard_to_dict(vcard_text)

        self.assertEqual(result["uid"], "contact-1")
        self.assertEqual(result["full_name"], "Taylor Example")
        self.assertEqual(result["nickname"], "Tay")
        self.assertEqual(result["organization"], "Example Co")
        self.assertEqual(result["job_title"], "Director")
        self.assertEqual(result["birthday"], "1990-01-02")
        self.assertEqual(result["emails"], ["taylor@example.com"])
        self.assertEqual(result["phones"], ["555-0100"])
        self.assertEqual(result["address"], "789 Pine St")
        self.assertEqual(result["urls"], ["https://example.com"])
        self.assertEqual(result["categories"], ["Friends", "Work"])
        self.assertEqual(result["note"], "Editable note")

    def test_duplicate_vcard_uses_new_uid_and_copy_name(self):
        vcard_text = (
            "BEGIN:VCARD\n"
            "VERSION:3.0\n"
            "UID:original-uid\n"
            "FN:Jane Smith\n"
            "N:Smith;Jane;;;\n"
            "EMAIL:jane@example.com\n"
            "END:VCARD\n"
        )

        new_uid, copied = duplicate_vcard(vcard_text)
        result = vcard_to_dict(copied)

        self.assertNotEqual(new_uid, "original-uid")
        self.assertEqual(result["uid"], new_uid)
        self.assertEqual(result["full_name"], "Jane Smith Copy")
        self.assertEqual(result["emails"], ["jane@example.com"])

    def test_merge_unknown_fields_preserves_custom_properties(self):
        original = (
            "BEGIN:VCARD\n"
            "VERSION:3.0\n"
            "UID:contact-1\n"
            "FN:Original Name\n"
            "N:Name;Original;;;\n"
            "X-CUSTOM-FIELD:Preserve Me\n"
            "X-ANOTHER;TYPE=WORK:Still Here\n"
            "END:VCARD\n"
        )
        rebuilt = build_vcard_from_fields(
            {
                "uid": "contact-1",
                "full_name": "Updated Name",
                "first_name": "Updated",
                "last_name": "Name",
                "emails": ["updated@example.com"],
            }
        )

        merged = merge_unknown_fields_into_vcard(original, rebuilt)

        self.assertIn("FN:Updated Name", merged)
        self.assertIn("X-CUSTOM-FIELD:Preserve Me", merged)
        self.assertIn("X-ANOTHER;TYPE=WORK:Still Here", merged)

    def test_merge_unknown_fields_skips_prodid_to_avoid_duplicate_singleton_fields(self):
        original_a = (
            "BEGIN:VCARD\n"
            "VERSION:3.0\n"
            "UID:contact-a\n"
            "FN:Ashley Jacoby\n"
            "N:Jacoby;Ashley;;;\n"
            "PRODID:-//Apple Inc.//iOS 26.5//EN\n"
            "X-CUSTOM-A:keep-a\n"
            "END:VCARD\n"
        )
        original_b = (
            "BEGIN:VCARD\n"
            "VERSION:3.0\n"
            "UID:contact-b\n"
            "FN:Ashley Jacoby\n"
            "N:Jacoby;Ashley;;;\n"
            "PRODID:-//Apple Inc.//iPhone OS 26.5//EN\n"
            "X-CUSTOM-B:keep-b\n"
            "END:VCARD\n"
        )
        rebuilt = build_vcard_from_fields(
            {
                "uid": "merged-contact",
                "full_name": "Ashley Jacoby",
                "first_name": "Ashley",
                "last_name": "Jacoby",
                "emails": ["ashley@example.test"],
            }
        )

        merged = merge_unknown_fields_into_vcard(original_a, rebuilt)
        merged = merge_unknown_fields_into_vcard(original_b, merged)

        self.assertEqual(merged.count("PRODID"), 0)
        self.assertIn("X-CUSTOM-A:keep-a", merged)
        self.assertIn("X-CUSTOM-B:keep-b", merged)
        self.assertEqual(vcard_to_dict(merged)["full_name"], "Ashley Jacoby")


if __name__ == "__main__":
    unittest.main()
