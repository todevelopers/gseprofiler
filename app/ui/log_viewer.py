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

# Horizontal overhead added to text width when estimating a flat tag-chip button
_CHIP_BTN_OVERHEAD = 20
# Extra width for the icon + inner spacing inside the "+N more" menu button
_MORE_BTN_ICON_OVERHEAD = 16

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


def _tag_display(tag: str) -> str:
    """Human-readable label for a tag chip / popover row. Entries with no tag
    have an empty-string tag; show a placeholder instead of a blank chip.
    (The log table TAG column keeps showing the raw value.)"""
    return tag if tag else "<empty>"


def _extract_log_tag(message: str) -> tuple[str | None, str]:
    m = _MSG_TAG_RE.match(message)
    if m:
        return m.group(1), m.group(2)
    return None, message


def _entry_tag(entry: LogEntry) -> str:
    tag, _ = _extract_log_tag(entry.message)
    return tag if tag else entry.identifier


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


class _TagBar(Gtk.Box):
    """Tag-bar container that fires a callback whenever its allocated width changes.

    GTK4 removed size-allocate as a public signal; overriding do_size_allocate
    in a Gtk.Box subclass is the idiomatic replacement.
    """

    __gtype_name__ = "GseTagBar"

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.on_width_changed: "Callable[[int], None] | None" = None
        self._last_w: int = 0

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        super().do_size_allocate(width, height, baseline)
        if width != self._last_w:
            self._last_w = width
            if self.on_width_changed is not None:
                self.on_width_changed(width)


