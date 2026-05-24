import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import cast

import gi

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from collections import deque

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from app.core.dbus_client import DBusClient
from app.core.journal_reader import JournalReader, LogEntry, parse_extra_args

_log = logging.getLogger(__name__)

MAX_ENTRIES = 5000
_DEFAULT_CMD = "journalctl --user -f"
_SETTINGS_KEY = "journal_cmd"
_INVALID_POS = GLib.MAXUINT

# Priority bucket → stat dot identifier. Buckets group the syslog priorities
# into four user-friendly severities.
_BUCKET_ERROR = "error"   # priority 0-3 (emerg / alert / crit / error)
_BUCKET_WARN = "warn"     # priority 4 (warning)
_BUCKET_INFO = "info"     # priority 5-6 (notice / info)
_BUCKET_DEBUG = "debug"   # priority 7 (debug)

_BUCKET_LABELS: dict[str, str] = {
    _BUCKET_ERROR: "ERROR",
    _BUCKET_WARN: "WARN",
    _BUCKET_INFO: "INFO",
    _BUCKET_DEBUG: "DEBUG",
}

# Hash-derived tag color palette (12 hues defined in style.css as tag-c0..tag-cB)
_TAG_PALETTE_SIZE = 12
_TAG_PALETTE_CHARS = "0123456789AB"
_TAG_CSS_CLASSES = tuple(f"tag-c{c}" for c in _TAG_PALETTE_CHARS)
_LEVEL_PILL_CLASSES = ("lvl-error", "lvl-warn", "lvl-info", "lvl-debug")

_MSG_TAG_RE = re.compile(r'^(?:JS LOG:\s*)?\[([^\]]+)\]\s*(.*)', re.DOTALL)


def _priority_bucket(priority: int) -> str:
    if priority <= 3:
        return _BUCKET_ERROR
    if priority == 4:
        return _BUCKET_WARN
    if priority <= 6:
        return _BUCKET_INFO
    return _BUCKET_DEBUG


def _bucket_pill_class(bucket: str) -> str:
    return {
        _BUCKET_ERROR: "lvl-error",
        _BUCKET_WARN: "lvl-warn",
        _BUCKET_INFO: "lvl-info",
        _BUCKET_DEBUG: "lvl-debug",
    }[bucket]


def _bucket_label(bucket: str) -> str:
    return _BUCKET_LABELS[bucket]


def _tag_color_class(tag: str) -> str:
    digest = hashlib.md5(tag.encode("utf-8")).digest()
    idx = digest[0] % _TAG_PALETTE_SIZE
    return _TAG_CSS_CLASSES[idx]


def _extract_log_tag(message: str) -> tuple[str | None, str]:
    m = _MSG_TAG_RE.match(message)
    if m:
        return m.group(1), m.group(2)
    return None, message


def _settings_path() -> Path:
    return Path(GLib.get_user_config_dir()) / "gse-profiler" / "log-viewer.json"


def _load_settings() -> dict[str, object]:
    p = _settings_path()
    if p.exists():
        try:
            return cast(dict[str, object], json.loads(p.read_text(encoding="utf-8")))
        except Exception as exc:
            _log.warning("Failed to load settings from %s: %s", p, exc)
    return {}


def _save_settings(data: dict) -> None:
    p = _settings_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        _log.error("Failed to save settings to %s: %s", p, exc)


class LogRowItem(GObject.Object):
    """One row in the log column view."""

    __gtype_name__ = "LogRowItem"

    def __init__(self, entry: LogEntry) -> None:
        super().__init__()
        self.entry = entry
        tag, body = _extract_log_tag(entry.message)
        self.tag = tag if tag else entry.identifier
        self.body = body
        self.bucket = _priority_bucket(entry.priority)
        self.time_str = entry.timestamp.strftime("%H:%M:%S.%f")[:-3]


