import json
import logging
from pathlib import Path
from typing import Any

import gi

gi.require_version("GLib", "2.0")
gi.require_version("GObject", "2.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import GLib, GObject, Gtk, Pango

from app.core.bridge_manager import BRIDGE_UUID
from app.core.dbus_client import DBusClient, ExtensionState
from app.core.github_installer import EXTENSIONS_ROOT, GitHubInstaller, GitHubSource

_log = logging.getLogger(__name__)

_ALL_DOT_CSS = ("success", "error", "dim-label", "warning")

_FAVORITES_PATH = Path(GLib.get_user_config_dir()) / "gse-profiler" / "favorites.json"


def _load_favorites() -> set[str]:
    try:
        if _FAVORITES_PATH.exists():
            return set(json.loads(_FAVORITES_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return set()


def _save_favorites(favorites: set[str]) -> None:
    try:
        _FAVORITES_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FAVORITES_PATH.write_text(
            json.dumps(sorted(favorites), indent=2), encoding="utf-8"
        )
    except Exception as exc:
        _log.warning("Failed to save favorites: %s", exc)


class _ExtRow(Gtk.ListBoxRow):
    def __init__(self, uuid: str, info: dict[str, Any]) -> None:
        super().__init__()
        self.uuid = uuid

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_margin_top(7)
        box.set_margin_bottom(7)

        self._dot = Gtk.Label(label="●")
        self._dot.set_valign(Gtk.Align.CENTER)
        box.append(self._dot)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text_box.set_hexpand(True)

        self._name_label = Gtk.Label()
        self._name_label.set_halign(Gtk.Align.START)
        self._name_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._name_label.set_hexpand(True)
        text_box.append(self._name_label)

        self._uuid_label = Gtk.Label()
        self._uuid_label.set_halign(Gtk.Align.START)
        self._uuid_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._uuid_label.set_hexpand(True)
        self._uuid_label.add_css_class("caption")
        self._uuid_label.add_css_class("dim-label")
        text_box.append(self._uuid_label)

        box.append(text_box)

        # GitHub source badge — hidden by default, shown for github-sourced ext.
        self._github_icon = Gtk.Image.new_from_icon_name(
            "folder-download-symbolic"
        )
        self._github_icon.set_valign(Gtk.Align.CENTER)
        self._github_icon.set_tooltip_text("Installed from GitHub")
        self._github_icon.add_css_class("dim-label")
        self._github_icon.set_visible(False)
        box.append(self._github_icon)

        # Update-available dot — only when github_installer found a newer SHA.
        self._update_dot = Gtk.Label(label="●")
        self._update_dot.set_valign(Gtk.Align.CENTER)
        self._update_dot.add_css_class("accent")
        self._update_dot.set_tooltip_text("Update available")
        self._update_dot.set_visible(False)
        box.append(self._update_dot)

        self.set_child(box)
        self.update(info)

    @property
    def display_name(self) -> str:
        return self._name_label.get_label() or self.uuid

    def update(
        self,
        info: dict[str, Any],
        *,
        github_source: GitHubSource | None = None,
        has_update: bool = False,
    ) -> None:
        name = info.get("name") or self.uuid
        state = info.get("state", ExtensionState.DISABLED)
        self._name_label.set_label(name)
        self._uuid_label.set_label(self.uuid)
        for css in _ALL_DOT_CSS:
            self._dot.remove_css_class(css)
        if state == ExtensionState.ENABLED:
            self._dot.add_css_class("success")
        elif state == ExtensionState.ERROR:
            self._dot.add_css_class("error")
        elif state in (ExtensionState.OUT_OF_DATE,):
            self._dot.add_css_class("warning")
        else:
            self._dot.add_css_class("dim-label")
        self.set_github_source(github_source)
        self.set_update_available(has_update)

    def set_github_source(self, source: GitHubSource | None) -> None:
        if source is None:
            self._github_icon.set_visible(False)
            return
        self._github_icon.set_visible(True)
        self._github_icon.set_tooltip_text(
            f"Installed from github.com/{source.owner}/{source.repo}"
            + (f" @ {source.short_sha}" if source.short_sha else "")
        )

    def set_update_available(self, has_update: bool) -> None:
        self._update_dot.set_visible(has_update)


class ExtensionListView(Gtk.Box):
    """Sidebar extension list with Favorites / User / System / Disabled sections."""

    __gtype_name__ = "ExtensionListView"

    __gsignals__ = {
        "extension-activated": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }

    def __init__(self, dbus_client: DBusClient, installer: GitHubInstaller) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._dbus = dbus_client
        self._installer = installer
        self._active_uuid: str | None = None
        self._rows: dict[str, _ExtRow] = {}
        self._search_text = ""
        self._in_restore = False
        self._favorites: set[str] = _load_favorites()
        self._last_extensions: dict[str, Any] = {}
        self._github_sources: dict[str, GitHubSource] = {}
        self._github_updates: dict[str, str] = {}

        self._build_ui()
        dbus_client.connect("extensions-changed", self._on_extensions_changed)

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._search = Gtk.SearchEntry()
        self._search.set_placeholder_text("Filter extensions…")
        self._search.set_margin_start(8)
        self._search.set_margin_end(8)
        self._search.set_margin_top(8)
        self._search.set_margin_bottom(4)
        self._search.connect("search-changed", self._on_search_changed)
        self.append(self._search)

        self._sections_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self._sections_box)
        self.append(scroll)

        self._fav_section,      self._fav_lb      = self._make_section("Favorites")
        self._user_section,     self._user_lb     = self._make_section("User Extensions")
        self._system_section,   self._system_lb   = self._make_section("System Extensions")
        self._disabled_section, self._disabled_lb = self._make_section("Disabled Extensions")

        for section in (
            self._fav_section,
            self._user_section,
            self._system_section,
            self._disabled_section,
        ):
            self._sections_box.append(section)

        self._listboxes = [
            self._fav_lb,
            self._user_lb,
            self._system_lb,
            self._disabled_lb,
        ]

    def _make_section(self, title: str) -> tuple[Gtk.Box, Gtk.ListBox]:
        title_lbl = Gtk.Label(label=title)
        title_lbl.set_hexpand(True)
        title_lbl.set_halign(Gtk.Align.START)
        title_lbl.add_css_class("heading")
        title_lbl.add_css_class("dim-label")

        arrow = Gtk.Image.new_from_icon_name("pan-down-symbolic")

        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        header_box.set_margin_start(4)
        header_box.set_margin_end(4)
        header_box.append(title_lbl)
        header_box.append(arrow)

        toggle_btn = Gtk.Button()
        toggle_btn.set_child(header_box)
        toggle_btn.add_css_class("flat")
        toggle_btn.set_margin_start(4)
        toggle_btn.set_margin_end(4)
        toggle_btn.set_margin_top(6)
        toggle_btn.set_margin_bottom(2)

        lb = Gtk.ListBox()
        lb.set_selection_mode(Gtk.SelectionMode.SINGLE)
        lb.add_css_class("boxed-list")
        lb.set_margin_start(8)
        lb.set_margin_end(8)
        lb.set_margin_bottom(4)
        lb.connect("row-selected", self._on_row_selected)

        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_transition_duration(200)
        revealer.set_reveal_child(True)
        revealer.set_child(lb)

        def _on_toggle(_btn: Gtk.Button) -> None:
            expanded = not revealer.get_reveal_child()
            revealer.set_reveal_child(expanded)
            arrow.set_from_icon_name(
                "pan-down-symbolic" if expanded else "pan-end-symbolic"
            )

        toggle_btn.connect("clicked", _on_toggle)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_visible(False)
        box.append(toggle_btn)
        box.append(revealer)
        return box, lb

    # ── Public API ─────────────────────────────────────────────────────────

    def set_active_uuid(self, uuid: str | None) -> None:
        """Highlight the given extension without emitting extension-activated."""
        self._active_uuid = uuid
        self._restore_selection()

    def is_favorite(self, uuid: str) -> bool:
        return uuid in self._favorites

    def mark_github_update_available(self, uuid: str, new_sha: str) -> None:
        """Mark a GitHub-sourced extension as having an upstream update."""
        self._github_updates[uuid] = new_sha
        row = self._rows.get(uuid)
        if row is not None:
            row.set_update_available(True)

    def toggle_favorite(self, uuid: str) -> None:
        if uuid in self._favorites:
            self._favorites.discard(uuid)
        else:
            self._favorites.add(uuid)
        _save_favorites(self._favorites)
        GLib.idle_add(self._rebuild_from_cache)

    def _rebuild_from_cache(self) -> bool:
        self._rebuild(self._last_extensions)
        return bool(GLib.SOURCE_REMOVE)

    # ── Signal handlers ────────────────────────────────────────────────────

    def _on_row_selected(self, lb: Gtk.ListBox, row: _ExtRow | None) -> None:
        if row is None or self._in_restore:
            return
        for other in self._listboxes:
            if other is not lb:
                other.unselect_all()
        self._active_uuid = row.uuid
        self.emit("extension-activated", row.uuid)

    def _on_extensions_changed(self, _dbus: DBusClient, extensions: dict[str, Any]) -> None:
        self._last_extensions = extensions
        self._rebuild(extensions)

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._search_text = entry.get_text().lower()
        self._apply_filter()

    # ── Internal helpers ───────────────────────────────────────────────────

    def _rebuild(self, extensions: dict[str, Any]) -> None:
        for lb in self._listboxes:
            child = lb.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                lb.remove(child)
                child = nxt
        self._rows.clear()

        # Recompute which extensions came from GitHub.  The registry is the
        # source of truth; reconcile it against the on-disk extension dirs
        # first (not the live D-Bus list — a just-installed extension is on
        # disk but unknown to gnome-shell until the next session), then
        # annotate the rows that actually exist in the current list.
        self._installer.registry.reconcile(EXTENSIONS_ROOT)
        self._github_sources = {
            uuid: src
            for uuid, src in self._installer.registry.all().items()
            if uuid in extensions
        }
        # Drop stale update flags for extensions that no longer exist.
        self._github_updates = {
            uuid: sha
            for uuid, sha in self._github_updates.items()
            if uuid in self._github_sources
        }

        fav_items:      list[tuple[str, dict]] = []
        user_items:     list[tuple[str, dict]] = []
        system_items:   list[tuple[str, dict]] = []
        disabled_items: list[tuple[str, dict]] = []

        for uuid, info in sorted(
            extensions.items(),
            key=lambda kv: (kv[1].get("name") or kv[0]).lower(),
        ):
            if uuid == BRIDGE_UUID:
                continue
            if uuid in self._favorites:
                fav_items.append((uuid, info))
                continue
            state = info.get("state", ExtensionState.DISABLED)
            ext_type = info.get("type", 2)
            if state == ExtensionState.ENABLED:
                if ext_type == 1:
                    system_items.append((uuid, info))
                else:
                    user_items.append((uuid, info))
            else:
                disabled_items.append((uuid, info))

        for lb, items in (
            (self._fav_lb,      fav_items),
            (self._user_lb,     user_items),
            (self._system_lb,   system_items),
            (self._disabled_lb, disabled_items),
        ):
            for uuid, info in items:
                row = _ExtRow(uuid, info)
                row.set_github_source(self._github_sources.get(uuid))
                row.set_update_available(uuid in self._github_updates)
                lb.append(row)
                self._rows[uuid] = row

        self._fav_section.set_visible(bool(fav_items))
        self._user_section.set_visible(bool(user_items))
        self._system_section.set_visible(bool(system_items))
        self._disabled_section.set_visible(bool(disabled_items))

        self._apply_filter()
        self._restore_selection()

    def _apply_filter(self) -> None:
        text = self._search_text
        for uuid, row in self._rows.items():
            if text:
                name = row.display_name.lower()
                row.set_visible(text in name or text in uuid.lower())
            else:
                row.set_visible(True)

        for section, lb in (
            (self._fav_section,      self._fav_lb),
            (self._user_section,     self._user_lb),
            (self._system_section,   self._system_lb),
            (self._disabled_section, self._disabled_lb),
        ):
            child = lb.get_first_child()
            has_visible = False
            while child:
                if isinstance(child, _ExtRow) and child.get_visible():
                    has_visible = True
                    break
                child = child.get_next_sibling()
            section.set_visible(has_visible)

    def _restore_selection(self) -> None:
        self._in_restore = True
        try:
            if not self._active_uuid or self._active_uuid not in self._rows:
                for lb in self._listboxes:
                    lb.unselect_all()
                return
            target = self._rows[self._active_uuid]
            for lb in self._listboxes:
                if target.get_parent() is lb:
                    lb.select_row(target)
                else:
                    lb.unselect_all()
        finally:
            self._in_restore = False
