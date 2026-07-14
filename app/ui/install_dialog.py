"""Unified dialog for installing a GNOME Shell extension.

Two tabs share one dialog:

- **GitHub** — paste an ``owner/repo`` or repository URL (unchanged from the
  original GitHub-only dialog).
- **extensions.gnome.org** — search-as-you-type against the EGO registry and
  install the selected result.

Both flows share the header **Install** button (which acts on the active tab)
and a single status/spinner row, and on success both close the dialog and
prompt for the shell restart via :func:`app.core.shell_restart.prompt_shell_restart`.
"""

from __future__ import annotations

import logging
from typing import Any

import gi

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk, Pango

from app.core.ego_client import is_compatible
from app.core.ego_installer import EgoInstaller
from app.core.github_installer import GitHubInstaller
from app.core.shell_restart import prompt_shell_restart

_log = logging.getLogger(__name__)

_SEARCH_DEBOUNCE_MS = 300


class _EgoResultRow(Gtk.ListBoxRow):
    """One extensions.gnome.org search result."""

    def __init__(self, result: dict[str, Any], *, compatible: bool) -> None:
        super().__init__()
        self.uuid: str = result["uuid"]
        self.result = result
        self.compatible = compatible

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_margin_top(7)
        box.set_margin_bottom(7)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name = Gtk.Label(label=result.get("name") or self.uuid)
        name.set_halign(Gtk.Align.START)
        name.set_hexpand(True)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.add_css_class("heading")
        top.append(name)
        if not compatible:
            badge = Gtk.Label(label="Not compatible")
            badge.set_valign(Gtk.Align.CENTER)
            badge.add_css_class("caption")
            badge.add_css_class("warning")
            top.append(badge)
        box.append(top)

        creator = result.get("creator") or ""
        description = result.get("description") or ""
        subtitle = " — ".join(part for part in (f"by {creator}" if creator else "", description) if part)
        if subtitle:
            sub = Gtk.Label(label=subtitle)
            sub.set_halign(Gtk.Align.START)
            sub.set_ellipsize(Pango.EllipsizeMode.END)
            sub.add_css_class("caption")
            sub.add_css_class("dim-label")
            box.append(sub)

        self.set_child(box)


