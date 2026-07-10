"""Export stage — produce HTML/JSON for the selected chats, with reactions.

This is the glue that drives the vendored KnugiHK engine over only the selected
chats, runs our reactions pass, and lays media out so the HTML is portable.

Media-path convention (from the engine): it reads each file at
``{media_folder}/Message/{ZMEDIALOCALPATH}`` and stores ``message.data`` as that
path with its first component stripped — e.g. ``Message/Media/<jid>/...``. So we
extract media under the workdir, run the engine with the workdir as CWD and
``media_folder="."``, then place the ``Message/`` tree next to the HTML.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from whatsapp_extractor.backup import Backup
from whatsapp_extractor.extract import BackupExtractor, MediaUsage
from whatsapp_extractor.link_previews import apply_link_previews
from whatsapp_extractor.reactions import apply_reactions
from whatsapp_extractor.system_messages import label_system_messages


@dataclass
class ExportResult:
    output_dir: Path
    chat_count: int
    media: MediaUsage
    reacted_messages: int
    html: bool
    json: bool


@contextlib.contextmanager
def _pushd(path: Path):
    prev = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


def export_chats(
    backup: Backup,
    jids: list[str],
    output_dir: Path,
    workdir: Path,
    *,
    password: str | None = None,
    want_html: bool = True,
    want_json: bool = False,
    media_progress: Callable[[int, int], None] | None = None,
) -> ExportResult:
    """Export ``jids`` from ``backup`` into ``output_dir``.

    ``workdir`` is a scratch directory (caller owns its lifecycle/cleanup).
    """
    from Whatsapp_Chat_Exporter import android_handler, ios_handler
    from Whatsapp_Chat_Exporter.data_model import ChatCollection, Timing

    output_dir.mkdir(parents=True, exist_ok=True)

    with BackupExtractor(backup, password=password) as extractor:
        chatdb = extractor.extract_database(workdir)
        media = extractor.extract_media(jids, workdir, progress=media_progress)

    # Build the chat model over only the selected chats.
    data = ChatCollection()
    db = sqlite3.connect(str(chatdb))
    db.row_factory = sqlite3.Row
    db.text_factory = lambda b: b.decode("utf-8", "replace")
    filter_chat = (list(jids), None)
    try:
        ios_handler.messages(db, data, ".", Timing(0), None, filter_chat, True, False)
        with _pushd(workdir):
            ios_handler.media(db, data, ".", None, filter_chat, True, False, False)
        # The engine's include filter also pulls in groups a selected 1:1 contact
        # belongs to; we want exactly the chats the user picked, so prune the rest.
        selected = set(jids)
        for jid in [j for j in data if j not in selected]:
            del data[jid]
        reacted = apply_reactions(db, data)
        apply_link_previews(db, data)
        label_system_messages(db, data)
    finally:
        db.close()

    # Render. create_html writes into output_dir; media must sit beside it.
    if want_html:
        # The engine substitutes "??" with each chat's name in the page title.
        android_handler.create_html(data, str(output_dir), headline="WhatsApp — ??")
        media_src = workdir / "Message"
        if media_src.is_dir():
            shutil.copytree(
                media_src, output_dir / "Message", dirs_exist_ok=True
            )
    if want_json:
        with (output_dir / "result.json").open("w", encoding="utf-8") as fh:
            json.dump(
                {jid: data.get_chat(jid).to_json() for jid in data},
                fh,
                ensure_ascii=False,
                indent=2,
            )

    return ExportResult(
        output_dir=output_dir,
        chat_count=len(list(data.keys())),
        media=media,
        reacted_messages=reacted,
        html=want_html,
        json=want_json,
    )
