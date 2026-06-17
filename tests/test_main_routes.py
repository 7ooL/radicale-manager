import os
import sys
import tempfile
import unittest
from unittest.mock import patch

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import main
from credential_store import CredentialStore


VCARD = (
    "BEGIN:VCARD\n"
    "VERSION:3.0\n"
    "UID:contact-1\n"
    "FN:Demo Contact\n"
    "N:Contact;Demo;;;\n"
    "EMAIL:demo@example.test\n"
    "END:VCARD\n"
)


class FakeRadicaleClient:
    contacts = {}

    def __init__(self, server_url, username, password):
        self.server_url = server_url
        self.username = username
        self.password = password

    def get_contact(self, contact_href):
        path = contact_href.strip("/")
        if path not in self.contacts:
            raise FileNotFoundError(path)
        return self.contacts[path], '"etag-1"'

    def put_contact(self, collection_path, filename, vcard_text, if_match=None):
        self.contacts[f"{collection_path.strip('/')}/{filename}"] = vcard_text
        return type("Response", (), {"status_code": 201})()

    def delete_contact(self, contact_href):
        del self.contacts[contact_href.strip("/")]
        return type("Response", (), {"status_code": 204})()

    def list_contacts(self, collection_path):
        prefix = f"{collection_path.strip('/')}/"
        return [
            {"href": href, "vcard": vcard, "etag": '"etag-1"'}
            for href, vcard in sorted(self.contacts.items())
            if href.startswith(prefix)
        ]


class MainRoutesTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = CredentialStore(os.path.join(self.temp_dir.name, "profiles.db"))
        main.credential_store = self.store
        main.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.client = main.app.test_client()
        self.profile_id = self.store.create_profile(
            "Demo",
            "http://radicale.example.test",
            "demo",
            password="secret",
        )
        self.family_profile_id = self.store.create_profile(
            "Family",
            "http://radicale.example.test",
            "family",
            password="secret",
        )
        self.store.save_or_update_address_books(
            self.profile_id,
            [
                {
                    "display_name": "Source",
                    "path": "demo/source",
                    "href": "/demo/source/",
                    "contact_count": 1,
                },
                {
                    "display_name": "Destination",
                    "path": "demo/dest",
                    "href": "/demo/dest/",
                    "contact_count": 0,
                },
            ],
        )
        self.store.save_or_update_address_books(
            self.family_profile_id,
            [
                {
                    "display_name": "Shared",
                    "path": "family/shared",
                    "href": "/family/shared/",
                    "contact_count": 0,
                },
            ],
        )
        FakeRadicaleClient.contacts = {
            "demo/source/contact-1.vcf": VCARD,
            "demo/source/contact-2.vcf": VCARD.replace("contact-1", "contact-2").replace("Demo Contact", "Second Contact"),
        }
        self.radicale_patch = patch.object(main, "RadicaleClient", FakeRadicaleClient)
        self.radicale_patch.start()

    def tearDown(self):
        self.radicale_patch.stop()
        self.temp_dir.cleanup()

    def test_contact_list_has_add_contact_action_when_empty(self):
        response = self.client.get(f"/profiles/{self.profile_id}/books/demo/dest/contacts")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Add Contact", response.data)
        self.assertIn(b"Add the first contact", response.data)

    def test_contact_list_has_bulk_move_destination_when_destinations_exist(self):
        response = self.client.get(f"/profiles/{self.profile_id}/books/demo/source/contacts")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Move selected", response.data)
        self.assertIn(b"Demo / Destination", response.data)

    def test_new_contact_creates_vcard_and_redirects_to_detail(self):
        with patch.object(main.uuid, "uuid4", return_value="new-contact"):
            response = self.client.post(
                f"/profiles/{self.profile_id}/books/demo/dest/contacts/new",
                data={
                    "save_mode": "structured",
                    "full_name": "New Person",
                    "first_name": "New",
                    "last_name": "Person",
                    "emails": "new@example.test",
                    "phones": "555-0100",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/profiles/1/books/demo/dest/contacts/new-contact.vcf", response.location)
        self.assertIn("demo/dest/new-contact.vcf", FakeRadicaleClient.contacts)
        self.assertIn("FN:New Person", FakeRadicaleClient.contacts["demo/dest/new-contact.vcf"])

    def test_move_contact_redirects_to_destination_contact(self):
        response = self.client.post(
            f"/profiles/{self.profile_id}/books/demo/source/contacts/contact-1.vcf/move",
            data={"dest": f"{self.profile_id}::demo/dest"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/profiles/1/books/demo/dest/contacts/contact-1.vcf", response.location)
        self.assertNotIn("demo/source/contact-1.vcf", FakeRadicaleClient.contacts)
        self.assertIn("demo/dest/contact-1.vcf", FakeRadicaleClient.contacts)
        self.assertIn("demo/source/contact-2.vcf", FakeRadicaleClient.contacts)
        books = self.store.get_cached_address_books(self.profile_id)
        counts = {book["path"]: book["contact_count"] for book in books}
        self.assertIsNone(counts["demo/source"])
        self.assertIsNone(counts["demo/dest"])

    def test_copy_contact_keeps_source_and_invalidates_destination_count(self):
        response = self.client.post(
            f"/profiles/{self.profile_id}/books/demo/source/contacts/contact-1.vcf/copy",
            data={"dest": f"{self.profile_id}::demo/dest"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("demo/source/contact-1.vcf", FakeRadicaleClient.contacts)
        self.assertIn("demo/dest/contact-1.vcf", FakeRadicaleClient.contacts)
        books = self.store.get_cached_address_books(self.profile_id)
        counts = {book["path"]: book["contact_count"] for book in books}
        self.assertEqual(counts["demo/source"], 1)
        self.assertIsNone(counts["demo/dest"])

    def test_bulk_move_moves_selected_contacts_and_invalidates_counts(self):
        response = self.client.post(
            f"/profiles/{self.profile_id}/books/demo/source/contacts/bulk",
            data={
                "bulk_action": "move",
                "dest": f"{self.profile_id}::demo/dest",
                "contact_filename": ["contact-1.vcf", "contact-2.vcf"],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/profiles/1/books/demo/source/contacts", response.location)
        self.assertNotIn("demo/source/contact-1.vcf", FakeRadicaleClient.contacts)
        self.assertNotIn("demo/source/contact-2.vcf", FakeRadicaleClient.contacts)
        self.assertIn("demo/dest/contact-1.vcf", FakeRadicaleClient.contacts)
        self.assertIn("demo/dest/contact-2.vcf", FakeRadicaleClient.contacts)
        books = self.store.get_cached_address_books(self.profile_id)
        counts = {book["path"]: book["contact_count"] for book in books}
        self.assertIsNone(counts["demo/source"])
        self.assertIsNone(counts["demo/dest"])

    def test_global_contacts_lists_contacts_with_book_context_and_actions(self):
        response = self.client.get("/contacts")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"All Contacts", response.data)
        self.assertIn(b"Demo Contact", response.data)
        self.assertIn(b"Second Contact", response.data)
        self.assertIn(b"Source", response.data)
        self.assertIn(b"Demo", response.data)
        self.assertIn(b"Move selected", response.data)
        self.assertIn(b"Select all contacts", response.data)
        self.assertIn(b"Family / Shared", response.data)
        self.assertIn(b"name=\"contact_ref\"", response.data)
        self.assertIn(b"/profiles/1/books/demo/source/contacts/contact-1.vcf/edit", response.data)

    def test_global_bulk_move_moves_selected_contacts_to_destination(self):
        response = self.client.post(
            "/contacts/bulk",
            data={
                "bulk_action": "move",
                "dest": f"{self.family_profile_id}::family/shared",
                "contact_ref": [
                    f"{self.profile_id}::demo/source::contact-1.vcf",
                    f"{self.profile_id}::demo/source::contact-2.vcf",
                ],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/contacts", response.location)
        self.assertNotIn("demo/source/contact-1.vcf", FakeRadicaleClient.contacts)
        self.assertNotIn("demo/source/contact-2.vcf", FakeRadicaleClient.contacts)
        self.assertIn("family/shared/contact-1.vcf", FakeRadicaleClient.contacts)
        self.assertIn("family/shared/contact-2.vcf", FakeRadicaleClient.contacts)
        demo_counts = {
            book["path"]: book["contact_count"]
            for book in self.store.get_cached_address_books(self.profile_id)
        }
        family_counts = {
            book["path"]: book["contact_count"]
            for book in self.store.get_cached_address_books(self.family_profile_id)
        }
        self.assertIsNone(demo_counts["demo/source"])
        self.assertIsNone(family_counts["family/shared"])


if __name__ == "__main__":
    unittest.main()