class InstallDialog(Adw.Dialog):
    """Adw.Dialog with GitHub and extensions.gnome.org install tabs."""

    __gtype_name__ = "InstallDialog"

    def __init__(
        self,
        github_installer: GitHubInstaller,
        ego_installer: EgoInstaller,
        shell_version: str | None,
    ) -> None:
        super().__init__()
        self._github = github_installer
        self._ego = ego_installer
        self._shell_version = shell_version
        self._busy = False
        self._search_source: int = 0
        self._search_cancellable: Gio.Cancellable | None = None

        self.set_title("Install Extension")
        self.set_content_width(500)
        self.set_content_height(560)
        self.set_can_close(True)
        self._build_ui()
        self.connect("closed", self._on_closed)

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()

        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_show_start_title_buttons(False)

        # A compact close button (instead of a "Cancel" label) leaves the full
        # header width to the view switcher so "extensions.gnome.org" fits.
        self._close_btn = Gtk.Button(icon_name="window-close-symbolic")
        self._close_btn.add_css_class("flat")
        self._close_btn.set_tooltip_text("Close")
        self._close_btn.connect("clicked", lambda _b: self.close())
        header.pack_start(self._close_btn)

        self._stack = Adw.ViewStack()
        github_page = self._stack.add_titled(
            self._build_github_page(), "github", "GitHub"
        )
        github_page.set_icon_name("source-github-symbolic")
        ego_page = self._stack.add_titled(
            self._build_ego_page(), "ego", "extensions.gnome.org"
        )
        ego_page.set_icon_name("source-gnome-symbolic")
        self._stack.connect("notify::visible-child-name", self._on_tab_changed)

        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self._stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        toolbar.add_top_bar(header)

        # ── Shared status row (below the stacked pages) ────────────────────
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self._stack)

        self._status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._status_box.set_margin_start(18)
        self._status_box.set_margin_end(18)
        self._status_box.set_margin_top(6)
        self._status_box.set_margin_bottom(12)
        self._status_box.set_visible(False)

        self._spinner = Gtk.Spinner()
        self._spinner.set_visible(False)
        self._status_box.append(self._spinner)

        self._status_label = Gtk.Label(xalign=0.0)
        self._status_label.set_wrap(True)
        self._status_label.set_hexpand(True)
        self._status_box.append(self._status_label)
        content.append(self._status_box)

        toolbar.set_content(content)

        # ── Bottom action bar (Install in the corner) ──────────────────────
        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        bottom.set_margin_start(12)
        bottom.set_margin_end(12)
        bottom.set_margin_top(6)
        bottom.set_margin_bottom(12)

        self._install_btn = Gtk.Button(label="Install")
        self._install_btn.add_css_class("suggested-action")
        self._install_btn.set_hexpand(True)
        self._install_btn.set_halign(Gtk.Align.END)
        self._install_btn.connect("clicked", self._on_install_clicked)
        bottom.append(self._install_btn)
        toolbar.add_bottom_bar(bottom)

        self.set_child(toolbar)

        self._update_install_sensitivity()

    def _build_github_page(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_start(18)
        body.set_margin_end(18)
        body.set_margin_top(12)
        body.set_margin_bottom(6)

        intro = Gtk.Label(
            label=(
                "Download and install a GNOME Shell extension from a GitHub "
                "repository. The default branch's latest commit is used. If "
                "the extension lives in a subdirectory, paste its folder URL "
                "and that folder is installed instead."
            ),
            wrap=True,
            xalign=0.0,
        )
        intro.add_css_class("dim-label")
        body.append(intro)

        group = Adw.PreferencesGroup()
        self._repo_row = Adw.EntryRow()
        self._repo_row.set_title("Repository")
        self._repo_row.set_input_purpose(Gtk.InputPurpose.URL)
        self._repo_row.connect("entry-activated", self._on_install_clicked)
        self._repo_row.connect("changed", lambda _r: self._update_install_sensitivity())
        group.add(self._repo_row)
        body.append(group)

        hint = Gtk.Label(
            label=(
                "Examples: owner/repo, https://github.com/owner/repo, "
                "or https://github.com/owner/repo/tree/main/src"
            ),
            wrap=True,
            xalign=0.0,
        )
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        body.append(hint)
        return body

    def _build_ego_page(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.set_margin_start(12)
        body.set_margin_end(12)
        body.set_margin_top(12)
        body.set_margin_bottom(6)

        self._search = Gtk.SearchEntry()
        self._search.set_placeholder_text("Search extensions.gnome.org…")
        self._search.connect("search-changed", self._on_search_changed)
        self._search.connect("activate", lambda _s: self._focus_first_result())
        body.append(self._search)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._results = Gtk.ListBox()
        self._results.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._results.add_css_class("boxed-list")
        self._results.connect("row-selected", lambda _lb, _r: self._update_install_sensitivity())
        self._results.connect("row-activated", self._on_result_activated)

        placeholder = Gtk.Label(label="Type an extension name to search.")
        placeholder.add_css_class("dim-label")
        placeholder.set_margin_top(24)
        placeholder.set_margin_bottom(24)
        self._results.set_placeholder(placeholder)

        scroll.set_child(self._results)
        body.append(scroll)
        return body

    # ── State helpers ─────────────────────────────────────────────────────

    def _on_tab_changed(self, _stack: Adw.ViewStack, _pspec: object) -> None:
        self._update_install_sensitivity()

    def _update_install_sensitivity(self) -> None:
        if self._busy:
            self._install_btn.set_sensitive(False)
            return
        name = self._stack.get_visible_child_name()
        if name == "ego":
            self._install_btn.set_sensitive(self._results.get_selected_row() is not None)
        else:
            self._install_btn.set_sensitive(bool(self._repo_row.get_text().strip()))

    def _set_busy(self, busy: bool, label: str = "") -> None:
        self._busy = busy
        self._close_btn.set_sensitive(True)
        self._repo_row.set_sensitive(not busy)
        self._search.set_sensitive(not busy)
        self._results.set_sensitive(not busy)
        self._spinner.set_visible(busy)
        if busy:
            self._spinner.start()
        else:
            self._spinner.stop()
        self._status_label.remove_css_class("error")
        if busy:
            self._status_label.add_css_class("dim-label")
            self._status_label.set_label(label or "Working…")
            self._status_box.set_visible(True)
        else:
            self._status_box.set_visible(bool(label))
            if label:
                self._status_label.set_label(label)
        self._update_install_sensitivity()

    def _show_status(self, message: str, *, error: bool) -> None:
        self._spinner.stop()
        self._spinner.set_visible(False)
        self._status_label.remove_css_class("dim-label")
        self._status_label.remove_css_class("error")
        if error:
            self._status_label.add_css_class("error")
        else:
            self._status_label.add_css_class("dim-label")
        self._status_label.set_label(message)
        self._status_box.set_visible(bool(message))

    def _show_error(self, message: str) -> None:
        self._set_busy(False)
        self._show_status(message, error=True)

    # ── Install ───────────────────────────────────────────────────────────

    def _on_install_clicked(self, _widget: Gtk.Widget) -> None:
        if self._busy:
            return
        if self._stack.get_visible_child_name() == "ego":
            self._install_ego()
        else:
            self._install_github()

    def _install_github(self) -> None:
        repo = self._repo_row.get_text().strip()
        if not repo:
            self._show_error("Please enter a GitHub repository.")
            return
        self._set_busy(True, "Checking repository…")
        self._github.install(
            repo, on_done=self._on_install_done, on_progress=self._on_progress
        )

    def _install_ego(self) -> None:
        row = self._results.get_selected_row()
        if not isinstance(row, _EgoResultRow):
            self._show_error("Select an extension to install.")
            return
        if not row.compatible:
            self._show_error(
                "This extension is not compatible with your GNOME Shell version."
            )
            return
        self._set_busy(True, "Looking up extension…")
        self._ego.install(
            row.uuid,
            self._shell_version,
            on_done=self._on_install_done,
            on_progress=self._on_progress,
        )

    def _on_result_activated(self, _lb: Gtk.ListBox, _row: Gtk.ListBoxRow) -> None:
        if not self._busy:
            self._install_ego()

    def _on_progress(self, message: str) -> None:
        if self._busy:
            self._status_label.set_label(message)

    def _on_install_done(self, uuid: str | None, error: str | None) -> None:
        if error is not None or uuid is None:
            self._show_error(error or "Install failed.")
            return
        parent = self.get_root() if isinstance(self.get_root(), Gtk.Window) else None
        self.close()
        GLib.idle_add(
            _show_restart_prompt,
            parent if isinstance(parent, Gtk.Window) else None,
        )

    # ── Search ────────────────────────────────────────────────────────────

    def _on_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        if self._search_source:
            GLib.source_remove(self._search_source)
            self._search_source = 0
        self._search_source = GLib.timeout_add(
            _SEARCH_DEBOUNCE_MS, self._run_search
        )

    def _run_search(self) -> bool:
        self._search_source = 0
        term = self._search.get_text().strip()
        # Cancel any in-flight search so its late result can't overwrite this one.
        if self._search_cancellable is not None:
            self._search_cancellable.cancel()
            self._search_cancellable = None
        if not term:
            self._clear_results()
            self._status_box.set_visible(False)
            return False  # GLib.SOURCE_REMOVE
        self._show_status("Searching…", error=False)
        self._spinner.set_visible(True)
        self._spinner.start()
        self._search_cancellable = Gio.Cancellable()
        # Don't filter the query by shell version — show every match and badge
        # the incompatible ones, so developers on newer shells still see (and
        # can inspect) extensions that haven't declared support yet.
        self._ego.client.search(
            term,
            None,
            self._on_search_results,
            self._search_cancellable,
        )
        return False  # GLib.SOURCE_REMOVE

    def _on_search_results(
        self, results: list[dict[str, Any]] | None, error: str | None
    ) -> None:
        self._spinner.stop()
        self._spinner.set_visible(False)
        if error is not None:
            self._show_status(error, error=True)
            return
        self._clear_results()
        if not results:
            self._show_status("No extensions found.", error=False)
            return
        self._status_box.set_visible(False)
        for result in results:
            compatible = is_compatible(
                result.get("shell_version_map") or {}, self._shell_version
            )
            self._results.append(_EgoResultRow(result, compatible=compatible))
        self._update_install_sensitivity()

    def _clear_results(self) -> None:
        child = self._results.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._results.remove(child)
            child = nxt
        self._update_install_sensitivity()

    def _focus_first_result(self) -> None:
        first = self._results.get_row_at_index(0)
        if first is not None:
            self._results.select_row(first)
            first.grab_focus()

    # ── Cleanup ───────────────────────────────────────────────────────────

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        if self._search_source:
            GLib.source_remove(self._search_source)
            self._search_source = 0
        if self._search_cancellable is not None:
            self._search_cancellable.cancel()
            self._search_cancellable = None


def _show_restart_prompt(parent: Gtk.Window | None) -> bool:
    prompt_shell_restart(parent, action="installed")
    return False  # GLib.SOURCE_REMOVE
