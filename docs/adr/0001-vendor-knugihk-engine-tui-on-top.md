# Vendor KnugiHK WhatsApp-Chat-Exporter as the engine; build a TUI + selective extraction on top

## Status

accepted

## Context

We need to extract WhatsApp chats (with reactions) from iPhone backups, fronted by a TUI for selecting chats by their stats. The hard parts — iOS backup decryption (keybag/AES via `iphone_backup_decrypt`), the WhatsApp SQLite schema (`ZWACHATSESSION`/`ZWAMESSAGE`/`ZWAMEDIAITEM`), and HTML/JSON export — are already solved and packaged in [KnugiHK/WhatsApp-Chat-Exporter](https://github.com/KnugiHK/WhatsApp-Chat-Exporter) (MIT). Two gaps remain for our goals: it has **no iOS reactions** (issue #77 open; the reactions PR #193 was Android-only) and it **extracts the entire WhatsApp media set up front**, so chat selection can't make extraction cheaper.

## Decision

**Vendor** KnugiHK's `Whatsapp_Chat_Exporter` package into this repo (MIT, keep their LICENSE/copyright) rather than depend on it, fork it, or rebuild it. On top of the vendored engine we own three additions:

1. **iOS reactions** — a pass that decodes the `ZWAMESSAGEINFO` protobuf and populates `message.reactions` (`{sender: emoji}`). The data model and both exporters (HTML template + JSON) already render this field, so no exporter changes are needed. Reference: damleborgne/whatsapp-conversation-exporter (MIT).
2. **Selective extraction** — decrypt/copy only the media belonging to the user's selected chats (map JID → message → `ZMEDIALOCALPATH` → `Manifest.db` fileID; use `iphone_backup_decrypt`'s `extract_files` `filter_callback`). The chat DB is always extracted first (small, fast) to compute stats.
3. **Textual TUI** — point at a backup, prompt for the password if encrypted, show per-chat stats (message/media counts, estimated size, date range), select chats, then export (reusing KnugiHK's `create_html`/`export_json`).

## Consequences

- We own the vendored code permanently and lose easy upstream updates; we accept this for full control over the reactions and selective-extraction changes, which both reach into engine internals.
- The reaction decoder is upstreamable to KnugiHK later as a goodwill PR, independent of our tool.
- Pinning to a copied snapshot insulates us from upstream schema/API churn.
