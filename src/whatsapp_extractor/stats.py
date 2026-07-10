"""Per-chat stats computed from the extracted ChatStorage.sqlite (+ Manifest).

Stats are the figures shown before export so the user can choose chats: message
count, media count & on-disk size, date range, and chat type. They come from
cheap aggregate queries and never require exporting first.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from whatsapp_extractor.extract import MediaUsage

# Core Data stores timestamps as seconds since 2001-01-01 UTC.
COCOA_EPOCH = 978307200


@dataclass
class ChatStats:
    jid: str
    name: str
    is_group: bool
    message_count: int
    media_count: int
    media_bytes: int
    first_message: datetime | None
    last_message: datetime | None

    @property
    def chat_type(self) -> str:
        return "group" if self.is_group else "1:1"


def _to_datetime(cocoa_ts: float | None) -> datetime | None:
    if cocoa_ts is None:
        return None
    return datetime.fromtimestamp(cocoa_ts + COCOA_EPOCH, tz=timezone.utc)


def compute_stats(
    chatdb: Path, media_usage: dict[str, MediaUsage]
) -> list[ChatStats]:
    """Return one ``ChatStats`` per chat session, busiest first."""
    conn = sqlite3.connect(str(chatdb))
    try:
        rows = conn.execute(
            """
            SELECT
                cs.ZCONTACTJID,
                cs.ZPARTNERNAME,
                cs.ZSESSIONTYPE,
                COUNT(m.Z_PK)                       AS message_count,
                MIN(m.ZMESSAGEDATE)                 AS first_date,
                MAX(m.ZMESSAGEDATE)                 AS last_date
            FROM ZWACHATSESSION cs
            LEFT JOIN ZWAMESSAGE m ON m.ZCHATSESSION = cs.Z_PK
            WHERE cs.ZCONTACTJID IS NOT NULL
            GROUP BY cs.Z_PK
            """
        ).fetchall()
    finally:
        conn.close()

    stats: list[ChatStats] = []
    for jid, name, _session_type, msg_count, first, last in rows:
        usage = media_usage.get(jid)
        stats.append(
            ChatStats(
                jid=jid,
                name=name or jid,
                is_group=jid.endswith("@g.us"),
                message_count=msg_count or 0,
                # Media count/size both from the backup's Manifest, so they reflect
                # exactly what selective extraction will copy for this chat.
                media_count=usage.file_count if usage else 0,
                media_bytes=usage.byte_count if usage else 0,
                first_message=_to_datetime(first),
                last_message=_to_datetime(last),
            )
        )

    stats.sort(key=lambda s: s.message_count, reverse=True)
    return stats
