"""JSON-backed per-namespace settings persistence.

A single :class:`Settings` object owns one JSON file under the app config
directory (``$XDG_CONFIG_HOME/gse-profiler/<namespace>.json``). Every write is
read-modify-write with **merge semantics**: the file is re-read, the change is
applied, and the whole dict is written back. This means independent call sites
(e.g. the profiler saving its ``mode`` and its ``paned_pos`` from different
handlers) never clobber each other's keys.

Previously ``profiler_view`` and ``log_viewer`` each carried their own
``_settings_path`` / ``_load_settings`` / ``_save_settings`` trio with subtly
diverging merge behaviour (the profiler merged inside ``_save_settings``; the
log viewer merged at the call site and overwrote otherwise). This module is the
single home for that logic and the seam behind which the Phase 9 GSettings
backend can later slot without touching any view code.
"""

import json
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

_log = logging.getLogger(__name__)

_APP_SUBDIR = "gse-profiler"


def _config_dir() -> Path:
    return Path(GLib.get_user_config_dir()) / _APP_SUBDIR


class Settings:
    """Merge-semantics JSON store scoped to a single ``namespace`` file."""

    def __init__(self, namespace: str) -> None:
        self._path = _config_dir() / f"{namespace}.json"

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, Any]:
        """Return the full settings dict (``{}`` when missing or unreadable)."""
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
                _log.warning("Settings at %s is not a JSON object; ignoring", self._path)
            except Exception as exc:
                _log.warning("Failed to load settings from %s: %s", self._path, exc)
        return {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.load().get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Merge a single ``key`` into the stored dict and persist."""
        self.update({key: value})

    def update(
        self, values: Mapping[str, Any], *, remove: Iterable[str] = ()
    ) -> None:
        """Merge ``values`` into the stored dict, drop any ``remove`` keys, persist.

        ``remove`` lets a caller clear obsolete keys in the same write — e.g. the
        log viewer dropping the legacy free-text ``journal_cmd`` while writing the
        structured ``capture`` spec.
        """
        data = self.load()
        data.update(values)
        for key in remove:
            data.pop(key, None)
        self._write(data)

    def _write(self, data: dict[str, Any]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            _log.error("Failed to save settings to %s: %s", self._path, exc)
