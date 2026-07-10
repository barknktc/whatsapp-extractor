"""Attach rich link-preview cards to URL messages.

When someone shares a link, WhatsApp fetches and **caches locally** a preview
(title, description, thumbnail) so the card keeps showing even after the source
page is gone. On iOS these are ``ZMESSAGETYPE == 7`` messages; the preview lives
in the row's ``ZWAMEDIAITEM``:

* ``ZTITLE``          — the card title
* ``ZMEDIAURL``       — the canonical link
* ``ZXMPPTHUMBPATH``  — a ``.thumb`` file (a small JPEG) = the cached image
* ``ZMETADATA``       — a protobuf whose field 3 is the description

The engine renders these as a bare hyperlink. This pass populates
``message.link_preview`` so the template can draw the same card the phone shows.
The ``.thumb`` files sit under ``Media/<jid>/`` and are already copied by the
selective-media extraction, so no extra files need to be pulled.
"""

from __future__ import annotations

import sqlite3
from urllib.parse import urlsplit

from whatsapp_extractor.reactions import _iter_fields

LINK_PREVIEW_TYPE = 7
_META_DESCRIPTION = 3  # field number of the description in ZMETADATA
_DESC_MAX = 160  # trim long descriptions to keep cards compact


def _description(blob) -> str | None:
    if not blob:
        return None
    try:
        for field, wire, value in _iter_fields(blob):
            if field == _META_DESCRIPTION and wire == 2 and isinstance(value, bytes):
                text = value.decode("utf-8", "replace").strip()
                if len(text) > _DESC_MAX:
                    text = text[:_DESC_MAX].rstrip() + "…"
                return text or None
    except (IndexError, ValueError):
        return None
    return None


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    host = urlsplit(url).netloc
    return host[4:] if host.startswith("www.") else host or None


def apply_link_previews(conn: sqlite3.Connection, data) -> int:
    """Populate ``message.link_preview`` for URL-preview messages.

    Run after the message pass. Returns the number of previews attached. Messages
    are located by ``(chat JID, message Z_PK)`` — the same keys the engine used —
    so this is a pure add-on requiring no engine change.
    """
    rows = conn.execute(
        """
        SELECT cs.ZCONTACTJID, mi.ZMESSAGE, mi.ZTITLE, mi.ZMEDIAURL,
               mi.ZXMPPTHUMBPATH, mi.ZMETADATA, m.ZTEXT
        FROM ZWAMEDIAITEM mi
        JOIN ZWAMESSAGE m ON m.Z_PK = mi.ZMESSAGE
        JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
        WHERE m.ZMESSAGETYPE = ?
        """,
        (LINK_PREVIEW_TYPE,),
    )

    touched = 0
    for chat_jid, message_pk, title, media_url, thumb_path, meta, text in rows:
        url = media_url or text
        title = (title or "").strip() or None
        description = _description(meta)
        # Nothing worth drawing a card for.
        if not (title or description or thumb_path):
            continue

        chat = data.get_chat(chat_jid) if chat_jid in data else None
        if chat is None:
            continue
        message = chat.get_message(message_pk)
        if message is None:
            continue

        message.link_preview = {
            "title": title,
            "description": description,
            "url": url,
            # Same path convention the engine uses for media: "Message/" + local path.
            "thumb": ("Message/" + thumb_path) if thumb_path else None,
            "domain": _domain(url),
        }
        touched += 1
    return touched
