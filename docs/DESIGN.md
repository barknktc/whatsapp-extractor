# WhatsApp Extractor — Design

A TUI tool to select WhatsApp chats from an iPhone backup (by their stats) and export the selected chats — **with reactions** — to HTML/JSON, decrypting and copying **only** the selected chats' media.

See [CONTEXT.md](../CONTEXT.md) for vocabulary and [ADR 0001](adr/0001-vendor-knugihk-engine-tui-on-top.md) for the core architecture decision.

## Resolved decisions

| Area | Decision |
|---|---|
| Input | A backup folder at any path the user points to (no live device). |
| Encryption | Support both encrypted and unencrypted backups; prompt for password when encrypted. |
| Language / runtime | Python ≥3.10, Textual for the TUI. |
| Decryption | `iphone_backup_decrypt` library (keybag/AES). No hand-rolled crypto. |
| Engine | **Vendor** KnugiHK/WhatsApp-Chat-Exporter (MIT) for schema parsing + HTML/JSON export. |
| Reactions | Must-have. Add an iOS pass decoding `ZWAMESSAGEINFO` protobuf → `message.reactions` (`{sender: emoji}`); rendered by existing exporters. Reference: damleborgne (MIT). |
| Extraction | **Selective**: always extract the small DBs first for stats; on export, decrypt/copy only selected chats' media. |
| Output | HTML by default (readable, media linked, reactions rendered); JSON as an option. |
| Stats columns | Message count; media count + estimated size; date range; chat type (1:1 / group). |
| v1 scope | 1:1 and group chats only. (Calls, Business, Status deferred.) |
| Packaging | uv project + `whatsapp-extractor` console entry point. |
| Working files | Decrypt to a temp workdir, write final output to a user-chosen folder, auto-delete the temp workdir (incl. decrypted DB) on exit. |
| TUI UX | Sortable columns, name search/filter, select-all/none + space-toggle, live selected-total (count + combined estimated size). |

## Flow

1. **Locate backup** — path from CLI arg or TUI input. Detect encryption via `Manifest.db` readability.
2. **Decrypt DBs** — extract `ChatStorage.sqlite` (+ contacts) into a temp workdir. Prompt password if encrypted; retry on failure.
3. **Compute stats** — aggregate queries over `ZWACHATSESSION`/`ZWAMESSAGE`/`ZWAMEDIAITEM`; media size summed from `Manifest.db` (the actual bytes to extract).
4. **Select** — TUI chat picker with sort/search/multi-select and live selected total.
5. **Selective extract** — for selected JIDs: map message → `ZMEDIALOCALPATH` → `Manifest.db` fileID; decrypt/copy only those files (encrypted: `extract_files` `filter_callback`; unencrypted: copy by fileID).
6. **Export** — run vendored `ios_handler.messages/media` with `--include` = selected JIDs, run the reactions pass, then `create_html` / `export_json` into the output folder.
7. **Cleanup** — delete temp workdir.

## Verified against a real backup

Spike run 2026-06-30 against an unencrypted iOS 26.1 backup (392 chats, 213k messages, 82.9k media). Findings:

- **Reaction protobuf** (`ZWAMESSAGEINFO.ZRECEIPTINFO`, a BLOB — note: the receipt protobuf, not a dedicated column). Reactions live under top-level **field 7** (length-delimited container):
  - sub-field **1** (repeated) = a reaction *from someone else*: `{1: target stanza id, 2: reactor JID, 3: emoji (UTF-8), 4: ts}`
  - sub-field **2** = *my own* reaction: `{1: target stanza id, 2: emoji (UTF-8), 3: ts}`
  - The row's `ZMESSAGE` FK is the reacted-to message; populate `message.reactions = {reactor_jid_or_"<me>": emoji}`. 19,430 messages carried reactions (1,823 multi-reaction; up to 14 on one group message). Decoder is ~30 lines of pure Python — **no protobuf dependency needed**.
