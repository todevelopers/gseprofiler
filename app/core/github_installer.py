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
import re
import shutil
import tarfile
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("GObject", "2.0")
gi.require_version("Soup", "3.0")
from gi.repository import Gio, GLib, GObject, Soup

# Shared low-level install plumbing.  ``EXTENSIONS_ROOT`` and ``InstallError``
# are re-exported here for backwards-compatible imports
# (``from app.core.github_installer import EXTENSIONS_ROOT, InstallError``).
from app.core.extension_install import (
    EXTENSIONS_ROOT,
    InstallError,
    install_into_place,
)
from app.core.extension_install import (
    emit_progress as _emit_progress,
)
from app.core.extension_install import (
    http_error as _http_error,
)
from app.core.extension_install import (
    safe_extract_tar as _safe_extract,
)

# Re-exported for backwards-compatible imports (``from app.core.github_installer
# import GitHubSource``).  The dataclass lives in its own module to avoid a
# circular import with the source registry.
from app.core.github_source import GitHubSource
from app.core.source_registry import SourceRegistry

try:
    import pathspec  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - optional at import time
    pathspec = None  # type: ignore[assignment]

_log = logging.getLogger(__name__)

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
# .../owner/repo/, and subdirectory URLs .../owner/repo/tree/<ref>/<subpath>.
# Owner: ASCII alnum + hyphen.  Repo: alnum + . _ - (neither may contain '/',
# which keeps the optional /tree/... suffix unambiguous).
#
# Limitation: a branch name containing slashes (e.g. ``feature/x``) cannot be
# distinguished from the subpath in a ``/tree/`` URL without querying GitHub,
# so the first segment after ``/tree/`` is always taken as the ref and the
# rest as the subpath.  Single-segment branches (main, master, …) — the common
# case — are unaffected.
_REPO_URL_RE = re.compile(
    r"^(?:https?://github\.com/)?"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})?)/"
    r"(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?"
    r"(?:/tree/(?P<ref>[^/]+)(?:/(?P<subpath>.+))?)?$"
)


class ParsedRepo(NamedTuple):
    """A parsed GitHub install target.

    ``ref`` and ``subpath`` are set only for ``/tree/<ref>/<subpath>`` URLs;
    otherwise they are ``None`` (default branch, repo root).
    """

    owner: str
    repo: str
    ref: str | None
    subpath: str | None


# ─── Public helpers ────────────────────────────────────────────────────────


def parse_repo_url(url: str) -> ParsedRepo | None:
    """Parse a GitHub repo identifier into a :class:`ParsedRepo`, or ``None``.

    Plain ``owner/repo`` and ``https://github.com/owner/repo`` forms yield
    ``ref=None, subpath=None``.  A subdirectory URL such as
    ``https://github.com/owner/repo/tree/main/src`` yields ``ref="main",
    subpath="src"``.
    """
    url = (url or "").strip()
    # Drop any query string / fragment (e.g. ``?tab=readme``, ``#L10``).
    url = url.split("#", 1)[0].split("?", 1)[0].rstrip("/\\")
    if not url:
        return None
    m = _REPO_URL_RE.match(url)
    if not m:
        return None
    subpath = m.group("subpath")
    if subpath is not None:
        # Normalise: collapse separators, strip leading/trailing slashes.
        subpath = subpath.strip("/").strip()
        # Reject path traversal — the subpath indexes into the extracted tree.
        if not subpath or ".." in Path(subpath).parts:
            return None
    return ParsedRepo(m.group("owner"), m.group("repo"), m.group("ref"), subpath)


# ─── Tarball extraction + filtering (runs on a worker thread) ─────────────


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


