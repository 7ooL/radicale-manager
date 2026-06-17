import logging
import os
import sqlite3
import hashlib
import base64
from datetime import datetime
from cryptography.fernet import Fernet, InvalidToken

LOGGER = logging.getLogger(__name__)


class CredentialStore:
    """Persist Radicale connection profiles and encrypted credentials."""

    def __init__(self, db_path, secret_key=None):
        self.db_path = db_path
        self.fernet = self._build_fernet(secret_key)
        directory = os.path.dirname(db_path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
        self._initialize_database()
        LOGGER.debug("CredentialStore initialized with db_path=%s", db_path)

    def _build_fernet(self, secret_key):
        """Create a Fernet cipher for secure profile password storage."""
        if not secret_key:
            return None
        digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
        token = base64.urlsafe_b64encode(digest)
        return Fernet(token)

    def _initialize_database(self):
        """Create the profile table if it does not already exist."""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,
                    server_url TEXT NOT NULL,
                    username TEXT NOT NULL,
                    password TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
        LOGGER.debug("CredentialStore database initialized")

    def _connect(self):
        """Open a SQLite connection to the profile database."""
        return sqlite3.connect(self.db_path)

    def _encrypt(self, text):
        """Encrypt sensitive profile text using Fernet if available."""
        if not self.fernet or text is None:
            return text
        return self.fernet.encrypt(text.encode("utf-8")).decode("utf-8")

    def _decrypt(self, text):
        """Decrypt stored profile text if encryption is configured."""
        if not self.fernet or text is None:
            return text
        try:
            return self.fernet.decrypt(text.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            LOGGER.warning("Failed to decrypt profile data with provided secret")
            return text

    def save_profile(self, name, server_url, username, password, save_password=False):
        """Save or update a profile, optionally encrypting the password."""
        encrypted = self._encrypt(password) if save_password and password else None
        created_at = datetime.utcnow().isoformat() + "Z"
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO profiles (name, server_url, username, password, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, server_url, username, encrypted, created_at),
            )
            conn.commit()
        LOGGER.info("Saved profile %s for server %s", name, server_url)

    def get_profiles(self):
        """Return all stored profiles with decrypted passwords when available."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT id, name, server_url, username, password, created_at FROM profiles ORDER BY name"
            )
            profiles = []
            for row in cursor.fetchall():
                profiles.append(
                    {
                        "id": row[0],
                        "name": row[1],
                        "server_url": row[2],
                        "username": row[3],
                        "password": self._decrypt(row[4]) if row[4] else None,
                        "created_at": row[5],
                    }
                )
        LOGGER.debug("Loaded %d profiles", len(profiles))
        return profiles

    def get_profile(self, profile_id):
        """Return a single profile by ID, with decrypted password if stored."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, server_url, username, password, created_at FROM profiles WHERE id = ?",
                (profile_id,),
            ).fetchone()
            if not row:
                LOGGER.debug("No profile found with id %s", profile_id)
                return None
            profile = {
                "id": row[0],
                "name": row[1],
                "server_url": row[2],
                "username": row[3],
                "password": self._decrypt(row[4]) if row[4] else None,
                "created_at": row[5],
            }
        LOGGER.debug("Loaded profile %s", profile_id)
        return profile
