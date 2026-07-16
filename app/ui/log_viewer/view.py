"""``LogViewerView`` — the live journalctl log viewer.

The view owns the reader pipeline, the filter state (severity dots, text search,
tag set) and the column view, and composes the capture panel and tag bar mixins
plus the free-function cell factories. Split out of the former single-file
``app/ui/log_viewer.py`` (~1450 lines) to mirror the ``app/ui/profiler`` package
layout; see the sibling modules for each concern.
"""

import json
import logging
from collections import deque
from collections.abc import Callable
from datetime import datetime

import gi

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk

from app.core.dbus_client import DBusClient
from app.core.journal_reader import (
    CaptureSpec,
    JournalReader,
    LogEntry,
    build_journal_cmd,
    capture_from_settings,
    parse_extra_args,
)
from app.core.keybindings import KeybindingManager, populate_shortcut_controller
from app.core.settings import Settings
from app.ui.log_viewer import factories
from app.ui.log_viewer.capture_panel import CapturePanelMixin
from app.ui.log_viewer.common import (
    _BUCKET_DEBUG,
    _BUCKET_ERROR,
    _BUCKET_INFO,
    _BUCKET_WARN,
    _SEARCH_HELP,
    MAX_ENTRIES,
    LogRowItem,
    _bucket_label,
    _entry_tag,
    _make_info_icon,
    _priority_bucket,
)
from app.ui.log_viewer.tag_bar import TagBarMixin

_log = logging.getLogger(__name__)


