"""Command-line entry point for whatsapp-extractor.

Phase 2: locate a backup, extract ChatStorage.sqlite into a temp workdir, and
print per-chat stats. The Textual TUI and selective export come in later phases.
"""

from __future__ import annotations

import argparse
import sys

from whatsapp_extractor import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whatsapp-extractor",
        description=(
            "Select WhatsApp chats from an iPhone backup by their stats and "
            "export the selected chats (with reactions) to HTML/JSON."
        ),
    )
    parser.add_argument(
        "backup",
        nargs="?",
        help="Path to the iPhone backup folder (the directory containing Manifest.db).",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print per-chat stats and exit (no TUI).",
    )
    parser.add_argument(
        "--export",
        nargs="+",
        metavar="JID",
        help="Export these chat JIDs (skips the picker). E.g. 123@s.whatsapp.net",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="DIR",
        default="whatsapp-export",
        help="Output folder for the export (default: ./whatsapp-export).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Also write a result.json alongside the HTML.",
    )
    parser.add_argument(
        "--no-html",
        action="store_true",
        help="Skip HTML output (use with --json for JSON-only).",
    )
    parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help="Do not delete the temp workdir on exit (for debugging).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _fmt_date(dt) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "—"


def _print_stats_table(stats) -> None:
    headers = ("Type", "Chat", "Messages", "Media", "Size", "From", "To")
    rows = [
        (
            s.chat_type,
            (s.name[:32]),
            f"{s.message_count:,}",
            f"{s.media_count:,}",
            _human_bytes(s.media_bytes),
            _fmt_date(s.first_message),
            _fmt_date(s.last_message),
        )
        for s in stats
    ]
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]
    # Left-align text columns, right-align numeric ones.
    aligns = ("<", "<", ">", ">", ">", "<", "<")

    def fmt_row(cells):
        return "  ".join(
            f"{c:{a}{w}}" for c, a, w in zip(cells, aligns, widths)
        )

    print(fmt_row(headers))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(fmt_row(r))

    total_bytes = sum(s.media_bytes for s in stats)
    total_msgs = sum(s.message_count for s in stats)
    print(
        f"\n{len(stats)} chats · {total_msgs:,} messages · "
        f"{_human_bytes(total_bytes)} of media"
    )


def run_stats(backup_path: str) -> int:
    from whatsapp_extractor.extract import DecryptError
    from whatsapp_extractor.service import load_stats

    prepared = _prepare_backup(backup_path)
    if prepared is None:
        return 2
    backup, password = prepared
    try:
        stats = load_stats(backup, password=password)
    except DecryptError as e:
        print(f"error: {e}", file=sys.stderr)
        return 4
    _print_stats_table(stats)
    return 0


def _prepare_backup(backup_path: str):
    """Validate a backup and obtain a password if it is encrypted.

    Returns ``(backup, password)`` or raises with a printed error / returns None.
    """
    from whatsapp_extractor.backup import Backup, BackupError

    backup = Backup(backup_path)
    try:
        info = backup.probe()
    except BackupError as e:
        print(f"error: {e}", file=sys.stderr)
        return None
    if not info.has_whatsapp:
        print("error: no WhatsApp data found in this backup.", file=sys.stderr)
        return None
    password = None
    if info.encrypted:
        from whatsapp_extractor.service import prompt_password

        print("This backup is encrypted.")
        password = prompt_password(backup)
        if password is None:
            print("error: no valid password provided.", file=sys.stderr)
            return None
    return backup, password


def run_export(args) -> int:
    from whatsapp_extractor.export import export_chats
    from whatsapp_extractor.extract import DecryptError
    from whatsapp_extractor.workdir import WorkDir
    from pathlib import Path

    prepared = _prepare_backup(args.backup)
    if prepared is None:
        return 2
    backup, password = prepared

    want_html = not args.no_html
    want_json = args.json
    if not want_html and not want_json:
        print("error: nothing to export (--no-html without --json).", file=sys.stderr)
        return 2

    output_dir = Path(args.output)
    bar = {"last": -1}

    def media_progress(done: int, total: int) -> None:
        pct = int(done * 100 / total) if total else 100
        if pct != bar["last"]:
            bar["last"] = pct
            print(f"\rExtracting media… {done}/{total} ({pct}%)", end="", flush=True)

    with WorkDir(keep=args.keep_workdir) as work:
        try:
            result = export_chats(
                backup,
                list(args.export),
                output_dir,
                work,
                password=password,
                want_html=want_html,
                want_json=want_json,
                media_progress=media_progress,
            )
        except DecryptError as e:
            print(f"\nerror: {e}", file=sys.stderr)
            return 4
    print()  # finish the progress line
    formats = []
    if result.html:
        formats.append("HTML")
    if result.json:
        formats.append("JSON")
    print(
        f"Exported {result.chat_count} chat(s) "
        f"({result.media.file_count:,} media files, "
        f"{_human_bytes(result.media.byte_count)}, "
        f"{result.reacted_messages:,} messages with reactions) "
        f"as {'+'.join(formats)} to {result.output_dir}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.backup:
        parser.print_help()
        return 0

    if args.export:
        return run_export(args)

    if args.stats:
        return run_stats(args.backup)

    # Default: launch the interactive chat picker.
    from whatsapp_extractor.tui import run_tui

    return run_tui(args)


if __name__ == "__main__":
    sys.exit(main())
