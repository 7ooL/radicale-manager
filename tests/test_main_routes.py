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

    def test_edit_connection_get_prefills_existing_profile_data(self):
        response = self.client.get(f"/connections/{self.profile_id}/edit")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Edit Connection", response.data)
        self.assertIn(b'value="Demo"', response.data)
        self.assertIn(b'value="http://radicale.example.test"', response.data)
        self.assertIn(b'value="demo"', response.data)
        self.assertIn(b"Leave blank to keep the current password.", response.data)

    def test_connection_detail_page_shows_address_book_management(self):
        response = self.client.get(f"/connections/{self.profile_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Demo", response.data)
        self.assertIn(b"Address Book", response.data)
        self.assertIn(b"Source", response.data)

    def test_security_settings_page_shows_encryption_status(self):
        response = self.client.get("/system/security")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Credential Security", response.data)
        self.assertIn(b"Profile secret", response.data)

    def test_pwa_manifest_and_service_worker_routes_exist(self):
        manifest = self.client.get("/manifest.json")
        self.assertEqual(manifest.status_code, 200)
        self.assertIn(b'"name": "Radicale Manager"', manifest.data)

        service_worker = self.client.get("/service-worker.js")
        self.assertEqual(service_worker.status_code, 200)
        self.assertIn(b"self.addEventListener", service_worker.data)

    def test_create_addressbook_uses_next_redirect_when_provided(self):
        response = self.client.post(
            f"/connections/{self.profile_id}/addressbooks/create",
            data={"display_name": "", "next": f"/connections/{self.profile_id}"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith(f"/connections/{self.profile_id}"))

    def test_edit_connection_post_updates_core_fields_and_keeps_password_when_blank(self):
        response = self.client.post(
            f"/connections/{self.profile_id}/edit",
            data={
                "profile_name": "Demo Updated",
                "server_url": "https://radicale.updated.example.test",
                "username": "demo-updated",
                "password": "",
                "enabled": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/connections", response.location)
        profile = self.store.get_profile(self.profile_id)
        self.assertEqual(profile["name"], "Demo Updated")
        self.assertEqual(profile["server_url"], "https://radicale.updated.example.test")
        self.assertEqual(profile["username"], "demo-updated")
        self.assertEqual(profile["password"], "secret")
        self.assertTrue(profile["enabled"])

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

    def test_bulk_copy_keeps_source_and_invalidates_destination_count(self):
        response = self.client.post(
            f"/profiles/{self.profile_id}/books/demo/source/contacts/bulk",
            data={
                "bulk_action": "copy",
                "dest": f"{self.profile_id}::demo/dest",
                "contact_filename": ["contact-1.vcf", "contact-2.vcf"],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("demo/source/contact-1.vcf", FakeRadicaleClient.contacts)
        self.assertIn("demo/source/contact-2.vcf", FakeRadicaleClient.contacts)
        self.assertIn("demo/dest/contact-1.vcf", FakeRadicaleClient.contacts)
        self.assertIn("demo/dest/contact-2.vcf", FakeRadicaleClient.contacts)
        books = self.store.get_cached_address_books(self.profile_id)
        counts = {book["path"]: book["contact_count"] for book in books}
        self.assertEqual(counts["demo/source"], 1)
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

    def test_global_search_filters_by_profile_and_query(self):
        response = self.client.get(f"/contacts?profile_id={self.profile_id}&has_email=1&q=second")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Second Contact", response.data)
        self.assertNotIn(b"Demo Contact", response.data)

    def test_global_quality_scan_page_shows_duplicate_and_issue_summary(self):
        response = self.client.get("/contacts/quality")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Global Quality Scan", response.data)
        self.assertIn(b"Possible Duplicates", response.data)
        self.assertIn(b"Missing Fields", response.data)
        self.assertIn(b"Warnings & Recommendations", response.data)
        self.assertIn(b"Average Health Score", response.data)
        self.assertIn(b"Low Health Contacts", response.data)
        self.assertIn(b"Match score", response.data)
        self.assertIn(b"Demo / Source", response.data)

    def test_global_quality_scan_respects_profile_scope_filter(self):
        FakeRadicaleClient.contacts["family/shared/contact-3.vcf"] = (
            "BEGIN:VCARD\n"
            "VERSION:3.0\n"
            "UID:contact-3\n"
            "FN:Family Contact\n"
            "N:Contact;Family;;;\n"
            "END:VCARD\n"
        )

        scoped = self.client.get(f"/contacts/quality?profile_ids={self.profile_id}")
        all_scopes = self.client.get("/contacts/quality")

        self.assertEqual(scoped.status_code, 200)
        self.assertEqual(all_scopes.status_code, 200)
        self.assertIn(b"2 contacts scanned across selected connections and books.", scoped.data)
        self.assertIn(b"3 contacts scanned across selected connections and books.", all_scopes.data)

    def test_global_quality_scan_detects_empty_and_deprecated_field_warnings(self):
        FakeRadicaleClient.contacts["demo/source/contact-empty.vcf"] = (
            "BEGIN:VCARD\n"
            "VERSION:3.0\n"
            "UID:contact-empty\n"
            "END:VCARD\n"
        )
        FakeRadicaleClient.contacts["demo/source/contact-deprecated.vcf"] = (
            "BEGIN:VCARD\n"
            "VERSION:3.0\n"
            "UID:contact-deprecated\n"
            "FN:Legacy Contact\n"
            "LABEL:Old Label Value\n"
            "END:VCARD\n"
        )

        response = self.client.get("/contacts/quality")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Empty contact", response.data)
        self.assertIn(b"Deprecated fields", response.data)
        self.assertIn(b"LABEL", response.data)
        self.assertIn(b"Recommendation:", response.data)

    def test_global_quality_scan_detects_similar_name_duplicates_with_confidence(self):
        FakeRadicaleClient.contacts["demo/source/contact-sim-1.vcf"] = (
            "BEGIN:VCARD\n"
            "VERSION:3.0\n"
            "UID:contact-sim-1\n"
            "FN:Alex Rivera\n"
            "N:Rivera;Alex;;;\n"
            "EMAIL:alex1@example.test\n"
            "TEL:555-0101\n"
            "END:VCARD\n"
        )
        FakeRadicaleClient.contacts["demo/source/contact-sim-2.vcf"] = (
            "BEGIN:VCARD\n"
            "VERSION:3.0\n"
            "UID:contact-sim-2\n"
            "FN:Alec Rivera\n"
            "N:Rivera;Alec;;;\n"
            "EMAIL:alec2@example.test\n"
            "TEL:555-0102\n"
            "END:VCARD\n"
        )

        response = self.client.get("/contacts/quality")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Name Similarity match: Alex Rivera ~ Alec Rivera", response.data)
        self.assertIn(b"Confidence", response.data)

    def test_quality_duplicate_action_queues_event_non_destructively(self):
        response = self.client.post(
            "/quality/duplicate-action",
            data={
                "action": "review",
                "duplicate_type": "Email",
                "duplicate_value": "demo@example.test",
                "match_score": "100",
                "confidence": "High",
                "scope": "global",
                "next": "/contacts/quality",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/contacts/quality"))
        action_events = self.store.get_recent_events(limit=5, actions=["quality_action"])
        self.assertTrue(action_events)
        latest = action_events[0]
        self.assertEqual(latest["details"].get("action"), "review")
        self.assertEqual(latest["details"].get("duplicate_type"), "Email")

    def test_global_quality_scan_shows_queue_stage_for_duplicate_group(self):
        self.store.add_event(
            "quality_action",
            details={
                "action": "review",
                "label": "Mark for review",
                "scope": "global",
                "duplicate_type": "Email",
                "duplicate_value": "demo@example.test",
                "match_score": "100",
                "confidence": "High",
                "status": "queued",
                "queue_key": "global|||Email|demo@example.test|review",
            },
            source="app",
        )

        response = self.client.get("/contacts/quality")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Mark for review: Ready", response.data)
        self.assertIn(b"Mark for review (Current: Ready)", response.data)

    def test_book_quality_scan_shows_merge_preview_link_for_queued_recommendation(self):
        queue_key = f"book|{self.profile_id}|demo/source|Email|demo@example.test|recommend_merge"
        self.store.add_event(
            "quality_action",
            profile_id=self.profile_id,
            collection_path="demo/source",
            details={
                "action": "recommend_merge",
                "label": "Recommend merge",
                "scope": "book",
                "duplicate_type": "Email",
                "duplicate_value": "demo@example.test",
                "match_score": "100",
                "confidence": "High",
                "status": "queued",
                "queue_key": queue_key,
                "duplicate_contacts": [
                    {
                        "display": "Demo Contact",
                        "profile_id": self.profile_id,
                        "profile_name": "Demo",
                        "book_path": "demo/source",
                        "book_name": "Source",
                        "filename": "contact-1.vcf",
                    },
                    {
                        "display": "Second Contact",
                        "profile_id": self.profile_id,
                        "profile_name": "Demo",
                        "book_path": "demo/source",
                        "book_name": "Source",
                        "filename": "contact-2.vcf",
                    },
                ],
            },
            source="app",
        )

        response = self.client.get(f"/profiles/{self.profile_id}/books/demo/source/contacts/quality")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Recommend merge: Ready", response.data)
        self.assertIn(b"Open merge preview", response.data)

    def test_quality_review_queue_lists_quality_action_items(self):
        self.store.add_event(
            "quality_action",
            profile_id=self.profile_id,
            collection_path="demo/source",
            details={
                "action": "review",
                "label": "Mark for review",
                "scope": "book",
                "duplicate_type": "Email",
                "duplicate_value": "demo@example.test",
                "match_score": "100",
                "confidence": "High",
                "status": "queued",
                "queue_key": "book|1|demo/source|Email|demo@example.test|review",
            },
            source="app",
        )

        response = self.client.get("/quality/review-queue")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Quality Review Queue", response.data)
        self.assertIn(b"Mark for review", response.data)
        self.assertIn(b"demo@example.test", response.data)
        self.assertIn(b"Ready", response.data)
        self.assertIn(b"Update workflow stage", response.data)
        self.assertIn(b"Active Queue", response.data)
        self.assertIn(b"Closed Items", response.data)
        self.assertIn(b"Workflow Stages", response.data)

    def test_quality_review_queue_shows_merge_preview_cards(self):
        self.store.add_event(
            "quality_action",
            details={
                "action": "recommend_merge",
                "label": "Recommend merge",
                "scope": "global",
                "duplicate_type": "Name Similarity",
                "duplicate_value": "Alex Rivera ~ Alec Rivera",
                "match_score": "85",
                "confidence": "Medium",
                "status": "queued",
                "queue_key": "global|||Name Similarity|Alex Rivera ~ Alec Rivera|recommend_merge",
                "duplicate_contacts": [
                    {
                        "display": "Demo Contact",
                        "email": "alex@example.test",
                        "phone": "555-0101",
                        "profile_id": self.profile_id,
                        "profile_name": "Demo",
                        "book_path": "demo/source",
                        "book_name": "Source",
                        "filename": "contact-1.vcf",
                    },
                    {
                        "display": "Second Contact",
                        "email": "alec@example.test",
                        "phone": "555-0102",
                        "profile_id": self.profile_id,
                        "profile_name": "Demo",
                        "book_path": "demo/source",
                        "book_name": "Source",
                        "filename": "contact-2.vcf",
                    },
                ],
            },
            source="app",
        )

        response = self.client.get("/quality/review-queue")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Contact A", response.data)
        self.assertIn(b"Contact B", response.data)
        self.assertIn(b"Suggested Result:", response.data)
        self.assertIn(b"Overall Match Score", response.data)
        self.assertIn(b"Preview and Apply Mitigation", response.data)

    def test_quality_review_queue_explains_when_merge_preview_is_unavailable(self):
        self.store.add_event(
            "quality_action",
            details={
                "action": "recommend_merge",
                "label": "Recommend merge",
                "scope": "global",
                "duplicate_type": "Name Similarity",
                "duplicate_value": "Alex Rivera ~ Alec Rivera",
                "match_score": "85",
                "confidence": "Medium",
                "status": "queued",
                "queue_key": "global|||Name Similarity|Alex Rivera ~ Alec Rivera|recommend_merge",
                "duplicate_contacts": [
                    {"display": "Alex Rivera", "email": "alex@example.test", "phone": "555-0101"},
                    {"display": "Alec Rivera", "email": "alec@example.test", "phone": "555-0102"},
                ],
            },
            source="app",
        )

        response = self.client.get("/quality/review-queue")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Preview data is missing for this queue item", response.data)
        self.assertIn(b"Open Merge Preview", response.data)

    def test_quality_merge_preview_rebuilds_missing_references_for_email_duplicate(self):
        queue_key = "global|||Email|demo@example.test|recommend_merge"
        self.store.add_event(
            "quality_action",
            details={
                "action": "recommend_merge",
                "label": "Recommend merge",
                "scope": "global",
                "duplicate_type": "Email",
                "duplicate_value": "demo@example.test",
                "match_score": "100",
                "confidence": "High",
                "status": "queued",
                "queue_key": queue_key,
                "duplicate_contacts": [
                    {"display": "Contact One", "email": "demo@example.test"},
                    {"display": "Contact Two", "email": "demo@example.test"},
                ],
            },
            source="app",
        )

        response = self.client.get(f"/quality/review-queue/merge-preview?queue_key={queue_key}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Merge Mitigation Preview", response.data)
        self.assertIn(b"Destination Address Book", response.data)

    def test_quality_review_queue_status_transition_updates_item_state(self):
        queue_key = "global|||Email|demo@example.test|review"
        self.store.add_event(
            "quality_action",
            details={
                "action": "review",
                "label": "Mark for review",
                "scope": "global",
                "duplicate_type": "Email",
                "duplicate_value": "demo@example.test",
                "match_score": "100",
                "confidence": "High",
                "status": "queued",
                "queue_key": queue_key,
            },
            source="app",
        )

        response = self.client.post(
            "/quality/review-queue/status",
            data={"queue_key": queue_key, "status": "in_review", "next": "/quality/review-queue"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/quality/review-queue"))
        events = self.store.get_recent_events(limit=5, actions=["quality_action_status"])
        self.assertTrue(events)
        latest = events[0]
        self.assertEqual(latest["details"].get("queue_key"), queue_key)
        self.assertEqual(latest["details"].get("status"), "in_review")

    def test_quality_merge_preview_page_loads_with_destination_options(self):
        queue_key = "global|||Name Similarity|Demo Contact ~ Second Contact|recommend_merge"
        self.store.add_event(
            "quality_action",
            details={
                "action": "recommend_merge",
                "label": "Recommend merge",
                "scope": "global",
                "duplicate_type": "Name Similarity",
                "duplicate_value": "Demo Contact ~ Second Contact",
                "match_score": "85",
                "confidence": "Medium",
                "status": "queued",
                "queue_key": queue_key,
                "duplicate_contacts": [
                    {
                        "display": "Demo Contact",
                        "profile_id": self.profile_id,
                        "profile_name": "Demo",
                        "book_path": "demo/source",
                        "book_name": "Source",
                        "filename": "contact-1.vcf",
                    },
                    {
                        "display": "Second Contact",
                        "profile_id": self.profile_id,
                        "profile_name": "Demo",
                        "book_path": "demo/source",
                        "book_name": "Source",
                        "filename": "contact-2.vcf",
                    },
                ],
            },
            source="app",
        )

        response = self.client.get(f"/quality/review-queue/merge-preview?queue_key={queue_key}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Merge Mitigation Preview", response.data)
        self.assertIn(b"Destination Address Book", response.data)
        self.assertIn(b"Apply Mitigation", response.data)
        self.assertIn(b"Use Contact A Values", response.data)
        self.assertIn(b"Use Contact B Values", response.data)

    def test_quality_merge_apply_creates_merged_contact_and_marks_mitigated(self):
        queue_key = "global|||Name Similarity|Demo Contact ~ Second Contact|recommend_merge"
        self.store.add_event(
            "quality_action",
            details={
                "action": "recommend_merge",
                "label": "Recommend merge",
                "scope": "global",
                "duplicate_type": "Name Similarity",
                "duplicate_value": "Demo Contact ~ Second Contact",
                "match_score": "85",
                "confidence": "Medium",
                "status": "queued",
                "queue_key": queue_key,
                "duplicate_contacts": [
                    {
                        "display": "Demo Contact",
                        "profile_id": self.profile_id,
                        "profile_name": "Demo",
                        "book_path": "demo/source",
                        "book_name": "Source",
                        "filename": "contact-1.vcf",
                    },
                    {
                        "display": "Second Contact",
                        "profile_id": self.profile_id,
                        "profile_name": "Demo",
                        "book_path": "demo/source",
                        "book_name": "Source",
                        "filename": "contact-2.vcf",
                    },
                ],
            },
            source="app",
        )

        response = self.client.post(
            "/quality/review-queue/merge-apply",
            data={
                "queue_key": queue_key,
                "full_name": "Merged Demo Contact",
                "organization": "",
                "job_title": "",
                "emails": "merged@example.test",
                "phones": "555-0109",
                "note": "Merged record",
                "destination": f"{self.profile_id}::demo/dest",
                "confirm_merge": "yes",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/quality/review-queue?status=resolved", response.location)
        self.assertTrue(any(path.startswith("demo/dest/") for path in FakeRadicaleClient.contacts.keys()))
        self.assertNotIn("demo/source/contact-1.vcf", FakeRadicaleClient.contacts)
        self.assertNotIn("demo/source/contact-2.vcf", FakeRadicaleClient.contacts)
        status_events = self.store.get_recent_events(limit=5, actions=["quality_action_status"])
        self.assertTrue(status_events)
        self.assertEqual(status_events[0]["details"].get("status"), "resolved")
        self.assertEqual(status_events[0]["details"].get("source_deleted_count"), 2)

    def test_quality_merge_preview_rejects_closed_item(self):
        queue_key = "global|||Name Similarity|Demo Contact ~ Second Contact|recommend_merge"
        self.store.add_event(
            "quality_action",
            details={
                "action": "recommend_merge",
                "label": "Recommend merge",
                "scope": "global",
                "duplicate_type": "Name Similarity",
                "duplicate_value": "Demo Contact ~ Second Contact",
                "match_score": "85",
                "confidence": "Medium",
                "status": "queued",
                "queue_key": queue_key,
                "duplicate_contacts": [
                    {
                        "display": "Demo Contact",
                        "profile_id": self.profile_id,
                        "profile_name": "Demo",
                        "book_path": "demo/source",
                        "book_name": "Source",
                        "filename": "contact-1.vcf",
                    },
                    {
                        "display": "Second Contact",
                        "profile_id": self.profile_id,
                        "profile_name": "Demo",
                        "book_path": "demo/source",
                        "book_name": "Source",
                        "filename": "contact-2.vcf",
                    },
                ],
            },
            source="app",
        )
        self.store.add_event(
            "quality_action_status",
            details={"queue_key": queue_key, "status": "resolved", "status_label": "Mitigated"},
            source="app",
        )

        response = self.client.get(f"/quality/review-queue/merge-preview?queue_key={queue_key}")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/quality/review-queue?status=resolved", response.location)

    def test_quality_review_queue_closed_items_hide_actions_and_show_compact_result(self):
        queue_key = "global|||Email|demo@example.test|recommend_merge"
        self.store.add_event(
            "quality_action",
            details={
                "action": "recommend_merge",
                "label": "Recommend merge",
                "scope": "global",
                "duplicate_type": "Email",
                "duplicate_value": "demo@example.test",
                "match_score": "100",
                "confidence": "High",
                "status": "queued",
                "queue_key": queue_key,
                "duplicate_contacts": [
                    {
                        "display": "Demo Contact",
                        "profile_id": self.profile_id,
                        "profile_name": "Demo",
                        "book_path": "demo/source",
                        "book_name": "Source",
                        "filename": "contact-1.vcf",
                    },
                    {
                        "display": "Second Contact",
                        "profile_id": self.profile_id,
                        "profile_name": "Demo",
                        "book_path": "demo/source",
                        "book_name": "Source",
                        "filename": "contact-2.vcf",
                    },
                ],
            },
            source="app",
        )
        self.store.add_event(
            "quality_action_status",
            details={
                "queue_key": queue_key,
                "status": "resolved",
                "status_label": "Mitigated",
                "mitigation": "merge_applied_and_sources_deleted",
                "dest_path": "demo/dest",
                "source_deleted_count": 2,
                "source_total_count": 2,
            },
            source="app",
        )

        response = self.client.get("/quality/review-queue?status=resolved")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Mitigation result:", response.data)
        self.assertIn(b"source duplicates removed", response.data)
        self.assertNotIn(b"Preview and Apply Mitigation", response.data)
        self.assertNotIn(b"Update workflow stage", response.data)

    def test_contact_transfer_page_lists_destinations(self):
        response = self.client.get(
            f"/profiles/{self.profile_id}/books/demo/source/contacts/contact-1.vcf/transfer?action=move"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Move Contact", response.data)
        self.assertIn(b"Demo / Destination", response.data)

    def test_address_books_page_lists_cached_books(self):
        response = self.client.get("/books")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Address Books", response.data)
        self.assertIn(b"Source", response.data)
        self.assertIn(b"Destination", response.data)

    def test_system_menu_page_lists_advanced_links(self):
        response = self.client.get("/system")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Settings", response.data)
        self.assertIn(b"Connections", response.data)
        self.assertIn(b"Event Log", response.data)
        self.assertIn(b"Quality Review Queue", response.data)
        self.assertIn(b"Routes Explorer", response.data)
        self.assertIn(b"Health JSON", response.data)

    def test_system_events_page_lists_recent_operation_entries(self):
        self.store.add_event(
            "delete",
            profile_id=self.profile_id,
            collection_path="demo/source",
            contact_filename="contact-1.vcf",
            details={"reason": "cleanup"},
        )

        response = self.client.get("/system/events")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Event Log", response.data)
        self.assertIn(b"Deleted contact", response.data)
        self.assertIn(b"Demo", response.data)
        self.assertIn(b"contact-1.vcf", response.data)
        self.assertIn(b"reason: cleanup", response.data)

    def test_quality_scan_persists_summary_for_dashboard_rollup(self):
        quality_response = self.client.get(f"/profiles/{self.profile_id}/books/demo/source/contacts/quality")
        self.assertEqual(quality_response.status_code, 200)
        self.assertIn(b"Average Health Score", quality_response.data)
        self.assertIn(b"Warnings & Recommendations", quality_response.data)
        self.assertIn(b"Lowest Health Contacts", quality_response.data)

        quality_events = self.store.get_recent_events(limit=10, actions=["quality_scan"])
        self.assertTrue(quality_events)
        latest = quality_events[0]
        self.assertEqual(latest["profile_id"], self.profile_id)
        self.assertEqual(latest["collection_path"], "demo/source")
        self.assertEqual(latest["details"].get("duplicate_groups"), 1)
        self.assertEqual(latest["details"].get("issues"), 2)

        dashboard = self.client.get("/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Duplicate Groups", dashboard.data)
        self.assertIn(b"Last Quality Scan", dashboard.data)
        self.assertNotIn(b"No scan results yet", dashboard.data)


if __name__ == "__main__":
    unittest.main()
