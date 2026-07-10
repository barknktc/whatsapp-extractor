"""Label WhatsApp internal/system messages instead of the bare placeholder.

WhatsApp stores non-conversational entries (group events, security/protocol
notices, and message kinds newer than the exporter) as message rows with no
text and no media. KnugiHK's template renders those as "Not supported WhatsApp
internal message", and — worse — its iOS handler mislabels *every* group event
that carries text as ``"The group name changed to <text>"``, even when that text
is a member JID or a JSON settings blob. This pass fixes both.

Group events (``ZMESSAGETYPE == 6``, group chats only) are reclassified from the
raw ``ZGROUPEVENTTYPE`` + ``ZTEXT`` + member. There is **no public enum** for
``ZGROUPEVENTTYPE`` and the codes don't reliably distinguish add/remove/promote
(verified against a real DB: chronology is inconsistent), so rather than guess an
action we decode only what the row's *content* proves:

* WhatsApp's own pre-rendered system sentence (marked with a leading U+200E LRM)
  → shown as-is (e.g. "You can't send messages to this group …").
* a JSON payload → "Group/community settings updated".
* member JID(s) in the text → the affected members' names.
* any other free text → the new group subject.
* otherwise → a generic "Group notification".

Protocol rows (``ZMESSAGETYPE == 10``, 1:1 and group) are WhatsApp's security
channel — no display text (any "text" is a raw JID or key hash). The earliest
empty type-10 row in a chat is the end-to-end-encryption banner; the rest become
a neutral "Security notification".

Everything else with no text/media becomes a neutral "System message". Messages
the engine already gave real text (media, "Message deleted", …) are left
untouched.
"""

from __future__ import annotations

import sqlite3

from whatsapp_extractor.reactions import build_name_resolver

GROUP_EVENT_TYPE = 6
PROTOCOL_TYPE = 10  # security/protocol notices: E2E banner, security-code changes, …
LRM = "‎"  # left-to-right mark: WhatsApp's flag for a pre-rendered system line

LABEL_GROUP = "ℹ️ Group notification"
LABEL_SYSTEM = "ℹ️ System message"
LABEL_E2E = "🔒 Messages and calls are end-to-end encrypted."
LABEL_SECURITY = "🔒 Security notification"
LABEL_MEDIA_MISSING = "🖼️ Media not available (never downloaded)"


def _names_from_jid_text(text, resolve):
    jids = [tok for tok in text.replace(",", " ").replace(";", " ").split() if "@" in tok]
    names = []
    for jid in jids:
        name = resolve(jid)
        if name not in names:
            names.append(name)
    return names


def classify_group_event(raw_text, member_jid, resolve) -> str:
    """Best-effort readable label for one group system event."""
    raw = raw_text or ""
    # WhatsApp already rendered this one for us.
    if raw.startswith(LRM):
        return "ℹ️ " + raw.lstrip(LRM).strip()

    text = raw.strip()
    if text:
        if text[:1] in "{[":  # community / linked-group / settings JSON
            return "ℹ️ Group/community settings updated"
        if "@" in text:  # member reference(s) — affected participants
            names = _names_from_jid_text(text, resolve)
            if names:
                shown = ", ".join(names[:5])
                if len(names) > 5:
                    shown += f", +{len(names) - 5} more"
                return f"ℹ️ Group members changed: {shown}"
            return LABEL_GROUP
        if not text.isdigit():  # free text → the new subject
            return f'ℹ️ Group subject changed to "{text}"'

    if member_jid:  # no usable text, but a member is referenced
        return f"ℹ️ Group membership changed: {resolve(member_jid)}"
    return LABEL_GROUP


def _group_event_details(conn: sqlite3.Connection) -> dict:
    """Map message Z_PK -> (raw ZTEXT, member JID) for every group event row."""
    rows = conn.execute(
        """
        SELECT m.Z_PK, m.ZTEXT, gm.ZMEMBERJID
        FROM ZWAMESSAGE m
        LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
        WHERE m.ZMESSAGETYPE = ?
        """,
        (GROUP_EVENT_TYPE,),
    )
    return {pk: (text, member_jid) for pk, text, member_jid in rows}


def _protocol_labels(conn: sqlite3.Connection) -> dict:
    """Map message Z_PK -> label for type-10 security/protocol rows.

    Type 10 carries no display text — it's WhatsApp's protocol channel (E2E
    banner, security-code changes, number-change notices; the "text", when
    present, is a raw JID or key hash). We show the E2E banner specifically
    (it's the first message of almost every chat) and lump the rest under a
    neutral security notification. Rows with media are left alone.
    """
    labels = {}
    # E2E banner = the earliest message of a chat, when it's an empty type-10 row.
    # SQLite's bare-column rule returns the row matching MIN(ZSORT) per group.
    for pk, mtype, text, media, _sort in conn.execute(
        """
        SELECT Z_PK, ZMESSAGETYPE, ZTEXT, ZMEDIAITEM, MIN(ZSORT)
        FROM ZWAMESSAGE GROUP BY ZCHATSESSION
        """
    ):
        if mtype == PROTOCOL_TYPE and text is None and media is None:
            labels[pk] = LABEL_E2E
    # Everything else on the protocol channel → a neutral security notice.
    for (pk,) in conn.execute(
        "SELECT Z_PK FROM ZWAMESSAGE WHERE ZMESSAGETYPE = ? AND ZMEDIAITEM IS NULL",
        (PROTOCOL_TYPE,),
    ):
        labels.setdefault(pk, LABEL_SECURITY)
    return labels


def label_system_messages(conn: sqlite3.Connection, data) -> int:
    """Relabel group events and fill any remaining empty/system messages.

    Run after the engine's message/media passes. Returns the number of messages
    relabelled.
    """
    resolve = build_name_resolver(conn)
    group_events = _group_event_details(conn)
    protocol = _protocol_labels(conn)
    media_pks = {
        pk for (pk,) in conn.execute(
            "SELECT Z_PK FROM ZWAMESSAGE WHERE ZMEDIAITEM IS NOT NULL"
        )
    }

    labelled = 0
    for jid in data:
        is_group = jid.endswith("@g.us")
        chat = data.get_chat(jid)
        for pk, message in chat._messages.items():
            if is_group and pk in group_events:
                raw_text, member_jid = group_events[pk]
                message.data = classify_group_event(raw_text, member_jid, resolve)
                message.meta = True
                labelled += 1
            elif pk in protocol:
                message.data = protocol[pk]
                message.meta = True
                labelled += 1
            elif not message.data:
                # A media row with no data is media that was never downloaded
                # (null local path); anything else is a genuine system row.
                message.data = LABEL_MEDIA_MISSING if pk in media_pks else LABEL_SYSTEM
                message.meta = True
                labelled += 1
    return labelled
