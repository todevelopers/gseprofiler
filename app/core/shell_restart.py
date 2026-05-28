"""Shared helper: prompt the user to log out so gnome-shell reloads extensions.

GNOME Shell on Wayland can only pick up newly installed / removed extensions
after a full session logout.  Both :mod:`app.core.bridge_manager` (bridge
extension lifecycle) and :mod:`app.core.github_installer` (user-installed
GitHub extensions) need the same prompt, so the dialog + Logout D-Bus call
live here.
"""

import logging

import gi

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk

_log = logging.getLogger(__name__)


def prompt_shell_restart(
    parent_window: Gtk.Window | None,
    *,
    title: str = "Shell Restart Required",
    body: str | None = None,
    action: str = "installed",
) -> None:
    """Show a logout-confirmation dialog. On confirm, asks SessionManager to log out.

    ``body`` overrides the default body text. ``action`` is interpolated into
    the default body (``"The extension was {action}."``).
    """
    if body is None:
        body = (
            f"The extension was {action}.\n\n"
            "GNOME Shell requires a full logout to reload extensions.\n"
            "Log out now?"
        )
    dialog = Adw.AlertDialog.new(title, body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("restart", "Log Out")
    dialog.set_response_appearance("restart", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.connect("response", _on_response)
    if parent_window:
        dialog.present(parent_window)


def _on_response(_dialog: Adw.AlertDialog, response: str) -> None:
    if response == "restart":
        _restart_shell()


def _restart_shell() -> None:
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as exc:
        _log.error("Cannot get session bus: %s", exc.message)
        return
    bus.call(
        "org.gnome.SessionManager",
        "/org/gnome/SessionManager",
        "org.gnome.SessionManager",
        "Logout",
        GLib.Variant("(u)", (1,)),
        None,
        Gio.DBusCallFlags.NONE,
        -1,
        None,
        _on_logout_done,
    )


def _on_logout_done(source: Gio.DBusConnection, result: Gio.AsyncResult) -> None:
    try:
        source.call_finish(result)
    except GLib.Error as exc:
        _log.error("SessionManager.Logout failed: %s", exc.message)
