"""Extraction stage — pulling WhatsApp files out of the backup's blob store.

A backup stores every file as a content-addressed blob at ``<fileID[:2]>/<fileID>``
under the backup root, with the mapping (domain, relativePath) -> fileID held in
``Manifest.db``. For an unencrypted backup we read ``Manifest.db`` directly and
copy blobs; for an encrypted one we go through ``iphone_backup_decrypt``, which
decrypts ``Manifest.db`` (``manifest_db_cursor``) and each blob on demand.

This module is the single seam where the two cases diverge — everything
downstream (stats, export) sees the same extracted files either way.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from whatsapp_extractor.backup import WHATSAPP_DOMAIN, Backup

# ChatStorage.sqlite relative path within the WhatsApp shared domain.
CHATSTORAGE_RELPATH = "ChatStorage.sqlite"
# Selected-chat media lives under this prefix, namespaced by chat JID:
#   Message/Media/<jid>/<a>/<b>/<uuid>.<ext>
MEDIA_PREFIX = "Message/Media/"


class DecryptError(Exception):
    """Decryption failed — typically a wrong/missing backup password."""


# Shown when the WhatsApp message DB isn't in the (successfully read) backup.
# The usual cause is WhatsApp's own end-to-end-encrypted chat backup (an in-app
# feature, separate from iOS backup encryption): its data isn't stored in the
# device backup at all, so there is nothing here to extract.
_E2E_HINT = (
    "WhatsApp's message database (ChatStorage.sqlite) is not in this backup. "
    "If WhatsApp end-to-end encrypted backup is enabled on the phone, its chats "
    "are not included in the device backup and cannot be read here. Make an "
    "iPhone backup with WhatsApp's end-to-end encrypted backup turned off, or "
    "ensure WhatsApp finished backing up to this device backup."
)


@dataclass
class MediaUsage:
    """Per-chat on-disk media footprint in the backup — the true cost to extract."""

    file_count: int
    byte_count: int


class BackupExtractor:
    """Opens a backup (encrypted or not) and extracts files from it.

    Use as a context manager so the underlying decryption handle is closed and
    any of its temp files are cleaned up.
    """

    def __init__(self, backup: Backup, password: str | None = None):
        self.backup = backup
        self.password = password
        self._info = backup.probe()
        self._plain_manifest: sqlite3.Connection | None = None
        self._enc = None  # iphone_backup_decrypt.EncryptedBackup, lazily opened

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "BackupExtractor":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._plain_manifest is not None:
            self._plain_manifest.close()
            self._plain_manifest = None
        if self._enc is not None:
            # EncryptedBackup cleans its temp files on garbage collection.
            self._enc = None

    # -- manifest access ---------------------------------------------------

    def _open_encrypted(self):
        if self._enc is not None:
            return self._enc
        if not self.password:
            raise DecryptError("This backup is encrypted; a password is required.")
        try:
            from iphone_backup_decrypt import EncryptedBackup
        except ImportError as e:  # pragma: no cover - dependency is declared
            raise DecryptError(
                "iphone_backup_decrypt is required for encrypted backups."
            ) from e
        try:
            self._enc = EncryptedBackup(
                backup_directory=str(self.backup.path),
                passphrase=self.password,
                cleanup=True,
                check_same_thread=False,
            )
            # Force keybag unlock now so a wrong password fails fast and clearly.
            self._enc.test_decryption()
        except Exception as e:  # library raises ValueError on bad password
            self._enc = None
            raise DecryptError(f"Could not open the encrypted backup: {e}") from e
        return self._enc

    def ensure_unlocked(self) -> None:
        """Force a decryption test now (encrypted backups only).

        Lets callers verify a password before doing any real work, so a wrong
        password fails fast and clearly rather than deep inside extraction. A
        no-op for unencrypted backups.
        """
        if self._info.encrypted:
            self._open_encrypted()

    def manifest_cursor(self) -> sqlite3.Cursor:
        """A cursor over the backup's ``Files`` table, decrypted if necessary."""
        if self._info.encrypted:
            return self._open_encrypted().manifest_db_cursor()
        if self._plain_manifest is None:
            self._plain_manifest = sqlite3.connect(str(self.backup.manifest_db))
        return self._plain_manifest.cursor()

    # -- extraction --------------------------------------------------------

    def extract_database(self, dest: Path) -> Path:
        """Extract ``ChatStorage.sqlite`` into ``dest`` and return its path."""
        out = dest / "ChatStorage.sqlite"
        if self._info.encrypted:
            from iphone_backup_decrypt import RelativePath

            enc = self._open_encrypted()
            try:
                enc.extract_file(
                    relative_path=RelativePath.WHATSAPP_MESSAGES,
                    domain_like=WHATSAPP_DOMAIN,
                    output_filename=str(out),
                )
            except FileNotFoundError as e:
                raise DecryptError(_E2E_HINT) from e
            return out
        # Unencrypted: look up the blob and copy it.
        file_id = self._lookup_file_id(CHATSTORAGE_RELPATH)
        if file_id is None:
            raise DecryptError(_E2E_HINT)
        shutil.copyfile(self._blob_path(file_id), out)
        return out

    def _lookup_file_id(self, relative_path: str) -> str | None:
        cur = self.manifest_cursor()
        cur.execute(
            "SELECT fileID FROM Files WHERE domain = ? AND relativePath = ?",
            (WHATSAPP_DOMAIN, relative_path),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def _blob_path(self, file_id: str) -> Path:
        return self.backup.path / file_id[:2] / file_id

    def extract_media(
        self,
        jids: list[str],
        dest_root: Path,
        progress: "Callable[[int, int], None] | None" = None,
    ) -> MediaUsage:
        """Extract only the selected chats' media into ``dest_root``.

        Files are written at ``dest_root/<relativePath>`` (i.e.
        ``dest_root/Message/Media/<jid>/...``), which is exactly the layout the
        vendored engine reads media from. Returns the count and bytes actually
        written. ``progress(done, total)`` is called as files are extracted.
        """
        targets = self._media_files_for(jids)
        total = len(targets)
        written_bytes = 0
        for done, (file_id, rel) in enumerate(targets, start=1):
            out = dest_root / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            if self._info.encrypted:
                from iphone_backup_decrypt import EncryptedBackup  # noqa: F401

                self._open_encrypted().extract_file(
                    relative_path=rel,
                    domain_like=WHATSAPP_DOMAIN,
                    output_filename=str(out),
                )
            else:
                shutil.copyfile(self._blob_path(file_id), out)
            try:
                written_bytes += out.stat().st_size
            except OSError:
                pass
            if progress is not None:
                progress(done, total)
        return MediaUsage(file_count=total, byte_count=written_bytes)

    def _media_files_for(self, jids: list[str]) -> list[tuple[str, str]]:
        """(fileID, relativePath) for every media file under the given JIDs."""
        cur = self.manifest_cursor()
        results: list[tuple[str, str]] = []
        for jid in jids:
            prefix = f"{MEDIA_PREFIX}{jid}/"
            cur.execute(
                "SELECT fileID, relativePath FROM Files "
                "WHERE domain = ? AND relativePath LIKE ? AND flags = 1",
                (WHATSAPP_DOMAIN, prefix + "%"),
            )
            results.extend(cur.fetchall())
        return results

    # -- stats inputs ------------------------------------------------------

    def media_usage_by_jid(self) -> dict[str, MediaUsage]:
        """On-disk media footprint per chat JID, from ``Manifest.db``.

        This is exactly what selective extraction will copy, so it is the honest
        "size to extract" shown in the picker. Blob sizes are read off disk and
        work for encrypted backups too (the encrypted blob is essentially the
        same size as the plaintext).
        """
        cur = self.manifest_cursor()
        cur.execute(
            "SELECT fileID, relativePath FROM Files "
            "WHERE domain = ? AND relativePath LIKE ? AND flags = 1",
            (WHATSAPP_DOMAIN, MEDIA_PREFIX + "%"),
        )
        usage: dict[str, MediaUsage] = {}
        for file_id, rel in cur.fetchall():
            jid = _jid_from_media_path(rel)
            if jid is None:
                continue
            blob = self._blob_path(file_id)
            try:
                size = blob.stat().st_size
            except OSError:
                continue
            entry = usage.get(jid)
            if entry is None:
                usage[jid] = MediaUsage(file_count=1, byte_count=size)
            else:
                entry.file_count += 1
                entry.byte_count += size
        return usage


def _jid_from_media_path(relative_path: str) -> str | None:
    # "Message/Media/<jid>/<a>/<b>/<file>" -> "<jid>"
    parts = relative_path.split("/")
    if len(parts) >= 3 and parts[0] == "Message" and parts[1] == "Media":
        return parts[2]
    return None
