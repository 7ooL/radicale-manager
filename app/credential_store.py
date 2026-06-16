import os
import sqlite3
import hashlib
import base64
from datetime import datetime
from cryptography.fernet import Fernet, InvalidToken


class CredentialStore:
    def __init__(self, db_path, secret_key=None):
        self.db_path = db_path
        self.fernet = self._build_fernet(secret_key)
        directory = os.path.dirname(db_path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
        self._initialize_database()

    def _build_fernet(self, secret_key):
        if not secret_key:
            return None
        digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
        token = base64.urlsafe_b64encode(digest)
        return Fernet(token)

    def _initialize_database(self):
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

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _encrypt(self, text):
        if not self.fernet or text is None:
            return text
        return self.fernet.encrypt(text.encode("utf-8")).decode("utf-8")

    def _decrypt(self, text):
        if not self.fernet or text is None:
            return text
        try:
            return self.fernet.decrypt(text.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            return text

    def save_profile(self, name, server_url, username, password, save_password=False):
        encrypted = self._encrypt(password) if save_password and password else None
        created_at = datetime.utcnow().isoformat() + "Z"
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO profiles (name, server_url, username, password, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, server_url, username, encrypted, created_at),
            )
            conn.commit()

    def get_profiles(self):
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
            return profiles

    def get_profile(self, profile_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, server_url, username, password, created_at FROM profiles WHERE id = ?",
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
                "created_at": row[5],
            }