class LogViewerView(Gtk.Box):
    """Live journalctl log viewer with structured column-view rendering."""

    def __init__(self, dbus_client: DBusClient) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._dbus = dbus_client
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
        self.append(tag_bar)
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
        self._update_list_stack()

    def _build_tag_bar(self) -> _TagBar:
        bar = _TagBar()
        bar.add_css_class("log-tagbar")
        self._tag_bar = bar
        bar.on_width_changed = self._on_tag_bar_width_changed

        # Pin chip — always first; updated when selected extension changes
        self._pin_chip = Gtk.ToggleButton()
        self._pin_chip.add_css_class("tag-chip")
        self._pin_chip.add_css_class("tag-chip-pin")
        self._pin_chip.add_css_class("flat")
        self._pin_chip.set_valign(Gtk.Align.CENTER)
        self._pin_chip.set_visible(False)
        self._pin_chip.set_tooltip_text("Show only logs for the selected extension")
        self._pin_chip.connect("toggled", self._on_pin_chip_toggled)
        bar.append(self._pin_chip)

        self._pin_sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        self._pin_sep.set_margin_start(2)
        self._pin_sep.set_margin_end(2)
        self._pin_sep.set_visible(False)
        bar.append(self._pin_sep)

        # Dynamic inline chip slots
        self._inline_chips_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        bar.append(self._inline_chips_box)

        # "+N more" overflow button with popover
        self._more_label = Gtk.Label(label="+0 more")
        more_icon = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        more_icon.set_pixel_size(12)
        more_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        more_content.append(self._more_label)
        more_content.append(more_icon)

        self._more_btn = Gtk.MenuButton()
        self._more_btn.set_child(more_content)
        self._more_btn.add_css_class("flat")
        self._more_btn.add_css_class("tag-chip")
        self._more_btn.set_valign(Gtk.Align.CENTER)
        self._more_btn.set_visible(False)
        bar.append(self._more_btn)

        # Popover: search entry + scrollable list of all tags
        popover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        popover_box.set_margin_top(8)
        popover_box.set_margin_bottom(8)
        popover_box.set_margin_start(8)
        popover_box.set_margin_end(8)

        self._popover_search = Gtk.SearchEntry()
        self._popover_search.set_placeholder_text("Filter tags…")
        self._popover_search.set_size_request(210, -1)
        self._popover_search.connect("search-changed", self._on_popover_search_changed)
        popover_box.append(self._popover_search)

        pop_scroll = Gtk.ScrolledWindow()
        pop_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        # Grow to fit the tag list (so short lists show every row) and only
        # start scrolling past max. Without propagate-natural-height the window
        # stays pinned at min_content_height and shows ~3 rows regardless.
        pop_scroll.set_propagate_natural_height(True)
        pop_scroll.set_min_content_height(0)
        pop_scroll.set_max_content_height(400)

        self._popover_list = Gtk.ListBox()
        self._popover_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._popover_list.add_css_class("tag-popover-list")
        self._popover_list.set_filter_func(self._popover_row_filter)
        pop_scroll.set_child(self._popover_list)
        popover_box.append(pop_scroll)

        popover = Gtk.Popover()
        popover.set_child(popover_box)
        popover.connect("show", self._on_popover_show)
        self._more_btn.set_popover(popover)

        # Spacer pushes Clear to the right edge
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bar.append(spacer)

        self._clear_tags_btn = Gtk.Button(label="Clear")
        self._clear_tags_btn.add_css_class("flat")
        self._clear_tags_btn.add_css_class("tag-clear")
        self._clear_tags_btn.set_valign(Gtk.Align.CENTER)
        self._clear_tags_btn.set_tooltip_text("Clear tag filter")
        self._clear_tags_btn.set_visible(False)
        self._clear_tags_btn.connect("clicked", self._on_clear_tags)
        bar.append(self._clear_tags_btn)

        return bar

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
        label.set_hexpand(False)
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
        settings = _load_settings()
        settings[_SETTINGS_KEY] = self._journal_cmd
        _save_settings(settings)

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
        """Update the pinned extension chip without auto-activating the filter."""
        self._pin_tag = uuid.split("@")[0] if uuid else None
        self._update_pin_chip()

    # ── Tag bar — builders / refreshers ───────────────────────────────────

    def _update_pin_chip(self) -> None:
        if self._pin_tag is None:
            self._pin_chip.set_visible(False)
            self._pin_sep.set_visible(False)
            return

        for cls in _TAG_CSS_CLASSES:
            self._pin_chip.remove_css_class(cls)
        self._pin_chip.add_css_class(_tag_color_class(self._pin_tag))
        self._pin_chip.set_label(f"{self._pin_tag} {self._tag_counts.get(self._pin_tag, 0)}")
        self._pin_chip.set_visible(True)
        self._pin_sep.set_visible(bool(self._tag_counts))

        self._block_tag_signals = True
        self._pin_chip.set_active(self._pin_tag in self._active_tags)
        self._block_tag_signals = False

    def _chip_natural_width(self, text: str) -> int:
        """Estimate the natural pixel width of a flat tag-chip button with *text*."""
        ctx = self._inline_chips_box.get_pango_context()
        layout = Pango.Layout.new(ctx)
        layout.set_text(text, -1)
        w, _ = layout.get_pixel_size()
        return w + _CHIP_BTN_OVERHEAD

    def _on_tag_bar_width_changed(self, _width: int) -> None:
        if not self._chips_rebuild_pending:
            self._chips_rebuild_pending = True
            GLib.idle_add(self._chips_rebuild_idle)

    def _chips_rebuild_idle(self) -> bool:
        self._chips_rebuild_pending = False
        self._rebuild_chips()
        return False

    def _compute_available_chips_width(self) -> int:
        """Available pixel width for the inline chip area (bar minus fixed elements)."""
        bar_w = self._tag_bar.get_width()
        if bar_w == 0:
            return 0
        taken = 0
        if self._pin_chip.get_visible():
            taken += self._pin_chip.get_width() + 4
        if self._pin_sep.get_visible():
            taken += max(self._pin_sep.get_width(), 1) + 4
        if self._clear_tags_btn.get_visible():
            taken += self._clear_tags_btn.get_width() + 4
        return max(0, bar_w - taken)

    def _rebuild_chips(self) -> None:
        """Rebuild inline tag chips, fitting as many as the available width allows."""
        while (child := self._inline_chips_box.get_first_child()):
            self._inline_chips_box.remove(child)
        self._inline_chip_widgets.clear()

        sorted_tags = sorted(self._tag_counts.items(), key=lambda x: -x[1])
        filtered = [(t, c) for t, c in sorted_tags if t != self._pin_tag]

        available = self._compute_available_chips_width()
        spacing = 4  # matches Box(spacing=4)

        if available > 0:
            # Greedily fit chips; each iteration decides whether there is room
            # for the next chip *plus* the "+N more" button if needed.
            more_w = (
                self._chip_natural_width(f"+{len(filtered)} more")
                + _MORE_BTN_ICON_OVERHEAD
            )
            inline: list[tuple[str, int]] = []
            used = 0
            for i, (tag, count) in enumerate(filtered):
                chip_w = self._chip_natural_width(f"{_tag_display(tag)} {count}")
                gap = spacing if inline else 0
                remaining = len(filtered) - i - 1
                overflow_reserve = (spacing + more_w) if remaining > 0 else 0
                if used + gap + chip_w + overflow_reserve <= available:
                    inline.append((tag, count))
                    used += gap + chip_w
                else:
                    break
            overflow = len(filtered) - len(inline)
        else:
            # Width not yet known — show up to 4 chips as an initial fallback.
            inline = filtered[:4]
            overflow = max(0, len(filtered) - 4)

        for tag, count in inline:
            btn = Gtk.ToggleButton(label=f"{_tag_display(tag)} {count}")
            btn.add_css_class("tag-chip")
            btn.add_css_class("flat")
            btn.add_css_class(_tag_color_class(tag))
            btn.set_valign(Gtk.Align.CENTER)
            btn.set_tooltip_text(f"Show only {_tag_display(tag)} entries")
            btn.set_active(tag in self._active_tags)
            btn.connect("toggled", self._on_inline_chip_toggled, tag)
            self._inline_chips_box.append(btn)
            self._inline_chip_widgets[tag] = btn

        if overflow > 0:
            self._more_label.set_label(f"+{overflow} more")
            self._more_btn.set_visible(True)
        else:
            self._more_btn.set_visible(False)

        self._pin_sep.set_visible(
            self._pin_tag is not None and bool(self._tag_counts)
        )

    def _rebuild_popover(self) -> None:
        """Rebuild the popover list from the current tag counts (called on open)."""
        while True:
            row = self._popover_list.get_row_at_index(0)
            if row is None:
                break
            self._popover_list.remove(row)
        self._popover_row_widgets.clear()

        for tag, count in sorted(self._tag_counts.items(), key=lambda x: -x[1]):
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row_box.set_margin_top(3)
            row_box.set_margin_bottom(3)
            row_box.set_margin_start(6)
            row_box.set_margin_end(8)

            check = Gtk.CheckButton()
            check.set_active(tag in self._active_tags)
            check.connect("toggled", self._on_popover_check_toggled, tag)

            tag_lbl = Gtk.Label(label=_tag_display(tag))
            tag_lbl.set_hexpand(True)
            tag_lbl.set_halign(Gtk.Align.START)
            tag_lbl.add_css_class("log-tag")
            tag_lbl.add_css_class(_tag_color_class(tag))

            count_lbl = Gtk.Label(label=str(count))
            count_lbl.add_css_class("dim-label")

            row_box.append(check)
            row_box.append(tag_lbl)
            row_box.append(count_lbl)

            list_row = Gtk.ListBoxRow()
            list_row.set_child(row_box)
            list_row._tag_value = tag  # type: ignore[attr-defined]
            self._popover_list.append(list_row)
            self._popover_row_widgets[tag] = (check, count_lbl)

    def _sync_tag_filter_state(self) -> None:
        """Sync all chip toggle states and the Clear button without firing handlers."""
        self._block_tag_signals = True
        for tag, btn in self._inline_chip_widgets.items():
            btn.set_active(tag in self._active_tags)
        if self._pin_tag is not None:
            self._pin_chip.set_active(self._pin_tag in self._active_tags)
        for tag, (check, _) in self._popover_row_widgets.items():
            check.set_active(tag in self._active_tags)
        self._block_tag_signals = False
        self._clear_tags_btn.set_visible(bool(self._active_tags))

    # ── Tag bar — signal handlers ─────────────────────────────────────────

    def _on_pin_chip_toggled(self, btn: Gtk.ToggleButton) -> None:
        if self._block_tag_signals or self._pin_tag is None:
            return
        if btn.get_active():
            self._active_tags.add(self._pin_tag)
        else:
            self._active_tags.discard(self._pin_tag)
        self._sync_tag_filter_state()
        self._rebuild_view()

    def _on_inline_chip_toggled(self, btn: Gtk.ToggleButton, tag: str) -> None:
        if self._block_tag_signals:
            return
        if btn.get_active():
            self._active_tags.add(tag)
        else:
            self._active_tags.discard(tag)
        self._sync_tag_filter_state()
        self._rebuild_view()

    def _on_popover_check_toggled(self, check: Gtk.CheckButton, tag: str) -> None:
        if self._block_tag_signals:
            return
        if check.get_active():
            self._active_tags.add(tag)
        else:
            self._active_tags.discard(tag)
        self._sync_tag_filter_state()
        self._rebuild_view()

    def _on_popover_show(self, _popover: Gtk.Popover) -> None:
        self._rebuild_popover()
        self._popover_search.set_text("")

    def _on_popover_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        self._popover_list.invalidate_filter()

    def _popover_row_filter(self, row: Gtk.ListBoxRow) -> bool:
        text = self._popover_search.get_text().lower()
        if not text:
            return True
        return text in getattr(row, "_tag_value", "").lower()

    def _on_clear_tags(self, _btn: Gtk.Button) -> None:
        self._active_tags.clear()
        self._sync_tag_filter_state()
        self._rebuild_view()

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

    def _refresh_chip_counts(self) -> None:
        """Update inline + pin chip labels in place when tag counts change
        without a full rebuild (a new tag triggers _rebuild_chips instead)."""
        for tag, btn in self._inline_chip_widgets.items():
            btn.set_label(f"{_tag_display(tag)} {self._tag_counts.get(tag, 0)}")
        if self._pin_tag is not None:
            self._pin_chip.set_label(
                f"{self._pin_tag} {self._tag_counts.get(self._pin_tag, 0)}"
            )

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
