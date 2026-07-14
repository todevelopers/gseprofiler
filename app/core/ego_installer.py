"""Install / update GNOME Shell extensions from extensions.gnome.org (EGO).

Mirrors :class:`app.core.github_installer.GitHubInstaller`: same async shape
(Soup on the main loop, extraction on a worker thread, results marshalled back
via :func:`GLib.idle_add`), the same ``installed`` / ``update-available`` /
``error`` signals, and the same shared :class:`~app.core.source_registry.SourceRegistry`
instance for provenance.

EGO ships a ``.shell-extension.zip`` whose files sit at the archive root
(``metadata.json`` at top level, GSettings schemas already compiled), so the
extraction path is simpler than GitHub's tarball handling — no git-metadata
filtering, no subdirectory resolution.
"""

from __future__ import annotations

import io
import json
import logging
import tempfile
import threading
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("GObject", "2.0")
gi.require_version("Soup", "3.0")
from gi.repository import Gio, GLib, GObject, Soup

from app.core.ego_client import EgoClient
from app.core.ego_source import EGO_BASE_URL, EgoSource
from app.core.extension_install import (
    EXTENSIONS_ROOT,
    InstallError,
    emit_progress,
    http_error,
    install_into_place,
    safe_extract_zip,
)
from app.core.source_registry import SourceRegistry

_log = logging.getLogger(__name__)

_USER_AGENT = "gse-profiler"

_InstallCallback = Callable[[str | None, str | None], None]
#: ``on_done(new_version, error)``: ``new_version`` is the newer EGO version
#: (as a string) when an update is available; both ``None`` means up to date.
_CheckCallback = Callable[[str | None, str | None], None]
_ProgressCallback = Callable[[str], None]


