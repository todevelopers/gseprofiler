import logging
from pathlib import Path
from typing import Any

import gi

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("GObject", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, GObject, Gtk

from app.core.dbus_client import (
    ALL_STATE_CSS as _ALL_STATE_CSS,
)
from app.core.dbus_client import (
    STATE_LABELS as _STATE_LABELS,
)
from app.core.dbus_client import (
    TRANSIENT_STATES as _TRANSIENT_STATES,
)
from app.core.dbus_client import (
    DBusClient,
    ExtensionState,
)
from app.core.extension_remover import remove_extension
from app.core.github_installer import GitHubInstaller, GitHubSource
from app.core.shell_restart import prompt_shell_restart

_log = logging.getLogger(__name__)

_CHECK_SUBTITLE = "Pull the latest commit and reinstall"


class DetailsView(Gtk.Stack):
    """Extension details panel: metadata, enable/disable, open folder/prefs."""

    __gtype_name__ = "DetailsView"

    __gsignals__ = {
        "favorite-toggled": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(
        self,
        dbus_client: DBusClient,
        installer: GitHubInstaller | None = None,
    ) -> None:
        super().__init__()
        self._dbus = dbus_client
        self._installer = installer
        self._active_uuid: str | None = None
        self._all_extensions: dict[str, Any] = {}
        self._pending_disable = False
        self._switch_handler: int = 0
        self._active_source: GitHubSource | None = None

        self._build_ui()
        dbus_client.connect("extensions-changed", self._on_extensions_changed)

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        placeholder = Adw.StatusPage()
        placeholder.set_icon_name("application-x-addon-symbolic")
        placeholder.set_title("No Extension Selected")
        placeholder.set_description("Select an extension from the list on the left.")
        placeholder.set_vexpand(True)
        self.add_named(placeholder, "placeholder")

        page = Adw.PreferencesPage()
        page.set_vexpand(True)

        # ── Header row ──────────────────────────────────────────────────────
        header_group = Adw.PreferencesGroup()
        self._header_row = Adw.ActionRow()
        self._header_row.set_icon_name("application-x-addon-symbolic")
        self._state_badge = Gtk.Label()
        self._state_badge.set_valign(Gtk.Align.CENTER)
        self._header_row.add_suffix(self._state_badge)

        self._star_btn = Gtk.ToggleButton()
        self._star_btn.set_icon_name("non-starred-symbolic")
        self._star_btn.add_css_class("flat")
        self._star_btn.set_valign(Gtk.Align.CENTER)
        self._star_btn.set_tooltip_text("Add to favorites")
        self._star_handler = self._star_btn.connect("toggled", self._on_star_toggled)
        self._header_row.add_suffix(self._star_btn)

        header_group.add(self._header_row)
        page.add(header_group)

        # ── Details group ───────────────────────────────────────────────────
        details_group = Adw.PreferencesGroup()
        details_group.set_title("Details")

        self._uuid_row = Adw.ActionRow()
        self._uuid_row.set_title("UUID")
        details_group.add(self._uuid_row)

        self._desc_row = Adw.ActionRow()
        self._desc_row.set_title("Description")
        details_group.add(self._desc_row)

        self._url_row = Adw.ActionRow()
        self._url_row.set_title("Homepage")
        # Show the URL as the subtitle (ellipsized to one line) and put a
        # small open-link button in the suffix.  Using the URL as a
        # LinkButton label caused the title to be character-wrapped on
        # long URLs.
        self._url_row.set_subtitle_lines(1)
        self._url_row.set_subtitle_selectable(True)
        self._active_url: str = ""
        url_btn = Gtk.Button(icon_name="adw-external-link-symbolic")
        url_btn.add_css_class("flat")
        url_btn.set_valign(Gtk.Align.CENTER)
        url_btn.set_tooltip_text("Open homepage in browser")
        url_btn.connect("clicked", self._on_open_homepage)
        self._url_row.add_suffix(url_btn)
        details_group.add(self._url_row)

        page.add(details_group)

        # ── Actions group ───────────────────────────────────────────────────
        actions_group = Adw.PreferencesGroup()
        actions_group.set_title("Actions")

        enable_row = Adw.ActionRow()
        enable_row.set_title("Enabled")
        enable_row.set_subtitle("Enable or disable this extension")
        self._switch = Gtk.Switch()
        self._switch.set_valign(Gtk.Align.CENTER)
        self._switch_handler = self._switch.connect("notify::active", self._on_switch_toggled)
        enable_row.add_suffix(self._switch)
        enable_row.set_activatable_widget(self._switch)
        actions_group.add(enable_row)

        self._folder_row = Adw.ActionRow()
        self._folder_row.set_title("Open Folder")
        self._folder_row.set_subtitle("Open extension directory in file manager")
        folder_btn = Gtk.Button(icon_name="folder-open-symbolic")
        folder_btn.add_css_class("flat")
        folder_btn.set_valign(Gtk.Align.CENTER)
        folder_btn.set_tooltip_text("Open extension folder")
        folder_btn.connect("clicked", self._on_open_folder)
        self._folder_row.add_suffix(folder_btn)
        actions_group.add(self._folder_row)

        self._prefs_row = Adw.ActionRow()
        self._prefs_row.set_title("Preferences")
        self._prefs_row.set_subtitle("Open extension settings dialog")
        prefs_btn = Gtk.Button(icon_name="preferences-system-symbolic")
        prefs_btn.add_css_class("flat")
        prefs_btn.set_valign(Gtk.Align.CENTER)
        prefs_btn.set_tooltip_text("Open extension preferences")
        prefs_btn.connect("clicked", self._on_open_prefs)
        self._prefs_row.add_suffix(prefs_btn)
        actions_group.add(self._prefs_row)

        # Uninstall — available for any user-installed extension.
        self._uninstall_row = Adw.ActionRow()
        self._uninstall_row.set_title("Uninstall")
        self._uninstall_row.set_subtitle("Remove this extension from your shell")
        self._uninstall_btn = Gtk.Button(label="Uninstall")
        self._uninstall_btn.add_css_class("destructive-action")
        self._uninstall_btn.set_valign(Gtk.Align.CENTER)
        self._uninstall_btn.set_tooltip_text("Uninstall this extension")
        self._uninstall_btn.connect("clicked", self._on_uninstall_clicked)
        self._uninstall_row.add_suffix(self._uninstall_btn)
        actions_group.add(self._uninstall_row)

        page.add(actions_group)

        # ── GitHub source group (only visible for GitHub-sourced extensions) ─
        self._github_group = Adw.PreferencesGroup()
        self._github_group.set_title("GitHub Source")
        self._github_group.set_visible(False)

        self._github_repo_row = Adw.ActionRow()
        self._github_repo_row.set_title("Repository")
        self._github_open_btn = Gtk.Button(icon_name="adw-external-link-symbolic")
        self._github_open_btn.add_css_class("flat")
        self._github_open_btn.set_valign(Gtk.Align.CENTER)
        self._github_open_btn.set_tooltip_text("Open on GitHub")
        self._github_open_btn.connect("clicked", self._on_open_github)
        self._github_repo_row.add_suffix(self._github_open_btn)
        self._github_group.add(self._github_repo_row)

        self._github_commit_row = Adw.ActionRow()
        self._github_commit_row.set_title("Installed commit")
        self._github_commit_row.set_subtitle_lines(1)
        self._github_commit_row.set_subtitle_selectable(True)
        self._github_commit_btn = Gtk.Button(icon_name="adw-external-link-symbolic")
        self._github_commit_btn.add_css_class("flat")
        self._github_commit_btn.set_valign(Gtk.Align.CENTER)
        self._github_commit_btn.set_tooltip_text("Open commit on GitHub")
        self._github_commit_btn.connect("clicked", self._on_open_commit)
        self._github_commit_row.add_suffix(self._github_commit_btn)
        self._github_group.add(self._github_commit_row)

        self._github_check_row = Adw.ActionRow()
        self._github_check_row.set_title("Check for Updates")
        self._github_check_row.set_subtitle(_CHECK_SUBTITLE)
        self._github_check_btn = Gtk.Button(label="Check")
        self._github_check_btn.set_valign(Gtk.Align.CENTER)
        self._github_check_btn.set_tooltip_text(
            "Check upstream and reinstall if a newer commit exists"
        )
        self._github_check_btn.connect("clicked", self._on_check_updates_clicked)
        self._github_check_row.add_suffix(self._github_check_btn)
        self._github_group.add(self._github_check_row)

        self._github_update_row = Adw.ActionRow()
        self._github_update_row.set_title("Update Available")
        self._github_update_btn = Gtk.Button(label="Update")
        self._github_update_btn.add_css_class("suggested-action")
        self._github_update_btn.set_valign(Gtk.Align.CENTER)
        self._github_update_btn.connect("clicked", self._on_update_clicked)
        self._github_update_row.add_suffix(self._github_update_btn)
        self._github_update_row.set_visible(False)
        self._github_group.add(self._github_update_row)

        page.add(self._github_group)

        self.add_named(page, "content")
        self.set_visible_child_name("placeholder")

    # ── Public API ─────────────────────────────────────────────────────────

    def set_active_extension(self, uuid: str | None) -> None:
        self._pending_disable = False
        self._active_uuid = uuid
        if uuid is None:
            self.set_visible_child_name("placeholder")
            return
        info = self._all_extensions.get(uuid, {})
        self._populate(uuid, info)
        self.set_visible_child_name("content")

    def set_favorite_state(self, is_fav: bool) -> None:
        self._star_btn.handler_block_by_func(self._on_star_toggled)
        self._star_btn.set_active(is_fav)
        self._star_btn.set_icon_name("starred-symbolic" if is_fav else "non-starred-symbolic")
        self._star_btn.set_tooltip_text("Remove from favorites" if is_fav else "Add to favorites")
        self._star_btn.handler_unblock_by_func(self._on_star_toggled)

    # ── Signal handlers ────────────────────────────────────────────────────

    def _on_star_toggled(self, btn: Gtk.ToggleButton) -> None:
        is_fav = btn.get_active()
        btn.set_icon_name("starred-symbolic" if is_fav else "non-starred-symbolic")
        btn.set_tooltip_text("Remove from favorites" if is_fav else "Add to favorites")
        self.emit("favorite-toggled")

    def _on_extensions_changed(self, _dbus: DBusClient, extensions: dict[str, Any]) -> None:
        self._all_extensions = extensions
        if self._active_uuid and self._active_uuid in extensions:
            self._refresh_in_place(self._active_uuid, extensions[self._active_uuid])

    def _on_switch_toggled(self, switch: Gtk.Switch, _pspec: object) -> None:
        if not self._active_uuid:
            return
        if switch.get_active():
            self._pending_disable = False
            self._dbus.enable_extension(self._active_uuid)
        else:
            self._pending_disable = True
            self._dbus.disable_extension(self._active_uuid)

    def _on_open_folder(self, _btn: Gtk.Button) -> None:
        if not self._active_uuid:
            return
        info = self._all_extensions.get(self._active_uuid, {})
        path = info.get("path", "")
        if path:
            uri = Gio.File.new_for_path(path).get_uri()
            try:
                Gio.AppInfo.launch_default_for_uri(uri, None)
            except GLib.Error as exc:
                _log.warning("Failed to open folder %s: %s", path, exc)

    def _on_open_prefs(self, _btn: Gtk.Button) -> None:
        if self._active_uuid:
            self._dbus.launch_extension_prefs(self._active_uuid)

    # ── Populate / refresh ─────────────────────────────────────────────────

    def _populate(self, uuid: str, info: dict[str, Any]) -> None:
        name = info.get("name") or uuid
        state = info.get("state", ExtensionState.DISABLED)
        description = info.get("description", "")
        url = info.get("url", "")
        path = info.get("path", "")
        has_prefs = info.get("hasPrefs", False)
        ext_type = info.get("type", 2)

        self._header_row.set_title(name)
        self._header_row.set_subtitle(uuid)

        self._uuid_row.set_subtitle(uuid)
        self._desc_row.set_subtitle(description or "—")
        self._desc_row.set_visible(True)

        if url:
            self._url_row.set_subtitle(url)
            self._active_url = url
            self._url_row.set_visible(True)
        else:
            self._url_row.set_subtitle("")
            self._active_url = ""
            self._url_row.set_visible(False)

        # State badge
        text, css = _STATE_LABELS.get(state, ("Unknown", "dim-label"))
        self._state_badge.set_label(text)
        for c in _ALL_STATE_CSS:
            self._state_badge.remove_css_class(c)
        if css:
            self._state_badge.add_css_class(css)

        # Switch
        self._switch.handler_block_by_func(self._on_switch_toggled)
        self._switch.set_active(state == ExtensionState.ENABLED)
        self._switch.set_sensitive(state not in _TRANSIENT_STATES)
        self._switch.handler_unblock_by_func(self._on_switch_toggled)

        self._folder_row.set_visible(bool(path))
        self._prefs_row.set_visible(has_prefs)

        # Uninstall only user-installed extensions (type 2); system
        # extensions (type 1) live in a read-only prefix and cannot be
        # removed by the user.
        self._uninstall_row.set_visible(bool(path) and ext_type == 2)

        # GitHub source group — provenance comes from the registry, keyed
        # by UUID (we never write into the extension's own metadata.json).
        source = self._installer.registry.get(uuid) if self._installer else None
        self._active_source = source
        if source is None:
            self._github_group.set_visible(False)
        else:
            self._github_group.set_visible(True)
            self._github_repo_row.set_subtitle(
                f"github.com/{source.owner}/{source.repo}"
            )
            commit_text = source.short_sha or source.commit_sha
            if source.ref:
                commit_text = f"{commit_text}  ({source.ref})"
            self._github_commit_row.set_subtitle(commit_text or "—")
            self._github_commit_btn.set_sensitive(bool(source.commit_sha))
            self._github_check_row.set_subtitle(_CHECK_SUBTITLE)
            self._github_check_btn.set_sensitive(self._installer is not None)
            new_sha = (
                self._installer.has_update(uuid) if self._installer else None
            )
            self._set_update_row(new_sha)

    def refresh_github_update(self, uuid: str, new_sha: str) -> None:
        """Called by the main window when an update becomes available."""
        if uuid == self._active_uuid:
            self._set_update_row(new_sha)

    def _set_update_row(self, new_sha: str | None) -> None:
        if new_sha:
            self._github_update_row.set_visible(True)
            self._github_update_row.set_subtitle(
                f"Upstream is at {new_sha[:7]} — click Update to reinstall."
            )
        else:
            self._github_update_row.set_visible(False)

    def _on_open_homepage(self, _btn: Gtk.Button) -> None:
        if not self._active_url:
            return
        try:
            Gio.AppInfo.launch_default_for_uri(self._active_url, None)
        except GLib.Error as exc:
            _log.warning("Failed to open URL %s: %s", self._active_url, exc)

    def _on_open_github(self, _btn: Gtk.Button) -> None:
        if self._active_source is None:
            return
        try:
            Gio.AppInfo.launch_default_for_uri(self._active_source.html_url, None)
        except GLib.Error as exc:
            _log.warning("Failed to open GitHub URL: %s", exc)

    def _on_open_commit(self, _btn: Gtk.Button) -> None:
        if self._active_source is None or not self._active_source.commit_sha:
            return
        url = (
            f"{self._active_source.html_url}/commit/"
            f"{self._active_source.commit_sha}"
        )
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
        except GLib.Error as exc:
            _log.warning("Failed to open commit URL: %s", exc)

    def _on_update_clicked(self, _btn: Gtk.Button) -> None:
        if self._installer is None or self._active_source is None:
            return
        self._github_update_btn.set_sensitive(False)
        self._github_update_row.set_subtitle("Checking repository…")
        parent = self.get_root() if isinstance(self.get_root(), Gtk.Window) else None
        self._installer.update(
            self._active_source,
            on_done=lambda uuid, err: self._on_update_done(parent, uuid, err),
            on_progress=self._github_update_row.set_subtitle,
        )

    def _on_update_done(
        self,
        parent: Gtk.Window | None,
        uuid: str | None,
        error: str | None,
    ) -> None:
        self._github_update_btn.set_sensitive(True)
        if error or not uuid:
            self._github_update_row.set_subtitle(error or "Update failed.")
            return
        self._github_update_row.set_visible(False)
        prompt_shell_restart(parent, action="updated")

    def _on_check_updates_clicked(self, _btn: Gtk.Button) -> None:
        if (
            self._installer is None
            or self._active_source is None
            or self._active_uuid is None
        ):
            return
        self._github_check_btn.set_sensitive(False)
        self._github_check_row.set_subtitle("Checking for updates…")
        src = self._active_source
        parent = self.get_root() if isinstance(self.get_root(), Gtk.Window) else None
        self._installer.check_update(
            self._active_uuid,
            src,
            on_done=lambda new_sha, err: self._on_check_done(parent, src, new_sha, err),
        )

    def _on_check_done(
        self,
        parent: Gtk.Window | None,
        src: GitHubSource,
        new_sha: str | None,
        error: str | None,
    ) -> None:
        if error:
            self._github_check_btn.set_sensitive(True)
            self._github_check_row.set_subtitle(error)
            return
        if not new_sha:
            self._github_check_btn.set_sensitive(True)
            self._github_check_row.set_subtitle("Up to date.")
            return
        # New commit upstream — pull and reinstall.
        self._github_check_row.set_subtitle(
            f"New commit {new_sha[:7]} found — downloading…"
        )
        if self._installer is None:
            self._github_check_btn.set_sensitive(True)
            return
        self._installer.update(
            src,
            on_done=lambda uuid, err: self._on_check_update_installed(parent, uuid, err),
            on_progress=self._github_check_row.set_subtitle,
        )

    def _on_check_update_installed(
        self,
        parent: Gtk.Window | None,
        uuid: str | None,
        error: str | None,
    ) -> None:
        self._github_check_btn.set_sensitive(True)
        if error or not uuid:
            self._github_check_row.set_subtitle(error or "Update failed.")
            return
        self._github_check_row.set_subtitle("Up to date.")
        self._github_update_row.set_visible(False)
        prompt_shell_restart(parent, action="updated")

    def _on_uninstall_clicked(self, _btn: Gtk.Button) -> None:
        if self._active_uuid is None:
            return
        info = self._all_extensions.get(self._active_uuid, {})
        path_str = info.get("path") or ""
        if not path_str:
            return
        uuid = self._active_uuid
        path = Path(path_str)
        parent = self.get_root() if isinstance(self.get_root(), Gtk.Window) else None

        dialog = Adw.AlertDialog.new(
            "Uninstall Extension?",
            f"Remove '{info.get('name') or uuid}' from your shell?",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Uninstall")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

        def _on_response(_d: Adw.AlertDialog, response: str) -> None:
            if response != "remove":
                return
            # Best-effort: disable first, then remove the directory.
            self._dbus.disable_extension(
                uuid,
                on_done=lambda _err: self._do_uninstall(uuid, path, parent),
            )

        dialog.connect("response", _on_response)
        if parent is not None:
            dialog.present(parent)

    def _do_uninstall(
        self,
        uuid: str,
        path: Path,
        parent: Gtk.Window | None,
    ) -> None:
        if not remove_extension(path):
            return
        # Drop the provenance entry too (reconcile would also catch it, but
        # do it eagerly so the registry stays in step with the filesystem).
        if self._installer is not None:
            self._installer.registry.remove(uuid)
        # Force-refresh and prompt for logout so gnome-shell forgets it.
        self._dbus.list_extensions()
        if self._active_uuid == uuid:
            self.set_active_extension(None)
        prompt_shell_restart(parent, action="removed")

    def _refresh_in_place(self, uuid: str, info: dict[str, Any]) -> None:
        state = info.get("state", ExtensionState.DISABLED)

        if state == ExtensionState.DISABLING:
            self._pending_disable = False
        elif state == ExtensionState.DISABLED:
            self._pending_disable = False

        text, css = _STATE_LABELS.get(state, ("Unknown", "dim-label"))
        self._state_badge.set_label(text)
        for c in _ALL_STATE_CSS:
            self._state_badge.remove_css_class(c)
        if css:
            self._state_badge.add_css_class(css)

        new_active = state == ExtensionState.ENABLED
        if new_active and self._pending_disable:
            new_active = False

        self._switch.handler_block_by_func(self._on_switch_toggled)
        self._switch.set_active(new_active)
        self._switch.set_sensitive(state not in _TRANSIENT_STATES)
        self._switch.handler_unblock_by_func(self._on_switch_toggled)