class LogViewerView(Gtk.Box):
    """Live journalctl log viewer with structured column-view rendering."""

    def __init__(self, dbus_client: DBusClient) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._dbus = dbus_client
        self._reader = JournalReader()
        self._entries: deque[LogEntry] = deque(maxlen=MAX_ENTRIES)

        # Filter state
        self._uuid_filter: str | None = None
        self._selected_uuid: str | None = None
        self._filter_selected = False
        self._active_buckets: set[str] = set()
        self._search_text = ""
        self._auto_scroll = True
        self._is_running = False

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

        settings = _load_settings()
        cmd = settings.get(_SETTINGS_KEY, _DEFAULT_CMD)
        self._journal_cmd: str = cmd if isinstance(cmd, str) else _DEFAULT_CMD

        # Column-view backing store and selection (multi-select for copy)
        self._store = Gio.ListStore(item_type=LogRowItem)
        self._selection = Gtk.MultiSelection.new(self._store)

        self._build_ui()

        self._reader.connect("log-entry", self._on_log_entry)
        self.connect("destroy", lambda _w: self._reader.stop())

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── Command bar ─────────────────────────────────────────────────────
        cmd_label = Gtk.Label(label="Command:")

        self._cmd_entry = Gtk.Entry()
        self._cmd_entry.set_text(self._journal_cmd)
        self._cmd_entry.set_hexpand(True)
        self._cmd_entry.set_placeholder_text("journalctl --user -f")
        self._cmd_entry.set_tooltip_text(
            "journalctl-compatible filter — supported flags: --user, --system, "
            "-t/--identifier, -u/--unit, -b/--boot, -p/--priority; "
            "--follow/-f and -o/-n/--after-cursor are ignored (managed internally)"
        )
        self._cmd_entry.connect("activate", self._on_cmd_activate)
        self._cmd_entry.connect("changed", self._on_cmd_changed)

        self._start_stop_btn = Gtk.Button()
        self._start_stop_btn.add_css_class("suggested-action")
        self._start_stop_btn.set_tooltip_text("Start reading the journal")
        self._start_stop_btn.connect("clicked", self._on_start_stop)
        self._set_start_stop_state(running=False)

        cmd_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        cmd_bar.add_css_class("log-cmdbar")
        cmd_bar.append(cmd_label)
        cmd_bar.append(self._cmd_entry)

        self._cmd_revealer = Gtk.Revealer()
        self._cmd_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._cmd_revealer.set_reveal_child(False)
        self._cmd_revealer.set_child(cmd_bar)

        # ── Filter bar ──────────────────────────────────────────────────────
        self._filter_selected_btn = Gtk.ToggleButton(label="Selected")
        self._filter_selected_btn.set_icon_name("application-x-addon-symbolic")
        self._filter_selected_btn.set_tooltip_text(
            "Show logs for the selected extension only"
        )
        self._filter_selected_btn.set_active(False)
        self._filter_selected_btn.connect("toggled", self._on_filter_selected_toggled)

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
        self._cmd_toggle_btn.set_tooltip_text("Show/hide command")
        self._cmd_toggle_btn.set_active(False)
        self._cmd_toggle_btn.connect("toggled", self._on_cmd_toggle)

        filter_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        filter_bar.add_css_class("log-filterbar")
        filter_bar.append(self._filter_selected_btn)
        filter_bar.append(self._search_entry)
        filter_bar.append(self._auto_scroll_btn)

        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        filter_bar.append(sep)
        filter_bar.append(self._copy_btn)
        filter_bar.append(export_btn)
        filter_bar.append(clear_btn)

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        filter_bar.append(sep2)
        filter_bar.append(self._cmd_toggle_btn)
        filter_bar.append(self._start_stop_btn)

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
        time_fac.connect("setup", self._time_setup)
        time_fac.connect("bind", self._time_bind)
        time_col = Gtk.ColumnViewColumn(title="TIME", factory=time_fac)
        time_col.set_fixed_width(110)
        time_col.set_resizable(True)
        col_view.append_column(time_col)

        # LEVEL pill column
        level_fac = Gtk.SignalListItemFactory()
        level_fac.connect("setup", self._level_setup)
        level_fac.connect("bind", self._level_bind)
        level_col = Gtk.ColumnViewColumn(title="LEVEL", factory=level_fac)
        level_col.set_fixed_width(70)
        level_col.set_resizable(True)
        col_view.append_column(level_col)

        # TAG column — colored monospace, e.g. [dash-to-dock]
        tag_fac = Gtk.SignalListItemFactory()
        tag_fac.connect("setup", self._tag_setup)
        tag_fac.connect("bind", self._tag_bind)
        tag_col = Gtk.ColumnViewColumn(title="TAG", factory=tag_fac)
        tag_col.set_fixed_width(190)
        tag_col.set_resizable(True)
        col_view.append_column(tag_col)

        # MESSAGE column
        msg_fac = Gtk.SignalListItemFactory()
        msg_fac.connect("setup", self._msg_setup)
        msg_fac.connect("bind", self._msg_bind)
        msg_col = Gtk.ColumnViewColumn(title="MESSAGE", factory=msg_fac)
        msg_col.set_expand(True)
        msg_col.set_resizable(True)
        col_view.append_column(msg_col)

        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_vexpand(True)
        self._scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._scroll.set_size_request(0, -1)
        self._scroll.set_child(col_view)

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
        self.append(self._cmd_revealer)
        self.append(status_bar)
        self.append(self._list_stack)

        shortcut_ctrl = Gtk.ShortcutController()
        shortcut_ctrl.set_scope(Gtk.ShortcutScope.MANAGED)
        shortcut_ctrl.add_shortcut(Gtk.Shortcut.new(
            Gtk.KeyvalTrigger.new(Gdk.KEY_s, Gdk.ModifierType.CONTROL_MASK),
            Gtk.CallbackAction.new(self._on_export_shortcut),
        ))
        self.add_controller(shortcut_ctrl)

        self._update_status_label()

    # ── Column factories ───────────────────────────────────────────────────

    def _make_cell_box(self, content: Gtk.Widget) -> Gtk.Box:
        """Wrap a cell's content widget in a Gtk.Box that fills the cell so
        the severity tint can be applied to the box background."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        box.set_hexpand(True)
        box.add_css_class("log-cell")
        box.append(content)
        return box

    def _apply_cell_tint(self, box: Gtk.Box, bucket: str) -> None:
        for cls in ("cell-warn", "cell-error"):
            box.remove_css_class(cls)
        if bucket == _BUCKET_ERROR:
            box.add_css_class("cell-error")
        elif bucket == _BUCKET_WARN:
            box.add_css_class("cell-warn")

    def _time_setup(self, _fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        label = Gtk.Label()
        label.set_halign(Gtk.Align.START)
        label.set_hexpand(True)
        label.add_css_class("log-time")
        list_item.set_child(self._make_cell_box(label))

    def _time_bind(self, _fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        item: LogRowItem = list_item.get_item()
        box: Gtk.Box = list_item.get_child()
        label: Gtk.Label = box.get_first_child()
        label.set_label(item.time_str)
        self._apply_cell_tint(box, item.bucket)

    def _level_setup(self, _fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        label = Gtk.Label()
        label.set_halign(Gtk.Align.START)
        label.set_valign(Gtk.Align.CENTER)
        label.add_css_class("log-level-pill")
        list_item.set_child(self._make_cell_box(label))

    def _level_bind(self, _fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        item: LogRowItem = list_item.get_item()
        box: Gtk.Box = list_item.get_child()
        label: Gtk.Label = box.get_first_child()
        for cls in _LEVEL_PILL_CLASSES:
            label.remove_css_class(cls)
        label.set_label(_bucket_label(item.bucket))
        label.add_css_class(_bucket_pill_class(item.bucket))
        self._apply_cell_tint(box, item.bucket)

    def _tag_setup(self, _fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        label = Gtk.Label()
        label.set_halign(Gtk.Align.START)
        label.set_hexpand(True)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.add_css_class("log-tag")
        list_item.set_child(self._make_cell_box(label))

    def _tag_bind(self, _fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        item: LogRowItem = list_item.get_item()
        box: Gtk.Box = list_item.get_child()
        label: Gtk.Label = box.get_first_child()
        for cls in _TAG_CSS_CLASSES:
            label.remove_css_class(cls)
        label.set_label(f"[{item.tag}]")
        label.add_css_class(_tag_color_class(item.tag))
        self._apply_cell_tint(box, item.bucket)

    def _msg_setup(self, _fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        label = Gtk.Label()
        label.set_halign(Gtk.Align.START)
        label.set_hexpand(True)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.add_css_class("log-message")
        list_item.set_child(self._make_cell_box(label))

    def _msg_bind(self, _fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        item: LogRowItem = list_item.get_item()
        box: Gtk.Box = list_item.get_child()
        label: Gtk.Label = box.get_first_child()
        label.set_label(item.body)
        self._apply_cell_tint(box, item.bucket)

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
            self._cmd_entry.set_sensitive(False)
        else:
            self._start_stop_btn.remove_css_class("destructive-action")
            self._start_stop_btn.add_css_class("suggested-action")
            self._start_stop_btn.set_tooltip_text("Start reading the journal")
            self._cmd_entry.set_sensitive(True)

    def _set_state_pill(self, running: bool) -> None:
        for c in ("running", "stopped"):
            self._state_pill.remove_css_class(c)
        if running:
            self._state_pill.set_label("RUNNING")
            self._state_pill.add_css_class("running")
        else:
            self._state_pill.set_label("STOPPED")
            self._state_pill.add_css_class("stopped")

    def _on_cmd_activate(self, _entry: Gtk.Entry) -> None:
        if not self._is_running:
            self._do_start()

    def _on_cmd_changed(self, entry: Gtk.Entry) -> None:
        self._journal_cmd = entry.get_text()
        _save_settings({_SETTINGS_KEY: self._journal_cmd})

    def _on_start_stop(self, _btn: Gtk.Button) -> None:
        if self._is_running:
            self._do_stop()
        else:
            self._do_start()

    def _do_start(self) -> None:
        extra = parse_extra_args(self._journal_cmd)
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
        """Update the currently selected extension for the 'Selected' filter."""
        self._selected_uuid = uuid
        if self._filter_selected:
            self._uuid_filter = uuid
            self._rebuild_view()

    # ── Signal handlers — filters ──────────────────────────────────────────

    def _on_cmd_toggle(self, btn: Gtk.ToggleButton) -> None:
        revealed = btn.get_active()
        self._cmd_revealer.set_reveal_child(revealed)
        btn.set_icon_name("pan-up-symbolic" if revealed else "pan-down-symbolic")

    def _on_filter_selected_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._filter_selected = btn.get_active()
        self._uuid_filter = self._selected_uuid if self._filter_selected else None
        self._rebuild_view()

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._search_text = entry.get_text()
        self._rebuild_view()

    def _on_auto_scroll_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._auto_scroll = btn.get_active()
        if self._auto_scroll:
            self._scroll_to_end()

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

    def _on_export(self, _btn: Gtk.Button) -> None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        if self._uuid_filter:
            short = self._uuid_filter.split("@")[0]
            base = f"gse-log_{short}_{ts}"
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

        self._entries.append(entry)
        self._bucket_counts[_priority_bucket(entry.priority)] += 1
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
        if self._uuid_filter:
            short = self._uuid_filter.split("@")[0]
            if f"[{short}]" not in entry.message:
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