class EgoInstaller(GObject.Object):
    """Async installer / updater for extensions.gnome.org-sourced extensions.

    Signals:
        installed (uuid)                    — install or update succeeded
        update-available (uuid, new_version) — a newer EGO version exists
        error (message)                     — install / update / check failed
    """

    __gtype_name__ = "EgoInstaller"

    @GObject.Signal(arg_types=(str,))
    def installed(self, uuid: str) -> None:
        """Emitted after a successful install or update."""

    @GObject.Signal(arg_types=(str, str))
    def update_available(self, uuid: str, new_version: str) -> None:
        """Emitted when an installed EGO-sourced ext has a newer version."""

    @GObject.Signal(arg_types=(str,))
    def error(self, message: str) -> None:
        """Emitted on install / update / check failure."""

    def __init__(
        self,
        registry: SourceRegistry | None = None,
        client: EgoClient | None = None,
    ) -> None:
        super().__init__()
        self._session = Soup.Session()
        self._session.set_user_agent(_USER_AGENT)
        self._client = client or EgoClient()
        # In-memory cache: uuid -> newer EGO version known to be available.
        self._known_updates: dict[str, int] = {}
        self._registry = registry or SourceRegistry()

    @property
    def registry(self) -> SourceRegistry:
        """The provenance registry, shared with the UI for source lookups."""
        return self._registry

    @property
    def client(self) -> EgoClient:
        """The EGO API client, shared with the install dialog's search."""
        return self._client

    # ── Public API ────────────────────────────────────────────────────────

    def install(
        self,
        uuid: str,
        shell_version: str | None,
        on_done: _InstallCallback | None = None,
        on_progress: _ProgressCallback | None = None,
    ) -> None:
        """Install an extension from EGO by UUID (async).

        Calls ``on_done(uuid, error_message)`` when finished (exactly one is
        set).  ``update`` re-uses this pipeline; the original install
        timestamp is preserved when re-installing.
        """
        _log.info("Installing %s from extensions.gnome.org", uuid)
        emit_progress(on_progress, "Looking up extension…")
        self._client.fetch_info(
            uuid,
            shell_version,
            lambda info, err: self._on_info_for_install(
                info, err, shell_version, on_done, on_progress
            ),
        )

    def update(
        self,
        source: EgoSource,
        shell_version: str | None,
        on_done: _InstallCallback | None = None,
        on_progress: _ProgressCallback | None = None,
    ) -> None:
        """Re-install the newest compatible version.  ``installed_at`` is kept."""
        self.install(source.uuid, shell_version, on_done, on_progress)

    def has_update(self, uuid: str) -> str | None:
        """Return the cached newer version (as a string) if one is known."""
        version = self._known_updates.get(uuid)
        return str(version) if version is not None else None

    def check_updates(
        self, extensions: dict[str, dict[str, Any]], shell_version: str | None
    ) -> None:
        """Query EGO for every installed EGO-sourced extension."""
        for uuid, src in self._registry.all().items():
            if isinstance(src, EgoSource) and uuid in extensions:
                self._check_one(uuid, src, shell_version)

    def check_update(
        self,
        uuid: str,
        source: EgoSource,
        shell_version: str | None,
        on_done: _CheckCallback | None = None,
    ) -> None:
        """Check a single EGO-sourced extension for a newer version."""
        self._check_one(uuid, source, shell_version, on_done)

    # ── Install pipeline ──────────────────────────────────────────────────

    def _on_info_for_install(
        self,
        info: dict[str, Any] | None,
        error: str | None,
        shell_version: str | None,
        on_done: _InstallCallback | None,
        on_progress: _ProgressCallback | None,
    ) -> None:
        if error is not None or info is None:
            self._fail(on_done, error or "Could not look up the extension.")
            return
        # Refuse extensions the running shell can't load.  EGO's info reply may
        # still carry a download_url for the latest (incompatible) upload, so
        # gate on the shell_version_map rather than trusting download_url alone.
        svm = info.get("shell_version_map") or {}
        if shell_version and shell_version not in svm:
            self._fail(
                on_done,
                f"This extension is not compatible with GNOME Shell {shell_version}.",
            )
            return
        download_url = info.get("download_url") or ""
        if not download_url:
            self._fail(
                on_done,
                "This extension has no download compatible with your GNOME Shell "
                "version.",
            )
            return
        emit_progress(on_progress, "Downloading archive…")
        self._download_zip(info, download_url, on_done, on_progress)

    def _download_zip(
        self,
        info: dict[str, Any],
        download_url: str,
        on_done: _InstallCallback | None,
        on_progress: _ProgressCallback | None,
    ) -> None:
        # ``download_url`` is a site-relative path (``/download-extension/...``).
        url = download_url if download_url.startswith("http") else EGO_BASE_URL + download_url
        msg = Soup.Message.new("GET", url)
        self._session.send_and_read_async(
            msg,
            GLib.PRIORITY_DEFAULT,
            None,
            self._on_zip,
            (info, msg, on_done, on_progress),
        )

    def _on_zip(
        self,
        session: Soup.Session,
        result: Gio.AsyncResult,
        user_data: tuple[dict[str, Any], Soup.Message, _InstallCallback | None, _ProgressCallback | None],
    ) -> None:
        info, msg, on_done, on_progress = user_data
        try:
            body = session.send_and_read_finish(result)
        except GLib.Error as exc:
            self._fail(on_done, f"Download failed: {exc.message}")
            return
        if msg.get_status() != Soup.Status.OK:
            self._fail(on_done, http_error(msg, b"", "download the extension"))
            return
        data = bytes(body.get_data() or b"")
        if not data:
            self._fail(on_done, "Empty archive from extensions.gnome.org.")
            return
        emit_progress(on_progress, "Extracting files…")
        threading.Thread(
            target=self._extract_install_blocking,
            args=(info, data, on_done, on_progress),
            daemon=True,
        ).start()

    # ── Worker thread ─────────────────────────────────────────────────────

    def _extract_install_blocking(
        self,
        info: dict[str, Any],
        zip_bytes: bytes,
        on_done: _InstallCallback | None,
        on_progress: _ProgressCallback | None,
    ) -> None:
        try:
            uuid = _do_extract_install_zip(zip_bytes, on_progress=on_progress)
        except InstallError as exc:
            GLib.idle_add(self._fail, on_done, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - log+surface unknowns
            _log.exception("Unexpected EGO install error")
            GLib.idle_add(self._fail, on_done, f"Install failed: {exc}")
            return
        # EGO addresses extensions by UUID; the info UUID must match what the
        # archive actually contains, or we'd record the wrong provenance.
        if uuid != info.get("uuid"):
            _log.warning(
                "Installed UUID %s differs from requested %s", uuid, info.get("uuid")
            )
        GLib.idle_add(self._finish_install, uuid, info, on_done)

    def _finish_install(
        self,
        uuid: str,
        info: dict[str, Any],
        on_done: _InstallCallback | None,
    ) -> bool:
        existing = self._registry.get(uuid)
        installed_at = (
            existing.installed_at
            if isinstance(existing, EgoSource)
            else datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        self._registry.set(
            uuid,
            EgoSource(
                pk=int(info["pk"]),
                uuid=uuid,
                version=int(info["version"]),
                version_tag=int(info["version_tag"]),
                name=str(info.get("name", uuid)),
                installed_at=installed_at,
                description=str(info.get("description", "")),
            ),
        )
        self._known_updates.pop(uuid, None)
        self.emit("installed", uuid)
        if on_done:
            on_done(uuid, None)
        return False  # GLib.SOURCE_REMOVE

    def _fail(self, on_done: _InstallCallback | None, message: str) -> bool:
        _log.warning("EGO install error: %s", message)
        self.emit("error", message)
        if on_done:
            on_done(None, message)
        return False  # GLib.SOURCE_REMOVE

    # ── Update detection ──────────────────────────────────────────────────

    def _check_one(
        self,
        uuid: str,
        src: EgoSource,
        shell_version: str | None,
        on_done: _CheckCallback | None = None,
    ) -> None:
        def report(info: dict[str, Any] | None, error: str | None) -> None:
            if error is not None or info is None:
                _log.info("EGO update check for %s failed: %s", uuid, error)
                if on_done:
                    on_done(None, error or "Update check failed.")
                return
            new_version = int(info.get("version", 0))
            if new_version <= src.version:
                if on_done:
                    on_done(None, None)
                return
            self._known_updates[uuid] = new_version
            self.emit("update-available", uuid, str(new_version))
            if on_done:
                on_done(str(new_version), None)

        self._client.fetch_info(uuid, shell_version, report)


# ─── Blocking install body (importable for unit tests) ────────────────────


def _do_extract_install_zip(
    zip_bytes: bytes,
    extensions_root: Path | None = None,
    on_progress: _ProgressCallback | None = None,
) -> str:
    """Extract an EGO ``.shell-extension.zip`` and move it into place.

    Returns the extension UUID from its ``metadata.json``.  Raises
    :class:`InstallError` on a bad archive or missing/invalid metadata.
    """
    root_dir = extensions_root or EXTENSIONS_ROOT

    with tempfile.TemporaryDirectory(prefix="gse-profiler-ego-") as tmp:
        tmp_path = Path(tmp)
        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                safe_extract_zip(zf, extract_dir)
        except (zipfile.BadZipFile, OSError) as exc:
            raise InstallError(f"Bad archive: {exc}") from exc

        src_root = _locate_extension(extract_dir)

        meta_path = src_root / "metadata.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise InstallError(f"metadata.json is not valid JSON: {exc}") from exc
        uuid = meta.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            raise InstallError("metadata.json has no valid 'uuid' field.")

        install_into_place(src_root, root_dir, uuid, on_progress=on_progress)
        return uuid


def _locate_extension(extract_dir: Path) -> Path:
    """Find the directory holding ``metadata.json`` in an extracted EGO zip.

    EGO archives put ``metadata.json`` at the root; fall back to a single
    match elsewhere in the tree for robustness.
    """
    if (extract_dir / "metadata.json").is_file():
        return extract_dir
    matches = sorted(
        extract_dir.rglob("metadata.json"),
        key=lambda p: (len(p.relative_to(extract_dir).parts), str(p)),
    )
    if not matches:
        raise InstallError(
            "Not a GNOME Shell extension (no metadata.json in the archive)."
        )
    return matches[0].parent