class LogViewerView(Gtk.Box, CapturePanelMixin, TagBarMixin):
    """Live journalctl log viewer with structured column-view rendering."""

    def __init__(self, dbus_client: DBusClient, keybindings: KeybindingManager) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._dbus = dbus_client
        self._keybindings = keybindings
        self._reader = JournalReader()
        self._entries: deque[LogEntry] = deque(maxlen=MAX_ENTRIES)

        # Filter state
        self._active_buckets: set[str] = set()
        self._active_tags: set[str] = set()
        self._pin_tag: str | None = None
        self._tag_counts: dict[str, int] = {}
        self._search_text = ""
        self._auto_scroll = True
        self._is_running = False

        # Guards against cascading signal handlers when updating chip UI state
        self._block_tag_signals = False
        # Same idea for the capture-panel controls (sync spec → widgets).
        self._block_capture_signals = False

        self._chips_rebuild_pending: bool = False

        # Chip widget registries (populated by _rebuild_chips / _rebuild_popover)
        self._inline_chip_widgets: dict[str, Gtk.ToggleButton] = {}
        self._popover_row_widgets: dict[str, tuple[Gtk.CheckButton, Gtk.Label]] = {}

        # Bucket counts across all entries
        self._bucket_counts: dict[str, int] = {
            _BUCKET_ERROR: 0,
            _BUCKET_WARN: 0,
            _BUCKET_INFO: 0,
            _BUCKET_DEBUG: 0,
        }

        # Stat dot toggle buttons keyed by bucket
        self._stat_buttons: dict[str, Gtk.ToggleButton] = {}
        self._stat_labels: dict[str, Gtk.Label] = {}

        # Persisted settings ("log-viewer" namespace). Capture spec drives what
        # the reader pulls from the journal; loaded here, migrating any
        # pre-structured "journal_cmd" value.
        self._settings = Settings("log-viewer")
        self._capture: CaptureSpec = capture_from_settings(self._settings.load())

        # Column-view backing store and selection (multi-select for copy)
        self._store = Gio.ListStore(item_type=LogRowItem)
        self._selection = Gtk.MultiSelection.new(self._store)

        self._build_ui()

        self._reader.connect("log-entry", self._on_log_entry)
        self._kb_changed_id = self._keybindings.connect_changed(self._on_keybindings_changed)
        self.connect("destroy", self._on_destroy)

    def _on_destroy(self, _widget: Gtk.Widget) -> None:
        self._reader.stop()
        self._keybindings.disconnect(self._kb_changed_id)

    def _on_keybindings_changed(self, _action_id: str) -> None:
        populate_shortcut_controller(
            self._shortcut_ctrl, self._keybindings, self._shortcut_bindings
        )

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── Start/Stop button (lives in the filter bar, drives the reader) ──
        self._start_stop_btn = Gtk.Button()
        self._start_stop_btn.add_css_class("suggested-action")
        self._start_stop_btn.set_tooltip_text("Start reading the journal")
        self._start_stop_btn.connect("clicked", self._on_start_stop)

        # ── Capture panel ("Source") — what the reader pulls from the journal.
        # This is the capture layer; the search/severity/tag controls below are
        # the live display layer over what was captured.
        capture_panel = self._build_capture_panel()
        self._set_start_stop_state(running=False)

        self._cmd_revealer = Gtk.Revealer()
        self._cmd_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._cmd_revealer.set_reveal_child(False)
        self._cmd_revealer.set_child(capture_panel)

        # ── Filter bar ──────────────────────────────────────────────────────
        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_hexpand(True)
        self._search_entry.set_placeholder_text("Search logs…")
        self._search_entry.connect("search-changed", self._on_search_changed)

        self._auto_scroll_btn = Gtk.ToggleButton()
        self._auto_scroll_btn.set_icon_name("go-bottom-symbolic")
        self._auto_scroll_btn.set_tooltip_text("Auto-scroll to bottom")
        self._auto_scroll_btn.set_active(True)
        self._auto_scroll_btn.connect("toggled", self._on_auto_scroll_toggled)

        clear_btn = Gtk.Button(icon_name="edit-clear-all-symbolic")
        clear_btn.add_css_class("flat")
        clear_btn.set_tooltip_text("Clear log")
        clear_btn.connect("clicked", self._on_clear)

        self._copy_btn = Gtk.Button(icon_name="edit-copy-symbolic")
        self._copy_btn.add_css_class("flat")
        self._copy_btn.set_tooltip_text("Copy selected rows (Ctrl/Shift-click to multi-select)")
        self._copy_btn.set_sensitive(False)
        self._copy_btn.connect("clicked", self._on_copy)

        export_btn = Gtk.Button(icon_name="document-save-symbolic")
        export_btn.add_css_class("flat")
        export_btn.set_tooltip_text("Export visible log (.txt or .json) (Ctrl+S)")
        export_btn.connect("clicked", self._on_export)

        self._cmd_toggle_btn = Gtk.ToggleButton()
        self._cmd_toggle_btn.set_icon_name("pan-down-symbolic")
        self._cmd_toggle_btn.add_css_class("flat")
        self._cmd_toggle_btn.set_tooltip_text("Show/hide capture source")
        self._cmd_toggle_btn.set_active(False)
        self._cmd_toggle_btn.connect("toggled", self._on_cmd_toggle)

        filter_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        filter_bar.add_css_class("log-filterbar")
        filter_bar.append(self._search_entry)
        filter_bar.append(self._auto_scroll_btn)

        filter_bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        filter_bar.append(_make_info_icon(_SEARCH_HELP))
        filter_bar.append(self._copy_btn)
        filter_bar.append(export_btn)
        filter_bar.append(clear_btn)

        filter_bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        filter_bar.append(self._cmd_toggle_btn)
        filter_bar.append(self._start_stop_btn)

        # ── Tag chip bar ─────────────────────────────────────────────────────
        tag_bar = self._build_tag_bar()

        # ── Status bar (counts + stat dots + state pill) ───────────────────
        self._status_lbl = Gtk.Label()
        self._status_lbl.set_halign(Gtk.Align.START)
        self._status_lbl.set_hexpand(True)
        self._status_lbl.add_css_class("log-status-text")

        dots_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        for bucket, dot_cls in (
            (_BUCKET_ERROR, "dot-error"),
            (_BUCKET_WARN, "dot-warn"),
            (_BUCKET_INFO, "dot-info"),
            (_BUCKET_DEBUG, "dot-debug"),
        ):
            btn = Gtk.ToggleButton()
            btn.add_css_class("log-stat-dot")
            btn.add_css_class(dot_cls)
            btn.add_css_class("flat")
            btn.set_tooltip_text(f"Show only {_bucket_label(bucket)} entries")
            label = Gtk.Label()
            label.set_label(f"{_bucket_label(bucket)} 0")
            btn.set_child(label)
            btn.connect("toggled", self._on_stat_dot_toggled, bucket)
            self._stat_buttons[bucket] = btn
            self._stat_labels[bucket] = label
            dots_box.append(btn)
        self._dots_box = dots_box

        self._state_pill = Gtk.Label()
        self._state_pill.add_css_class("log-state-pill")
        self._set_state_pill(running=False)

        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        status_bar.add_css_class("log-statusbar")
        status_bar.append(self._status_lbl)
        status_bar.append(dots_box)
        status_bar.append(self._state_pill)

        # ── Column view ─────────────────────────────────────────────────────
        self._selection.connect("selection-changed", self._on_selection_changed)

        col_view = Gtk.ColumnView(model=self._selection)
        col_view.set_vexpand(True)
        col_view.set_show_row_separators(False)
        col_view.set_show_column_separators(False)
        col_view.add_css_class("log-view")
        self._col_view = col_view

        # TIME column — each cell wraps its content in a Gtk.Box so the
        # severity tint can be applied to the box's background (avoids
        # walking the widget tree to reach the private row widget).
        time_fac = Gtk.SignalListItemFactory()
        time_fac.connect("setup", factories.time_setup)
        time_fac.connect("bind", factories.time_bind)
        time_col = Gtk.ColumnViewColumn(title="TIME", factory=time_fac)
        time_col.set_fixed_width(110)
        time_col.set_resizable(True)
        col_view.append_column(time_col)

        # LEVEL pill column
        level_fac = Gtk.SignalListItemFactory()
        level_fac.connect("setup", factories.level_setup)
        level_fac.connect("bind", factories.level_bind)
        level_col = Gtk.ColumnViewColumn(title="LEVEL", factory=level_fac)
        level_col.set_fixed_width(70)
        level_col.set_resizable(True)
        col_view.append_column(level_col)

        # TAG column — colored monospace, e.g. [dash-to-dock]
        tag_fac = Gtk.SignalListItemFactory()
        tag_fac.connect("setup", factories.tag_setup)
        tag_fac.connect("bind", factories.tag_bind)
        tag_col = Gtk.ColumnViewColumn(title="TAG", factory=tag_fac)
        tag_col.set_fixed_width(190)
        tag_col.set_resizable(True)
        col_view.append_column(tag_col)

        # MESSAGE column
        msg_fac = Gtk.SignalListItemFactory()
        msg_fac.connect("setup", factories.msg_setup)
        msg_fac.connect("bind", factories.msg_bind)
        msg_col = Gtk.ColumnViewColumn(title="MESSAGE", factory=msg_fac)
        msg_col.set_resizable(True)
        col_view.append_column(msg_col)
        self._msg_col = msg_col

        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_vexpand(True)
        self._scroll.set_size_request(0, -1)
        self._scroll.set_child(col_view)
        self._apply_wrap_policy()

        # ── Empty-state stack (wraps the list) ─────────────────────────────
        self._empty_page = Adw.StatusPage()
        self._empty_page.set_icon_name("text-x-generic-symbolic")
        self._empty_page.set_title("No log entries yet")
        self._empty_page.set_description(
            "Press Start to begin tailing the system journal."
        )
        self._empty_page.set_vexpand(True)

        empty_start_btn = Gtk.Button()
        empty_start_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        empty_start_box.append(Gtk.Image.new_from_icon_name("media-playback-start-symbolic"))
        empty_start_box.append(Gtk.Label(label="Start"))
        empty_start_btn.set_child(empty_start_box)
        empty_start_btn.add_css_class("suggested-action")
        empty_start_btn.add_css_class("pill")
        empty_start_btn.set_halign(Gtk.Align.CENTER)
        empty_start_btn.connect("clicked", self._on_start_stop)
        self._empty_start_btn = empty_start_btn
        self._empty_page.set_child(empty_start_btn)

        self._list_stack = Gtk.Stack()
        self._list_stack.set_vexpand(True)
        self._list_stack.add_named(self._empty_page, "empty")
        self._list_stack.add_named(self._scroll, "data")
        self._list_stack.set_visible_child_name("empty")

        self.append(filter_bar)
        # Capture source reveals directly under the search/run row (its toggle
        # lives there); the tag filter sits below it.
        self.append(self._cmd_revealer)
        self.append(tag_bar)
        self.append(status_bar)
        self.append(self._list_stack)

        # Tab-scoped shortcuts. MANAGED scope + Gtk.Stack unmapping the hidden
        # pages means these are only live while the Logs tab is selected.
        # Bindings come from the KeybindingManager catalog so they stay in
        # sync with the editable shortcuts dialog.
        self._shortcut_ctrl = Gtk.ShortcutController()
        self._shortcut_ctrl.set_scope(Gtk.ShortcutScope.MANAGED)
        self._shortcut_bindings: list[tuple[str, Callable[..., bool]]] = [
            ("logs-export", self._on_export_shortcut),
            ("logs-run", self._on_run_shortcut),
            ("logs-search", self._on_search_shortcut),
            ("logs-clear", self._on_clear_shortcut),
        ]
        populate_shortcut_controller(
            self._shortcut_ctrl, self._keybindings, self._shortcut_bindings
        )
        self.add_controller(self._shortcut_ctrl)

        self._update_status_label()
        self._update_list_stack()

    # ── Command bar handlers ───────────────────────────────────────────────

    def _set_start_stop_state(self, running: bool) -> None:
        icon_name = "media-playback-stop-symbolic" if running else "media-playback-start-symbolic"
        label_text = "Stop" if running else "Start"

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.append(Gtk.Image.new_from_icon_name(icon_name))
        box.append(Gtk.Label(label=label_text))
        self._start_stop_btn.set_child(box)

        if running:
            self._start_stop_btn.remove_css_class("suggested-action")
            self._start_stop_btn.add_css_class("destructive-action")
            self._start_stop_btn.set_tooltip_text("Stop reading the journal")
            self._set_capture_panel_sensitive(False)
        else:
            self._start_stop_btn.remove_css_class("destructive-action")
            self._start_stop_btn.add_css_class("suggested-action")
            self._start_stop_btn.set_tooltip_text("Start reading the journal")
            self._set_capture_panel_sensitive(True)

    def _set_state_pill(self, running: bool) -> None:
        for c in ("running", "stopped"):
            self._state_pill.remove_css_class(c)
        if running:
            self._state_pill.set_label("RUNNING")
            self._state_pill.add_css_class("running")
        else:
            self._state_pill.set_label("STOPPED")
            self._state_pill.add_css_class("stopped")

    def _on_start_stop(self, _btn: Gtk.Button) -> None:
        if self._is_running:
            self._do_stop()
        else:
            self._do_start()

    def _do_start(self) -> None:
        extra = parse_extra_args(build_journal_cmd(self._capture))
        self._reader.start(extra_args=extra)
        self._is_running = True
        self._set_start_stop_state(running=True)
        self._set_state_pill(running=True)
        self._update_list_stack()

    def _do_stop(self) -> None:
        self._reader.stop()
        self._is_running = False
        self._set_start_stop_state(running=False)
        self._set_state_pill(running=False)
        self._update_list_stack()

    # ── Public API ─────────────────────────────────────────────────────────

    def set_selected_extension(self, uuid: str | None) -> None:
        """Update the pinned extension chip without auto-activating the filter."""
        self._pin_tag = uuid.split("@")[0] if uuid else None
        self._update_pin_chip()

    # ── Signal handlers — filters ──────────────────────────────────────────

    def _on_cmd_toggle(self, btn: Gtk.ToggleButton) -> None:
        revealed = btn.get_active()
        self._cmd_revealer.set_reveal_child(revealed)
        btn.set_icon_name("pan-up-symbolic" if revealed else "pan-down-symbolic")

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._search_text = entry.get_text()
        self._rebuild_view()

    def _on_auto_scroll_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._auto_scroll = btn.get_active()
        if self._auto_scroll:
            self._scroll_to_end()

    def _apply_wrap_policy(self) -> None:
        # Column sizes to cell content (no ellipsize → natural = text width)
        # so content can overflow the viewport and trigger horizontal scroll.
        self._msg_col.set_expand(False)
        self._scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

    def _on_stat_dot_toggled(self, btn: Gtk.ToggleButton, bucket: str) -> None:
        if btn.get_active():
            self._active_buckets.add(bucket)
        else:
            self._active_buckets.discard(bucket)
        self._rebuild_view()

    # ── Signal handlers — toolbar ──────────────────────────────────────────

    def _on_clear(self, _btn: Gtk.Button) -> None:
        self._entries.clear()
        self._store.splice(0, self._store.get_n_items(), [])
        for b in self._bucket_counts:
            self._bucket_counts[b] = 0
        self._tag_counts.clear()
        self._active_tags.clear()
        self._rebuild_chips()
        self._sync_tag_filter_state()
        self._refresh_stat_dots()
        self._update_status_label()
        self._update_list_stack()

    def _on_copy(self, _btn: Gtk.Button) -> None:
        lines = [self._format_row_for_copy(item) for item in self._selected_items()]
        if not lines:
            return
        self.get_clipboard().set("\n".join(lines))

    def _selected_items(self) -> list[LogRowItem]:
        bitset = self._selection.get_selection()
        n = bitset.get_size()
        if n == 0:
            return []
        items: list[LogRowItem] = []
        for i in range(n):
            pos = bitset.get_nth(i)
            item = self._store.get_item(pos)
            if item is not None:
                items.append(item)
        return items

    def _format_row_for_copy(self, item: LogRowItem) -> str:
        return (
            f"{item.time_str} "
            f"[{_bucket_label(item.bucket)}] "
            f"[{item.tag}] {item.body}"
        )

    def _on_selection_changed(self, _sel: Gtk.MultiSelection, _pos: int, _n: int) -> None:
        self._copy_btn.set_sensitive(self._selection.get_selection().get_size() > 0)

    def _on_export_shortcut(self, _widget: Gtk.Widget, _args: object) -> bool:
        self._on_export(None)
        return True

    def _on_run_shortcut(self, _widget: Gtk.Widget, _args: object) -> bool:
        self._on_start_stop(None)
        return True

    def _on_search_shortcut(self, _widget: Gtk.Widget, _args: object) -> bool:
        self._search_entry.grab_focus()
        return True

    def _on_clear_shortcut(self, _widget: Gtk.Widget, _args: object) -> bool:
        self._on_clear(None)
        return True

    def _on_export(self, _btn: Gtk.Button) -> None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        if len(self._active_tags) == 1:
            tag = next(iter(self._active_tags))
            base = f"gse-log_{tag}_{ts}"
        else:
            base = f"gse-log_{ts}"

        dialog = Gtk.FileDialog()
        dialog.set_title("Export Log")
        dialog.set_initial_name(f"{base}.txt")

        # File-type filters — text default, JSON as the second choice
        txt_filter = Gtk.FileFilter()
        txt_filter.set_name("Text file (.txt)")
        txt_filter.add_pattern("*.txt")

        json_filter = Gtk.FileFilter()
        json_filter.set_name("JSON file (.json)")
        json_filter.add_pattern("*.json")

        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(txt_filter)
        filters.append(json_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(txt_filter)

        dialog.save(self.get_root(), None, self._on_export_save, None)  # type: ignore[arg-type]

    def _on_export_save(
        self,
        dialog: Gtk.FileDialog,
        result: Gio.AsyncResult,
        _user_data: None,
    ) -> None:
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return

        path = gfile.get_path() or ""
        is_json = path.lower().endswith(".json")
        text = self._render_export(as_json=is_json)
        gfile.replace_contents_bytes_async(
            GLib.Bytes.new(text.encode("utf-8")),
            None,
            False,
            Gio.FileCreateFlags.REPLACE_DESTINATION,
            None,
            self._on_file_written,
            None,
        )

    def _render_export(self, *, as_json: bool) -> str:
        n = self._store.get_n_items()
        if as_json:
            rows = []
            for i in range(n):
                item: LogRowItem = self._store.get_item(i)
                rows.append({
                    "timestamp": item.entry.timestamp.isoformat(),
                    "priority": item.entry.priority,
                    "level": item.entry.priority_name,
                    "tag": item.tag,
                    "identifier": item.entry.identifier,
                    "message": item.body,
                })
            return json.dumps(rows, indent=2)
        lines = []
        for i in range(n):
            row: LogRowItem = self._store.get_item(i)
            lines.append(
                f"{row.entry.timestamp.strftime('%H:%M:%S.%f')[:-3]} "
                f"[{row.entry.priority_name:<7}] "
                f"[{row.tag}] "
                f"{row.body}"
            )
        return "\n".join(lines) + ("\n" if lines else "")

    def _on_file_written(
        self,
        gfile: Gio.File,
        result: Gio.AsyncResult,
        _user_data: None,
    ) -> None:
        try:
            gfile.replace_contents_finish(result)
        except GLib.Error as exc:
            _log.error("Log export failed: %s", exc)

    # ── Journal entry handling ─────────────────────────────────────────────

    def _on_log_entry(self, _reader: JournalReader, entry: LogEntry) -> None:
        if len(self._entries) == MAX_ENTRIES:
            evicted = self._entries[0]
            self._bucket_counts[_priority_bucket(evicted.priority)] -= 1
            evicted_tag = _entry_tag(evicted)
            self._tag_counts[evicted_tag] = max(0, self._tag_counts.get(evicted_tag, 0) - 1)
            if self._tag_counts[evicted_tag] == 0:
                del self._tag_counts[evicted_tag]

        self._entries.append(entry)
        self._bucket_counts[_priority_bucket(entry.priority)] += 1

        tag = _entry_tag(entry)
        is_new_tag = tag not in self._tag_counts
        self._tag_counts[tag] = self._tag_counts.get(tag, 0) + 1
        if is_new_tag:
            self._rebuild_chips()
        else:
            self._refresh_chip_counts()

        self._refresh_stat_dots()
        self._update_list_stack()

        if self._entry_matches(entry):
            row = LogRowItem(entry)
            self._store.append(row)
            if self._auto_scroll:
                GLib.idle_add(self._scroll_to_end)
            self._update_status_label()

    def _update_list_stack(self) -> None:
        """Swap between the empty Adw.StatusPage and the live list view."""
        target = "data" if self._entries else "empty"
        if self._list_stack.get_visible_child_name() != target:
            self._list_stack.set_visible_child_name(target)
        # Hide the filter controls (severity dots + tag chips) on the empty
        # placeholder — there is nothing to filter until entries arrive.
        has_entries = target == "data"
        self._dots_box.set_visible(has_entries)
        self._tag_bar.set_visible(has_entries)
        # Keep description + button in sync with running state.
        if target == "empty":
            if self._is_running:
                self._empty_page.set_description(
                    "Tailing the journal — waiting for the first entry…"
                )
                self._empty_start_btn.set_visible(False)
            else:
                self._empty_page.set_description(
                    "Press Start above to begin tailing the system journal."
                )
                self._empty_start_btn.set_visible(True)

    def _entry_matches(self, entry: LogEntry) -> bool:
        bucket = _priority_bucket(entry.priority)
        if self._active_buckets and bucket not in self._active_buckets:
            return False
        if self._active_tags and _entry_tag(entry) not in self._active_tags:
            return False
        if self._search_text and self._search_text.lower() not in entry.message.lower():
            return False
        return True

    # ── View rebuild ──────────────────────────────────────────────────────

    def _rebuild_view(self) -> None:
        items = [LogRowItem(e) for e in self._entries if self._entry_matches(e)]
        self._store.splice(0, self._store.get_n_items(), items)
        self._update_status_label()
        if self._auto_scroll:
            GLib.idle_add(self._scroll_to_end)

    def _refresh_stat_dots(self) -> None:
        for bucket, label in self._stat_labels.items():
            label.set_label(f"{_bucket_label(bucket)} {self._bucket_counts[bucket]}")

    def _update_status_label(self) -> None:
        visible = self._store.get_n_items()
        total = len(self._entries)
        word = "line" if total == 1 else "lines"
        self._status_lbl.set_label(f"Showing {visible} of {total} {word}")

    # ── Scroll helpers ─────────────────────────────────────────────────────

    def _scroll_to_end(self) -> bool:
        vadj = self._scroll.get_vadjustment()
        vadj.set_value(vadj.get_upper() - vadj.get_page_size())
        return False