def _filter_tree(root: Path, repo_root: Path | None = None) -> None:
    """Remove files unrelated to the extension from ``root`` in-place.

    Order matters: pattern files (``.gitattributes`` / ``.gitignore``) are
    read **before** they are deleted by the always-strip pass.  When ``root``
    is a subdirectory install, ``repo_root`` supplies the repository-level
    pattern files that also govern it.
    """
    # 1. Collect patterns while pattern files still exist.  Read the
    #    repo-root files first (when installing a subdirectory) so their
    #    patterns apply too, then the extension dir's own.
    pattern_roots = [root]
    if repo_root is not None and repo_root.resolve() != root.resolve():
        pattern_roots.insert(0, repo_root)
    export_ignore: list[str] = []
    gitignored: list[str] = []
    for base in pattern_roots:
        export_ignore += _read_export_ignore_patterns(base / ".gitattributes")
        gitignored += _read_gitignore_patterns(base / ".gitignore")
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
#: ``on_done(new_sha, error)``: ``new_sha`` is the newer upstream commit when
#: an update is available; both ``None`` means up to date; ``error`` is set on
#: failure.
_CheckCallback = Callable[[str | None, str | None], None]
_ProgressCallback = Callable[[str], None]


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

    def __init__(self, registry: SourceRegistry | None = None) -> None:
        super().__init__()
        self._session = Soup.Session()
        self._session.set_user_agent(_USER_AGENT)
        # In-memory cache: uuid -> new SHA known to be available upstream.
        self._known_updates: dict[str, str] = {}
        # Provenance store (UUID -> GitHubSource), persisted to sources.json.
        self._registry = registry or SourceRegistry()

    @property
    def registry(self) -> SourceRegistry:
        """The provenance registry, shared with the UI for source lookups."""
        return self._registry

    # ── Public API ────────────────────────────────────────────────────────

    def install(
        self,
        repo_url: str,
        on_done: _InstallCallback | None = None,
        on_progress: _ProgressCallback | None = None,
    ) -> None:
        """Install a GitHub repo as a GNOME Shell extension (async).

        Calls ``on_done(uuid, error_message)`` when finished.  Exactly one
        of the two arguments is set.  ``on_progress(message)`` is called on
        the main loop at each pipeline stage.
        """
        parsed = parse_repo_url(repo_url)
        if parsed is None:
            self._fail(on_done, "Not a valid GitHub repository (use owner/repo or URL).")
            return
        owner, repo, ref, subpath = parsed
        _log.info(
            "Installing from github.com/%s/%s%s%s",
            owner,
            repo,
            f" (ref {ref})" if ref else "",
            f" subpath {subpath}" if subpath else "",
        )
        _emit_progress(on_progress, "Checking repository…")
        if ref is not None:
            # The URL pinned a branch — skip the default-branch lookup.
            _emit_progress(on_progress, "Fetching latest commit…")
            self._resolve_commit(owner, repo, ref, subpath, on_done, on_progress)
        else:
            self._resolve_default_branch(owner, repo, subpath, on_done, on_progress)

    def update(
        self,
        source: GitHubSource,
        on_done: _InstallCallback | None = None,
        on_progress: _ProgressCallback | None = None,
    ) -> None:
        """Re-install from upstream HEAD.  ``installed_at`` is preserved."""
        _log.info("Updating from github.com/%s/%s", source.owner, source.repo)
        _emit_progress(on_progress, "Fetching latest commit…")
        # Re-resolve the tracked ref directly so the recorded subpath is honoured.
        self._resolve_commit(
            source.owner,
            source.repo,
            source.ref,
            source.subpath or None,
            on_done,
            on_progress,
        )

    def has_update(self, uuid: str) -> str | None:
        """Return the cached new SHA if an update is known to be available."""
        return self._known_updates.get(uuid)

    def check_updates(self, extensions: dict[str, dict[str, Any]]) -> None:
        """Query upstream HEAD for every installed GitHub-sourced extension."""
        for uuid, src in self._registry.all().items():
            if isinstance(src, GitHubSource) and uuid in extensions:
                self._check_one(uuid, src)

    def check_update(
        self,
        uuid: str,
        source: GitHubSource,
        on_done: _CheckCallback | None = None,
    ) -> None:
        """Check a single GitHub-sourced extension for a newer commit.

        Reports the result via ``on_done(new_sha, error)`` and, when an
        update is found, also emits ``update-available`` like the bulk
        :meth:`check_updates`.
        """
        self._check_one(uuid, source, on_done)

    # ── Install pipeline: API + download (main loop, async) ──────────────

    def _resolve_default_branch(
        self,
        owner: str,
        repo: str,
        subpath: str | None,
        on_done: _InstallCallback | None,
        on_progress: _ProgressCallback | None = None,
    ) -> None:
        url = f"{_GITHUB_API}/repos/{owner}/{repo}"
        msg = Soup.Message.new("GET", url)
        msg.get_request_headers().append("Accept", "application/vnd.github+json")
        self._session.send_and_read_async(
            msg,
            GLib.PRIORITY_DEFAULT,
            None,
            self._on_repo_info,
            (owner, repo, subpath, msg, on_done, on_progress),
        )

    def _on_repo_info(
        self,
        session: Soup.Session,
        result: Gio.AsyncResult,
        user_data: tuple,
    ) -> None:
        owner, repo, subpath, msg, on_done, on_progress = user_data
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
        _emit_progress(on_progress, "Fetching latest commit…")
        self._resolve_commit(owner, repo, branch, subpath, on_done, on_progress)

    def _resolve_commit(
        self,
        owner: str,
        repo: str,
        ref: str,
        subpath: str | None,
        on_done: _InstallCallback | None,
        on_progress: _ProgressCallback | None = None,
    ) -> None:
        url = f"{_GITHUB_API}/repos/{owner}/{repo}/commits/{ref}"
        msg = Soup.Message.new("GET", url)
        msg.get_request_headers().append("Accept", "application/vnd.github+json")
        self._session.send_and_read_async(
            msg,
            GLib.PRIORITY_DEFAULT,
            None,
            self._on_commit_info,
            (owner, repo, ref, subpath, msg, on_done, on_progress),
        )

    def _on_commit_info(
        self,
        session: Soup.Session,
        result: Gio.AsyncResult,
        user_data: tuple,
    ) -> None:
        owner, repo, ref, subpath, msg, on_done, on_progress = user_data
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
        _emit_progress(on_progress, "Downloading archive…")
        self._download_tarball(owner, repo, ref, sha, subpath, on_done, on_progress)

    def _download_tarball(
        self,
        owner: str,
        repo: str,
        ref: str,
        sha: str,
        subpath: str | None,
        on_done: _InstallCallback | None,
        on_progress: _ProgressCallback | None = None,
    ) -> None:
        url = f"{_GITHUB_API}/repos/{owner}/{repo}/tarball/{sha}"
        msg = Soup.Message.new("GET", url)
        # Soup follows redirects (to codeload.github.com) by default.
        self._session.send_and_read_async(
            msg,
            GLib.PRIORITY_DEFAULT,
            None,
            self._on_tarball,
            (owner, repo, ref, sha, subpath, msg, on_done, on_progress),
        )

    def _on_tarball(
        self,
        session: Soup.Session,
        result: Gio.AsyncResult,
        user_data: tuple,
    ) -> None:
        owner, repo, ref, sha, subpath, msg, on_done, on_progress = user_data
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
        _emit_progress(on_progress, "Extracting files…")
        # Hand off to a worker thread; results return via GLib.idle_add.
        threading.Thread(
            target=self._extract_install_blocking,
            args=(owner, repo, ref, sha, subpath, data, on_done, on_progress),
            daemon=True,
        ).start()

    # ── Worker thread: extract + filter + move into place ────────────────

    def _extract_install_blocking(
        self,
        owner: str,
        repo: str,
        ref: str,
        sha: str,
        subpath: str | None,
        tarball: bytes,
        on_done: _InstallCallback | None,
        on_progress: _ProgressCallback | None = None,
    ) -> None:
        try:
            uuid, resolved_subpath = _do_extract_install(
                tarball, subpath=subpath, on_progress=on_progress
            )
        except InstallError as exc:
            GLib.idle_add(self._fail, on_done, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - log+surface unknowns
            _log.exception("Unexpected install error")
            GLib.idle_add(self._fail, on_done, f"Install failed: {exc}")
            return
        GLib.idle_add(
            self._finish_install,
            uuid,
            owner,
            repo,
            ref,
            sha,
            resolved_subpath,
            on_done,
        )

    # ── Main-loop callbacks for worker results ───────────────────────────

    def _finish_install(
        self,
        uuid: str,
        owner: str,
        repo: str,
        ref: str,
        sha: str,
        subpath: str,
        on_done: _InstallCallback | None,
    ) -> bool:
        # Preserve the original install timestamp when re-installing (update).
        existing = self._registry.get(uuid)
        installed_at = (
            existing.installed_at
            if existing is not None
            else datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        self._registry.set(
            uuid,
            GitHubSource(
                owner=owner,
                repo=repo,
                ref=ref,
                commit_sha=sha,
                installed_at=installed_at,
                subpath=subpath,
            ),
        )
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

    def _check_one(
        self,
        uuid: str,
        src: GitHubSource,
        on_done: _CheckCallback | None = None,
    ) -> None:
        url = f"{_GITHUB_API}/repos/{src.owner}/{src.repo}/commits/{src.ref}"
        msg = Soup.Message.new("GET", url)
        msg.get_request_headers().append("Accept", "application/vnd.github+json")
        self._session.send_and_read_async(
            msg,
            GLib.PRIORITY_LOW,
            None,
            self._on_check_done,
            (uuid, src, msg, on_done),
        )

    def _on_check_done(
        self,
        session: Soup.Session,
        result: Gio.AsyncResult,
        user_data: tuple[str, GitHubSource, Soup.Message, _CheckCallback | None],
    ) -> None:
        uuid, src, msg, on_done = user_data

        def report(new_sha: str | None, error: str | None) -> None:
            if on_done:
                on_done(new_sha, error)

        try:
            bytes_ = session.send_and_read_finish(result)
        except GLib.Error as exc:
            _log.info("Update check for %s failed: %s", uuid, exc.message)
            report(None, f"Network error: {exc.message}")
            return
        status = msg.get_status()
        if status != Soup.Status.OK:
            _log.info(
                "Update check for %s returned HTTP %s — skipping",
                uuid,
                int(status),
            )
            report(None, f"GitHub returned HTTP {int(status)}.")
            return
        try:
            data = json.loads(bytes(bytes_.get_data() or b""))
        except json.JSONDecodeError:
            report(None, "Bad response from GitHub.")
            return
        new_sha = data.get("sha")
        if not new_sha or new_sha == src.commit_sha:
            report(None, None)
            return
        self._known_updates[uuid] = new_sha
        self.emit("update-available", uuid, new_sha)
        report(new_sha, None)


# ─── Blocking install body (importable for unit tests) ────────────────────


def _do_extract_install(
    tarball: bytes,
    extensions_root: Path | None = None,
    subpath: str | None = None,
    on_progress: _ProgressCallback | None = None,
) -> tuple[str, str]:
    """Extract, validate, filter, and move into the extensions directory.

    ``subpath`` pins the subdirectory (relative to the repo root) that holds
    the extension.  When ``None``, ``metadata.json`` is looked up at the repo
    root and, failing that, auto-detected anywhere in the tree.

    Returns ``(uuid, resolved_subpath)`` on success — ``resolved_subpath`` is
    ``""`` for a root install and is recorded in the source registry so
    updates re-resolve the same directory.  Raises :class:`InstallError`.
    The upstream tree is installed verbatim — provenance is recorded
    separately in the source registry, never written into ``metadata.json``.
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
        repo_root = top_dirs[0]

        # Locate the extension directory within the extracted repo.
        src_root, resolved_subpath = _locate_extension(repo_root, subpath)

        meta_path = src_root / "metadata.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise InstallError(f"metadata.json is not valid JSON: {exc}") from exc
        uuid = meta.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            raise InstallError("metadata.json has no valid 'uuid' field.")

        # Filter out unrelated files (git meta + export-ignore + .gitignore).
        # When installing a subdirectory, the repo-root .gitignore /
        # .gitattributes still govern it, so feed both pattern roots.
        _filter_tree(src_root, repo_root=repo_root)

        if not meta_path.is_file():
            raise InstallError("Filter removed required file: metadata.json")

        # Compile GSettings schemas and move into place (replacing any
        # existing install).  Repos almost never commit the compiled binary
        # (it lives in their .gitignore); without it GNOME Shell crashes the
        # extension as soon as it tries to open a GSettings instance.  Both
        # steps run on the staging copy so a failure leaves the current
        # install untouched.
        install_into_place(src_root, root_dir, uuid, on_progress=on_progress)
        return uuid, resolved_subpath


def _locate_extension(
    repo_root: Path, subpath: str | None
) -> tuple[Path, str]:
    """Resolve the directory holding ``metadata.json`` inside ``repo_root``.

    With an explicit ``subpath`` it must contain ``metadata.json``.  Without
    one, the repo root is preferred; failing that, the tree is searched and a
    single match is accepted automatically (shallowest wins on ties — but an
    ambiguous tie at the same depth is rejected).  Returns
    ``(extension_dir, resolved_subpath)`` where ``resolved_subpath`` is POSIX
    and ``""`` for a root install.
    """
    if subpath:
        candidate = (repo_root / subpath).resolve()
        # Guard against traversal escaping the extracted tree.
        try:
            candidate.relative_to(repo_root.resolve())
        except ValueError as exc:
            raise InstallError(f"Unsafe subdirectory path: {subpath}") from exc
        if not candidate.is_dir():
            raise InstallError(f"Subdirectory '{subpath}' not found in the repository.")
        if not (candidate / "metadata.json").is_file():
            raise InstallError(
                f"No metadata.json in '{subpath}' — not a GNOME Shell extension."
            )
        return candidate, subpath

    # No subpath: prefer the repo root.
    if (repo_root / "metadata.json").is_file():
        return repo_root, ""

    # Auto-detect: search the tree for metadata.json files.
    matches = sorted(
        repo_root.rglob("metadata.json"),
        key=lambda p: (len(p.relative_to(repo_root).parts), str(p)),
    )
    if not matches:
        raise InstallError(
            "Not a GNOME Shell extension (no metadata.json found in the repository)."
        )
    if len(matches) > 1:
        # Accept a unique shallowest match; reject genuine ambiguity at the
        # same depth so we never silently install the wrong subdirectory.
        depths = [len(p.relative_to(repo_root).parts) for p in matches]
        if depths[0] == depths[1]:
            rels = ", ".join(
                str(p.relative_to(repo_root).parent.as_posix()) or "."
                for p in matches[:4]
            )
            raise InstallError(
                "Multiple extensions found in this repository "
                f"({rels}). Add the subdirectory to the URL, e.g. "
                ".../tree/<branch>/<path>."
            )
    ext_dir = matches[0].parent
    return ext_dir, ext_dir.relative_to(repo_root).as_posix()
