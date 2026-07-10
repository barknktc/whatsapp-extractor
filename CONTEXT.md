# WhatsApp Extractor

A TUI tool that lets a user select WhatsApp chats from an iPhone backup — seeing per-chat stats first — and export the selected chats (with reactions) to a readable format.

## Language

### Pipeline stages

The word "extract" is overloaded in this space; we split it into three distinct stages and use these terms precisely.

**Decryption**:
Turning an encrypted backup's hashed, AES-encrypted files into readable files, using the backup password and keybag. Only applies to encrypted backups.
_Avoid_: unlock, decode.

**Extraction**:
Pulling WhatsApp's files (the message database and media files) out of the backup's content-addressed blob store and into a normal folder tree with real filenames. Encompasses decryption when the backup is encrypted.
_Avoid_: dump, copy.

**Export**:
Producing human-readable output (HTML or JSON) for a set of selected chats from the extracted database and media.
_Avoid_: render, convert, "extract" (reserve that for the overall act / the Extraction stage).

### Backup

**iPhone backup**:
A Finder/iTunes backup directory created on a computer, identified by its `Manifest.db` and `Manifest.plist`. The input to this tool, located at any path the user points to.
_Avoid_: iTunes folder, sync data.

**Encrypted backup**:
A backup created with "Encrypt local backup" enabled. Encryption is a property of the backup *container*, not of WhatsApp's data — the chat database is the same either way; it just can't be read without the password.

**WhatsApp domain**:
The backup namespace that holds all of WhatsApp's files (`AppDomainGroup-group.net.whatsapp.WhatsApp.shared`). The unit that the underlying engine decrypts/extracts as a whole.
_Avoid_: app folder, container.

### WhatsApp data

**Chat**:
A single conversation thread, either one-to-one or a group, stored as a `ZWACHATSESSION` row. The unit the user selects in the TUI.
_Avoid_: conversation, thread, session.

**JID**:
The WhatsApp identifier for a chat or member (e.g. `<number>@s.whatsapp.net`, or `...@g.us` for groups). The stable key used to include/exclude chats during export.
_Avoid_: contact id, phone number.

**Message**:
A single entry in a chat (`ZWAMESSAGE`): text, media, a call record, or a system/meta line.

**Media item**:
A file attached to a message (`ZWAMEDIAITEM`) — photo, video, voice note, document, sticker, vCard.
_Avoid_: attachment, file.

**Reaction**:
An emoji a participant attached to a message, stored on iOS in `ZWAMESSAGEINFO` as a serialized protobuf blob (who reacted, with which emoji). A must-have output of this tool.
_Avoid_: like, emoji response.

### TUI concepts

**Selection**:
The user's chosen set of chats (and any per-chat options) to export. Expressed downstream as a JID include filter.

**Stats**:
The per-chat figures shown before export to inform selection — message count, media count, estimated media size, date range. Computed by lightweight aggregate queries; never requires exporting first.
_Avoid_: metrics, summary.
