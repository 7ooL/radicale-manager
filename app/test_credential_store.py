import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from app.credential_store import CredentialStore, HAS_FERNET


class CredentialStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "profiles.db")
        self.store = CredentialStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_profile_crud_and_enabled_filter(self):
        alpha_id = self.store.create_profile(
            "Alpha",
            "https://radicale.example.test",
            "alpha",
            password="secret",
            enabled=True,
        )
        beta_id = self.store.create_profile(
            "Beta",
            "https://radicale.example.test",
            "beta",
            enabled=False,
        )

        profile = self.store.get_profile(alpha_id)
        self.assertEqual(profile["name"], "Alpha")
        self.assertEqual(profile["server_url"], "https://radicale.example.test")
        self.assertEqual(profile["username"], "alpha")
        self.assertEqual(profile["password"], "secret")
        self.assertTrue(profile["enabled"])

        enabled_profiles = self.store.get_enabled_profiles()
        self.assertEqual([p["id"] for p in enabled_profiles], [alpha_id])

        self.store.update_profile(
            beta_id,
            name="Beta Updated",
            server_url="https://other.example.test",
            username="beta-updated",
            password="new-secret",
            enabled=True,
        )
        updated = self.store.get_profile(beta_id)
        self.assertEqual(updated["name"], "Beta Updated")
        self.assertEqual(updated["server_url"], "https://other.example.test")
        self.assertEqual(updated["username"], "beta-updated")
        self.assertEqual(updated["password"], "new-secret")
        self.assertTrue(updated["enabled"])

        self.store.delete_profile(alpha_id)
        self.assertIsNone(self.store.get_profile(alpha_id))
        self.assertIsNotNone(self.store.get_profile(beta_id))

    @unittest.skipUnless(HAS_FERNET, "cryptography is not installed")
    def test_profile_password_encryption_round_trip(self):
        encrypted_store = CredentialStore(self.db_path, secret_key="test-secret")
        profile_id = encrypted_store.create_profile(
            "Encrypted",
            "https://radicale.example.test",
            "user",
            password="plain-password",
        )

        profile = encrypted_store.get_profile(profile_id)
        self.assertEqual(profile["password"], "plain-password")

        with closing(sqlite3.connect(self.db_path)) as conn:
            stored_password = conn.execute(
                "SELECT password_encrypted FROM connection_profiles WHERE id = ?",
                (profile_id,),
            ).fetchone()[0]
        self.assertNotEqual(stored_password, "plain-password")
        self.assertTrue(stored_password)

    def test_address_book_cache_insert_update_and_delete(self):
        profile_id = self.store.create_profile(
            "Books",
            "https://radicale.example.test",
            "books",
        )

        self.store.save_or_update_address_books(
            profile_id,
            [
                {
                    "displayname": "Personal",
                    "path": "books/personal",
                    "href": "/books/personal/",
                    "contact_count": 4,
                },
                {
                    "name": "Work",
                    "path": "books/work",
                    "href": "/books/work/",
                    "contact_count": 7,
                },
            ],
        )

        books = self.store.get_cached_address_books(profile_id)
        self.assertEqual([book["display_name"] for book in books], ["Personal", "Work"])
        self.assertEqual(books[0]["contact_count"], 4)

        self.store.save_or_update_address_books(
            profile_id,
            [
                {
                    "display_name": "Personal Updated",
                    "path": "books/personal",
                    "href": "/books/personal/",
                    "contact_count": 5,
                },
            ],
        )

        updated_books = self.store.get_cached_address_books(profile_id)
        personal = next(book for book in updated_books if book["path"] == "books/personal")
        self.assertEqual(personal["display_name"], "Personal Updated")
        self.assertEqual(personal["contact_count"], 5)

        self.store.delete_address_books(profile_id)
        self.assertEqual(self.store.get_cached_address_books(profile_id), [])

    def test_connection_status_updates_success_and_failure(self):
        profile_id = self.store.create_profile(
            "Status",
            "https://radicale.example.test",
            "status",
        )

        self.store.update_connection_status(profile_id, False, "bad credentials")
        failed = self.store.get_profile(profile_id)
        self.assertEqual(failed["last_error"], "bad credentials")
        self.assertIsNone(failed["last_successful_connect_at"])

        self.store.update_connection_status(profile_id, True)
        succeeded = self.store.get_profile(profile_id)
        self.assertIsNone(succeeded["last_error"])
        self.assertTrue(succeeded["last_successful_connect_at"])

    def test_feature_registry_upsert_and_delete(self):
        self.store.add_feature("Duplicates", status="planned", description="Find matches")
        self.store.add_feature("Duplicates", status="complete", description="Find matches v2")

        features = self.store.get_features()
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["name"], "Duplicates")
        self.assertEqual(features[0]["status"], "complete")
        self.assertEqual(features[0]["description"], "Find matches v2")

        self.store.delete_feature(features[0]["id"])
        self.assertEqual(self.store.get_features(), [])


if __name__ == "__main__":
    unittest.main()
