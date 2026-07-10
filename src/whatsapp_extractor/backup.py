"""Locating and probing an iPhone backup.

An iPhone backup is a directory identified by ``Manifest.db`` + ``Manifest.plist``.
Whether it is encrypted is recorded in ``Manifest.plist`` (``IsEncrypted``); when
encrypted, ``Manifest.db`` itself is encrypted and must be opened via
``iphone_backup_decrypt`` with the backup password.
"""

from __future__ import annotations

import plistlib
from dataclasses import dataclass
from pathlib import Path

# The backup namespace holding all of WhatsApp's files. The engine extracts this
# domain as a whole; we filter within it by chat JID for selective extraction.
WHATSAPP_DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
WHATSAPP_APP_KEY = "group.net.whatsapp.WhatsApp.shared"


class BackupError(Exception):
    """A backup path is missing required files or is otherwise unusable."""


@dataclass
class BackupInfo:
    encrypted: bool
    product_version: str | None
    has_whatsapp: bool


class Backup:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.manifest_db = self.path / "Manifest.db"
        self.manifest_plist = self.path / "Manifest.plist"

    def validate(self) -> None:
        if not self.path.is_dir():
            raise BackupError(f"Not a directory: {self.path}")
        if not self.manifest_plist.is_file():
            raise BackupError(
                f"No Manifest.plist in {self.path} — this does not look like an iPhone backup."
            )
        if not self.manifest_db.is_file():
            raise BackupError(f"No Manifest.db in {self.path}.")

    def _load_manifest_plist(self) -> dict:
        with self.manifest_plist.open("rb") as fh:
            return plistlib.load(fh)

    def probe(self) -> BackupInfo:
        """Read backup-level facts without decrypting anything.

        ``Manifest.plist`` is always readable (it is not encrypted) and lists the
        installed apps, so WhatsApp presence and encryption state are known up
        front, before any password is needed.
        """
        self.validate()
        manifest = self._load_manifest_plist()
        encrypted = bool(manifest.get("IsEncrypted", False))
        product_version = manifest.get("Lockdown", {}).get("ProductVersion")
        applications = manifest.get("Applications", {}) or {}
        has_whatsapp = WHATSAPP_APP_KEY in applications
        return BackupInfo(
            encrypted=encrypted,
            product_version=product_version,
            has_whatsapp=has_whatsapp,
        )