- **Media mapping**: `ZWAMEDIAITEM.ZMEDIALOCALPATH` = `Media/<jid>/.../file.ext`; in `Manifest.db` the same file is `Message/` + that path under domain `AppDomainGroup-group.net.whatsapp.WhatsApp.shared`. Because the **JID is embedded in the path**, selective extraction is a `Message/Media/<jid>/` prefix filter — no per-message join required. `Files.flags`: `1` = file, `2` = directory.
- **Media size for stats**: `ZWAMEDIAITEM.ZFILESIZE` exists (per-item logical size); the *actual bytes to extract* are the on-disk blob sizes summed from `Manifest.db` by JID prefix. One 1:1 chat alone = 3,547 files / 306 MB — confirms selective extraction's payoff.
- **Encryption detection**: `Manifest.plist` `IsEncrypted` bool (this backup: `False`); when `True`, `Manifest.db` is itself encrypted and needs `iphone_backup_decrypt`.

## System / internal messages

WhatsApp stores non-conversational entries (group events, security/protocol
notices, newer message kinds) as message rows with no text and no media; the
engine renders these as "Not supported WhatsApp internal message". A post-pass
([`system_messages.py`](../src/whatsapp_extractor/system_messages.py)) relabels
them. `ZMESSAGETYPE 6` (group-only) events are reclassified from raw
`ZGROUPEVENTTYPE` + `ZTEXT` + member; all other empty rows → "System message".

`ZMESSAGETYPE 10` is WhatsApp's **security/protocol channel** — not calls. It has
no display text (any "text" is a raw JID or key hash). The *earliest* empty
type-10 row in a chat is the **end-to-end-encryption banner** (verified: it's the
first message in 359 of the 392 chats); the rest → a neutral "Security
notification". Media rows whose file was never downloaded (`ZMEDIALOCALPATH` NULL)
have no `message.data` and the engine would show them as the generic placeholder;
we label them "Media not available" instead.

There is no public `ZGROUPEVENTTYPE` enum and the codes do **not** reliably
distinguish add/remove/promote (verified: member-event chronology is
inconsistent), so we decode only what the row content *proves* rather than
guessing an action:

- leading U+200E (WhatsApp's pre-rendered system line) → show the text as-is
- JSON payload → "Group/community settings updated"
- member JID(s) in text → the affected members' names
- other free text → the new group subject
- otherwise → generic "Group notification"

This also fixes an upstream bug where the engine labelled *every* text-bearing
group event as `"The group name changed to <text>"` — including member-JID and
JSON rows.

## Link previews

When a link is shared, WhatsApp fetches and **caches the preview locally**, so the
card survives the source page being deleted. On iOS these are `ZMESSAGETYPE 7`
messages; the preview lives in the row's `ZWAMEDIAITEM`: `ZTITLE` (title),
`ZMEDIAURL` (canonical URL), `ZXMPPTHUMBPATH` (a `.thumb` file — a small JPEG =
the cached image), and `ZMETADATA` (a protobuf whose field 3 is the description).
The `.thumb` files sit under `Media/<jid>/`, so the selective-media extraction
already copies them — no extra files to pull.

A post-pass ([`link_previews.py`](../src/whatsapp_extractor/link_previews.py))
populates `message.link_preview` and the template draws the same card the phone
shows (thumbnail + title + description + domain). Verified: 4,476 type-7 rows in
the backup, 3,777 with cached thumbnails.

## Open items to verify during implementation

- Confirm KnugiHK internal function signatures we call against the pinned vendored snapshot.
- Group `chat type` member-count source (`ZWAGROUPMEMBER` / `ZWAGROUPINFO`) if shown.
- Re-confirm the reaction protobuf shape against an *encrypted* backup and an older WhatsApp version if available (layout expected stable, but unverified outside this one DB).

## Build phases

1. **Vendor + skeleton** — copy `Whatsapp_Chat_Exporter` (with LICENSE) into the repo; uv project; `whatsapp-extractor` entry point; wire `iphone_backup_decrypt`.
2. **Decrypt + stats (no TUI)** — CLI that extracts the DB and prints per-chat stats. Validates the backup/decrypt/Manifest path end to end.
3. **Reactions pass** — decode `ZWAMESSAGEINFO`; verify against a real backup; confirm HTML/JSON render.
4. **Selective extraction** — media-by-selected-chats; verify byte counts match estimates.
5. **TUI** — Textual chat picker (stats, sort/search/select, live total) → export screen → progress.
6. **Polish** — password retry, cleanup, error states (E2E-encrypted backup, missing WhatsApp domain, wrong password).
