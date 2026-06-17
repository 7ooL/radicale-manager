import unittest
import vobject

from app.contact_utils import (
    build_vcard_from_fields,
    extract_address,
    extract_emails,
    extract_note,
    extract_organization,
    extract_phones,
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

        result = vcard_to_dict(vcard_text)

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


if __name__ == "__main__":
    unittest.main()
