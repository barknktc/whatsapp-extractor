"""High-level operations shared by the CLI and the TUI."""

from __future__ import annotations

import getpass
from typing import Callable

from whatsapp_extractor.backup import Backup
from whatsapp_extractor.extract import BackupExtractor, DecryptError
from whatsapp_extractor.stats import ChatStats, compute_stats
from whatsapp_extractor.workdir import WorkDir


def verify_password(backup: Backup, password: str) -> None:
    """Raise ``DecryptError`` if ``password`` can't unlock the encrypted backup.

    Cheap: it only forces the keybag unlock, without extracting anything.
    """
    with BackupExtractor(backup, password=password) as extractor:
        extractor.ensure_unlocked()


def prompt_password(
    backup: Backup,
    *,
    max_attempts: int = 3,
    prompt: str = "Backup password: ",
    getpass_fn: Callable[[str], str] = getpass.getpass,
    echo: Callable[[str], None] = print,
) -> str | None:
    """Prompt for the backup password, verifying it and retrying on failure.

    Returns the verified password, or ``None`` if the user cancels (empty input
    or Ctrl-C/EOF) or exhausts ``max_attempts``.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            password = getpass_fn(prompt)
        except (EOFError, KeyboardInterrupt):
            echo("")
            return None
        if not password:
            return None
        try:
            verify_password(backup, password)
            return password
        except DecryptError:
            remaining = max_attempts - attempt
            echo(
                f"Incorrect password. {remaining} attempt(s) left."
                if remaining
                else "Incorrect password."
            )
    return None


def load_stats(backup: Backup, password: str | None = None) -> list[ChatStats]:
    """Extract the chat DB into a throwaway workdir and compute per-chat stats.

    Only the small databases are touched here — no media is copied — so this is
    cheap enough to run before the user has selected anything.
    """
    with WorkDir() as work:
        with BackupExtractor(backup, password=password) as extractor:
            chatdb = extractor.extract_database(work)
            media_usage = extractor.media_usage_by_jid()
        return compute_stats(chatdb, media_usage)
