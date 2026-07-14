"""Persistent registry mapping extension UUIDs to their install source.

Stored as ``sources.json`` in the app's own data directory so we never
modify the upstream extension's ``metadata.json``.  All I/O is defensive:
a missing, unreadable, or corrupt file degrades gracefully to an empty
registry, and writes are atomic (temp file + ``os.replace``) so a crash
mid-write cannot truncate the store.

Two source kinds are stored side by side, discriminated by a ``kind`` field
on each entry: ``"github"`` (a :class:`GitHubSource`) and ``"ego"`` (an
:class:`EgoSource`, extensions.gnome.org).  Entries written before the EGO
support existed have no ``kind`` field and are read back as GitHub sources.

Expected scale is tens of extensions, so a flat JSON file loaded into memory
is plenty — no database needed.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from app.core.ego_source import EgoSource
from app.core.github_source import GitHubSource

_log = logging.getLogger(__name__)

#: Either provenance kind stored in the registry.
Source = GitHubSource | EgoSource


def _serialize(src: Source) -> dict:
    """Serialise a source to a JSON dict tagged with its discriminator kind."""
    entry = src.to_dict()
    entry["kind"] = "ego" if isinstance(src, EgoSource) else "github"
    return entry


def _default_path() -> Path:
    """Location of ``sources.json`` in the app's data dir.

    Unlike the extensions directory, this is *our* data, so the Flatpak
    app-scoped ``GLib.get_user_data_dir()`` is exactly what we want both
    inside and outside the sandbox.  ``gi`` is imported lazily so the
    registry stays unit-testable (with an explicit path) where PyGObject
    is unavailable.
    """
    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    return Path(GLib.get_user_data_dir()) / "gse-profiler" / "sources.json"


class SourceRegistry:
    """UUID → :class:`Source` map persisted to ``sources.json``."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else _default_path()
        self._sources: dict[str, Source] = {}
        self._load()

    # ── Loading ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._sources = {}
            return
        except OSError as exc:
            _log.warning("Could not read source registry %s: %s", self._path, exc)
            self._sources = {}
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            _log.warning(
                "Source registry %s is corrupt (%s); starting empty",
                self._path,
                exc,
            )
            self._sources = {}
            return
        sources: dict[str, Source] = {}
        if isinstance(data, dict):
            for uuid, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                # Absent ``kind`` means a pre-EGO GitHub entry.
                kind = entry.get("kind", "github")
                src: Source | None
                if kind == "ego":
                    src = EgoSource.from_dict(entry)
                else:
                    src = GitHubSource.from_dict(entry)
                if src is not None:
                    sources[uuid] = src
        self._sources = sources

    # ── Persisting ───────────────────────────────────────────────────────

    def _persist(self) -> None:
        data = {uuid: _serialize(src) for uuid, src in self._sources.items()}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=".sources-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                os.replace(tmp, self._path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            _log.error("Could not write source registry %s: %s", self._path, exc)

    # ── Public API ───────────────────────────────────────────────────────

    def get(self, uuid: str) -> Source | None:
        return self._sources.get(uuid)

    def set(self, uuid: str, source: Source) -> None:
        self._sources[uuid] = source
        self._persist()

    def remove(self, uuid: str) -> bool:
        """Drop the entry for ``uuid``.  Returns True if one was removed."""
        if uuid in self._sources:
            del self._sources[uuid]
            self._persist()
            return True
        return False

    def all(self) -> dict[str, Source]:
        """Return a copy of the full UUID → source map."""
        return dict(self._sources)

    def reconcile(self, extensions_root: Path | str) -> bool:
        """Prune entries whose installed directory no longer exists.

        ``extensions_root`` is the per-user extensions directory; an entry
        is kept as long as ``extensions_root/<uuid>`` is present on disk.
        This deliberately does *not* key off the live D-Bus extension list:
        a freshly installed extension sits on disk but is unknown to
        gnome-shell until the next session, and must not be pruned in that
        window.  Returns True if anything was pruned (and persisted).
        """
        root = Path(extensions_root)
        stale = [uuid for uuid in self._sources if not (root / uuid).is_dir()]
        if not stale:
            return False
        for uuid in stale:
            del self._sources[uuid]
        _log.info("Pruned %d stale source entr%s", len(stale), "y" if len(stale) == 1 else "ies")
        self._persist()
        return True
