"""Capture-source panel mixin.

The capture panel is the *capture layer*: structured controls (scope, boot,
source preset, min priority, plus a raw-command escape hatch) that decide what
``journalctl`` pulls from the journal before reading starts. It is independent of
the live Search / severity / tag filters, which only narrow already-captured
lines.

``CapturePanelMixin`` is mixed into ``LogViewerView`` and drives a ``CaptureSpec``
persisted through the shared :class:`app.core.settings.Settings` store. The
``TYPE_CHECKING`` block declares the view-owned state it depends on.
"""

from dataclasses import asdict
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from app.core.journal_reader import CaptureSpec, build_journal_cmd
from app.ui.log_viewer.common import (
    _CAPTURE_HELP,
    _CAPTURE_KEY,
    _PRIORITY_BY_INDEX,
    _SCOPE_BY_INDEX,
    _SOURCE_BY_INDEX,
    _make_info_icon,
)

if TYPE_CHECKING:
    from app.core.settings import Settings


class CapturePanelMixin:
    """Builds and maintains the "Source" capture panel for ``LogViewerView``."""

    if TYPE_CHECKING:
        _capture: CaptureSpec
        _block_capture_signals: bool
        _settings: Settings
        _capture_panel: Gtk.Box
        _scope_dd: Gtk.DropDown
        _boot_check: Gtk.CheckButton
        _source_dd: Gtk.DropDown
        _source_value_entry: Gtk.Entry
        _prio_dd: Gtk.DropDown
        _raw_switch: Gtk.Switch
        _raw_entry: Gtk.Entry

    def _build_capture_panel(self) -> Gtk.Widget:
        """Structured controls for the capture layer (what to pull from the
        journal). Generates a journalctl command consumed by the reader; a
        hidden "Advanced" raw override is kept as an escape hatch for power
        users without exposing journalctl to everyone."""
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        panel.add_css_class("log-cmdbar")

        # Main row — scope, boot, source preset and custom value on one line.
        main = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        main.append(_make_info_icon(_CAPTURE_HELP))
        main.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        main.append(Gtk.Label(label="Scope:"))
        self._scope_dd = Gtk.DropDown.new_from_strings(["User", "System", "Both"])
        self._scope_dd.set_tooltip_text(
            "Which journal to read (--user / --system / both)"
        )
        self._scope_dd.connect("notify::selected", self._on_scope_changed)
        main.append(self._scope_dd)

        self._boot_check = Gtk.CheckButton(label="This boot only")
        self._boot_check.set_tooltip_text("Limit to the current boot (-b)")
        self._boot_check.connect("toggled", self._on_boot_toggled)
        main.append(self._boot_check)

        main.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        from_lbl = Gtk.Label(label="Logs from:")
        main.append(from_lbl)
        self._source_dd = Gtk.DropDown.new_from_strings(
            ["GNOME Shell", "Everything", "Custom unit…", "Custom identifier…"]
        )
        self._source_dd.set_tooltip_text(
            "GNOME Shell captures all extension logs — extensions run inside "
            "the gnome-shell process"
        )
        self._source_dd.connect("notify::selected", self._on_source_changed)
        main.append(self._source_dd)

        self._source_value_entry = Gtk.Entry()
        self._source_value_entry.set_hexpand(True)
        self._source_value_entry.set_placeholder_text("e.g. gnome-shell.service")
        self._source_value_entry.connect("changed", self._on_source_value_changed)
        main.append(self._source_value_entry)

        main.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        main.append(Gtk.Label(label="Min priority:"))
        self._prio_dd = Gtk.DropDown.new_from_strings(
            ["All", "Error", "Warning", "Info"]
        )
        self._prio_dd.set_tooltip_text(
            "Drop lower-severity entries at the source (-p). Leave at All and "
            "use the severity dots to filter live."
        )
        self._prio_dd.connect("notify::selected", self._on_priority_changed)
        main.append(self._prio_dd)
        panel.append(main)

        # Advanced — raw journalctl override (power-user escape hatch)
        adv = Gtk.Expander(label="Advanced")
        adv_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        adv_box.set_margin_top(8)

        raw_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._raw_switch = Gtk.Switch()
        self._raw_switch.set_valign(Gtk.Align.CENTER)
        self._raw_switch.connect("notify::active", self._on_raw_switch_toggled)
        raw_row.append(self._raw_switch)
        raw_row.append(Gtk.Label(label="Use custom journalctl command"))
        adv_box.append(raw_row)

        self._raw_entry = Gtk.Entry()
        self._raw_entry.set_hexpand(True)
        self._raw_entry.set_placeholder_text("journalctl …")
        self._raw_entry.set_tooltip_text(
            "Supported flags: --user/--system, -t/--identifier, -u/--unit, "
            "-b/--boot, -p/--priority. --follow/-o/-n are managed internally."
        )
        self._raw_entry.connect("changed", self._on_raw_entry_changed)
        adv_box.append(self._raw_entry)

        adv.set_child(adv_box)
        panel.append(adv)

        self._capture_panel = panel
        self._sync_capture_controls_from_spec()
        return panel

    def _sync_capture_controls_from_spec(self) -> None:
        """Push the current CaptureSpec into the widgets without firing handlers."""
        self._block_capture_signals = True
        spec = self._capture
        self._scope_dd.set_selected(
            _SCOPE_BY_INDEX.index(spec.scope) if spec.scope in _SCOPE_BY_INDEX else 0
        )
        self._boot_check.set_active(spec.this_boot)
        self._source_dd.set_selected(
            _SOURCE_BY_INDEX.index(spec.source) if spec.source in _SOURCE_BY_INDEX else 0
        )
        self._source_value_entry.set_text(spec.source_value)
        try:
            self._prio_dd.set_selected(_PRIORITY_BY_INDEX.index(spec.min_priority))
        except ValueError:
            self._prio_dd.set_selected(0)
        self._raw_switch.set_active(spec.raw_override)
        self._raw_entry.set_text(
            spec.raw_text if spec.raw_override else build_journal_cmd(spec)
        )
        self._block_capture_signals = False
        self._update_capture_sensitivity()

    def _update_capture_sensitivity(self) -> None:
        """Reflect raw-override and custom-source state in widget enablement."""
        raw = self._capture.raw_override
        for w in (self._scope_dd, self._boot_check, self._source_dd, self._prio_dd):
            w.set_sensitive(not raw)
        custom = self._capture.source in ("unit", "identifier")
        self._source_value_entry.set_visible(custom)
        self._source_value_entry.set_sensitive(not raw and custom)
        self._raw_entry.set_sensitive(raw)

    def _apply_capture_change(self) -> None:
        """Recompute the effective command, refresh the preview, and persist."""
        self._update_capture_sensitivity()
        if not self._capture.raw_override:
            # Keep the raw entry as a live preview of the generated command.
            self._block_capture_signals = True
            self._raw_entry.set_text(build_journal_cmd(self._capture))
            self._block_capture_signals = False
        self._persist_capture()

    def _persist_capture(self) -> None:
        # Merge in the structured capture spec and drop the obsolete free-text
        # "journal_cmd" key in the same write (migration cleanup).
        self._settings.update(
            {_CAPTURE_KEY: asdict(self._capture)}, remove=("journal_cmd",)
        )

    def _set_capture_panel_sensitive(self, enabled: bool) -> None:
        self._capture_panel.set_sensitive(enabled)
        if enabled:
            self._update_capture_sensitivity()

    # ── Capture panel handlers ─────────────────────────────────────────────

    def _on_scope_changed(self, dd: Gtk.DropDown, _pspec: object) -> None:
        if self._block_capture_signals:
            return
        self._capture.scope = _SCOPE_BY_INDEX[dd.get_selected()]
        self._apply_capture_change()

    def _on_boot_toggled(self, check: Gtk.CheckButton) -> None:
        if self._block_capture_signals:
            return
        self._capture.this_boot = check.get_active()
        self._apply_capture_change()

    def _on_source_changed(self, dd: Gtk.DropDown, _pspec: object) -> None:
        if self._block_capture_signals:
            return
        self._capture.source = _SOURCE_BY_INDEX[dd.get_selected()]
        self._apply_capture_change()

    def _on_source_value_changed(self, entry: Gtk.Entry) -> None:
        if self._block_capture_signals:
            return
        self._capture.source_value = entry.get_text()
        self._apply_capture_change()

    def _on_priority_changed(self, dd: Gtk.DropDown, _pspec: object) -> None:
        if self._block_capture_signals:
            return
        self._capture.min_priority = _PRIORITY_BY_INDEX[dd.get_selected()]
        self._apply_capture_change()

    def _on_raw_switch_toggled(self, sw: Gtk.Switch, _pspec: object) -> None:
        if self._block_capture_signals:
            return
        active = sw.get_active()
        if active and not self._capture.raw_text.strip():
            # Seed the override with the command the structured controls produce.
            self._capture.raw_text = build_journal_cmd(self._capture)
            self._block_capture_signals = True
            self._raw_entry.set_text(self._capture.raw_text)
            self._block_capture_signals = False
        self._capture.raw_override = active
        self._apply_capture_change()

    def _on_raw_entry_changed(self, entry: Gtk.Entry) -> None:
        if self._block_capture_signals:
            return
        if self._capture.raw_override:
            self._capture.raw_text = entry.get_text()
            self._persist_capture()
