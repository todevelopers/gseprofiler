"""Install GNOME Shell extensions from GitHub.

V1 covers: download default-branch tarball from github.com via libsoup3,
validate ``metadata.json``, filter out files unrelated to the extension
(git meta + ``.gitattributes`` ``export-ignore`` + ``.gitignore`` patterns),
install into the extensions directory, and record the source commit SHA in
``metadata.json`` so we can later detect upstream updates.

Network I/O is async via :class:`Soup.Session`.  Extraction + filesystem
mutation runs on a worker thread; results are marshalled back to the GTK
main loop via :func:`GLib.idle_add`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("GObject", "2.0")
gi.require_version("Soup", "3.0")
from gi.repository import Gio, GLib, GObject, Soup

try:
    import pathspec  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - optional at import time
    pathspec = None  # type: ignore[assignment]

_log = logging.getLogger(__name__)

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

#: Custom ``metadata.json`` key that records where this extension came from.
#: GNOME Shell ignores unknown keys (same trick as ``bundle-hash`` for the
#: bridge extension).
SOURCE_KEY = "_gse_profiler_source"

_GITHUB_API = "https://api.github.com"
_USER_AGENT = "gse-profiler"

# Files unconditionally stripped from the installed tree (after applying
# any ``.gitattributes`` / ``.gitignore`` rules).  We delete them last so
# their contents are still available while the filter step is reading them.
_ALWAYS_STRIP_NAMES = frozenset({
    ".gitignore",
    ".gitattributes",
    ".gitmodules",
    ".gitkeep",
    ".gitlab-ci.yml",
})
_ALWAYS_STRIP_DIRS = frozenset({".git", ".github", ".gitlab"})

# Accepts:  owner/repo, https://github.com/owner/repo, .../owner/repo.git,
# .../owner/repo/.  Owner: ASCII alnum + hyphen.  Repo: alnum + . _ -.
_REPO_URL_RE = re.compile(
    r"^(?:https?://github\.com/)?"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})?)/"
    r"(?P<repo>[A-Za-z0-9._-]+?)"
    r"(?:\.git)?/?$"
)


class InstallError(Exception):
    """Raised when an install/update fails with a user-presentable message."""


@dataclass
class GitHubSource:
    """Metadata about where a GitHub-sourced extension came from.

    Persisted into ``metadata.json`` under :data:`SOURCE_KEY`.
    """

    owner: str
    repo: str
    ref: str  # branch name we tracked (default branch in V1)
    commit_sha: str
    installed_at: str  # ISO-8601 UTC

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GitHubSource | None:
        try:
            return cls(
                owner=str(d["owner"]),
                repo=str(d["repo"]),
                ref=str(d["ref"]),
                commit_sha=str(d["commit_sha"]),
                installed_at=str(d.get("installed_at", "")),
            )
        except (KeyError, TypeError):
            return None

    @property
    def html_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"

    @property
    def short_sha(self) -> str:
        return self.commit_sha[:7] if self.commit_sha else ""


# ─── Public helpers ────────────────────────────────────────────────────────


def parse_repo_url(url: str) -> tuple[str, str] | None:
    """Parse a GitHub repo identifier into ``(owner, repo)``, or ``None``."""
    url = (url or "").strip()
    if not url:
        return None
    m = _REPO_URL_RE.match(url)
    if not m:
        return None
    return m.group("owner"), m.group("repo")


def read_source(extension_path: Path) -> GitHubSource | None:
    """Read the recorded :class:`GitHubSource` for an installed extension.

    Returns ``None`` if the extension's ``metadata.json`` is missing, invalid,
    or does not carry our :data:`SOURCE_KEY`.
    """
    try:
        meta = json.loads(
            (extension_path / "metadata.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    src = meta.get(SOURCE_KEY)
    if not isinstance(src, dict):
        return None
    return GitHubSource.from_dict(src)


def list_github_extensions(
    extensions: dict[str, dict[str, Any]],
) -> dict[str, GitHubSource]:
    """Map UUIDs to :class:`GitHubSource` for GitHub-sourced extensions."""
    out: dict[str, GitHubSource] = {}
    for uuid, info in extensions.items():
        path = info.get("path") or ""
        if not path:
            continue
        src = read_source(Path(path))
        if src is not None:
            out[uuid] = src
    return out


# ─── Tarball extraction + filtering (runs on a worker thread) ─────────────


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
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


def _read_export_ignore_patterns(gitattributes: Path) -> list[str]:
    """Return file globs marked ``export-ignore`` in a ``.gitattributes`` file."""
    patterns: list[str] = []
    try:
        text = gitattributes.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return patterns
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pattern, attrs = parts[0], parts[1:]
        if "export-ignore" in attrs:
            patterns.append(pattern)
    return patterns


def _read_gitignore_patterns(gitignore: Path) -> list[str]:
    """Return non-comment, non-blank patterns from a ``.gitignore`` file."""
    patterns: list[str] = []
    try:
        text = gitignore.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return patterns
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def _filter_tree(root: Path) -> None:
    """Remove files unrelated to the extension from ``root`` in-place.

    Order matters: pattern files (``.gitattributes`` / ``.gitignore``) are
    read **before** they are deleted by the always-strip pass.
    """
    # 1. Collect patterns while pattern files still exist.
    export_ignore = _read_export_ignore_patterns(root / ".gitattributes")
    gitignored = _read_gitignore_patterns(root / ".gitignore")
    all_patterns = export_ignore + gitignored

    # 2. Apply patterns via pathspec (git-wildmatch).
    if all_patterns and pathspec is not None:
        spec = pathspec.PathSpec.from_lines("gitwildmatch", all_patterns)
        # Walk deepest-first so directory deletion doesn't invalidate child
        # paths still being iterated.
        for p in sorted(root.rglob("*"), key=lambda x: -len(str(x))):
            if not p.exists():
                continue
            rel = p.relative_to(root).as_posix()
            check = rel + "/" if p.is_dir() else rel
            if spec.match_file(check):
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    try:
                        p.unlink()
                    except OSError:
                        pass
    elif all_patterns and pathspec is None:  # pragma: no cover
        _log.warning(
            "pathspec not installed — skipping .gitattributes / .gitignore filter"
        )

    # 3. Strip git meta files / dirs unconditionally.
    for p in list(root.rglob("*")):
        if not p.exists():
            continue
        if p.is_dir() and p.name in _ALWAYS_STRIP_DIRS:
            shutil.rmtree(p, ignore_errors=True)
        elif p.is_file() and p.name in _ALWAYS_STRIP_NAMES:
            try:
                p.unlink()
            except OSError:
                pass

    # 4. Sweep empty directories left behind by the filter (e.g. a ``tests/``
    #    whose contents were all ``export-ignore``).  Deepest-first.
    for p in sorted(root.rglob("*"), key=lambda x: -len(x.parts)):
        if p.is_dir():
            try:
                next(p.iterdir())
            except StopIteration:
                try:
                    p.rmdir()
                except OSError:
                    pass


# ─── Installer ────────────────────────────────────────────────────────────


_InstallCallback = Callable[[str | None, str | None], None]


class GitHubInstaller(GObject.Object):
    """Async installer / updater for GitHub-sourced GNOME Shell extensions.

    Signals:
        installed (uuid)          — install or update succeeded
        update-available (uuid, new_sha) — upstream has a newer commit
        error (message)           — install / update / check failed
    """

    __gtype_name__ = "GitHubInstaller"

    @GObject.Signal(arg_types=(str,))
    def installed(self, uuid: str) -> None:
        """Emitted after a successful install or update."""

    @GObject.Signal(arg_types=(str, str))
    def update_available(self, uuid: str, new_sha: str) -> None:
        """Emitted when an installed GitHub-sourced ext has a newer commit."""

    @GObject.Signal(arg_types=(str,))
    def error(self, message: str) -> None:
        """Emitted on install / update / check failure."""

    def __init__(self) -> None:
        super().__init__()
        self._session = Soup.Session()
        self._session.set_user_agent(_USER_AGENT)
        # In-memory cache: uuid -> new SHA known to be available upstream.
        self._known_updates: dict[str, str] = {}

    # ── Public API ────────────────────────────────────────────────────────

    def install(
        self,
        repo_url: str,
        on_done: _InstallCallback | None = None,
    ) -> None:
        """Install a GitHub repo as a GNOME Shell extension (async).

        Calls ``on_done(uuid, error_message)`` when finished.  Exactly one
        of the two arguments is set.
        """
        parsed = parse_repo_url(repo_url)
        if parsed is None:
            self._fail(on_done, "Not a valid GitHub repository (use owner/repo or URL).")
            return
        owner, repo = parsed
        _log.info("Installing from github.com/%s/%s", owner, repo)
        self._resolve_default_branch(owner, repo, on_done)

    def update(
        self,
        source: GitHubSource,
        on_done: _InstallCallback | None = None,
    ) -> None:
        """Re-install from upstream HEAD.  ``installed_at`` is preserved."""
        _log.info("Updating from github.com/%s/%s", source.owner, source.repo)
        self._resolve_default_branch(source.owner, source.repo, on_done)

    def has_update(self, uuid: str) -> str | None:
        """Return the cached new SHA if an update is known to be available."""
        return self._known_updates.get(uuid)

    def check_updates(self, extensions: dict[str, dict[str, Any]]) -> None:
        """Query upstream HEAD for every GitHub-sourced extension."""
        for uuid, src in list_github_extensions(extensions).items():
            self._check_one(uuid, src)

    def uninstall(self, extension_path: Path) -> bool:
        """Remove an installed extension directory.  Returns ``True`` on success.

        Caller is responsible for disabling the extension first.
        """
        try:
            shutil.rmtree(extension_path)
        except FileNotFoundError:
            return True
        except OSError as exc:
            _log.error("Uninstall failed: %s", exc)
            self.emit("error", str(exc))
            return False
        _log.info("Removed %s", extension_path)
        return True

    # ── Install pipeline: API + download (main loop, async) ──────────────

    def _resolve_default_branch(
        self,
        owner: str,
        repo: str,
        on_done: _InstallCallback | None,
    ) -> None:
        url = f"{_GITHUB_API}/repos/{owner}/{repo}"
        msg = Soup.Message.new("GET", url)
        msg.get_request_headers().append("Accept", "application/vnd.github+json")
        self._session.send_and_read_async(
            msg,
            GLib.PRIORITY_DEFAULT,
            None,
            self._on_repo_info,
            (owner, repo, msg, on_done),
        )

    def _on_repo_info(
        self,
        session: Soup.Session,
        result: Gio.AsyncResult,
        user_data: tuple[str, str, Soup.Message, _InstallCallback | None],
    ) -> None:
        owner, repo, msg, on_done = user_data
        try:
            body = bytes(session.send_and_read_finish(result).get_data() or b"")
        except GLib.Error as exc:
            self._fail(on_done, f"Network error: {exc.message}")
            return
        status = msg.get_status()
        if status != Soup.Status.OK:
            self._fail(on_done, _http_error(msg, body, "look up repository"))
            return
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            self._fail(on_done, f"Bad response from GitHub: {exc}")
            return
        branch = data.get("default_branch")
        if not branch:
            self._fail(on_done, "GitHub did not return a default branch.")
            return
        self._resolve_commit(owner, repo, branch, on_done)

    def _resolve_commit(
        self,
        owner: str,
        repo: str,
        ref: str,
        on_done: _InstallCallback | None,
    ) -> None:
        url = f"{_GITHUB_API}/repos/{owner}/{repo}/commits/{ref}"
        msg = Soup.Message.new("GET", url)
        msg.get_request_headers().append("Accept", "application/vnd.github+json")
        self._session.send_and_read_async(
            msg,
            GLib.PRIORITY_DEFAULT,
            None,
            self._on_commit_info,
            (owner, repo, ref, msg, on_done),
        )

    def _on_commit_info(
        self,
        session: Soup.Session,
        result: Gio.AsyncResult,
        user_data: tuple[str, str, str, Soup.Message, _InstallCallback | None],
    ) -> None:
        owner, repo, ref, msg, on_done = user_data
        try:
            body = bytes(session.send_and_read_finish(result).get_data() or b"")
        except GLib.Error as exc:
            self._fail(on_done, f"Network error: {exc.message}")
            return
        status = msg.get_status()
        if status != Soup.Status.OK:
            self._fail(on_done, _http_error(msg, body, "look up commit"))
            return
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            self._fail(on_done, f"Bad response from GitHub: {exc}")
            return
        sha = data.get("sha")
        if not sha:
            self._fail(on_done, "GitHub did not return a commit SHA.")
            return
        self._download_tarball(owner, repo, ref, sha, on_done)

    def _download_tarball(
        self,
        owner: str,
        repo: str,
        ref: str,
        sha: str,
        on_done: _InstallCallback | None,
    ) -> None:
        url = f"{_GITHUB_API}/repos/{owner}/{repo}/tarball/{sha}"
        msg = Soup.Message.new("GET", url)
        # Soup follows redirects (to codeload.github.com) by default.
        self._session.send_and_read_async(
            msg,
            GLib.PRIORITY_DEFAULT,
            None,
            self._on_tarball,
            (owner, repo, ref, sha, msg, on_done),
        )

    def _on_tarball(
        self,
        session: Soup.Session,
        result: Gio.AsyncResult,
        user_data: tuple[str, str, str, str, Soup.Message, _InstallCallback | None],
    ) -> None:
        owner, repo, ref, sha, msg, on_done = user_data
        try:
            bytes_ = session.send_and_read_finish(result)
        except GLib.Error as exc:
            self._fail(on_done, f"Download failed: {exc.message}")
            return
        status = msg.get_status()
        if status != Soup.Status.OK:
            self._fail(on_done, _http_error(msg, b"", "download tarball"))
            return
        data = bytes(bytes_.get_data() or b"")
        if not data:
            self._fail(on_done, "Empty tarball from GitHub.")
            return
        # Hand off to a worker thread; results return via GLib.idle_add.
        threading.Thread(
            target=self._extract_install_blocking,
            args=(owner, repo, ref, sha, data, on_done),
            daemon=True,
        ).start()

    # ── Worker thread: extract + filter + move into place ────────────────

    def _extract_install_blocking(
        self,
        owner: str,
        repo: str,
        ref: str,
        sha: str,
        tarball: bytes,
        on_done: _InstallCallback | None,
    ) -> None:
        try:
            uuid = _do_extract_install(owner, repo, ref, sha, tarball)
        except InstallError as exc:
            GLib.idle_add(self._fail, on_done, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - log+surface unknowns
            _log.exception("Unexpected install error")
            GLib.idle_add(self._fail, on_done, f"Install failed: {exc}")
            return
        GLib.idle_add(self._finish_install, uuid, on_done)

    # ── Main-loop callbacks for worker results ───────────────────────────

    def _finish_install(self, uuid: str, on_done: _InstallCallback | None) -> bool:
        self._known_updates.pop(uuid, None)
        self.emit("installed", uuid)
        if on_done:
            on_done(uuid, None)
        return False  # GLib.SOURCE_REMOVE

    def _fail(self, on_done: _InstallCallback | None, message: str) -> bool:
        _log.warning("GitHub install error: %s", message)
        self.emit("error", message)
        if on_done:
            on_done(None, message)
        return False  # GLib.SOURCE_REMOVE

    # ── Update detection ─────────────────────────────────────────────────

    def _check_one(self, uuid: str, src: GitHubSource) -> None:
        url = f"{_GITHUB_API}/repos/{src.owner}/{src.repo}/commits/{src.ref}"
        msg = Soup.Message.new("GET", url)
        msg.get_request_headers().append("Accept", "application/vnd.github+json")
        self._session.send_and_read_async(
            msg,
            GLib.PRIORITY_LOW,
            None,
            self._on_check_done,
            (uuid, src, msg),
        )

    def _on_check_done(
        self,
        session: Soup.Session,
        result: Gio.AsyncResult,
        user_data: tuple[str, GitHubSource, Soup.Message],
    ) -> None:
        uuid, src, msg = user_data
        try:
            bytes_ = session.send_and_read_finish(result)
        except GLib.Error as exc:
            _log.info("Update check for %s failed: %s", uuid, exc.message)
            return
        status = msg.get_status()
        if status != Soup.Status.OK:
            _log.info(
                "Update check for %s returned HTTP %s — skipping",
                uuid,
                int(status),
            )
            return
        try:
            data = json.loads(bytes(bytes_.get_data() or b""))
        except json.JSONDecodeError:
            return
        new_sha = data.get("sha")
        if not new_sha or new_sha == src.commit_sha:
            return
        self._known_updates[uuid] = new_sha
        self.emit("update-available", uuid, new_sha)


# ─── Blocking install body (importable for unit tests) ────────────────────


def _do_extract_install(
    owner: str,
    repo: str,
    ref: str,
    sha: str,
    tarball: bytes,
    extensions_root: Path | None = None,
) -> str:
    """Extract, validate, filter, and move into the extensions directory.

    Returns the extension UUID on success.  Raises :class:`InstallError`.
    """
    root_dir = extensions_root or EXTENSIONS_ROOT

    with tempfile.TemporaryDirectory(prefix="gse-profiler-install-") as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "src.tar.gz"
        archive.write_bytes(tarball)

        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()
        try:
            with tarfile.open(archive, "r:gz") as tar:
                _safe_extract(tar, extract_dir)
        except (tarfile.TarError, OSError) as exc:
            raise InstallError(f"Bad tarball: {exc}") from exc

        # GitHub tarballs always contain a single top-level dir: ``{repo}-{sha}/``.
        top_dirs = [p for p in extract_dir.iterdir() if p.is_dir()]
        if len(top_dirs) != 1:
            raise InstallError("Unexpected tarball layout (no single top-level dir).")
        src_root = top_dirs[0]

        meta_path = src_root / "metadata.json"
        if not meta_path.is_file():
            raise InstallError(
                "Not a GNOME Shell extension (no metadata.json at repo root)."
            )
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise InstallError(f"metadata.json is not valid JSON: {exc}") from exc
        uuid = meta.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            raise InstallError("metadata.json has no valid 'uuid' field.")

        # Filter out unrelated files (git meta + export-ignore + .gitignore).
        _filter_tree(src_root)

        if not meta_path.is_file():
            raise InstallError("Filter removed required file: metadata.json")

        # Preserve ``installed_at`` if we are replacing an earlier install.
        target = root_dir / uuid
        preserved_installed_at: str | None = None
        if target.exists():
            existing = read_source(target)
            if existing is not None:
                preserved_installed_at = existing.installed_at

        installed_at = preserved_installed_at or datetime.now(
            timezone.utc
        ).isoformat(timespec="seconds")

        # Re-read meta (filter may have rewritten it if metadata.json was
        # in .gitignore — though the guard above would have rejected that).
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        source = GitHubSource(
            owner=owner,
            repo=repo,
            ref=ref,
            commit_sha=sha,
            installed_at=installed_at,
        )
        meta[SOURCE_KEY] = source.to_dict()
        meta_path.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        # Compile GSettings schemas if the extension ships any.  Repos almost
        # never commit the compiled binary (it lives in their .gitignore);
        # without it GNOME Shell crashes the extension as soon as it tries
        # to open a GSettings instance.  Done on the staging copy so a
        # failure leaves the existing install untouched.
        _compile_schemas(src_root)

        # Move into place (replace existing).
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_root), str(target))
        _log.info("Installed %s to %s", uuid, target)
        return uuid


def _compile_schemas(extension_root: Path) -> None:
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


# ─── HTTP error helpers ───────────────────────────────────────────────────


def _http_error(msg: Soup.Message, body: bytes, action: str) -> str:
    status = int(msg.get_status())
    reason = msg.get_reason_phrase() or ""
    # GitHub puts a human-readable error in the JSON ``message`` field.
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
                f"GitHub rate limit exceeded while trying to {action}.{reset_hint}"
            )
    if detail:
        return f"Failed to {action}: {detail} (HTTP {status} {reason}).".strip()
    return f"Failed to {action}: HTTP {status} {reason}.".strip()
