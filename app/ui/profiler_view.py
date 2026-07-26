import json
import logging
import secrets
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any

import gi

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from app.core.dbus_client import DBusClient, ExtensionState
from app.core.exporters import to_speedscope, to_trace_event
from app.core.keybindings import KeybindingManager, populate_shortcut_controller
from app.core.message_router import MessageRouter
from app.core.settings import Settings
from app.core.socket_server import SocketServer
from app.ui.profiler import desaturate_color
from app.ui.profiler.flamegraph import FlamegraphView
from app.ui.profiler.histogram import HistogramView
from app.ui.profiler.stat_card import StatCard, StatCardStrip
from app.ui.profiler.swimlane import SwimlaneView

_log = logging.getLogger(__name__)

_MODES = ("flamegraph", "swimlane", "histogram")
_MODE_LABELS: dict[str, str] = {
    "flamegraph": "Flamegraph",
    "swimlane": "Swimlane",
    "histogram": "Histogram",
}
_DEFAULT_MODE = "swimlane"
_MODE_HINTS: dict[str, str] = {
    "flamegraph": (
        "Shows function calls as a nested stack. Each bar's width reflects how long"
        " the call took relative to the total span. Bars stacked vertically show the"
        " call hierarchy: caller at the bottom, callees above. Wider bars are slower."
        " Click any bar to highlight all calls to that function."
    ),
    "swimlane": (
        "Shows each unique function in its own horizontal lane. Each colored segment"
        " marks one invocation, its width reflecting duration. Lanes are sorted by"
        " total time, slowest at top. Useful for spotting call frequency and whether"
        " invocations overlap in time. Click a segment to select that function."
    ),
    "histogram": (
        "Ranks the top functions by self time spent inside the function itself,"
        " excluding callees. Each bar's width is the total self time summed across all"
        " calls. Functions with the widest bars are your bottlenecks. Bars in red"
        " exceed 70 % of the chart maximum. Click a bar to select that function."
    ),
}

_FN_HINT = (
    "Lists functions that were observed at least once while recording, with"
    " aggregated stats across all calls. Instrumented functions that were not"
    " called do not appear as rows. Observed data remains accumulated across"
    " Stop/Start until it is cleared; the instrumented count describes the"
    " latest discovery scan."
    " Total is the full duration including time spent in nested calls."
    " Self is time the function spent in its own code only; if it calls other"
    " functions, their time is not counted. A function with high Total but low"
    " Self is mostly waiting on its callees. Avg and Max show the average and"
    " slowest single call duration. The Distribution bar shows two overlapping"
    " bars: the lighter one for Total, the darker one for Self, both scaled"
    " relative to the busiest function and color-coded by load."
)


_MAX_RAW_EVENTS = 50_000


def _fmt_ms(v: float) -> str:
    if v >= 1000.0:
        return f"{v / 1000.0:.2f} s"
    if v >= 1.0:
        return f"{v:.2f} ms"
    return f"{v:.3f} ms"


def _count_label(count: int, singular: str) -> str:
    suffix = "" if count == 1 else "s"
    return f"{count} {singular}{suffix}"


class FunctionStat(GObject.Object):
    """Aggregated timing statistics for a single profiled function."""

    __gtype_name__ = "FunctionStat"

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self.count: int = 0
        self.total_ms: float = 0.0
        self.self_ms: float = 0.0
        self.max_ms: float = 0.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0

    def record(self, duration_ms: float) -> None:
        self.count += 1
        self.total_ms += duration_ms
        if duration_ms > self.max_ms:
            self.max_ms = duration_ms


