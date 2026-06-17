import logging
import os
import sqlite3
from datetime import datetime
from cryptography.fernet import Fernet, InvalidToken

LOGGER = logging.getLogger(__name__)


class CredentialStore:
    """Manage connection profiles and cached address books in SQLite.

    This class exposes the methods requested by the feature spec:
    - create_profile, update_profile, delete_profile, get_profile, get_profiles,
      get_enabled_profiles
    - save_or_update_address_books, get_cached_address_books, delete_address_books
    - update_connection_status
    """

    def __init__(self, db_path, secret_key=None):
        self.db_path = db_path
        self.fernet = self._build_fernet(secret_key)
        directory = os.path.dirname(db_path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
        self._initialize_database()
        LOGGER.debug("CredentialStore initialized with db_path=%s", db_path)

    def _build_fernet(self, secret_key):
        if not secret_key:
            return None
        # Derive a 32-byte key from the secret and make a Fernet instance
        digest = __import__("hashlib").sha256(secret_key.encode("utf-8")).digest()
        token = __import__("base64").urlsafe_b64encode(digest)
        return Fernet(token)

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _initialize_database(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS connection_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    server_url TEXT NOT NULL,
                    username TEXT NOT NULL,
                    password_encrypted TEXT,
                    enabled INTEGER DEFAULT 1,
                    created_at TEXT,
                    updated_at TEXT,
                    last_successful_connect_at TEXT,
                    last_error TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS address_books (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id INTEGER NOT NULL,
                    display_name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    href TEXT,
                    contact_count INTEGER,
                    enabled INTEGER DEFAULT 1,
                    last_seen_at TEXT,
                    UNIQUE(profile_id, path)
                )
                """
            )
            conn.commit()
        LOGGER.debug("CredentialStore database initialized")

    def _encrypt(self, text):
        if text is None:
            return None
        if not self.fernet:
            return text
        return self.fernet.encrypt(text.encode("utf-8")).decode("utf-8")

    def _decrypt(self, text):
        if text is None:
            return None
        if not self.fernet:
            return text
        try:
            return self.fernet.decrypt(text.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            LOGGER.warning("Failed to decrypt stored credentials with provided secret")
            return text

    # Profile CRUD
    def create_profile(self, name, server_url, username, password=None, enabled=True):
        now = datetime.utcnow().isoformat() + "Z"
        encrypted = self._encrypt(password) if password else None
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO connection_profiles (name, server_url, username, password_encrypted, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, server_url, username, encrypted, 1 if enabled else 0, now, now),
            )
            conn.commit()
            profile_id = cur.lastrowid
        LOGGER.info("Created profile %s (%s)", name, profile_id)
        return profile_id

    def update_profile(self, profile_id, name=None, server_url=None, username=None, password=None, enabled=None):
        now = datetime.utcnow().isoformat() + "Z"
        fields = []
        params = []
        if name is not None:
            fields.append("name = ?")
            params.append(name)
        if server_url is not None:
            fields.append("server_url = ?")
            params.append(server_url)
        if username is not None:
            fields.append("username = ?")
            params.append(username)
        if password is not None:
            fields.append("password_encrypted = ?")
            params.append(self._encrypt(password))
        if enabled is not None:
            fields.append("enabled = ?")
            params.append(1 if enabled else 0)
        fields.append("updated_at = ?")
        params.append(now)
        params.append(profile_id)
        set_clause = ", ".join(fields)
        with self._connect() as conn:
            conn.execute(f"UPDATE connection_profiles SET {set_clause} WHERE id = ?", params)
            conn.commit()
        LOGGER.info("Updated profile %s", profile_id)

    def delete_profile(self, profile_id):
        with self._connect() as conn:
            conn.execute("DELETE FROM address_books WHERE profile_id = ?", (profile_id,))
            conn.execute("DELETE FROM connection_profiles WHERE id = ?", (profile_id,))
            conn.commit()
        LOGGER.info("Deleted profile and cached books %s", profile_id)

    def get_profile(self, profile_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, server_url, username, password_encrypted, enabled, created_at, updated_at, last_successful_connect_at, last_error FROM connection_profiles WHERE id = ?",
                (profile_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "name": row[1],
                "server_url": row[2],
                "username": row[3],
                "password": self._decrypt(row[4]) if row[4] else None,
                "enabled": bool(row[5]),
                "created_at": row[6],
                "updated_at": row[7],
                "last_successful_connect_at": row[8],
                "last_error": row[9],
            }

    def get_profiles(self):
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT id, name, server_url, username, enabled, created_at, updated_at, last_successful_connect_at, last_error FROM connection_profiles ORDER BY name"
            )
            rows = cursor.fetchall()
            profiles = [
                {
                    "id": r[0],
                    "name": r[1],
                    "server_url": r[2],
                    "username": r[3],
                    "enabled": bool(r[4]),
                    "created_at": r[5],
                    "updated_at": r[6],
                    "last_successful_connect_at": r[7],
                    "last_error": r[8],
                }
                for r in rows
            ]
        return profiles

    def get_enabled_profiles(self):
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT id, name, server_url, username, enabled, created_at, updated_at, last_successful_connect_at, last_error FROM connection_profiles WHERE enabled = 1 ORDER BY name"
            )
            rows = cursor.fetchall()
            profiles = [
                {
                    "id": r[0],
                    "name": r[1],
                    "server_url": r[2],
                    "username": r[3],
                    "enabled": bool(r[4]),
                    "created_at": r[5],
                    "updated_at": r[6],
                    "last_successful_connect_at": r[7],
                    "last_error": r[8],
                }
                for r in rows
            ]
        return profiles

    # Address book cache
    def save_or_update_address_books(self, profile_id, books):
        now = datetime.utcnow().isoformat() + "Z"
        with self._connect() as conn:
            for b in books:
                display_name = b.get("displayname") or b.get("name") or b.get("display_name")
                path = b.get("path")
                href = b.get("href")
                contact_count = b.get("contact_count")
                # Try update, insert if not exists
                conn.execute(
                    "INSERT INTO address_books (profile_id, display_name, path, href, contact_count, enabled, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(profile_id, path) DO UPDATE SET display_name=excluded.display_name, href=excluded.href, contact_count=excluded.contact_count, last_seen_at=excluded.last_seen_at",
                    (profile_id, display_name, path, href, contact_count, 1, now),
                )
            conn.commit()
        LOGGER.info("Saved/updated %d address books for profile %s", len(books), profile_id)

    def get_cached_address_books(self, profile_id):
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT id, profile_id, display_name, path, href, contact_count, enabled, last_seen_at FROM address_books WHERE profile_id = ? ORDER BY display_name",
                (profile_id,),
            )
            rows = cursor.fetchall()
            books = [
                {
                    "id": r[0],
                    "profile_id": r[1],
                    "display_name": r[2],
                    "path": r[3],
                    "href": r[4],
                    "contact_count": r[5],
                    "enabled": bool(r[6]),
                    "last_seen_at": r[7],
                }
                for r in rows
            ]
        return books

    def delete_address_books(self, profile_id):
        with self._connect() as conn:
            conn.execute("DELETE FROM address_books WHERE profile_id = ?", (profile_id,))
            conn.commit()
        LOGGER.info("Deleted cached address books for profile %s", profile_id)

    def update_connection_status(self, profile_id, success, error_message=None):
        now = datetime.utcnow().isoformat() + "Z"
        with self._connect() as conn:
            if success:
                conn.execute(
                    "UPDATE connection_profiles SET last_successful_connect_at = ?, last_error = NULL, updated_at = ? WHERE id = ?",
                    (now, now, profile_id),
                )
            else:
                conn.execute(
                    "UPDATE connection_profiles SET last_error = ?, updated_at = ? WHERE id = ?",
                    (error_message or "", now, profile_id),
                )
            conn.commit()
        LOGGER.debug("Updated connection status for %s success=%s", profile_id, success)
