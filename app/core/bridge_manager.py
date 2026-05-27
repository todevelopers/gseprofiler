import json
import logging
import os
import shutil
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk

from app.core.dbus_client import DBusClient, ExtensionState

_log = logging.getLogger(__name__)

BRIDGE_UUID = "gse-profiler-bridge@todevelopers"
_IN_FLATPAK: bool = os.path.exists("/.flatpak-info")

# Inside a Flatpak sandbox GLib.get_user_data_dir() returns the app-scoped
# directory (~/.var/app/<id>/data), not the host ~/.local/share that gnome-shell
# actually watches.  Use the real home-relative path when sandboxed.
_INSTALL_PATH = (
    Path.home() / ".local" / "share" / "gnome-shell" / "extensions" / BRIDGE_UUID
    if _IN_FLATPAK
    else Path(GLib.get_user_data_dir()) / "gnome-shell" / "extensions" / BRIDGE_UUID
)


class BridgeManager:
    """Manages installation and lifecycle of the bridge GNOME Shell extension."""

    def __init__(self, project_root: Path, dbus_client: DBusClient) -> None:
        self._source = project_root / "bridge-extension"
        self._dbus = dbus_client

    @property
    def is_installed(self) -> bool:
        return _INSTALL_PATH.exists()

    def ensure_installed(self, parent_window: Gtk.Window | None = None) -> None:
        """Auto-bootstrap: prompt user before installing if bridge is missing."""
        if not _INSTALL_PATH.exists():
            self._prompt_install(parent_window)
        elif not self._is_up_to_date():
            self._prompt_update(parent_window)
        elif not self._dbus.is_extension_known(BRIDGE_UUID):
            self._prompt_restart(parent_window)
        elif self._dbus.get_extension_state(BRIDGE_UUID) != ExtensionState.ENABLED:
            self._dbus.enable_extension(BRIDGE_UUID)

    def install(self, parent_window: Gtk.Window | None = None) -> None:
        """Manual install: no confirmation prompt, install or enable directly."""
        if not _INSTALL_PATH.exists():
            self._do_install(parent_window)
        elif not self._dbus.is_extension_known(BRIDGE_UUID):
            self._prompt_restart(parent_window)
        else:
            self._dbus.enable_extension(BRIDGE_UUID)

    def deactivate(self) -> None:
        """Disable the bridge extension without removing it."""
        self._dbus.disable_extension(BRIDGE_UUID)

    def reinstall(self, parent_window: Gtk.Window | None = None) -> None:
        """Force-reinstall the bridge extension."""
        self._do_install(parent_window)

    def uninstall(self, parent_window: Gtk.Window | None = None) -> None:
        """Disable and remove the bridge extension, then prompt for shell restart.

        Disable runs asynchronously; the directory is removed only after the
        D-Bus call completes so the bridge has a chance to clean up its socket
        and indicator before its files disappear.
        """
        if not _INSTALL_PATH.exists():
            _show_error(parent_window, "Bridge extension is not installed.")
            return

        state = self._dbus.get_extension_state(BRIDGE_UUID)
        if state is None or state == ExtensionState.DISABLED:
            self._finish_uninstall(parent_window, error=None)
            return

        self._dbus.disable_extension(
            BRIDGE_UUID,
            on_done=lambda err: self._finish_uninstall(parent_window, error=err),
        )

    def _finish_uninstall(
        self, parent_window: Gtk.Window | None, error: GLib.Error | None
    ) -> None:
        if error is not None:
            _log.warning(
                "Disable of %s failed before uninstall (%s); removing anyway",
                BRIDGE_UUID,
                error,
            )
        try:
            shutil.rmtree(_INSTALL_PATH)
            _log.info("Bridge extension removed from %s", _INSTALL_PATH)
        except FileNotFoundError:
            pass
        except OSError as exc:
            _log.error("Bridge uninstall failed: %s", exc)
            _show_error(parent_window, str(exc))
            return
        self._prompt_restart(parent_window, uninstall=True)

    # ── Private ───────────────────────────────────────────────────────────

    def _is_up_to_date(self) -> bool:
        """Return True if the installed bridge matches the bundled one (by bundle-hash)."""
        try:
            bundled_hash = json.loads(
                (self._source / "metadata.json").read_text(encoding="utf-8")
            ).get("bundle-hash")
        except (OSError, json.JSONDecodeError):
            return True  # bundled metadata unreadable — can't compare, don't block

        if bundled_hash is None:
            return True  # bundle-hash not yet generated, feature inactive

        try:
            installed_hash = json.loads(
                (_INSTALL_PATH / "metadata.json").read_text(encoding="utf-8")
            ).get("bundle-hash")
        except (OSError, json.JSONDecodeError):
            return False  # installed metadata missing or corrupt — needs reinstall

        return bool(bundled_hash == installed_hash)

    def _prompt_update(self, parent_window: Gtk.Window | None) -> None:
        dialog = Adw.AlertDialog.new(
            "Bridge Extension Update Required",
            "The bridge extension bundled with this version of GSE Profiler "
            "differs from the installed one.\n\n"
            "Reinstall now to apply the update?",
        )
        dialog.add_response("later", "Later")
        dialog.add_response("reinstall", "Reinstall")
        dialog.set_response_appearance("reinstall", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("reinstall")
        dialog.connect("response", self._on_update_response, parent_window)
        if parent_window:
            dialog.present(parent_window)

    def _on_update_response(
        self, _dialog: Adw.AlertDialog, response: str, parent_window: Gtk.Window | None
    ) -> None:
        if response == "reinstall":
            self._do_install(parent_window)

    def _prompt_install(self, parent_window: Gtk.Window | None) -> None:
        dialog = Adw.AlertDialog.new(
            "Bridge Extension Required",
            "GSE Profiler uses a bridge GNOME Shell extension to enable profiling "
            "and inspection features.\n\n"
            "The extension will be installed and GNOME Shell will need to restart. "
            "Install now?\n\n"
            "Note: if you later uninstall this app, remove the bridge extension "
            "manually first.",
        )
        dialog.add_response("cancel", "Not Now")
        dialog.add_response("install", "Install")
        dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("install")
        dialog.connect("response", self._on_install_response, parent_window)
        if parent_window:
            dialog.present(parent_window)

    def _on_install_response(
        self, _dialog: Adw.AlertDialog, response: str, parent_window: Gtk.Window | None
    ) -> None:
        if response == "install":
            self._do_install(parent_window)

    def _do_install(self, parent_window: Gtk.Window | None) -> None:
        try:
            if _INSTALL_PATH.exists():
                shutil.rmtree(_INSTALL_PATH)
            _INSTALL_PATH.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(self._source, _INSTALL_PATH)
            _log.info("Bridge installed to %s", _INSTALL_PATH)
        except OSError as exc:
            _log.error("Bridge install failed: %s", exc)
            _show_error(parent_window, str(exc))
            return
        self._prompt_restart(parent_window)

    def _prompt_restart(self, parent_window: Gtk.Window | None, *, uninstall: bool = False) -> None:
        wayland = (
            bool(GLib.getenv("WAYLAND_DISPLAY"))
            or GLib.getenv("XDG_SESSION_TYPE") == "wayland"
        )
        action = "removed" if uninstall else "installed"

        if wayland:
            body = (
                f"The bridge extension was {action}.\n\n"
                "On Wayland, GNOME Shell requires a full logout to reload extensions.\n"
                "Log out now?"
            )
            restart_label = "Log Out"
            response_key = "restart"
        elif uninstall:
            # X11 uninstall: ask before restarting — auto-restart would freeze the
            # screen without warning.
            body = (
                "The bridge extension was removed.\n\n"
                "GNOME Shell must restart to fully unload it.\n"
                "Restart now?"
            )
            restart_label = "Restart Shell"
            response_key = "restart"
        else:
            # X11 install: restart immediately, no dialog needed.
            self._restart_shell()
            return

        dialog = Adw.AlertDialog.new("Shell Restart Required", body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response(response_key, restart_label)
        dialog.set_response_appearance(response_key, Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_restart_response, response_key)
        if parent_window:
            dialog.present(parent_window)

    def _on_restart_response(self, _dialog: Adw.AlertDialog, response: str, restart_key: str) -> None:
        if response == restart_key:
            self._restart_shell()

    def _restart_shell(self) -> None:
        wayland = (
            bool(GLib.getenv("WAYLAND_DISPLAY"))
            or GLib.getenv("XDG_SESSION_TYPE") == "wayland"
        )
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            _log.error("Cannot get session bus: %s", exc.message)
            return

        if wayland:
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
                self._on_restart_call_done,
                "Logout",
            )
        else:
            bus.call(
                "org.gnome.Shell",
                "/org/gnome/Shell",
                "org.gnome.Shell",
                "Eval",
                GLib.Variant(
                    "(s)",
                    ('Meta.restart("Restarting GNOME Shell for GSE Profiler…")',),
                ),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
                self._on_restart_call_done,
                "Eval",
            )

    def _on_restart_call_done(
        self, source: Gio.DBusConnection, result: Gio.AsyncResult, label: str
    ) -> None:
        try:
            source.call_finish(result)
        except GLib.Error as exc:
            _log.error("Shell restart D-Bus call (%s) failed: %s", label, exc.message)


def _show_error(parent_window: Gtk.Window | None, message: str) -> None:
    dialog = Adw.AlertDialog.new("Installation Failed", message)
    dialog.add_response("ok", "OK")
    if parent_window:
        dialog.present(parent_window)