class ProfilerView(Gtk.Stack):
    """Live function timing profiler with three switchable timeline modes."""

    # Emitted when a global (bridge) keyboard shortcut drives profiling, so the
    # main window can surface a toast and bring the Profiler tab forward.
    __gsignals__ = {
        "show-toast": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "request-attention": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(
        self,
        dbus_client: DBusClient,
        socket_server: SocketServer,
        keybindings: KeybindingManager,
    ) -> None:
        super().__init__()
        self._dbus = dbus_client
        self._socket = socket_server
        self._keybindings = keybindings
        self._profiling = False
        self._refresh_pending = False
        self._target_uuid: str | None = None
        # Randomize the per-process base so a late batch from a bridge session
        # that outlived a crashed/restarted app cannot collide with the first
        # recording generation of the new process.
        self._start_generation = secrets.randbits(48)
        self._active_start_generation = 0
        self._accepted_event_generation = 0
        self._pending_start_acks: deque[tuple[int, str]] = deque()

        # Data
        self._stats: dict[str, FunctionStat] = {}
        self._raw_events: deque[dict[str, Any]] = deque(maxlen=_MAX_RAW_EVENTS)
        self._selected_fn: str | None = None
        self._filter_text: str = ""
        self._max_total_ms: float = 1.0
        self._instrumented_functions: int | None = None
        self._visited_objects: int | None = None
        self._skipped_functions: int | None = None
        self._instrumentation_truncated = False

        # Recording stopwatch
        self._rec_start_ts: float | None = None
        self._rec_timer_id: int = 0

        self._settings = Settings("profiler")
        settings = self._settings.load()
        mode = settings.get("mode", _DEFAULT_MODE)
        self._mode: str = mode if mode in _MODES else _DEFAULT_MODE
        self._hide_idle: bool = bool(settings.get("hide_idle", False))
        raw_pos = settings.get("paned_pos")
        self._paned_pos: int | None = int(raw_pos) if raw_pos is not None else None
        self._paned_save_id: int = 0
        self._paned_default_set: bool = False

        self._store: Gio.ListStore = Gio.ListStore(item_type=FunctionStat)

        self._build_ui()

        # Bridge messages are routed by type through a MessageRouter instead of
        # a per-view type filter (see app/core/message_router.py).
        self._router = MessageRouter(socket_server)
        self._router.on("profile_event", self._on_profile_event)
        self._router.on("profile_batch", self._on_profile_batch)
        self._router.on("profiling_started", self._on_profiling_started)
        self._router.on("profiling_stopped", self._on_profiling_stopped)
        self._router.on("toggle_profiling", self._on_toggle_profiling)
        self._router.on("restart_profiling", self._on_restart_profiling)

        self._signal_ids: list[tuple[GObject.Object, int]] = [
            (socket_server, socket_server.connect("client-connected", self._on_client_connected)),
            (socket_server, socket_server.connect("client-disconnected", self._on_client_disconnected)),
            (dbus_client, dbus_client.connect("extensions-changed", self._on_extensions_changed)),
        ]
        self._kb_changed_id = self._keybindings.connect_changed(self._on_keybindings_changed)
        self.connect("destroy", self._on_destroy)

    def _on_destroy(self, _widget: Gtk.Widget) -> None:
        self._router.shutdown()
        for obj, sig_id in self._signal_ids:
            obj.disconnect(sig_id)
        self._signal_ids.clear()
        self._keybindings.disconnect(self._kb_changed_id)

    def _on_keybindings_changed(self, _action_id: str) -> None:
        populate_shortcut_controller(
            self._shortcut_ctrl, self._keybindings, self._shortcut_bindings
        )

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        placeholder = Adw.StatusPage()
        placeholder.set_icon_name("power-profile-performance-symbolic")
        placeholder.set_title("No Extension Selected")
        placeholder.set_description("Select an enabled extension from the list to start profiling.")
        placeholder.set_child(self._build_placeholder_actions(
            "Select an enabled extension to start"
        ))
        self.add_named(placeholder, "placeholder")

        disabled = Adw.StatusPage()
        disabled.set_icon_name("power-profile-performance-symbolic")
        disabled.set_title("Extension Disabled")
        disabled.set_description("Enable the extension to start profiling.")
        disabled.set_child(self._build_placeholder_actions(
            "Enable the extension to start"
        ))
        self.add_named(disabled, "disabled")

        bridge_offline_page = Adw.StatusPage()
        bridge_offline_page.set_icon_name("network-offline-symbolic")
        bridge_offline_page.set_title("Bridge Extension Offline")
        bridge_offline_page.set_description(
            "The bridge extension is not running. Enable it in the Extensions tab"
            " to start profiling, or load a saved profile from a file."
        )
        bo_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bo_actions.set_halign(Gtk.Align.CENTER)
        bo_open_btn = Gtk.Button()
        bo_open_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bo_open_inner.append(Gtk.Image.new_from_icon_name("document-open-symbolic"))
        bo_open_inner.append(Gtk.Label(label="Open File…"))
        bo_open_btn.set_child(bo_open_inner)
        bo_open_btn.add_css_class("pill")
        bo_open_btn.connect("clicked", self._on_load)
        bo_actions.append(bo_open_btn)
        bridge_offline_page.set_child(bo_actions)
        self.add_named(bridge_offline_page, "bridge-offline")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add_named(content, "content")
        self.set_visible_child_name("placeholder")

        content.append(self._build_toolbar())

        # Tab-scoped shortcuts. MANAGED scope + Gtk.Stack unmapping the hidden
        # pages means these are only live while the Profiler tab is selected.
        # Bindings come from the KeybindingManager catalog so they stay in
        # sync with the editable shortcuts dialog.
        self._shortcut_ctrl = Gtk.ShortcutController()
        self._shortcut_ctrl.set_scope(Gtk.ShortcutScope.MANAGED)
        self._shortcut_bindings: list[tuple[str, Callable[..., bool]]] = [
            ("profiler-save", self._on_save_shortcut),
            ("profiler-load", self._on_load_shortcut),
            ("profiler-clear", self._on_clear_shortcut),
            ("profiler-filter", self._on_filter_shortcut),
            ("profiler-run", self._on_run_shortcut),
        ]
        populate_shortcut_controller(
            self._shortcut_ctrl, self._keybindings, self._shortcut_bindings
        )
        self.add_controller(self._shortcut_ctrl)

        # Sub-stack: empty placeholder vs. populated dashboard
        self._inner_stack = Gtk.Stack()
        self._inner_stack.set_vexpand(True)
        self._inner_stack.add_named(self._build_empty_state(), "empty")
        self._inner_stack.add_named(self._build_data_view(), "data")
        self._inner_stack.set_visible_child_name("empty")
        content.append(self._inner_stack)

    # ── Toolbar ───────────────────────────────────────────────────────────

    def _build_toolbar(self) -> Gtk.Box:
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.add_css_class("prof-toolbar")

        # Recording pill on the LEFT, in a Revealer so it fades in/out.
        # The spacer that follows absorbs the width change, so the
        # right-anchored action group never shifts when recording toggles.
        self._rec_revealer = Gtk.Revealer()
        self._rec_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_RIGHT)
        self._rec_revealer.set_transition_duration(180)
        self._rec_revealer.set_reveal_child(False)
        self._rec_revealer.set_valign(Gtk.Align.CENTER)

        rec_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        rec_box.add_css_class("prof-rec")
        rec_box.set_valign(Gtk.Align.CENTER)
        dot = Gtk.Box()
        dot.add_css_class("prof-rec-dot")
        dot.set_valign(Gtk.Align.CENTER)
        dot.set_halign(Gtk.Align.CENTER)
        dot.set_size_request(8, 8)
        rec_box.append(dot)
        self._rec_label = Gtk.Label(label="Recording")
        self._rec_label.set_valign(Gtk.Align.CENTER)
        rec_box.append(self._rec_label)
        self._rec_revealer.set_child(rec_box)
        toolbar.append(self._rec_revealer)

        self._file_label = Gtk.Label()
        self._file_label.add_css_class("dim-label")
        self._file_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self._file_label.set_max_width_chars(40)
        self._file_label.set_valign(Gtk.Align.CENTER)
        self._file_label.set_margin_start(2)
        toolbar.append(self._file_label)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        toolbar.append(spacer)

        # Save is round-trip (Load reads it back); exports are one-way,
        # hence the menu section separator between the two groups.
        save_section = Gio.Menu()
        save_section.append("Save Profile (JSON)…", "profiler-export.save")
        export_section = Gio.Menu()
        export_section.append("Export for speedscope…", "profiler-export.speedscope")
        export_section.append(
            "Export for Firefox Profiler / Perfetto…", "profiler-export.trace"
        )
        save_menu = Gio.Menu()
        save_menu.append_section(None, save_section)
        save_menu.append_section(None, export_section)

        self._save_btn = Adw.SplitButton(icon_name="document-save-symbolic")
        self._save_btn.add_css_class("flat")
        self._save_btn.set_tooltip_text("Save profile to JSON file (Ctrl+S)")
        self._save_btn.set_dropdown_tooltip("Save or export profile")
        self._save_btn.set_menu_model(save_menu)
        self._save_btn.set_sensitive(False)
        self._save_btn.connect("clicked", self._on_save)
        toolbar.append(self._save_btn)

        export_group = Gio.SimpleActionGroup()
        save_action = Gio.SimpleAction.new("save", None)
        save_action.connect("activate", lambda *_: self._on_save(None))
        export_group.add_action(save_action)
        for action_name in ("speedscope", "trace"):
            action = Gio.SimpleAction.new(action_name, None)
            action.connect("activate", self._on_export_action, action_name)
            export_group.add_action(action)
        self.insert_action_group("profiler-export", export_group)

        self._load_btn = Gtk.Button(icon_name="document-open-symbolic")
        self._load_btn.add_css_class("flat")
        self._load_btn.set_tooltip_text("Load profile from JSON file")
        self._load_btn.connect("clicked", self._on_load)
        toolbar.append(self._load_btn)

        self._clear_btn = Gtk.Button(icon_name="edit-clear-symbolic")
        self._clear_btn.add_css_class("flat")
        self._clear_btn.set_tooltip_text("Clear all profiling data")
        self._clear_btn.set_sensitive(False)
        self._clear_btn.connect("clicked", self._on_clear)
        toolbar.append(self._clear_btn)

        toolbar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        self._start_stop_btn = Gtk.Button()
        self._start_stop_btn.set_sensitive(False)
        self._start_stop_btn.connect("clicked", self._on_start_stop)
        self._set_start_stop_state(running=False)
        toolbar.append(self._start_stop_btn)

        return toolbar

    def _set_start_stop_state(self, running: bool) -> None:
        self._set_run_button_state(self._start_stop_btn, running)
        empty_btn = getattr(self, "_empty_start_btn", None)
        if empty_btn is not None:
            self._set_run_button_state(empty_btn, running)
        self._update_empty_state()

    @staticmethod
    def _set_run_button_state(button: Gtk.Button, running: bool) -> None:
        icon_name = "media-playback-stop-symbolic" if running else "media-playback-start-symbolic"
        label_text = "Stop" if running else "Start"
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.append(Gtk.Image.new_from_icon_name(icon_name))
        box.append(Gtk.Label(label=label_text))
        button.set_child(box)

        if running:
            button.remove_css_class("suggested-action")
            button.add_css_class("destructive-action")
            button.set_tooltip_text("Stop profiling")
        else:
            button.remove_css_class("destructive-action")
            button.add_css_class("suggested-action")
            button.set_tooltip_text("Start profiling selected extension")

    def _set_start_stop_sensitive(self, sensitive: bool) -> None:
        self._start_stop_btn.set_sensitive(sensitive)
        empty_btn = getattr(self, "_empty_start_btn", None)
        if empty_btn is not None:
            empty_btn.set_sensitive(sensitive)

    # ── Empty state (inside content) ──────────────────────────────────────

    def _build_placeholder_actions(self, start_tooltip: str) -> Gtk.Widget:
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.CENTER)

        start_btn = Gtk.Button()
        start_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        start_box.append(Gtk.Image.new_from_icon_name("media-playback-start-symbolic"))
        start_box.append(Gtk.Label(label="Start"))
        start_btn.set_child(start_box)
        start_btn.add_css_class("suggested-action")
        start_btn.add_css_class("pill")
        start_btn.set_sensitive(False)
        start_btn.set_tooltip_text(start_tooltip)
        actions.append(start_btn)

        open_btn = Gtk.Button()
        open_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        open_box.append(Gtk.Image.new_from_icon_name("document-open-symbolic"))
        open_box.append(Gtk.Label(label="Open File…"))
        open_btn.set_child(open_box)
        open_btn.add_css_class("pill")
        open_btn.connect("clicked", self._on_load)
        actions.append(open_btn)

        return actions

    def _build_empty_state(self) -> Gtk.Widget:
        page = Adw.StatusPage()
        self._empty_page = page
        page.set_icon_name("power-profile-performance-symbolic")
        page.set_title("Ready to profile")
        page.set_vexpand(True)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.CENTER)

        self._empty_start_btn = Gtk.Button()
        self._empty_start_btn.add_css_class("pill")
        self._empty_start_btn.connect("clicked", self._on_start_stop)
        self._set_run_button_state(self._empty_start_btn, self._profiling)
        self._empty_start_btn.set_sensitive(self._start_stop_btn.get_sensitive())
        actions.append(self._empty_start_btn)

        load_btn = Gtk.Button()
        load_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        load_box.append(Gtk.Image.new_from_icon_name("document-open-symbolic"))
        load_box.append(Gtk.Label(label="Load profile…"))
        load_btn.set_child(load_box)
        load_btn.add_css_class("pill")
        load_btn.connect("clicked", self._on_load)
        actions.append(load_btn)

        page.set_child(actions)
        self._update_empty_state()
        return page

    def _update_empty_state(self) -> None:
        page = getattr(self, "_empty_page", None)
        if page is None:
            return

        if self._profiling:
            page.set_title("Recording")
            if self._instrumented_functions is None:
                description = (
                    "Discovering reachable functions. Use the extension while "
                    "recording to capture calls."
                )
            else:
                description = (
                    f"{_count_label(self._instrumented_functions, 'function')} "
                    "instrumented in the latest scan; "
                    f"{len(self._stats)} observed in the recording. Use the "
                    "extension to capture calls."
                )
        else:
            page.set_title("Ready to profile")
            if self._instrumented_functions is not None and not self._raw_events:
                description = (
                    "The last scan instrumented "
                    f"{_count_label(self._instrumented_functions, 'function')}, "
                    "but no calls were observed."
                )
            else:
                description = (
                    "The bridge instruments functions reachable from the extension's "
                    "live object graph and records calls made while profiling."
                )

        warnings = []
        if self._skipped_functions:
            warnings.append(
                f"{_count_label(self._skipped_functions, 'function')} "
                "could not be instrumented"
            )
        if self._instrumentation_truncated:
            warnings.append("the traversal safety limit was reached")
        if warnings:
            description += " Note: " + "; ".join(warnings) + "."
        page.set_description(description)

    # ── Data view (cards + timeline panel + table) ────────────────────────

    def _build_data_view(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.set_vexpand(True)

        # Stat cards — fixed strip above the resizable split
        cards = self._build_stat_cards()
        cards.set_margin_start(16)
        cards.set_margin_end(16)
        cards.set_margin_top(14)
        cards.set_margin_bottom(10)
        body.append(cards)

        # Paned: top = timeline, bottom = functions table
        self._paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self._paned.set_vexpand(True)
        self._paned.set_wide_handle(True)
        self._paned.add_css_class("prof-paned")

        tl_panel = self._build_timeline_panel()
        tl_panel.set_vexpand(True)
        tl_panel.set_margin_start(16)
        tl_panel.set_margin_end(16)
        self._paned.set_start_child(tl_panel)
        self._paned.set_resize_start_child(True)
        self._paned.set_shrink_start_child(False)

        fn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        fn_box.set_vexpand(True)
        fn_box.set_margin_start(16)
        fn_box.set_margin_end(16)
        fn_box.set_margin_top(8)
        fn_box.set_margin_bottom(14)
        fn_box.append(self._build_functions_header())
        fn_box.append(self._build_stats_table())
        self._paned.set_end_child(fn_box)
        self._paned.set_resize_end_child(True)
        self._paned.set_shrink_end_child(False)

        body.append(self._paned)

        if self._paned_pos is not None:
            self._paned.set_position(self._paned_pos)
        else:
            self._paned.connect("notify::height", self._on_paned_height_notify)
        self._paned.connect("notify::position", self._on_paned_position_notify)

        return body

    # ── Stat cards ────────────────────────────────────────────────────────

    def _build_stat_cards(self) -> Gtk.Widget:
        strip = StatCardStrip(spacing=10)

        # Order: numbers first, the truncation-prone function-name card last.
        self._card_calls = StatCard("Total calls")
        self._card_wall = StatCard("Wall time")
        self._card_max = StatCard("Max call", sub_growable=True)
        self._card_hot = StatCard("Hottest function", mono_value=True, value_growable=True)

        for card in (self._card_calls, self._card_wall, self._card_max, self._card_hot):
            strip.add_card(card)

        # Initialise to "no data" placeholders.
        self._update_stat_cards()
        return strip

    def _update_stat_cards(self) -> None:
        n_calls = sum(s.count for s in self._stats.values())
        wall_ms = sum(s.self_ms for s in self._stats.values())
        self._card_calls.set_value(f"{n_calls:,}".replace(",", " "))
        self._card_calls.set_sub(
            f"across {_count_label(len(self._stats), 'observed function')}"
        )

        self._card_wall.set_value(_fmt_ms(wall_ms))
        self._card_wall.set_sub("sum of self time")

        if self._stats:
            hot = max(self._stats.values(), key=lambda s: s.total_ms)
            self._card_hot.set_value(hot.name, tooltip=hot.name)
            self._card_hot.set_sub(f"{_fmt_ms(hot.total_ms)} · {hot.count} calls")

            worst = max(self._stats.values(), key=lambda s: s.max_ms)
            self._card_max.set_value(_fmt_ms(worst.max_ms))
            self._card_max.set_sub(worst.name, tooltip=worst.name)
        else:
            self._card_hot.set_value("—")
            self._card_hot.set_sub("no data")
            self._card_max.set_value("—")
            self._card_max.set_sub("no data")

    # ── Timeline panel (3 modes) ──────────────────────────────────────────

    def _build_timeline_panel(self) -> Gtk.Widget:
        frame = Gtk.Frame()
        frame.add_css_class("prof-tl-panel")

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        frame.set_child(outer)

        # Header row
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        head.add_css_class("prof-tl-head")

        title = Gtk.Label(label="Timeline")
        title.set_xalign(0.0)
        title.add_css_class("prof-tl-title")
        head.append(title)

        self._tl_caption = Gtk.Label(xalign=0.0)
        self._tl_caption.add_css_class("prof-tl-sub")
        head.append(self._tl_caption)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        head.append(spacer)

        # Info icon — shows a tooltip describing the current graph mode.
        self._info_icon = Gtk.Image.new_from_icon_name("dialog-information-symbolic")
        self._info_icon.add_css_class("prof-info-btn")
        self._info_icon.set_tooltip_text(_MODE_HINTS[self._mode])
        head.append(self._info_icon)

        # Hide-idle toggle — icon-only, between info icon and mode tabs.
        self._show_gaps_btn = Gtk.ToggleButton()
        self._show_gaps_btn.set_icon_name("edit-select-symbolic")
        self._show_gaps_btn.add_css_class("flat")
        self._show_gaps_btn.set_active(self._hide_idle)
        self._show_gaps_btn.set_tooltip_text("Collapse idle gaps on the timeline")
        self._show_gaps_btn.connect("toggled", self._on_show_gaps_toggled)
        head.append(self._show_gaps_btn)

        # Mode tabs — three ToggleButtons grouped so exactly one is active.
        tabs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        tabs.add_css_class("prof-tabs")
        self._mode_btns: dict[str, Gtk.ToggleButton] = {}
        group_anchor: Gtk.ToggleButton | None = None
        for m in _MODES:
            btn = Gtk.ToggleButton(label=_MODE_LABELS[m])
            btn.add_css_class("prof-tab")
            btn.set_size_request(0, -1)
            if group_anchor is None:
                group_anchor = btn
            else:
                btn.set_group(group_anchor)
            if m == self._mode:
                btn.set_active(True)
            btn.connect("toggled", self._on_mode_toggled, m)
            self._mode_btns[m] = btn
            tabs.append(btn)
        tabs.set_size_request(0, -1)
        head.append(tabs)

        outer.append(head)

        # Stack of three views, each in its own scrolled window.
        self._tl_stack = Gtk.Stack()
        self._tl_stack.set_vexpand(True)
        self._tl_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._tl_stack.set_transition_duration(120)

        self._flamegraph = FlamegraphView()
        self._flamegraph.connect("function-selected", self._on_graph_selected)
        self._swimlane = SwimlaneView()
        self._swimlane.connect("function-selected", self._on_graph_selected)
        self._histogram = HistogramView()
        self._histogram.connect("function-selected", self._on_graph_selected)

        show_gaps = not self._hide_idle
        self._flamegraph.set_show_gaps(show_gaps)
        self._swimlane.set_show_gaps(show_gaps)

        for name, widget in (
            ("flamegraph", self._flamegraph),
            ("swimlane", self._swimlane),
            ("histogram", self._histogram),
        ):
            sw = Gtk.ScrolledWindow()
            sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            sw.set_vexpand(True)
            sw.set_child(widget)
            self._tl_stack.add_named(sw, name)
        self._tl_stack.set_visible_child_name(self._mode)

        outer.append(self._tl_stack)
        return frame

    def _on_mode_toggled(self, btn: Gtk.ToggleButton, mode: str) -> None:
        if not btn.get_active():
            return
        if mode == self._mode:
            return
        self._mode = mode
        self._tl_stack.set_visible_child_name(mode)
        self._info_icon.set_tooltip_text(_MODE_HINTS[mode])
        self._settings.set("mode", mode)
        self._update_active_graph()

    def _on_show_gaps_toggled(self, btn: Gtk.ToggleButton) -> None:
        show = not btn.get_active()  # active=True means gaps are hidden
        self._flamegraph.set_show_gaps(show)
        self._swimlane.set_show_gaps(show)
        self._settings.set("hide_idle", btn.get_active())

    # ── Paned position persistence ────────────────────────────────────────

    def _on_paned_height_notify(self, paned: Gtk.Paned, _param: GObject.ParamSpec) -> None:
        if self._paned_default_set:
            return
        h = paned.get_height()
        if h <= 0:
            return
        self._paned_default_set = True
        paned.set_position(h // 2)

    def _on_paned_position_notify(self, _paned: Gtk.Paned, _param: GObject.ParamSpec) -> None:
        if self._paned_save_id:
            GLib.source_remove(self._paned_save_id)
        self._paned_save_id = GLib.timeout_add(400, self._do_save_paned_pos)

    def _do_save_paned_pos(self) -> bool:
        self._settings.set("paned_pos", self._paned.get_position())
        self._paned_save_id = 0
        return bool(GLib.SOURCE_REMOVE)

    # ── Functions section header (filter search) ─────────────────────────

    def _build_functions_header(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        title = Gtk.Label(label="Observed Functions")
        title.set_xalign(0.0)
        title.add_css_class("prof-section-title")
        box.append(title)

        self._fn_caption = Gtk.Label(xalign=0.0)
        self._fn_caption.add_css_class("prof-section-sub")
        box.append(self._fn_caption)

        self._fn_info = Gtk.Image.new_from_icon_name("dialog-information-symbolic")
        self._fn_info.add_css_class("prof-info-btn")
        self._fn_info.set_tooltip_text(_FN_HINT)
        box.append(self._fn_info)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        box.append(spacer)

        self._filter_entry = Gtk.SearchEntry()
        self._filter_entry.set_placeholder_text("Filter functions")
        self._filter_entry.set_width_chars(20)
        self._filter_entry.connect("search-changed", self._on_filter_changed)
        box.append(self._filter_entry)
        return box

    # ── Stats table ───────────────────────────────────────────────────────

    def _build_stats_table(self) -> Gtk.Widget:
        sort_model = Gtk.SortListModel(model=self._store)
        selection = Gtk.SingleSelection(model=sort_model)
        selection.set_autoselect(False)
        selection.set_can_unselect(True)
        selection.set_selected(Gtk.INVALID_LIST_POSITION)
        # Feedback-loop guard for graph→table syncs.
        self._table_sync = False
        selection.connect("selection-changed", self._on_table_selection_changed)

        col_view = Gtk.ColumnView(model=selection)
        col_view.set_show_column_separators(False)
        col_view.set_show_row_separators(True)
        col_view.set_vexpand(True)
        col_view.add_css_class("prof-table")
        sort_model.set_sorter(col_view.get_sorter())

        col_view.append_column(self._make_text_col("Function", "name", str, expand=True, mono=True))
        col_view.append_column(self._make_distribution_col())
        col_view.append_column(self._make_text_col("Calls", "count", str))
        col_view.append_column(self._make_text_col("Total", "total_ms", _fmt_ms))
        col_view.append_column(self._make_text_col("Self", "self_ms", _fmt_ms))
        col_view.append_column(self._make_text_col("Avg", "avg_ms", _fmt_ms))
        col_view.append_column(self._make_text_col("Max", "max_ms", _fmt_ms))

        # Default sort: Total desc.
        col_view.sort_by_column(col_view.get_columns().get_item(3), Gtk.SortType.DESCENDING)

        self._selection = selection
        self._col_view = col_view
        self._sort_model = sort_model

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_size_request(0, -1)
        scroll.set_child(col_view)
        return scroll

    # Column helpers ──────────────────────────────────────────────────────

    def _make_text_col(
        self,
        title: str,
        attr: str,
        fmt: Callable[..., str] = str,
        *,
        expand: bool = False,
        mono: bool = False,
        default_sort_desc: bool = False,
    ) -> Gtk.ColumnViewColumn:
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._text_setup, attr, mono)
        factory.connect("bind", self._text_bind, attr, fmt)

        sorter = Gtk.CustomSorter.new(self._sorter_func, attr)
        col = Gtk.ColumnViewColumn(title=title, factory=factory, sorter=sorter)
        col.set_expand(expand)
        return col

    def _text_setup(
        self,
        _factory: Gtk.SignalListItemFactory,
        item: Gtk.ListItem,
        attr: str,
        mono: bool,
    ) -> None:
        label = Gtk.Label()
        label.set_xalign(0.0 if attr == "name" else 1.0)
        label.set_margin_start(6)
        label.set_margin_end(6)
        if mono:
            label.add_css_class("prof-table-fn")
            label.set_ellipsize(Pango.EllipsizeMode.END)
        else:
            label.add_css_class("prof-table-num")
        self._add_deselect_gesture(label, item)
        item.set_child(label)

    @staticmethod
    def _text_bind(
        _factory: Gtk.SignalListItemFactory,
        item: Gtk.ListItem,
        attr: str,
        fmt: Callable[..., str],
    ) -> None:
        stat: FunctionStat = item.get_item()
        label: Gtk.Label = item.get_child()
        label.set_text(fmt(getattr(stat, attr)))

    @staticmethod
    def _sorter_func(a: FunctionStat, b: FunctionStat, attr: str) -> int:
        va = getattr(a, attr)
        vb = getattr(b, attr)
        if va < vb:
            return -1
        if va > vb:
            return 1
        return 0

    # Distribution column — two overlapping bars in a Cairo cell ──────────

    def _make_distribution_col(self) -> Gtk.ColumnViewColumn:
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._dist_setup)
        factory.connect("bind", self._dist_bind)
        col = Gtk.ColumnViewColumn(title="Distribution", factory=factory)
        col.set_fixed_width(180)
        col.set_resizable(True)
        return col

    def _dist_setup(self, _factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
        area = Gtk.DrawingArea()
        area.set_content_height(12)
        area.set_hexpand(True)
        area.set_valign(Gtk.Align.CENTER)
        area.set_margin_start(8)
        area.set_margin_end(8)
        # Defaults; bind() overwrites.
        area._pct_total = 0.0  # type: ignore[attr-defined]
        area._pct_self = 0.0  # type: ignore[attr-defined]
        area.set_draw_func(self._dist_draw)
        self._add_deselect_gesture(area, item)
        item.set_child(area)

    def _dist_bind(self, _factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
        stat: FunctionStat = item.get_item()
        area: Gtk.DrawingArea = item.get_child()
        max_total = max(self._max_total_ms, 1e-9)
        area._pct_total = min(stat.total_ms / max_total, 1.0)  # type: ignore[attr-defined]
        area._pct_self = min(stat.self_ms / max_total, 1.0)  # type: ignore[attr-defined]
        area.queue_draw()

    @staticmethod
    def _dist_draw(area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        pct_total = getattr(area, "_pct_total", 0.0)
        pct_self = getattr(area, "_pct_self", 0.0)

        dark = Adw.StyleManager.get_default().get_dark()
        track = (0.55, 0.55, 0.60, 0.18) if dark else (0.10, 0.10, 0.12, 0.10)
        is_hot = pct_total > 0.7
        is_warm = pct_total > 0.4
        if is_hot:
            base = desaturate_color(0.90, 0.18, 0.20)
        elif is_warm:
            base = desaturate_color(0.90, 0.65, 0.04)
        else:
            base = desaturate_color(0.21, 0.52, 0.89)

        bar_h = 6
        y = (height - bar_h) / 2
        # Track
        cr.set_source_rgba(*track)
        cr.rectangle(0, y, width, bar_h)
        cr.fill()
        # Total fill (lighter)
        cr.set_source_rgba(*base, 0.35)
        cr.rectangle(0, y, width * pct_total, bar_h)
        cr.fill()
        # Self overlay (full saturation)
        cr.set_source_rgba(*base, 1.0)
        cr.rectangle(0, y, width * pct_self, bar_h)
        cr.fill()

    # Table selection handler ─────────────────────────────────────────────

    def _add_deselect_gesture(self, widget: Gtk.Widget, item: Gtk.ListItem) -> None:
        click = Gtk.GestureClick()
        click.set_button(1)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._on_cell_deselect_press, item)
        widget.add_controller(click)

    def _on_cell_deselect_press(
        self,
        gesture: Gtk.GestureClick,
        _n: int,
        _x: float,
        _y: float,
        item: Gtk.ListItem,
    ) -> None:
        if item.get_selected():
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._apply_selected_fn(None, sync_table=True)

    def _on_table_selection_changed(
        self,
        sel: Gtk.SingleSelection,
        _position: int,
        _n_items: int,
    ) -> None:
        if self._table_sync:
            return
        if sel.get_selected() == Gtk.INVALID_LIST_POSITION:
            self._apply_selected_fn(None, sync_table=False)
            return
        item = sel.get_selected_item()
        if item is None:
            return
        self._apply_selected_fn(item.name, sync_table=False)

    # ── Public API ────────────────────────────────────────────────────────

    def set_target_extension(self, uuid: str | None) -> None:
        """Set the extension to profile. Stops ongoing profiling if UUID changes."""
        if self._profiling and uuid != self._target_uuid:
            self._socket.send({"type": "stop_profiling"})
            self._set_stopped()
        self._target_uuid = uuid
        self._update_visible_child()

    def _update_visible_child(self) -> None:
        uuid = self._target_uuid
        if uuid is None:
            self.set_visible_child_name("placeholder")
            self._set_start_stop_sensitive(False)
            return
        if self._dbus.get_extension_state(uuid) != ExtensionState.ENABLED:
            if self._profiling:
                self._socket.send({"type": "stop_profiling"})
                self._set_stopped()
            self.set_visible_child_name("disabled")
            self._set_start_stop_sensitive(False)
            return
        bridge_online = self._socket.is_client_connected
        if not bridge_online and not self._raw_events:
            self.set_visible_child_name("bridge-offline")
            self._set_start_stop_sensitive(False)
            return
        self.set_visible_child_name("content")
        self._set_start_stop_sensitive(bridge_online)

    def _on_extensions_changed(
        self, _dbus: DBusClient, _extensions: dict[str, Any]
    ) -> None:
        if self._target_uuid is not None:
            self._update_visible_child()

    # ── Button handlers ───────────────────────────────────────────────────

    def _on_start_stop(self, _btn: Gtk.Button) -> None:
        if self._profiling:
            self._stop_profiling()
        else:
            self._start_profiling()

    def _start_profiling(self) -> bool:
        """Begin profiling the current target. Returns True if it started."""
        uuid = self._target_uuid
        _log.debug("Start — uuid=%r connected=%s", uuid, self._socket.is_client_connected)
        if not uuid:
            _log.warning("Start requested but no target extension set")
            return False

        self._start_generation += 1
        self._active_start_generation = self._start_generation
        self._accepted_event_generation = self._start_generation
        self._pending_start_acks.append((self._start_generation, uuid))
        self._reset_instrumentation()
        self._socket.send(
            {
                "type": "start_profiling",
                "uuid": uuid,
                "sessionId": self._start_generation,
            }
        )
        self._profiling = True
        self._rec_start_ts = GLib.get_monotonic_time() / 1e6
        self._start_rec_timer()
        self._set_start_stop_state(running=True)
        self._update_recording_pill()
        return True

    def _stop_profiling(self) -> None:
        self._socket.send({"type": "stop_profiling"})
        self._set_stopped()

    # ── Global (bridge) shortcut handlers ─────────────────────────────────

    def _handle_global_toggle(self) -> None:
        """Start/stop from the global Super+F5 shortcut (app may be unfocused)."""
        self.emit("request-attention")
        if self._profiling:
            self._stop_profiling()
            self.emit("show-toast", "Profiling stopped (keyboard shortcut)")
        elif self._can_start_profiling():
            self._start_profiling()
            self.emit("show-toast", "Profiling started (keyboard shortcut)")
        else:
            self.emit("show-toast", "Select an enabled extension to start profiling")

    def _handle_global_restart(self) -> None:
        """Clear all data and start a fresh session (global Super+Shift+F5).

        Restart is a single "start from zero" operation: wipe the recorded
        session, then re-arm profiling on the current target. We deliberately
        do not send a separate stop_profiling first — the bridge's
        ``startProfiling()`` unpatches and re-patches when already running, so a
        lone start_profiling restarts it cleanly in one round-trip. An extra
        stop would only emit a late profiling_stopped ack that races the new
        session (see :meth:`_on_profiling_stopped`).

        Data is cleared only when profiling can actually be restarted — never
        wipe a recorded session if there is nothing to restart into (no target,
        disabled extension, or bridge offline)."""
        self.emit("request-attention")
        if not self._can_start_profiling():
            if self._profiling:
                self._stop_profiling()
            self.emit("show-toast", "Select an enabled extension to start profiling")
            return
        self._clear_data()
        self._file_label.set_text("")
        self._file_label.set_tooltip_text("")
        self._flush_refresh()
        self._update_visible_child()
        self._start_profiling()
        self.emit("show-toast", "Profiling restarted (keyboard shortcut)")

    def _can_start_profiling(self) -> bool:
        uuid = self._target_uuid
        return (
            uuid is not None
            and self._dbus.get_extension_state(uuid) == ExtensionState.ENABLED
            and self._socket.is_client_connected
        )

    def _set_stopped(self) -> None:
        self._profiling = False
        self._active_start_generation = 0
        self._stop_rec_timer()
        self._rec_start_ts = None
        self._set_start_stop_state(running=False)
        self._update_recording_pill()
        enabled = (
            self._target_uuid is not None
            and self._dbus.get_extension_state(self._target_uuid) == ExtensionState.ENABLED
            and self._socket.is_client_connected
        )
        self._set_start_stop_sensitive(enabled)

    def _start_rec_timer(self) -> None:
        if self._rec_timer_id:
            return
        self._rec_timer_id = GLib.timeout_add_seconds(1, self._rec_timer_tick)

    def _stop_rec_timer(self) -> None:
        if self._rec_timer_id:
            GLib.source_remove(self._rec_timer_id)
            self._rec_timer_id = 0

    def _rec_timer_tick(self) -> bool:
        if not self._profiling:
            self._rec_timer_id = 0
            return False  # GLib.SOURCE_REMOVE
        self._update_recording_pill()
        return True  # GLib.SOURCE_CONTINUE

    @staticmethod
    def _fmt_elapsed(seconds: int) -> str:
        if seconds >= 3600:
            h, rem = divmod(seconds, 3600)
            m, s = divmod(rem, 60)
            return f"{h}:{m:02d}:{s:02d}"
        m, s = divmod(seconds, 60)
        return f"{m}:{s:02d}"

    def _update_recording_pill(self) -> None:
        if self._profiling:
            if self._rec_start_ts is not None:
                elapsed = int((GLib.get_monotonic_time() / 1e6) - self._rec_start_ts)
            else:
                elapsed = 0
            n = len(self._raw_events)
            word = "event" if n == 1 else "events"
            self._rec_label.set_text(
                f" Recording · {self._fmt_elapsed(elapsed)} · {n} {word}"
            )
            self._rec_revealer.set_reveal_child(True)
        else:
            self._rec_revealer.set_reveal_child(False)

    def _on_save_shortcut(self, _widget: Gtk.Widget, _args: object) -> bool:
        if self._save_btn.get_sensitive():
            self._on_save(None)
        return True

    def _on_load_shortcut(self, _widget: Gtk.Widget, _args: object) -> bool:
        if self._load_btn.get_sensitive():
            self._on_load(None)
        return True

    def _on_clear_shortcut(self, _widget: Gtk.Widget, _args: object) -> bool:
        if self._clear_btn.get_sensitive():
            self._on_clear(None)
        return True

    def _on_filter_shortcut(self, _widget: Gtk.Widget, _args: object) -> bool:
        if self.get_visible_child_name() == "content":
            self._filter_entry.grab_focus()
        return True

    def _on_run_shortcut(self, _widget: Gtk.Widget, _args: object) -> bool:
        if self._start_stop_btn.get_sensitive():
            self._on_start_stop(None)
        return True

    def _default_profile_basename(self) -> str:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        if self._target_uuid:
            short = self._target_uuid.split("@")[0]
            return f"gse-profile_{short}_{ts}"
        return f"gse-profile_{ts}"

    def _on_save(self, _btn: Gtk.Widget | None) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Save Profile")
        dialog.set_initial_name(f"{self._default_profile_basename()}.json")
        dialog.save(self.get_root(), None, self._on_save_response)

    def _on_save_response(
        self, dialog: Gtk.FileDialog, result: Gio.AsyncResult
    ) -> None:
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return
        payload = {
            "events": list(self._raw_events),
            "stats": {
                name: {
                    "count": s.count,
                    "total_ms": s.total_ms,
                    "self_ms": s.self_ms,
                    "max_ms": s.max_ms,
                }
                for name, s in self._stats.items()
            },
        }
        try:
            gfile.replace_contents(
                json.dumps(payload, indent=2).encode(),
                None,
                False,
                Gio.FileCreateFlags.REPLACE_DESTINATION,
                None,
            )
        except GLib.Error as exc:
            _log.error("Failed to save profile: %s", exc)

    def _on_export_action(
        self, _action: Gio.SimpleAction, _param: object, fmt: str
    ) -> None:
        suffix = ".speedscope.json" if fmt == "speedscope" else ".trace.json"
        dialog = Gtk.FileDialog()
        dialog.set_title("Export Profile")
        dialog.set_initial_name(f"{self._default_profile_basename()}{suffix}")
        dialog.save(
            self.get_root(),
            None,
            lambda d, result: self._on_export_response(d, result, fmt),
        )

    def _on_export_response(
        self, dialog: Gtk.FileDialog, result: Gio.AsyncResult, fmt: str
    ) -> None:
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return
        events = list(self._raw_events)
        if fmt == "speedscope":
            payload = to_speedscope(
                events, name=self._target_uuid or "gse-profiler profile"
            )
        else:
            payload = to_trace_event(events)
        try:
            gfile.replace_contents(
                json.dumps(payload, indent=2).encode(),
                None,
                False,
                Gio.FileCreateFlags.REPLACE_DESTINATION,
                None,
            )
        except GLib.Error as exc:
            _log.error("Failed to export profile: %s", exc)

    def _on_load(self, _btn: Gtk.Button) -> None:
        filt = Gtk.FileFilter()
        filt.set_name("JSON files")
        filt.add_pattern("*.json")
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        filters.append(filt)

        dialog = Gtk.FileDialog()
        dialog.set_title("Load Profile")
        dialog.set_filters(filters)
        dialog.open(self.get_root(), None, self._on_load_response)

    def _on_load_response(
        self, dialog: Gtk.FileDialog, result: Gio.AsyncResult
    ) -> None:
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        try:
            _ok, contents, _etag = gfile.load_contents(None)
            data = json.loads(contents.decode())
        except (GLib.Error, json.JSONDecodeError, UnicodeDecodeError) as exc:
            _log.error("Failed to load profile: %s", exc)
            return

        self._clear_data()
        self._reset_instrumentation()
        for event in data.get("events", []):
            self._ingest_event(event, schedule_refresh=False)
        self._file_label.set_text(gfile.get_basename() or "")
        self._file_label.set_tooltip_text(gfile.get_path() or "")
        self.set_visible_child_name("content")
        uuid = self._target_uuid
        self._set_start_stop_sensitive(
            uuid is not None
            and self._dbus.get_extension_state(uuid) == ExtensionState.ENABLED
            and self._socket.is_client_connected
        )
        self._flush_refresh()

    def _on_clear(self, _btn: Gtk.Button) -> None:
        if not self._profiling:
            self._accepted_event_generation = 0
        self._clear_data()
        if not self._profiling:
            self._reset_instrumentation()
        self._file_label.set_text("")
        self._file_label.set_tooltip_text("")
        self._flush_refresh()
        self._update_visible_child()

    # ── Filter / selection wiring ─────────────────────────────────────────

    def _on_filter_changed(self, entry: Gtk.SearchEntry) -> None:
        self._filter_text = entry.get_text().strip().lower()
        self._flamegraph.set_filter_text(self._filter_text)
        self._swimlane.set_filter_text(self._filter_text)
        self._histogram.set_filter_text(self._filter_text)
        # Re-splice the store with current filter.
        self._refresh_table_only()

    def _on_graph_selected(self, _graph: Gtk.Widget, fn: str) -> None:
        new = fn if fn else None
        self._apply_selected_fn(new, sync_table=True)

    def _apply_selected_fn(self, fn: str | None, *, sync_table: bool) -> None:
        self._selected_fn = fn
        self._flamegraph.set_selected_fn(fn)
        self._swimlane.set_selected_fn(fn)
        self._histogram.set_selected_fn(fn)
        if sync_table:
            self._sync_table_selection(fn)

    def _sync_table_selection(self, fn: str | None) -> None:
        pos = Gtk.INVALID_LIST_POSITION
        if fn is not None:
            n = self._sort_model.get_n_items()
            for i in range(n):
                item: FunctionStat = self._sort_model.get_item(i)
                if item is not None and item.name == fn:
                    pos = i
                    break
        self._table_sync = True
        try:
            self._selection.set_selected(pos)
        finally:
            self._table_sync = False

    # ── Socket handlers ───────────────────────────────────────────────────

    def _accepts_profile_message(self, msg: dict[str, Any]) -> bool:
        """Reject events from a superseded recording generation."""
        if "sessionId" not in msg:
            return True  # legacy bridge payload
        session_id = self._nonnegative_count(msg.get("sessionId"))
        return (
            session_id is not None
            and session_id == self._accepted_event_generation
        )

    def _on_profile_event(self, msg: dict[str, Any]) -> None:
        if not self._accepts_profile_message(msg):
            _log.debug("Ignoring profile_event from a stale session")
            return
        _log.debug(
            "profile_event: fn=%s dur=%.3fms",
            msg.get("function"),
            (msg.get("end", 0) - msg.get("start", 0)) * 1000,
        )
        self._ingest_event(msg)

    def _on_profile_batch(self, msg: dict[str, Any]) -> None:
        if not self._accepts_profile_message(msg):
            _log.debug("Ignoring profile_batch from a stale session")
            return
        events = msg.get("events")
        if not isinstance(events, list):
            _log.warning("Ignoring malformed profile_batch without an events array")
            return

        ingested = 0
        for event in events:
            if not isinstance(event, dict):
                _log.warning("Ignoring non-object event in profile_batch")
                continue
            if not self._accepts_profile_message(event):
                _log.debug("Ignoring event from a stale session within profile_batch")
                continue
            self._ingest_event(event, schedule_refresh=False)
            ingested += 1
        if ingested:
            _log.debug("profile_batch: ingested %d events", ingested)
            self._schedule_refresh()

    def _on_profiling_started(self, msg: dict[str, Any]) -> None:
        _log.info("profiling_started: uuid=%s ok=%s", msg.get("uuid"), msg.get("ok"))

        if not self._pending_start_acks:
            _log.debug("Ignoring profiling_started with no pending start request")
            return

        if "sessionId" in msg:
            ack_session_id = self._nonnegative_count(msg.get("sessionId"))
            if ack_session_id is None:
                _log.warning("Ignoring profiling_started with an invalid sessionId")
                return
            pending = next(
                (
                    item
                    for item in self._pending_start_acks
                    if item[0] == ack_session_id
                ),
                None,
            )
            if pending is None:
                _log.debug(
                    "Ignoring profiling_started for unknown session %d",
                    ack_session_id,
                )
                return
            self._pending_start_acks.remove(pending)
            generation, requested_uuid = pending
        else:
            # Compatibility with bridge versions that predate session IDs;
            # their socket replies are ordered.
            generation, requested_uuid = self._pending_start_acks.popleft()
        ack_uuid = msg.get("uuid")
        if isinstance(ack_uuid, str) and ack_uuid != requested_uuid:
            _log.warning(
                "Ignoring out-of-order profiling_started for %s (expected %s)",
                ack_uuid,
                requested_uuid,
            )
            return

        is_current = (
            self._profiling
            and generation == self._active_start_generation
            and requested_uuid == self._target_uuid
        )
        if not is_current:
            _log.debug(
                "Ignoring stale profiling_started for generation %d (active=%d)",
                generation,
                self._active_start_generation,
            )
            return

        if not msg.get("ok"):
            _log.warning(
                "Bridge could not find stateObj for %s — no functions patched",
                requested_uuid,
            )
            self._set_stopped()
            return

        self._apply_instrumentation_stats(msg)

    def _on_profiling_stopped(self, _msg: dict[str, Any]) -> None:
        # Direct ack to a stop_profiling we sent (target change, extension
        # disabled, socket teardown, or an explicit stop). The UI already
        # transitioned to stopped locally at send time, so this ack is
        # purely confirmatory. If the user has since started a new session,
        # _profiling is True again and this late ack is stale — acting on it
        # would tear down the running session's recording pill while the
        # bridge keeps profiling. The bridge only ever emits this in
        # response to our own stop, so _profiling=True here always means a
        # newer start superseded it.
        _log.debug("profiling_stopped received (profiling=%s)", self._profiling)
        if not self._profiling:
            self._set_stopped()

    def _on_toggle_profiling(self, _msg: dict[str, Any]) -> None:
        _log.debug("toggle_profiling received (global shortcut)")
        self._handle_global_toggle()

    def _on_restart_profiling(self, _msg: dict[str, Any]) -> None:
        _log.debug("restart_profiling received (global shortcut)")
        self._handle_global_restart()

    def _on_client_connected(self, _server: SocketServer) -> None:
        self._update_visible_child()

    def _on_client_disconnected(self, _server: SocketServer) -> None:
        self._pending_start_acks.clear()
        self._accepted_event_generation = 0
        if self._profiling:
            self._set_stopped()
        self._update_visible_child()

    @staticmethod
    def _nonnegative_count(value: object) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def _reset_instrumentation(self) -> None:
        self._instrumented_functions = None
        self._visited_objects = None
        self._skipped_functions = None
        self._instrumentation_truncated = False
        self._update_instrumentation_ui()

    def _apply_instrumentation_stats(self, msg: dict[str, Any]) -> None:
        # All fields are optional for compatibility with older bridge versions.
        self._instrumented_functions = self._nonnegative_count(
            msg.get("patchedFunctions")
        )
        self._visited_objects = self._nonnegative_count(msg.get("visitedObjects"))
        self._skipped_functions = self._nonnegative_count(
            msg.get("skippedFunctions")
        )
        self._instrumentation_truncated = msg.get("truncated") is True
        self._update_instrumentation_ui()

    def _update_instrumentation_ui(self) -> None:
        self._update_empty_state()
        if not hasattr(self, "_fn_caption"):
            return
        self._refresh_table_only()

        details = []
        if self._instrumented_functions is not None:
            details.append(
                f"{_count_label(self._instrumented_functions, 'function')} instrumented"
            )
        if self._visited_objects is not None:
            details.append(
                f"{_count_label(self._visited_objects, 'object')} visited"
            )
        if self._skipped_functions is not None:
            details.append(
                f"{_count_label(self._skipped_functions, 'function')} skipped"
            )
        if self._instrumentation_truncated:
            details.append("traversal stopped at a safety limit")
        suffix = "\n\nLatest scan: " + " · ".join(details) if details else ""
        self._fn_info.set_tooltip_text(_FN_HINT + suffix)

    # ── Data management ──────────────────────────────────────────────────

    def _ingest_event(
        self, event: dict[str, Any], *, schedule_refresh: bool = True
    ) -> None:
        name = event.get("function", "?")
        duration_ms = (event.get("end", 0.0) - event.get("start", 0.0)) * 1000.0

        if name not in self._stats:
            self._stats[name] = FunctionStat(name)
        self._stats[name].record(duration_ms)
        self._raw_events.append(event)

        if schedule_refresh:
            self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        if self._refresh_pending:
            return
        self._refresh_pending = True
        GLib.timeout_add(80, self._flush_refresh_cb)

    def _flush_refresh_cb(self) -> bool:
        self._flush_refresh()
        return bool(GLib.SOURCE_REMOVE)

    def _flush_refresh(self) -> None:
        self._refresh_pending = False
        self._recompute_self_times()

        # Update toggle visibility of empty state vs. data view.
        if self._raw_events:
            self._inner_stack.set_visible_child_name("data")
            self._clear_btn.set_sensitive(True)
            self._save_btn.set_sensitive(True)
        else:
            self._inner_stack.set_visible_child_name("empty")
            self._clear_btn.set_sensitive(False)
            self._save_btn.set_sensitive(False)

        # Cache the max total before splicing so the Distribution cells can read it.
        self._max_total_ms = max((s.total_ms for s in self._stats.values()), default=1.0)

        self._refresh_table_only()
        self._update_stat_cards()
        self._update_active_graph()
        self._update_timeline_caption()
        self._update_recording_pill()
        self._update_empty_state()

    def _refresh_table_only(self) -> None:
        ft = self._filter_text
        items = []
        for s in self._stats.values():
            if ft and ft not in s.name.lower():
                continue
            items.append(self._stat_snapshot(s))
        self._store.splice(0, self._store.get_n_items(), items)
        summary = [f"{len(self._stats)} observed total"]
        if self._instrumented_functions is not None:
            summary.append(f"{self._instrumented_functions} instrumented last scan")
        if self._skipped_functions:
            summary.append(f"{self._skipped_functions} skipped")
        if self._instrumentation_truncated:
            summary.append("limited")
        if ft:
            summary.insert(0, f"{len(items)} shown")
        self._fn_caption.set_text(" · ".join(summary))
        # Splice resets selection — restore from our authoritative state.
        self._sync_table_selection(self._selected_fn)

    def _update_active_graph(self) -> None:
        # Push events to all three so a quick tab-switch is instant.
        events = list(self._raw_events)
        self._flamegraph.set_events(events)
        self._swimlane.set_events(events)
        self._histogram.set_stats(list(self._stats.values()))

    def _update_timeline_caption(self) -> None:
        if not self._raw_events:
            self._tl_caption.set_text("")
            return
        t0 = min(e["start"] for e in self._raw_events)
        t1 = max(e["end"] for e in self._raw_events)
        span_ms = (t1 - t0) * 1000.0
        self._tl_caption.set_text(f"{len(self._raw_events)} events · {_fmt_ms(span_ms)} span")

    @staticmethod
    def _stat_snapshot(stat: FunctionStat) -> FunctionStat:
        snap = FunctionStat(stat.name)
        snap.count = stat.count
        snap.total_ms = stat.total_ms
        snap.self_ms = stat.self_ms
        snap.max_ms = stat.max_ms
        return snap

    def _recompute_self_times(self) -> None:
        """Aggregate per-function self-time = total minus direct children.

        Stack-based pass over events sorted by (start ASC, end DESC) so
        parents are visited before their children. Each event's full
        duration is added to its own self bucket, then its parent's bucket
        is decremented by that duration — leaving each event with exclusive
        (non-callee) wall-clock time. Results sum per function name into
        ``FunctionStat.self_ms``.
        """
        for s in self._stats.values():
            s.self_ms = 0.0
        if not self._raw_events:
            return
        ordered = sorted(self._raw_events, key=lambda e: (e["start"], -e["end"]))
        stack: list[dict[str, Any]] = []
        event_self: dict[int, float] = {}
        for e in ordered:
            while stack and stack[-1]["end"] <= e["start"]:
                stack.pop()
            dur_ms = (e["end"] - e["start"]) * 1000.0
            event_self[id(e)] = dur_ms
            if stack:
                event_self[id(stack[-1])] -= dur_ms
            stack.append(e)
        for e in ordered:
            stat = self._stats.get(e["function"])
            if stat is not None:
                stat.self_ms += event_self[id(e)]

    def _clear_data(self) -> None:
        self._stats.clear()
        self._raw_events.clear()
        self._apply_selected_fn(None, sync_table=True)
