"""Keyboard-shortcuts editor dialog.

A theme-consistent Adw.Dialog listing every shortcut grouped by scope, driven
by :data:`app.core.keybindings.CATALOG` so it can never drift out of sync
with the actual controllers in main.py and the tab views — those are built
from the same catalog via ``populate_shortcut_controller``.

Each row lets the user capture a new key combination directly (click, then
press the new shortcut) and reset it back to its default. Global rows (the
bridge-owned Super+F5 shortcuts) are disabled while the bridge is not
connected, since an edit would have nowhere to go.
"""

from __future__ import annotations

from dataclasses import dataclass

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gtk

from app.core.keybindings import CATALOG, SPEC_BY_ID, ActionSpec, KeybindingManager

# Display order/metadata for each scope. (title, description | None).
_SCOPE_ORDER: tuple[str, ...] = ("global", "general", "profiler", "logs", "inspector")
_SCOPE_META: dict[str, tuple[str, str | None]] = {
    "global": ("Global", "Work even when the GSE Profiler window is not focused."),
    "general": ("General", None),
    "profiler": ("Profiler tab", None),
    "logs": ("Logs tab", None),
    "inspector": ("Inspector tab", None),
}

# Bare modifier keypresses never form a complete shortcut on their own —
# ignore them while capturing instead of treating "just pressed Ctrl" as
# a (invalid) accelerator.
_MODIFIER_KEYVALS: frozenset[int] = frozenset((
    Gdk.KEY_Shift_L, Gdk.KEY_Shift_R,
    Gdk.KEY_Control_L, Gdk.KEY_Control_R,
    Gdk.KEY_Alt_L, Gdk.KEY_Alt_R,
    Gdk.KEY_Super_L, Gdk.KEY_Super_R,
    Gdk.KEY_Meta_L, Gdk.KEY_Meta_R,
    Gdk.KEY_Caps_Lock, Gdk.KEY_Shift_Lock,
    Gdk.KEY_Num_Lock, Gdk.KEY_Scroll_Lock,
    Gdk.KEY_ISO_Level3_Shift,
))


def _human_label(accel: str) -> str:
    """Translate a GTK accelerator string (``<Control>s``) into a
    display label (``Ctrl+S``) suitable for :func:`_keycaps`."""
    ok, keyval, mods = Gtk.accelerator_parse(accel)
    if not ok or keyval == 0:
        return accel
    return str(Gtk.accelerator_get_label(keyval, mods))


def _keycaps(label: str) -> Gtk.Widget:
    """Render a label like ``Ctrl+Shift+F5`` as a row of keycaps."""
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
    box.set_valign(Gtk.Align.CENTER)
    keys = label.split("+")
    for i, key in enumerate(keys):
        if i > 0:
            plus = Gtk.Label(label="+")
            plus.add_css_class("dim-label")
            box.append(plus)
        cap = Gtk.Label(label=key)
        cap.add_css_class("keycap")
        box.append(cap)
    return box


@dataclass
class _Row:
    capture_btn: Gtk.Button
    reset_btn: Gtk.Button


