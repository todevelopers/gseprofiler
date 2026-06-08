"""Modal dialog for installing a GNOME Shell extension from GitHub."""

import logging

import gi

gi.require_version("Adw", "1")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk

from app.core.github_installer import GitHubInstaller
from app.core.shell_restart import prompt_shell_restart

_log = logging.getLogger(__name__)


class GitHubInstallDialog(Adw.Dialog):
    """Adw.Dialog that asks for a GitHub repo, installs it, and prompts logout."""

    __gtype_name__ = "GitHubInstallDialog"

    def __init__(self, installer: GitHubInstaller) -> None:
        super().__init__()
        self._installer = installer
        self._busy = False
        self.set_title("Install from GitHub")
        self.set_content_width(440)
        self.set_can_close(True)
        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()

        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_show_start_title_buttons(False)

        self._cancel_btn = Gtk.Button(label="Cancel")
        self._cancel_btn.connect("clicked", lambda _b: self.close())
        header.pack_start(self._cancel_btn)

        self._install_btn = Gtk.Button(label="Install")
        self._install_btn.add_css_class("suggested-action")
        self._install_btn.connect("clicked", self._on_install_clicked)
        header.pack_end(self._install_btn)

        toolbar.add_top_bar(header)

        # ── Body ──────────────────────────────────────────────────────────
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_start(18)
        body.set_margin_end(18)
        body.set_margin_top(12)
        body.set_margin_bottom(18)

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
        self._entry_row = Adw.EntryRow()
        self._entry_row.set_title("Repository")
        self._entry_row.set_input_purpose(Gtk.InputPurpose.URL)
        self._entry_row.connect("entry-activated", self._on_install_clicked)
        group.add(self._entry_row)
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

        # Status area (error message or progress).
        self._status_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )
        self._status_box.set_visible(False)

        # Gtk.Spinner is widely available; Adw.Spinner only since libadwaita
        # 1.6 — keep the dialog compatible with older targets too.
        self._spinner = Gtk.Spinner()
        self._spinner.set_visible(False)
        self._status_box.append(self._spinner)

        self._status_label = Gtk.Label(xalign=0.0)
        self._status_label.set_wrap(True)
        self._status_label.set_hexpand(True)
        self._status_box.append(self._status_label)

        body.append(self._status_box)

        toolbar.set_content(body)
        self.set_child(toolbar)

        self.set_focus(self._entry_row)
        self.set_default_widget(self._install_btn)

    # ── State helpers ─────────────────────────────────────────────────────

    def _set_busy(self, busy: bool, label: str = "") -> None:
        self._busy = busy
        self._install_btn.set_sensitive(not busy)
        self._entry_row.set_sensitive(not busy)
        self._spinner.set_visible(busy)
        if busy:
            self._spinner.start()
        else:
            self._spinner.stop()
        self._status_label.remove_css_class("error")
        if busy:
            self._status_label.add_css_class("dim-label")
            self._status_label.set_label(label or "Downloading…")
            self._status_box.set_visible(True)
        else:
            self._status_box.set_visible(bool(label))
            if label:
                self._status_label.set_label(label)

    def _show_error(self, message: str) -> None:
        self._set_busy(False)
        self._status_label.remove_css_class("dim-label")
        self._status_label.add_css_class("error")
        self._status_label.set_label(message)
        self._status_box.set_visible(True)

    # ── Signal handlers ───────────────────────────────────────────────────

    def _on_install_clicked(self, _widget: Gtk.Widget) -> None:
        if self._busy:
            return
        repo = self._entry_row.get_text().strip()
        if not repo:
            self._show_error("Please enter a GitHub repository.")
            return
        self._set_busy(True, "Checking repository…")
        self._installer.install(
            repo,
            on_done=self._on_install_done,
            on_progress=self._on_progress,
        )

    def _on_progress(self, message: str) -> None:
        if self._busy:
            self._status_label.set_label(message)

    def _on_install_done(self, uuid: str | None, error: str | None) -> None:
        if error is not None or uuid is None:
            self._show_error(error or "Install failed.")
            return
        parent = self.get_root() if isinstance(self.get_root(), Gtk.Window) else None
        self.close()
        # Schedule the restart prompt to run after the dialog has closed so it
        # gets the correct parent window focus.
        GLib.idle_add(
            _show_restart_prompt,
            parent if isinstance(parent, Gtk.Window) else None,
            uuid,
        )


def _show_restart_prompt(parent: Gtk.Window | None, _uuid: str) -> bool:
    prompt_shell_restart(parent, action="installed")
    return False  # GLib.SOURCE_REMOVE
