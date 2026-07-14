"""Low-level install plumbing shared by the GitHub and EGO installers.

Both installers download an archive, extract it to a staging area, validate
``metadata.json``, compile any GSettings schemas, and move the result into the
per-user extensions directory.  The archive-specific bits differ (GitHub ships
a ``.tar.gz`` wrapped in a single top-level dir and needs git-metadata
filtering; EGO ships a ``.shell-extension.zip`` with files at the archive root),
but the leaf operations below are identical, so they live here and both
installers import them.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tarfile
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Soup", "3.0")
from gi.repository import GLib, Soup

_log = logging.getLogger(__name__)

#: ``on_progress(message)`` — called on the GTK main loop at each pipeline stage.
ProgressCallback = Callable[[str], None]

_IN_FLATPAK: bool = os.path.exists("/.flatpak-info")

# Inside a Flatpak sandbox ``GLib.get_user_data_dir()`` returns the app-scoped
# directory (~/.var/app/<id>/data), not the host ~/.local/share that
# gnome-shell actually watches.  Use the real home-relative path when
# sandboxed.  Identical to the rationale in ``bridge_manager``.
EXTENSIONS_ROOT: Path = (
    Path.home() / ".local" / "share" / "gnome-shell" / "extensions"
    if _IN_FLATPAK
    else Path(GLib.get_user_data_dir()) / "gnome-shell" / "extensions"
)


class InstallError(Exception):
    """Raised when an install/update fails with a user-presentable message."""


# ─── Archive extraction (path-traversal-guarded) ──────────────────────────


def safe_extract_tar(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract ``tar`` into ``dest`` with path-traversal protection."""
    # ``filter='data'`` is the supported safe extraction mode in 3.12+.
    # On older 3.11.x without the filter API, fall back to manual checks.
    if hasattr(tarfile, "data_filter"):
        tar.extractall(dest, filter="data")  # type: ignore[call-arg]
        return
    dest_resolved = dest.resolve()
    for member in tar.getmembers():
        member_path = (dest / member.name).resolve()
        try:
            member_path.relative_to(dest_resolved)
        except ValueError as exc:
            raise InstallError(
                f"Tarball contains unsafe path: {member.name}"
            ) from exc
    tar.extractall(dest)


def safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract ``zf`` into ``dest`` with path-traversal protection.

    Rejects absolute paths and any member that would resolve outside
    ``dest`` (``../`` escapes, symlink-style names).  ``zipfile`` has no
    ``data`` filter equivalent, so the check is always manual.
    """
    dest_resolved = dest.resolve()
    for name in zf.namelist():
        # Normalise separators; a zip may legitimately use forward slashes.
        member_path = (dest / name).resolve()
        try:
            member_path.relative_to(dest_resolved)
        except ValueError as exc:
            raise InstallError(f"Archive contains unsafe path: {name}") from exc
    zf.extractall(dest)


# ─── GSettings schema compilation ─────────────────────────────────────────


def compile_schemas(extension_root: Path) -> None:
    """Run ``glib-compile-schemas`` on the extension's ``schemas/`` directory.

    No-op when the extension has no schemas.  Raises :class:`InstallError`
    when ``schemas/*.gschema.xml`` exists but compilation fails — the
    extension will not work without ``gschemas.compiled``.
    """
    schemas_dir = extension_root / "schemas"
    if not schemas_dir.is_dir():
        return
    if not any(schemas_dir.glob("*.gschema.xml")) and not any(
        schemas_dir.glob("*.gschema.override")
    ):
        # Directory exists but no schemas to compile (e.g. only README).
        return
    try:
        result = subprocess.run(
            ["glib-compile-schemas", str(schemas_dir)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        raise InstallError(
            "glib-compile-schemas is not available — cannot compile "
            "GSettings schemas for this extension."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise InstallError(
            "Compiling GSettings schemas timed out."
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise InstallError(
            "Failed to compile GSettings schemas: "
            + (detail or f"exit code {result.returncode}")
        )
    # glib-compile-schemas exits 0 even when individual files are malformed;
    # it just prints "Error on line ..." to stderr and skips them.  Surface
    # those so the developer notices.
    stderr = (result.stderr or "").strip()
    if stderr and ("error" in stderr.lower() or "warning" in stderr.lower()):
        raise InstallError("Failed to compile GSettings schemas: " + stderr)
    _log.info("Compiled GSettings schemas in %s", schemas_dir)


# ─── Move staged extension into the extensions directory ──────────────────


def install_into_place(
    src_root: Path,
    extensions_root: Path | str,
    uuid: str,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """Compile schemas on ``src_root`` and move it to ``extensions_root/uuid``.

    Replaces any existing install of the same UUID.  Compilation runs on the
    staging copy so a failure leaves the current install untouched.  Returns
    the final install path.
    """
    src_root = Path(src_root)
    extensions_root = Path(extensions_root)
    compile_schemas(src_root)
    if on_progress:
        GLib.idle_add(on_progress, "Installing extension…")
    target = extensions_root / uuid
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src_root), str(target))
    _log.info("Installed %s to %s", uuid, target)
    return target


# ─── Progress + HTTP error helpers ────────────────────────────────────────


def emit_progress(callback: ProgressCallback | None, message: str) -> None:
    if callback:
        callback(message)


def http_error(msg: Soup.Message, body: bytes, action: str) -> str:
    """Build a user-presentable message from a failed Soup request.

    ``body`` is the (optional) response body; when the server returns a JSON
    ``message`` field it is folded into the text.  GitHub's rate-limit
    response gets a dedicated hint.
    """
    import json

    status = int(msg.get_status())
    reason = msg.get_reason_phrase() or ""
    detail = ""
    if body:
        try:
            detail = str(json.loads(body).get("message", "")).strip()
        except (json.JSONDecodeError, AttributeError):
            detail = ""
    # Rate-limit gets a specific hint.
    if status == 403:
        remaining = msg.get_response_headers().get_one("X-RateLimit-Remaining")
        if remaining == "0":
            reset = msg.get_response_headers().get_one("X-RateLimit-Reset") or ""
            reset_hint = ""
            if reset.isdigit():
                try:
                    when = datetime.fromtimestamp(int(reset), tz=timezone.utc)
                    reset_hint = f" Try again after {when.strftime('%H:%M UTC')}."
                except (OverflowError, OSError, ValueError):
                    reset_hint = ""
            return (
                f"Rate limit exceeded while trying to {action}.{reset_hint}"
            )
    if detail:
        return f"Failed to {action}: {detail} (HTTP {status} {reason}).".strip()
    return f"Failed to {action}: HTTP {status} {reason}.".strip()