class _ShortcutsEditor:
    """Owns the dialog's widgets and capture-mode state machine."""

    def __init__(self, manager: KeybindingManager) -> None:
        self._manager = manager
        self._rows: dict[str, _Row] = {}
        self._capturing_id: str | None = None

        self.dialog = Adw.Dialog()
        self.dialog.set_title("Keyboard Shortcuts")
        self.dialog.set_content_width(520)
        self.dialog.set_content_height(640)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(12)
        content.set_margin_bottom(24)
        content.set_margin_start(18)
        content.set_margin_end(18)

        for scope in _SCOPE_ORDER:
            specs = [s for s in CATALOG if s.scope == scope]
            if not specs:
                continue
            title, description = _SCOPE_META[scope]
            group = Adw.PreferencesGroup()
            group.set_title(title)
            if description:
                group.set_description(description)
            for spec in specs:
                group.add(self._build_row(spec))
            content.append(group)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(content)

        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(scroller)

        header = Adw.HeaderBar()
        reset_all_btn = Gtk.Button(label="Reset All")
        reset_all_btn.set_tooltip_text("Reset every shortcut to its default")
        reset_all_btn.connect("clicked", self._on_reset_all_clicked)
        header.pack_end(reset_all_btn)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self._toast_overlay)
        self.dialog.set_child(toolbar)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.dialog.add_controller(key_ctrl)

        self._changed_id = manager.connect_changed(self._on_manager_changed)
        self.dialog.connect("closed", self._on_dialog_closed)

        for action_id in self._rows:
            self._refresh_row(action_id)

    # ── Row construction ─────────────────────────────────────────────────

    def _build_row(self, spec: ActionSpec) -> Adw.ActionRow:
        row = Adw.ActionRow()
        row.set_title(spec.title)

        capture_btn = Gtk.Button()
        capture_btn.add_css_class("flat")
        capture_btn.set_valign(Gtk.Align.CENTER)
        capture_btn.connect("clicked", self._on_capture_clicked, spec.id)

        reset_btn = Gtk.Button()
        reset_btn.set_icon_name("edit-undo-symbolic")
        reset_btn.add_css_class("flat")
        reset_btn.set_valign(Gtk.Align.CENTER)
        reset_btn.set_tooltip_text("Reset to default")
        reset_btn.connect("clicked", self._on_reset_clicked, spec.id)

        suffix = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        suffix.set_valign(Gtk.Align.CENTER)
        suffix.append(capture_btn)
        suffix.append(reset_btn)
        row.add_suffix(suffix)

        self._rows[spec.id] = _Row(capture_btn=capture_btn, reset_btn=reset_btn)
        return row

    def _refresh_row(self, action_id: str) -> None:
        spec = SPEC_BY_ID[action_id]
        row = self._rows[action_id]
        accels = self._manager.get_accels(action_id)
        label = _human_label(accels[0]) if accels else "Unset"
        row.capture_btn.set_child(_keycaps(label))
        row.reset_btn.set_visible(list(accels) != list(spec.default))
        if spec.kind == "global":
            available = self._manager.global_available
            row.capture_btn.set_sensitive(available)
            row.capture_btn.set_tooltip_text(
                None if available else "Bridge extension is not connected"
            )

    # ── Capture mode ─────────────────────────────────────────────────────

    def _on_capture_clicked(self, _btn: Gtk.Button, action_id: str) -> None:
        spec = SPEC_BY_ID[action_id]
        if spec.kind == "global" and not self._manager.global_available:
            return
        if self._capturing_id == action_id:
            return
        if self._capturing_id is not None:
            self._cancel_capture()
        self._capturing_id = action_id
        row = self._rows[action_id]
        row.capture_btn.add_css_class("suggested-action")
        row.capture_btn.set_child(Gtk.Label(label="Press a shortcut… (Esc to cancel)"))

    def _cancel_capture(self) -> None:
        if self._capturing_id is None:
            return
        action_id = self._capturing_id
        self._capturing_id = None
        row = self._rows.get(action_id)
        if row is not None:
            row.capture_btn.remove_css_class("suggested-action")
        self._refresh_row(action_id)

    def _on_key_pressed(
        self,
        _ctrl: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if self._capturing_id is None:
            return False
        action_id = self._capturing_id

        if keyval == Gdk.KEY_Escape:
            self._cancel_capture()
            return True
        if keyval in _MODIFIER_KEYVALS:
            return True

        mods = state & Gtk.accelerator_get_default_mod_mask()
        if not Gtk.accelerator_valid(keyval, mods):
            self._show_toast("Not a valid shortcut")
            return True

        accel = Gtk.accelerator_name(keyval, mods)
        conflict_id = self._manager.find_conflict(action_id, accel)
        if conflict_id is not None:
            conflict_title = SPEC_BY_ID[conflict_id].title
            self._show_toast(f"Already used by “{conflict_title}”")
            return True

        self._capturing_id = None
        row = self._rows[action_id]
        row.capture_btn.remove_css_class("suggested-action")
        self._manager.set_accels(action_id, [accel])
        return True

    # ── Reset ────────────────────────────────────────────────────────────

    def _on_reset_clicked(self, _btn: Gtk.Button, action_id: str) -> None:
        if self._capturing_id == action_id:
            self._cancel_capture()
        self._manager.reset(action_id)

    def _on_reset_all_clicked(self, _btn: Gtk.Button) -> None:
        if self._capturing_id is not None:
            self._cancel_capture()
        self._manager.reset_all()

    # ── Manager sync / teardown ─────────────────────────────────────────

    def _on_manager_changed(self, _action_id: str) -> None:
        for action_id in self._rows:
            if action_id != self._capturing_id:
                self._refresh_row(action_id)

    def _on_dialog_closed(self, _dialog: Adw.Dialog) -> None:
        self._manager.disconnect(self._changed_id)

    def _show_toast(self, message: str) -> None:
        toast = Adw.Toast.new(message)
        toast.set_timeout(3)
        self._toast_overlay.add_toast(toast)


def build_shortcuts_dialog(manager: KeybindingManager) -> Adw.Dialog:
    """Build the keyboard-shortcuts editor dialog."""
    return _ShortcutsEditor(manager).dialog
