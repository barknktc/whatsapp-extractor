"""Textual TUI — the chat picker.

This is the product's front door: point it at a backup, see per-chat stats, pick
chats, and export them. It does selection only — it never opens conversations.

Flow: a startup worker reads the backup and computes stats (the heavy bit is the
Manifest scan); the picker then lets the user sort/search/select with a live
running total of what the selection will cost to extract; export runs in a
worker so the UI stays responsive.
"""

from __future__ import annotations

from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    LoadingIndicator,
    Static,
)

from whatsapp_extractor.stats import ChatStats

CHECK_ON = "✔"
CHECK_OFF = "·"

# (label, key function, default descending?)
SORTS = {
    "messages": ("Messages", lambda s: s.message_count, True),
    "size": ("Size", lambda s: s.media_bytes, True),
    "media": ("Media", lambda s: s.media_count, True),
    "name": ("Name", lambda s: s.name.lower(), False),
    "date": ("Last msg", lambda s: (s.last_message is not None, s.last_message), True),
}


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _fmt_date(dt) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "—"


class ExportDialog(ModalScreen[dict | None]):
    """Collects output folder + formats, returns a dict or None if cancelled."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    ExportDialog { align: center middle; }
    #box {
        width: 70; height: auto; padding: 1 2;
        border: thick $accent; background: $surface;
    }
    #box Label { margin-top: 1; }
    #buttons { margin-top: 1; height: auto; align-horizontal: right; }
    #buttons Button { margin-left: 2; }
    """

    def __init__(self, count: int, total_bytes: int):
        super().__init__()
        self._count = count
        self._total_bytes = total_bytes

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(
                f"Export {self._count} chat(s) · {human_bytes(self._total_bytes)} of media"
            )
            yield Label("Output folder:")
            yield Input(value="whatsapp-export", id="outdir")
            yield Checkbox("HTML", value=True, id="html")
            yield Checkbox("JSON", value=False, id="json")
            with Vertical(id="buttons"):
                yield Button("Export", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss(
            {
                "output": self.query_one("#outdir", Input).value.strip()
                or "whatsapp-export",
                "html": self.query_one("#html", Checkbox).value,
                "json": self.query_one("#json", Checkbox).value,
            }
        )

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ProgressScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    ProgressScreen { align: center middle; }
    #pbox { width: 70; height: auto; padding: 1 2; border: thick $accent; background: $surface; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="pbox"):
            yield Label("Exporting…", id="pmsg")
            yield LoadingIndicator()

    def set_message(self, text: str) -> None:
        self.query_one("#pmsg", Label).update(text)


class PickerApp(App):
    CSS = """
    /* #search and #total are deliberately not docked: Header/Footer already
       dock top/bottom, and a second widget docked to the same edge overlaps
       (Header was hidden under the search box, #total under the Footer).
       Compose order + DataTable's 1fr keep them pinned in place. */
    #total { height: 1; padding: 0 1; background: $panel; color: $text; }
    DataTable { height: 1fr; }
    """

    BINDINGS = [
        Binding("space", "toggle", "Select"),
        Binding("a", "select_all", "All"),
        Binding("n", "select_none", "None"),
        Binding("e", "export", "Export"),
        Binding("m", "sort('messages')", "Sort msgs"),
        Binding("z", "sort('size')", "Sort size"),
        Binding("c", "sort('name')", "Sort name"),
        Binding("d", "sort('date')", "Sort date"),
        Binding("slash", "focus_search", "Search"),
        Binding("q", "quit", "Quit"),
    ]

    COLUMNS = ("", "Type", "Chat", "Messages", "Media", "Size", "From", "To")

    def __init__(self, args):
        super().__init__()
        self._args = args
        self._backup = None
        self._password = None
        self._all: list[ChatStats] = []
        self._visible: list[ChatStats] = []
        self._selected: set[str] = set()
        self._search = ""
        self._sort = "messages"

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Input(placeholder="Type to search chats…", id="search")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
        yield Static("Loading backup…", id="total")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        for col in self.COLUMNS:
            table.add_column(col, key=col or "check")
        self.title = "WhatsApp Extractor"
        self.sub_title = "reading backup…"
        self._load()

    # -- startup load ------------------------------------------------------

    @work(thread=True, exclusive=True)
    def _load(self) -> None:
        from whatsapp_extractor.backup import Backup
        from whatsapp_extractor.extract import DecryptError
        from whatsapp_extractor.service import load_stats

        backup = Backup(self._args.backup)
        info = backup.probe()
        self._backup = backup
        password = getattr(self._args, "_password", None)
        try:
            stats = load_stats(backup, password=password)
        except DecryptError as e:
            self.call_from_thread(self._load_failed, str(e))
            return
        self._password = password
        self.call_from_thread(self._load_done, stats)

    def _load_failed(self, message: str) -> None:
        self.sub_title = "error"
        self.query_one("#total", Static).update(f"Error: {message}")

    def _load_done(self, stats: list[ChatStats]) -> None:
        self._all = stats
        self.sub_title = f"{len(stats)} chats"
        self._rebuild()
        self.query_one(DataTable).focus()

    # -- table rendering ---------------------------------------------------

    def _row_cells(self, s: ChatStats) -> tuple:
        check = CHECK_ON if s.jid in self._selected else CHECK_OFF
        return (
            check,
            s.chat_type,
            s.name,
            f"{s.message_count:,}",
            f"{s.media_count:,}",
            human_bytes(s.media_bytes),
            _fmt_date(s.first_message),
            _fmt_date(s.last_message),
        )

    def _rebuild(self) -> None:
        """Recompute the visible list (search + sort) and repaint the table."""
        label, keyfn, desc = SORTS[self._sort]
        items = self._all
        if self._search:
            q = self._search.lower()
            items = [s for s in items if q in s.name.lower() or q in s.jid.lower()]
        self._visible = sorted(items, key=keyfn, reverse=desc)

        table = self.query_one(DataTable)
        table.clear()
        for s in self._visible:
            table.add_row(*self._row_cells(s), key=s.jid)
        self.sub_title = f"{len(self._visible)}/{len(self._all)} chats · sort: {label}"
        self._update_total()

    def _update_total(self) -> None:
        chosen = [s for s in self._all if s.jid in self._selected]
        total_bytes = sum(s.media_bytes for s in chosen)
        total_msgs = sum(s.message_count for s in chosen)
        self.query_one("#total", Static).update(
            f"Selected: {len(chosen)} chats · {total_msgs:,} messages · "
            f"{human_bytes(total_bytes)} of media   "
            f"(space=select  a=all  n=none  e=export)"
        )

    # -- selection ---------------------------------------------------------

    def _current_jid(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        return self._visible[table.cursor_row].jid

    def action_toggle(self) -> None:
        jid = self._current_jid()
        if jid is None:
            return
        table = self.query_one(DataTable)
        row = table.cursor_row
        if jid in self._selected:
            self._selected.discard(jid)
            check = CHECK_OFF
        else:
            self._selected.add(jid)
            check = CHECK_ON
        table.update_cell_at(Coordinate(row, 0), check)
        self._update_total()
        # Advance the cursor so repeated space ticks down the list.
        if row + 1 < table.row_count:
            table.move_cursor(row=row + 1)

    def action_select_all(self) -> None:
        for s in self._visible:
            self._selected.add(s.jid)
        self._repaint_checks()

    def action_select_none(self) -> None:
        self._selected.clear()
        self._repaint_checks()

    def _repaint_checks(self) -> None:
        table = self.query_one(DataTable)
        for row, s in enumerate(self._visible):
            table.update_cell_at(
                Coordinate(row, 0),
                CHECK_ON if s.jid in self._selected else CHECK_OFF,
            )
        self._update_total()

    # -- search & sort -----------------------------------------------------

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        self._search = event.value
        self._rebuild()

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_sort(self, key: str) -> None:
        self._sort = key
        self._rebuild()

    # -- export ------------------------------------------------------------

    def action_export(self) -> None:
        if not self._selected:
            self.bell()
            self.notify("Select at least one chat first.", severity="warning")
            return
        chosen = [s for s in self._all if s.jid in self._selected]
        total = sum(s.media_bytes for s in chosen)
        self.push_screen(
            ExportDialog(len(chosen), total), self._on_export_config
        )

    def _on_export_config(self, config: dict | None) -> None:
        if config is None:
            return
        if not config["html"] and not config["json"]:
            self.notify("Pick at least one format.", severity="warning")
            return
        progress = ProgressScreen()
        self.push_screen(progress)
        self._run_export(config, progress)

    @work(thread=True)
    def _run_export(self, config: dict, progress: ProgressScreen) -> None:
        from whatsapp_extractor.export import export_chats
        from whatsapp_extractor.extract import DecryptError
        from whatsapp_extractor.workdir import WorkDir

        jids = [s.jid for s in self._all if s.jid in self._selected]

        def media_progress(done: int, total: int) -> None:
            self.call_from_thread(
                progress.set_message, f"Extracting media… {done}/{total}"
            )

        try:
            with WorkDir() as work:
                result = export_chats(
                    self._backup,
                    jids,
                    Path(config["output"]),
                    work,
                    password=self._password,
                    want_html=config["html"],
                    want_json=config["json"],
                    media_progress=media_progress,
                )
        except DecryptError as e:
            self.call_from_thread(self._export_failed, str(e))
            return
        self.call_from_thread(self._export_done, result)

    def _export_failed(self, message: str) -> None:
        self.pop_screen()  # progress
        self.notify(f"Export failed: {message}", severity="error", timeout=10)

    def _export_done(self, result) -> None:
        self.pop_screen()  # progress
        self.notify(
            f"Exported {result.chat_count} chat(s) "
            f"({result.media.file_count:,} media files, "
            f"{human_bytes(result.media.byte_count)}, "
            f"{result.reacted_messages:,} with reactions) to {result.output_dir}",
            timeout=12,
        )


def run_tui(args) -> int:
    from whatsapp_extractor.backup import Backup, BackupError

    # Probe up front so encryption/password and obvious errors are handled in a
    # normal terminal, before we take over the screen.
    backup = Backup(args.backup)
    try:
        info = backup.probe()
    except BackupError as e:
        print(f"error: {e}")
        return 2
    if not info.has_whatsapp:
        print("error: no WhatsApp data found in this backup.")
        return 3

    password = None
    if info.encrypted:
        from whatsapp_extractor.service import prompt_password

        print("This backup is encrypted.")
        password = prompt_password(backup)
        if password is None:
            print("error: no valid password provided.")
            return 4
    args._password = password

    PickerApp(args).run()
    return 0
