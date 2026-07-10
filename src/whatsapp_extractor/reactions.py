"""iOS reaction decoding — the piece KnugiHK's iOS handler is missing.

Reactions are not their own table; they live inside the per-message receipt
protobuf at ``ZWAMESSAGEINFO.ZRECEIPTINFO`` (a BLOB), keyed to the reacted-to
message by ``ZWAMESSAGEINFO.ZMESSAGE`` (= ``ZWAMESSAGE.Z_PK``).

Layout (verified against a real iOS 26 backup), top-level **field 7** is the
reactions container:

* sub-field **1** (repeated) — a reaction *from someone else*::

      {1: target stanza id, 2: reactor JID, 3: emoji (UTF-8), 4: timestamp}

* sub-field **2** — *my own* reaction::

      {1: target stanza id, 2: emoji (UTF-8), 3: timestamp}

The whole format is a handful of varints and length-delimited fields, so we read
it with a tiny inline parser rather than pulling in a protobuf dependency.
"""

from __future__ import annotations

import sqlite3
from typing import Callable, Iterator

# Field numbers in the receipt / reaction protobuf.
_F_REACTIONS = 7  # top-level container
_SUB_OTHERS = 1  # reactions by other people (repeated)
_SUB_MINE = 2  # my own reaction
_R_JID = 2  # reactor JID, in an "others" entry
_R_EMOJI_OTHERS = 3  # emoji, in an "others" entry
_R_EMOJI_MINE = 2  # emoji, in a "mine" entry


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7


def _iter_fields(buf: bytes) -> Iterator[tuple[int, int, object]]:
    """Yield ``(field_number, wire_type, value)`` for one protobuf message.

    Length-delimited values come back as ``bytes``; varints as ``int``. Wire
    types we don't expect are skipped defensively so a malformed blob can't crash
    the export.
    """
    i = 0
    n = len(buf)
    while i < n:
        tag, i = _read_varint(buf, i)
        field = tag >> 3
        wire = tag & 0x7
        if wire == 0:  # varint
            val, i = _read_varint(buf, i)
            yield field, wire, val
        elif wire == 2:  # length-delimited
            length, i = _read_varint(buf, i)
            yield field, wire, buf[i : i + length]
            i += length
        elif wire == 5:  # 32-bit
            i += 4
        elif wire == 1:  # 64-bit
            i += 8
        else:  # unknown / group — stop, can't safely continue
            return


def _as_text(value: object) -> str | None:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return None


def decode_reactions(blob: bytes) -> list[tuple[str | None, str]]:
    """Return ``[(reactor_jid_or_None, emoji), ...]`` for one receipt blob.

    ``reactor_jid`` is ``None`` for the account owner's own reaction.
    """
    out: list[tuple[str | None, str]] = []
    try:
        for field, wire, value in _iter_fields(blob):
            if field != _F_REACTIONS or wire != 2:
                continue
            for sub_field, sub_wire, sub_value in _iter_fields(value):  # type: ignore[arg-type]
                if sub_wire != 2:
                    continue
                entry = {f: v for f, _w, v in _iter_fields(sub_value)}  # type: ignore[arg-type]
                if sub_field == _SUB_OTHERS:
                    emoji = _as_text(entry.get(_R_EMOJI_OTHERS))
                    if emoji:
                        out.append((_as_text(entry.get(_R_JID)), emoji))
                elif sub_field == _SUB_MINE:
                    emoji = _as_text(entry.get(_R_EMOJI_MINE))
                    if emoji:
                        out.append((None, emoji))
    except (IndexError, ValueError):
        # Truncated/unexpected blob — skip rather than fail the whole export.
        return out
    return out


def _number_from_jid(jid: str) -> str:
    return "+" + jid.split("@", 1)[0] if "@" in jid else jid


def build_name_resolver(conn: sqlite3.Connection) -> Callable[[str], str]:
    """Map a reactor JID to the best display name available in the DB."""
    names: dict[str, str] = {}

    # Group members (often have a saved contact name).
    for jid, name in conn.execute(
        "SELECT ZMEMBERJID, ZCONTACTNAME FROM ZWAGROUPMEMBER "
        "WHERE ZMEMBERJID IS NOT NULL AND ZCONTACTNAME IS NOT NULL AND ZCONTACTNAME <> ''"
    ):
        names.setdefault(jid, name)

    # Push names (the name the user set on their own WhatsApp profile).
    for jid, name in conn.execute(
        "SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME "
        "WHERE ZJID IS NOT NULL AND ZPUSHNAME IS NOT NULL AND ZPUSHNAME <> ''"
    ):
        names.setdefault(jid, name)

    # Saved 1:1 contact names take priority.
    for jid, name in conn.execute(
        "SELECT ZCONTACTJID, ZPARTNERNAME FROM ZWACHATSESSION "
        "WHERE ZCONTACTJID IS NOT NULL AND ZPARTNERNAME IS NOT NULL AND ZPARTNERNAME <> ''"
    ):
        names[jid] = name

    def resolve(jid: str) -> str:
        return names.get(jid) or _number_from_jid(jid)

    return resolve


def apply_reactions(conn: sqlite3.Connection, data, me_label: str = "You") -> int:
    """Populate ``message.reactions`` across an already-built ChatCollection.

    Returns the number of messages that received at least one reaction. Messages
    are located by ``(chat JID, message Z_PK)`` — the same keys the engine used
    when it built ``data`` — so this is a pure add-on requiring no engine change.
    """
    resolve = build_name_resolver(conn)
    rows = conn.execute(
        """
        SELECT cs.ZCONTACTJID, mi.ZMESSAGE, mi.ZRECEIPTINFO
        FROM ZWAMESSAGEINFO mi
        JOIN ZWAMESSAGE m ON m.Z_PK = mi.ZMESSAGE
        JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
        WHERE mi.ZRECEIPTINFO IS NOT NULL
        """
    )

    touched = 0
    for chat_jid, message_pk, blob in rows:
        decoded = decode_reactions(blob)
        if not decoded:
            continue
        chat = data.get_chat(chat_jid) if chat_jid in data else None
        if chat is None:
            continue
        message = chat.get_message(message_pk)
        if message is None:
            continue
        reactions = {
            (me_label if reactor is None else resolve(reactor)): emoji
            for reactor, emoji in decoded
        }
        if reactions:
            message.reactions = reactions
            touched += 1
    return touched
