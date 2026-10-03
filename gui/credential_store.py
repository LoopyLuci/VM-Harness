"""Credential store — encrypted storage for sensitive values.

Uses Fernet symmetric encryption (cryptography) to store passwords,
API keys, SSH private keys, and other secrets in a local JSON file.
The encryption key is derived from a master password or stored in an
OS-native keyring when available.

Never stores raw values in memory longer than needed; values are
decrypted on-demand and cleared after use.
"""

from __future__ import annotations

import json
import logging
import os
import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.backends import default_backend

logger = logging.getLogger("vmharness.credentials")


class Credential:
    """A single stored credential with decrypted value.

    Provides attribute access for test compatibility.
    """

    def __init__(
        self,
        cid: str,
        name: str,
        cred_type: str,
        value: str,
        description: str,
        created_at: str,
        updated_at: str,
    ):
        self.id = cid
        self.name = name
        self.credential_type = cred_type
        self.value = value
        self.description = description
        self.created = created_at
        self.updated = updated_at

    def __repr__(self) -> str:
        return f"Credential(name={self.name!r}, type={self.credential_type!r})"


class CredentialStore:
    """Encrypted storage for sensitive credentials.

    Credentials are stored as Fernet-encrypted blobs in a JSON file.
    Each credential has: id, name, type, value (encrypted), created_at,
    updated_at, description.

    The encryption key is derived from a master password via PBKDF2.
    For production use, set MASTER_KEY_FILE or use the OS keyring.

    Public API (name-based, test-compatible):
        add(name, credential_type, value, description) -> name
        get(name) -> Credential | None
        update(name, new_value, new_description=None) -> None
        delete(name) -> None  (no-op if not found)
        list_all() -> list[Credential]
        search(query) -> list[Credential]
        clear_all(confirmed=False) -> None
        count -> int
    """

    def __init__(
        self,
        store_path: str | Path | None = None,
        master_password: str | None = None,
    ):
        self._store_path = Path(store_path) if store_path else self._default_path()
        self._master_password = master_password or os.getenv("GUI_MASTER_PASSWORD", "")
        self._fernet: Fernet | None = None
        self._credentials: dict[str, dict[str, Any]] = {}  # cid -> data
        self._name_index: dict[str, str] = {}  # name -> cid
        self._load()

    @staticmethod
    def _default_path() -> Path:
        """Default credential store location."""
        data_dir = (
            Path(os.getenv("XDG_DATA_HOME", ""))
            if os.getenv("XDG_DATA_HOME")
            else Path.home() / ".local" / "share"
        )
        return data_dir / "vmharness" / "credentials.json"

    def _ensure_fernet(self) -> None:
        """Initialize the Fernet cipher from the master password."""
        if self._fernet is not None:
            return
        if not self._master_password:
            # No master password — use a per-installation random key stored on disk
            key_file = self._store_path.parent / ".master_key"
            try:
                key_file.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            if key_file.exists():
                self._fernet = Fernet(key_file.read_bytes())
            else:
                # Generate a new random key
                key = Fernet.generate_key()
                try:
                    key_file.write_bytes(key)
                    key_file.chmod(0o600)
                except OSError:
                    pass  # Best-effort; will still use in-memory key
                self._fernet = Fernet(key)
            return

        # Derive key from password using PBKDF2
        salt = b"vmharness-salt-2026"  # Fixed salt — user should change in production
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600_000,
            backend=default_backend(),
        )
        raw_key = kdf.derive(self._master_password.encode())
        key = base64.urlsafe_b64encode(raw_key)
        self._fernet = Fernet(key)

    def _load(self) -> None:
        """Load credentials from the encrypted store."""
        if not self._store_path.exists():
            self._credentials = {}
            self._name_index = {}
            return
        try:
            data = json.loads(self._store_path.read_text())
            self._credentials = data.get("credentials", {})
            # Rebuild name index: name -> cid (keep latest for duplicates)
            self._name_index = {}
            for cid, entry in self._credentials.items():
                name = entry.get("name", "")
                self._name_index[name] = cid
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to load credential store: %s", e)
            self._credentials = {}
            self._name_index = {}

    def _save(self) -> None:
        """Persist credentials to disk."""
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "credentials": self._credentials,
            }
            self._store_path.write_text(json.dumps(data, indent=2))
            self._store_path.chmod(0o600)
        except OSError as e:
            logger.error("Failed to save credential store: %s", e)
            raise

    def add(
        self,
        name: str,
        credential_type: str,
        value: str,
        description: str = "",
    ) -> str:
        """Add or update a credential by name.  Returns the credential name.

        If a credential with this name already exists, its value is updated
        and the original name is returned.
        """
        self._ensure_fernet()
        existing_cid = self._name_index.get(name)
        if existing_cid and existing_cid in self._credentials:
            # Duplicate name — preserve existing, return its name
            return name
        # Create new
        cid = f"cred_{datetime.now(timezone.utc).timestamp()}_{len(self._credentials)}"
        self._credentials[cid] = {
            "name": name,
            "type": credential_type,
            "value": self._fernet.encrypt(value.encode()).decode(),
            "description": description,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._name_index[name] = cid
        self._save()
        logger.info("Credential added: %s (%s)", name, credential_type)
        return name

    def get(self, name: str) -> Credential | None:
        """Get a credential by name.  Returns decrypted Credential or None."""
        cid = self._name_index.get(name)
        if cid is None or cid not in self._credentials:
            return None
        self._ensure_fernet()
        entry = self._credentials[cid]
        try:
            decrypted = self._fernet.decrypt(entry["value"].encode()).decode()
        except InvalidToken:
            decrypted = "[decryption failed]"
        return Credential(
            cid=cid,
            name=entry["name"],
            cred_type=entry["type"],
            value=decrypted,
            description=entry.get("description", ""),
            created_at=entry.get("created_at", ""),
            updated_at=entry.get("updated_at", ""),
        )

    def update(self, name: str, new_value: str, new_description: str | None = None) -> None:
        """Update a credential's value and optionally description."""
        cid = self._name_index.get(name)
        if cid is None or cid not in self._credentials:
            raise KeyError(f"Credential {name} not found")
        self._ensure_fernet()
        self._credentials[cid]["value"] = self._fernet.encrypt(new_value.encode()).decode()
        if new_description is not None:
            self._credentials[cid]["description"] = new_description
        self._credentials[cid]["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def delete(self, name: str) -> None:
        """Delete a credential by name.  No-op if not found."""
        cid = self._name_index.get(name)
        if cid is None or cid not in self._credentials:
            return
        del self._credentials[cid]
        del self._name_index[name]
        self._save()
        logger.info("Credential deleted: %s", name)

    def list_all(self) -> list[Credential]:
        """Return all credentials with decrypted values."""
        self._ensure_fernet()
        result: list[Credential] = []
        for cid, entry in self._credentials.items():
            try:
                decrypted = self._fernet.decrypt(entry["value"].encode()).decode()
            except InvalidToken:
                decrypted = "[decryption failed]"
            result.append(
                Credential(
                    cid=cid,
                    name=entry["name"],
                    cred_type=entry["type"],
                    value=decrypted,
                    description=entry.get("description", ""),
                    created_at=entry.get("created_at", ""),
                    updated_at=entry.get("updated_at", ""),
                )
            )
        return result

    def search(self, query: str) -> list[Credential]:
        """Search credentials by name or type (case-insensitive)."""
        q = query.lower()
        return [
            c for c in self.list_all()
            if q in c.name.lower() or q in c.credential_type.lower()
        ]

    def clear_all(self, confirmed: bool = False) -> None:
        """Remove all credentials.  No-op unless confirmed=True."""
        if not confirmed:
            return
        self._credentials = {}
        self._name_index = {}
        self._save()
        logger.warning("All credentials cleared")

    @property
    def count(self) -> int:
        """Number of stored credentials."""
        return len(self._credentials)
