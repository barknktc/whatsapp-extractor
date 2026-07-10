"""A temporary working directory that auto-deletes on exit.

Decrypted/extracted data (the chat database, then selected media) is sensitive,
so it lives in a temp workdir that is removed when we are done — nothing of the
user's chat data is left on disk except the export they explicitly asked for.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from types import TracebackType


class WorkDir:
    def __init__(self, keep: bool = False):
        self.keep = keep
        self.path: Path | None = None

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="whatsapp-extractor-"))
        return self.path

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.path and self.path.exists() and not self.keep:
            shutil.rmtree(self.path, ignore_errors=True)
