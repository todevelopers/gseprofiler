import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from app.core.bridge_manager import BRIDGE_UUID, BridgeManager
from app.core.dbus_client import DBusClient, ExtensionState
from app.core.ego_client import EgoClient
from app.core.ego_installer import EgoInstaller
from app.core.github_installer import GitHubInstaller
from app.core.socket_server import SocketServer
from app.core.source_registry import SourceRegistry
from app.ui.details_view import DetailsView
from app.ui.extension_list import ExtensionListView
from app.ui.inspector_view import InspectorView
from app.ui.install_dialog import InstallDialog
from app.ui.log_viewer import LogViewerView
from app.ui.profiler_view import ProfilerView
from app.ui.shortcuts_dialog import build_shortcuts_dialog

APP_ID = "io.github.todevelopers.GseProfiler"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_BASE_VERSION = "1.2.0"

# Window sizing. Below the minimum the packed toolbars (the 4-tab WIDE view
# switcher and the log-viewer filter row) hit their own minimums and the layout
# starts reflowing/jittering, so the compositor is told not to allow the window
# to shrink past this threshold.
_DEFAULT_WINDOW_SIZE = (1200, 760)
_MIN_WINDOW_SIZE = (915, 560)


def _compute_version() -> str:
    # Inside a Flatpak sandbox there is no git repo — return the baked-in version.
    if os.path.exists("/.flatpak-info"):
        return _BASE_VERSION
    try:
        subprocess.run(
            ["git", "-C", str(_PROJECT_ROOT), "describe", "--tags", "--exact-match", "HEAD"],
            capture_output=True, check=True, text=True,
        )
        return _BASE_VERSION
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    try:
        result = subprocess.run(
            ["git", "-C", str(_PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, check=True, text=True,
        )
        return f"{_BASE_VERSION}-alpha+{result.stdout.strip()}"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return f"{_BASE_VERSION}-alpha"


APP_VERSION = _compute_version()


class _ConnectionChip(Gtk.Label):
    """Header bar chip showing live socket connection state."""

    def __init__(self) -> None:
        super().__init__()
        self.set_ellipsize(Pango.EllipsizeMode.END)
        self.set_width_chars(0)
        self.set_max_width_chars(14)
        self.set_connected(False)

    def set_connected(self, connected: bool) -> None:
        if connected:
            self.set_text("● Connected")
            self.remove_css_class("dim-label")
            self.add_css_class("success")
        else:
            self.set_text("● Disconnected")
            self.remove_css_class("success")
            self.add_css_class("dim-label")


class MainWindow(Adw.ApplicationWindow):
    def __init__(
        self,
        dbus_client: DBusClient,
        socket_server: SocketServer,
        bridge: BridgeManager,
        installer: GitHubInstaller,
        ego_installer: EgoInstaller,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self._dbus = dbus_client
        self._socket = socket_server
        self._bridge = bridge
        self._installer = installer
        self._ego_installer = ego_installer
        self._active_uuid: str | None = None
        self._last_extensions: dict[str, Any] = {}
        self._active_toast: Adw.Toast | None = None
        self.set_title("GSE Profiler")
        self.set_default_size(*_DEFAULT_WINDOW_SIZE)
        # Enforce a minimum size so the window cannot be shrunk into the range
        # where the toolbars clamp and the UI starts jittering.
        self.set_size_request(*_MIN_WINDOW_SIZE)
        self._register_actions()
        self._build_ui()
        self._update_bridge_actions()
        socket_server.connect("client-connected", self._on_client_connected)
        socket_server.connect("client-disconnected", self._on_client_disconnected)
        dbus_client.connect("extensions-changed", self._on_extensions_changed)
        installer.connect("installed", self._on_extension_installed)
        installer.connect("update-available", self._on_github_update_available)
        ego_installer.connect("installed", self._on_extension_installed)
        ego_installer.connect("update-available", self._on_ego_update_available)

    def _register_actions(self) -> None:
        self._install_action = Gio.SimpleAction.new("install-bridge", None)
        self._install_action.connect("activate", self._on_install_bridge)
        self.add_action(self._install_action)

        self._reinstall_action = Gio.SimpleAction.new("reinstall-bridge", None)
        self._reinstall_action.connect("activate", self._on_reinstall_bridge)
        self.add_action(self._reinstall_action)

        self._uninstall_action = Gio.SimpleAction.new("uninstall-bridge", None)
        self._uninstall_action.connect("activate", self._on_uninstall_bridge)
        self.add_action(self._uninstall_action)

        install_action = Gio.SimpleAction.new("install-extension", None)
        install_action.connect("activate", self._on_install_extension)
        self.add_action(install_action)

        toggle_action = Gio.SimpleAction.new("toggle-sidebar", None)
        toggle_action.connect("activate", self._on_toggle_sidebar)
        self.add_action(toggle_action)

        select_tab_action = Gio.SimpleAction.new(
            "select-tab", GLib.VariantType.new("s")
        )
        select_tab_action.connect("activate", self._on_select_tab)
        self.add_action(select_tab_action)

        shortcuts_action = Gio.SimpleAction.new("show-shortcuts", None)
        shortcuts_action.connect("activate", self._on_show_shortcuts)
        self.add_action(shortcuts_action)

    def _update_bridge_actions(self) -> None:
        installed = self._bridge.is_installed
        self._install_action.set_enabled(not installed)
        self._reinstall_action.set_enabled(installed)
        self._uninstall_action.set_enabled(installed)

    def _build_ui(self) -> None:
        self._sidebar_position = 260

        self._sidebar_toggle_btn = Gtk.ToggleButton()
        self._sidebar_toggle_btn.set_icon_name("sidebar-show-symbolic")
        self._sidebar_toggle_btn.set_tooltip_text("Toggle Left Panel (F9)")
        self._sidebar_toggle_btn.set_active(True)
        self._sidebar_toggle_btn.connect("toggled", self._on_sidebar_btn_toggled)

        # ── Content views ──────────────────────────────────────────────────
        self._details_view = DetailsView(
            self._dbus, self._installer, self._ego_installer
        )
        self._profiler_view = ProfilerView(self._dbus, self._socket)
        self._inspector_view = InspectorView(self._dbus, self._socket)
        self._logs_view = LogViewerView(self._dbus)

        # ── ViewStack (tabs) ───────────────────────────────────────────────
        self._view_stack = Adw.ViewStack()

        details_page = self._view_stack.add_titled(self._details_view, "details", "Details")
        details_page.set_icon_name("application-x-addon-symbolic")

        self._profiler_page = self._view_stack.add_titled(
            self._profiler_view, "profiler", "Profiler"
        )
        self._profiler_page.set_icon_name("power-profile-performance-symbolic")

        self._inspector_page = self._view_stack.add_titled(
            self._inspector_view, "inspector", "Inspector"
        )
        self._inspector_page.set_icon_name("edit-find-symbolic")

        self._logs_page = self._view_stack.add_titled(self._logs_view, "logs", "Logs")
        self._logs_page.set_icon_name("text-x-generic-symbolic")

        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self._view_stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)

        content_header = Adw.HeaderBar()
        content_header.set_title_widget(switcher)
        content_header.pack_start(self._sidebar_toggle_btn)

        # Toast overlay wraps the tab stack so global-shortcut feedback
        # (e.g. "Profiling started") can surface from any tab.
        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(self._view_stack)

        self._profiler_view.connect("show-toast", self._on_profiler_toast)
        self._profiler_view.connect("request-attention", self._on_profiler_attention)

        content_toolbar = Adw.ToolbarView()
        content_toolbar.add_top_bar(content_header)
        content_toolbar.set_content(self._toast_overlay)

        # ── Sidebar (extension list) ───────────────────────────────────────
        self._ext_list = ExtensionListView(self._dbus, self._installer)
        self._ext_list.connect("extension-activated", self._on_extension_activated)
        self._details_view.connect("favorite-toggled", self._on_favorite_toggled)

        menu = Gio.Menu()
        install_section = Gio.Menu()
        install_section.append("Install Extension…", "win.install-extension")
        menu.append_section("Extensions", install_section)
        section = Gio.Menu()
        section.append("Install Bridge", "win.install-bridge")
        section.append("Reinstall Bridge", "win.reinstall-bridge")
        section.append("Uninstall Bridge", "win.uninstall-bridge")
        menu.append_section("Bridge Extension", section)
        about_section = Gio.Menu()
        about_section.append("Keyboard Shortcuts", "win.show-shortcuts")
        about_section.append("About GSE Profiler", "app.about")
        menu.append_section(None, about_section)

        menu_btn = Gtk.MenuButton()
        menu_btn.set_icon_name("open-menu-symbolic")
        menu_btn.set_tooltip_text("Application menu")
        menu_btn.set_menu_model(menu)
        # Gtk CSS has no `margin: auto`, so center the section header
        # labels in code each time the menu popover is shown.
        menu_popover = menu_btn.get_popover()
        if menu_popover is not None:
            menu_popover.connect("map", self._center_menu_titles)

        github_add_btn = Gtk.Button()
        github_add_btn.set_icon_name("list-add-symbolic")
        github_add_btn.set_tooltip_text("Install an extension…")
        github_add_btn.set_action_name("win.install-extension")

        self._conn_chip = _ConnectionChip()
        self._conn_chip.add_css_class("caption")

        sidebar_title = Gtk.Label(label="GSE Profiler")
        sidebar_title.set_ellipsize(Pango.EllipsizeMode.END)
        sidebar_title.set_width_chars(0)
        sidebar_title.add_css_class("heading")

        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title_box.set_valign(Gtk.Align.CENTER)
        title_box.append(sidebar_title)
        title_box.append(self._conn_chip)

        sidebar_header = Adw.HeaderBar()
        sidebar_header.set_title_widget(title_box)
        sidebar_header.set_show_start_title_buttons(False)
        sidebar_header.set_show_end_title_buttons(False)
        sidebar_header.pack_start(github_add_btn)
        sidebar_header.pack_end(menu_btn)

        self._sidebar_toolbar = Adw.ToolbarView()
        self._sidebar_toolbar.add_top_bar(sidebar_header)
        self._sidebar_toolbar.set_content(self._ext_list)
        self._sidebar_toolbar.add_css_class("sidebar-pane")

        # ── Resizable paned split ──────────────────────────────────────────
        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._paned.set_start_child(self._sidebar_toolbar)
        self._paned.set_end_child(content_toolbar)
        self._paned.set_position(self._sidebar_position)
        self._paned.set_resize_start_child(False)
        self._paned.set_resize_end_child(True)
        # Neither child may shrink below its minimum. Letting the start child
        # shrink (the old behaviour) made narrowing the window squeeze the
        # extension list past its 180px floor down to nothing, with no way to
        # drag it back — worse under Flatpak where the content pane's natural
        # minimum is larger. With shrink disabled on both sides the 180px
        # size request below is a hard floor: the list can't be clipped or
        # collapsed by accident, and the content pane keeps its right-edge
        # controls (window buttons, log-viewer start/stop). The window's own
        # minimum size absorbs the rest.
        self._paned.set_shrink_start_child(False)
        self._paned.set_shrink_end_child(False)
        self._sidebar_toolbar.set_size_request(180, -1)
        self._paned.connect("notify::position", self._on_paned_position_changed)

        self.set_content(self._paned)

    # ── Extension selection ────────────────────────────────────────────────

    def _on_extension_activated(
        self, _list: ExtensionListView, uuid: str
    ) -> None:
        self._active_uuid = uuid

        self._details_view.set_active_extension(uuid)
        self._details_view.set_favorite_state(self._ext_list.is_favorite(uuid))
        self._profiler_view.set_target_extension(uuid)
        self._inspector_view.set_target_extension(uuid)
        self._logs_view.set_selected_extension(uuid)


    def _on_favorite_toggled(self, _details: DetailsView) -> None:
        if self._active_uuid:
            self._ext_list.toggle_favorite(self._active_uuid)

    # ── Menu styling ───────────────────────────────────────────────────────

    def _center_menu_titles(self, popover: Gtk.Widget) -> None:
        """Center the ``label.title`` section headers in the burger menu.

        Gtk CSS has no ``margin: auto``, so we set the alignment on the
        actual label widgets each time the popover is mapped.
        """
        self._center_title_labels(popover)

    def _center_title_labels(self, widget: Gtk.Widget) -> None:
        if isinstance(widget, Gtk.Label) and widget.has_css_class("title"):
            widget.set_halign(Gtk.Align.CENTER)
            widget.set_xalign(0.5)
        child = widget.get_first_child()
        while child is not None:
            self._center_title_labels(child)
            child = child.get_next_sibling()

    # ── Sidebar toggle ─────────────────────────────────────────────────────

    def _on_toggle_sidebar(self, _action: Gio.SimpleAction, _param: object) -> None:
        self._sidebar_toggle_btn.set_active(not self._sidebar_toggle_btn.get_active())

    # ── Tab selection / shortcuts help ─────────────────────────────────────

    def _on_select_tab(self, _action: Gio.SimpleAction, param: GLib.Variant) -> None:
        self._view_stack.set_visible_child_name(param.get_string())

    def _on_show_shortcuts(self, _action: Gio.SimpleAction, _param: object) -> None:
        build_shortcuts_dialog().present(self)

    # ── Global-shortcut feedback (from ProfilerView) ───────────────────────

    def _on_profiler_toast(self, _view: ProfilerView, message: str) -> None:
        # Only ever show one shortcut-feedback toast at a time. Rapid Super+F5
        # presses would otherwise queue a toast each, which the user then has
        # to dismiss one by one — dismiss the previous before adding the next.
        if self._active_toast is not None:
            self._active_toast.dismiss()
        toast = Adw.Toast.new(message)
        toast.set_timeout(3)
        toast.connect("dismissed", self._on_toast_dismissed)
        self._active_toast = toast
        self._toast_overlay.add_toast(toast)

    def _on_toast_dismissed(self, toast: Adw.Toast) -> None:
        if self._active_toast is toast:
            self._active_toast = None

    def _on_profiler_attention(self, _view: ProfilerView) -> None:
        # A global profiling shortcut fired — bring the Profiler tab forward
        # so the recording state is visible.
        self._view_stack.set_visible_child_name("profiler")

    def _on_sidebar_btn_toggled(self, btn: Gtk.ToggleButton) -> None:
        if btn.get_active():
            self._sidebar_toolbar.set_visible(True)
            self._paned.set_position(self._sidebar_position)
        else:
            pos = self._paned.get_position()
            if pos > 0:
                self._sidebar_position = pos
            self._sidebar_toolbar.set_visible(False)

    def _on_paned_position_changed(self, paned: Gtk.Paned, _pspec: object) -> None:
        if self._sidebar_toolbar.get_visible():
            pos = paned.get_position()
            if pos > 0:
                self._sidebar_position = pos

    # ── D-Bus / socket handlers ────────────────────────────────────────────

    def _on_extensions_changed(
        self, _dbus: DBusClient, extensions: dict[str, Any]
    ) -> None:
        self._last_extensions = extensions
        self._update_bridge_actions()

        if self._active_uuid:
            if self._active_uuid not in extensions:
                # Extension was removed
                self._active_uuid = None
                self._details_view.set_active_extension(None)
                self._profiler_view.set_target_extension(None)
                self._inspector_view.set_target_extension(None)
                self._logs_view.set_selected_extension(None)

    def _on_client_connected(self, _server: SocketServer) -> None:
        self._conn_chip.set_connected(True)

    def _on_client_disconnected(self, _server: SocketServer) -> None:
        self._conn_chip.set_connected(False)

    # ── Bridge actions ─────────────────────────────────────────────────────

    def _on_install_bridge(self, _action: Gio.SimpleAction, _param: object) -> None:
        self._bridge.install(parent_window=self)
        GLib.idle_add(self._update_bridge_actions)

    def _on_reinstall_bridge(self, _action: Gio.SimpleAction, _param: object) -> None:
        self._bridge.reinstall(parent_window=self)
        GLib.idle_add(self._update_bridge_actions)

    def _on_uninstall_bridge(self, _action: Gio.SimpleAction, _param: object) -> None:
        self._bridge.uninstall(parent_window=self)
        GLib.idle_add(self._update_bridge_actions)

    # ── Extension install ──────────────────────────────────────────────────

    def _on_install_extension(
        self, _action: Gio.SimpleAction, _param: object
    ) -> None:
        dialog = InstallDialog(
            self._installer, self._ego_installer, self._dbus.shell_version
        )
        dialog.present(self)

    def _on_extension_installed(
        self, _installer: object, _uuid: str
    ) -> None:
        # Force-refresh the extension list so the new extension appears once
        # gnome-shell reloads (also useful while developing without logout).
        self._dbus.list_extensions()

    def _on_github_update_available(
        self, _installer: GitHubInstaller, uuid: str, new_sha: str
    ) -> None:
        self._ext_list.mark_update_available(uuid, new_sha)
        self._details_view.refresh_github_update(uuid, new_sha)

    def _on_ego_update_available(
        self, _installer: EgoInstaller, uuid: str, new_version: str
    ) -> None:
        self._ext_list.mark_update_available(uuid, new_version)
        self._details_view.refresh_ego_update(uuid, new_version)


class Application(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)
        self.connect("activate", self._on_activate)
        self._dbus_client = DBusClient()
        self._socket_server = SocketServer()
        self._bridge = BridgeManager(_PROJECT_ROOT, self._dbus_client)
        # One shared provenance registry (sources.json) backs both installers.
        self._registry = SourceRegistry()
        self._installer = GitHubInstaller(self._registry)
        self._ego_installer = EgoInstaller(self._registry, EgoClient())
        self._win: MainWindow | None = None
        self._bootstrap_handler: int = 0
        self._update_check_handler: int = 0

    def _on_activate(self, _app: "Application") -> None:
        self._load_css()
        self._socket_server.start()
        self._win = MainWindow(
            application=self,
            dbus_client=self._dbus_client,
            socket_server=self._socket_server,
            bridge=self._bridge,
            installer=self._installer,
            ego_installer=self._ego_installer,
        )
        self.set_accels_for_action("win.toggle-sidebar", ["F9"])
        self.set_accels_for_action("win.select-tab::details", ["<Control>1"])
        self.set_accels_for_action("win.select-tab::profiler", ["<Control>2"])
        self.set_accels_for_action("win.select-tab::inspector", ["<Control>3"])
        self.set_accels_for_action("win.select-tab::logs", ["<Control>4"])
        self.set_accels_for_action(
            "win.show-shortcuts", ["<Control>question", "F1"]
        )
        self.set_accels_for_action("app.quit", ["<Control>q"])

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._on_about)
        self.add_action(about_action)

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self._win.present()
        self._bootstrap_handler = self._dbus_client.connect(
            "extensions-changed", self._on_ready_for_bootstrap
        )
        self._update_check_handler = self._dbus_client.connect(
            "extensions-changed", self._on_ready_for_update_check
        )
        self._dbus_client.connect("extensions-changed", self._on_bridge_state_changed)

    def _load_css(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        icon_theme = Gtk.IconTheme.get_for_display(display)
        icon_theme.add_search_path(str(_PROJECT_ROOT / "app" / "data" / "icons"))
        css_path = _PROJECT_ROOT / "app" / "data" / "style.css"
        if not css_path.exists():
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(css_path))
        Gtk.StyleContext.add_provider_for_display(
            display,
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _on_about(self, _action: Gio.SimpleAction, _param: object) -> None:
        window = Adw.AboutWindow(
            transient_for=self._win,
            application_name="GSE Profiler",
            application_icon="io.github.todevelopers.GseProfiler",
            version=APP_VERSION,
            website="https://todevelopers.github.io/flatpaks/gseprofiler",
            issue_url="https://github.com/todevelopers/gse-profiler/issues",
            support_url="https://github.com/todevelopers/gse-profiler/discussions",
            license_type=Gtk.License.GPL_3_0,
            developer_name="Tomáš Gažovič",
            developers=["Tomáš Gažovič"],
            copyright="© 2026 Tomáš Gažovič",
        )
        window.present()

    def do_shutdown(self) -> None:
        self._bridge.deactivate()
        self._socket_server.stop()
        Adw.Application.do_shutdown(self)

    def _on_bridge_state_changed(self, _dbus: DBusClient, extensions: dict) -> None:
        bridge = extensions.get(BRIDGE_UUID)
        if bridge and bridge["state"] == ExtensionState.DISABLED:
            self._socket_server.disconnect_client()

    def _on_ready_for_bootstrap(self, _dbus: DBusClient, _extensions: dict) -> None:
        self._dbus_client.disconnect(self._bootstrap_handler)
        self._bootstrap_handler = 0
        self._bridge.ensure_installed(parent_window=self._win)

    def _on_ready_for_update_check(
        self, _dbus: DBusClient, extensions: dict[str, Any]
    ) -> None:
        # Run the update checks exactly once, on the first extensions list.
        if self._update_check_handler:
            self._dbus_client.disconnect(self._update_check_handler)
            self._update_check_handler = 0
        self._installer.check_updates(extensions)
        self._ego_installer.check_updates(extensions, self._dbus_client.shell_version)


def main() -> None:
    debug = "--debug" in sys.argv
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    app = Application()
    argv = [a for a in sys.argv if a != "--debug"]
    app.run(argv)


if __name__ == "__main__":
    main()
