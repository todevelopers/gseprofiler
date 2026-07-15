"""Keyboard-shortcuts help dialog.

A theme-consistent Adw.Dialog listing every shortcut grouped by scope, built
by hand rather than with the deprecated Gtk.ShortcutsWindow. Keep the entries
here in sync with the actual controllers in main.py and the tab views.
"""

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gtk

# (section title, section description | None, [(accel, action), …]).
# Accels are split on "+" into individual keycaps for rendering.
_SHORTCUTS: list[tuple[str, str | None, list[tuple[str, str]]]] = [
    (
        "Global",
        "Work even when the GSE Profiler window is not focused.",
        [
            ("Super+F9", "Toggle profiling (start / stop)"),
            ("Super+Shift+F9", "Restart profiling (stop, clear, start)"),
        ],
    ),
    (
        "General",
        None,
        [
            ("Ctrl+1", "Details tab"),
            ("Ctrl+2", "Profiler tab"),
            ("Ctrl+3", "Inspector tab"),
            ("Ctrl+4", "Logs tab"),
            ("F9", "Toggle left panel"),
            ("Ctrl+?", "Keyboard shortcuts"),
            ("Ctrl+Q", "Quit"),
        ],
    ),
    (
        "Profiler tab",
        None,
        [
            ("Ctrl+R", "Start / stop profiling"),
            ("Ctrl+F", "Focus the function filter"),
            ("Ctrl+S", "Save profile"),
            ("Ctrl+O", "Load profile"),
            ("Ctrl+L", "Clear profiling data"),
        ],
    ),
    (
        "Logs tab",
        None,
        [
            ("Ctrl+R", "Start / stop reading the journal"),
            ("Ctrl+F", "Focus the search field"),
            ("Ctrl+S", "Export the visible log"),
            ("Ctrl+L", "Clear the log"),
        ],
    ),
    (
        "Inspector tab",
        None,
        [
            ("Ctrl+R", "Refresh properties"),
        ],
    ),
]


def _keycaps(accel: str) -> Gtk.Widget:
    """Render an accelerator like ``Ctrl+Shift+F9`` as a row of keycaps."""
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
    box.set_valign(Gtk.Align.CENTER)
    keys = accel.split("+")
    for i, key in enumerate(keys):
        if i > 0:
            plus = Gtk.Label(label="+")
            plus.add_css_class("dim-label")
            box.append(plus)
        cap = Gtk.Label(label=key)
        cap.add_css_class("keycap")
        box.append(cap)
    return box


def build_shortcuts_dialog() -> Adw.Dialog:
    """Build the keyboard-shortcuts help dialog."""
    dialog = Adw.Dialog()
    dialog.set_title("Keyboard Shortcuts")
    dialog.set_content_width(480)
    dialog.set_content_height(620)

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
    content.set_margin_top(12)
    content.set_margin_bottom(24)
    content.set_margin_start(18)
    content.set_margin_end(18)

    for title, description, rows in _SHORTCUTS:
        group = Adw.PreferencesGroup()
        group.set_title(title)
        if description:
            group.set_description(description)
        for accel, action in rows:
            row = Adw.ActionRow()
            row.set_title(action)
            row.add_suffix(_keycaps(accel))
            group.add(row)
        content.append(group)

    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroller.set_vexpand(True)
    scroller.set_child(content)

    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(Adw.HeaderBar())
    toolbar.set_content(scroller)

    dialog.set_child(toolbar)
    return dialog
