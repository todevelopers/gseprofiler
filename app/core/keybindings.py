"""Catalog and persistence for user-customizable keyboard shortcuts.

Three shortcut mechanisms exist in the app, unified here behind one catalog
and one canonical accelerator format (GTK accelerator strings, e.g.
``"<Control>s"``, ``"F9"``, ``"<Super><Shift>F5"``):

- ``"app"``    — window/application ``Gio.SimpleAction`` accelerators, applied
                 via ``Gio.Application.set_accels_for_action``.
- ``"tab"``    — per-tab shortcuts live only while that tab is selected,
                 applied via a ``Gtk.ShortcutController`` built by
                 :func:`populate_shortcut_controller`.
- ``"global"`` — shell-level shortcuts (Super+F5 etc.) registered by the
                 bridge extension via its own GSettings schema. The app has
                 no direct dconf access in Flatpak (no ``ca.desrt.dconf``
                 permission granted), so edits are relayed over the existing
                 socket to the bridge, which owns the setting.

``app``/``tab`` overrides persist to ``shortcuts.json`` next to
``sources.json`` (same atomic-write pattern as
:class:`app.core.source_registry.SourceRegistry`). ``global`` values are
*not* written here — they live in the bridge's GSettings (persisted by
dconf on the GNOME Shell side); this module only caches the last value the
bridge reported.

This module intentionally never imports ``gi`` at module scope (only lazily,
inside functions that need it) so :class:`KeybindingManager` stays importable
and unit-testable on plain Windows Python, matching ``SourceRegistry``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gi.repository import Gtk

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActionSpec:
    """One customizable shortcut's identity, defaults, and wiring target."""

    id: str
    kind: str  # "app" | "tab" | "global"
    scope: str  # "global" | "general" | "profiler" | "logs" | "inspector"
    title: str
    default: tuple[str, ...]
    detailed_action: str | None = None  # kind == "app", e.g. "win.select-tab::profiler"
    settings_key: str | None = None  # kind == "global", e.g. "toggle-profiling"


# Mirrors the previous hardcoded defaults exactly (main.py accels, the
# per-view Gtk.ShortcutController triggers, and the bridge's gschema
# defaults) so this refactor is behaviour-preserving until a user edits.
CATALOG: tuple[ActionSpec, ...] = (
    # ── Global (bridge / GSettings-backed; fires even when unfocused) ──────
    ActionSpec(
        id="toggle-profiling", kind="global", scope="global",
        title="Toggle profiling (start / stop)",
        default=("<Super>F5",), settings_key="toggle-profiling",
    ),
    ActionSpec(
        id="restart-profiling", kind="global", scope="global",
        title="Restart profiling (stop, clear, start)",
        default=("<Super><Shift>F5",), settings_key="restart-profiling",
    ),
    # ── General (app-wide GActions) ─────────────────────────────────────────
    ActionSpec(
        id="select-tab-details", kind="app", scope="general",
        title="Details tab", default=("<Control>1",),
        detailed_action="win.select-tab::details",
    ),
    ActionSpec(
        id="select-tab-profiler", kind="app", scope="general",
        title="Profiler tab", default=("<Control>2",),
        detailed_action="win.select-tab::profiler",
    ),
    ActionSpec(
        id="select-tab-inspector", kind="app", scope="general",
        title="Inspector tab", default=("<Control>3",),
        detailed_action="win.select-tab::inspector",
    ),
    ActionSpec(
        id="select-tab-logs", kind="app", scope="general",
        title="Logs tab", default=("<Control>4",),
        detailed_action="win.select-tab::logs",
    ),
    ActionSpec(
        id="toggle-sidebar", kind="app", scope="general",
        title="Toggle left panel", default=("F9",),
        detailed_action="win.toggle-sidebar",
    ),
    ActionSpec(
        id="show-shortcuts", kind="app", scope="general",
        title="Keyboard shortcuts", default=("<Control>question", "F1"),
        detailed_action="win.show-shortcuts",
    ),
    ActionSpec(
        id="quit", kind="app", scope="general",
        title="Quit", default=("<Control>q",),
        detailed_action="app.quit",
    ),
    # ── Profiler tab (tab-scoped; live only while that tab is selected) ────
    ActionSpec(
        id="profiler-run", kind="tab", scope="profiler",
        title="Start / stop profiling", default=("<Control>r",),
    ),
    ActionSpec(
        id="profiler-filter", kind="tab", scope="profiler",
        title="Focus the function filter", default=("<Control>f",),
    ),
    ActionSpec(
        id="profiler-save", kind="tab", scope="profiler",
        title="Save profile", default=("<Control>s",),
    ),
    ActionSpec(
        id="profiler-load", kind="tab", scope="profiler",
        title="Load profile", default=("<Control>o",),
    ),
    ActionSpec(
        id="profiler-clear", kind="tab", scope="profiler",
        title="Clear profiling data", default=("<Control>l",),
    ),
    # ── Logs tab ─────────────────────────────────────────────────────────
    ActionSpec(
        id="logs-run", kind="tab", scope="logs",
        title="Start / stop reading the journal", default=("<Control>r",),
    ),
    ActionSpec(
        id="logs-search", kind="tab", scope="logs",
        title="Focus the search field", default=("<Control>f",),
    ),
    ActionSpec(
        id="logs-export", kind="tab", scope="logs",
        title="Export the visible log", default=("<Control>s",),
    ),
    ActionSpec(
        id="logs-clear", kind="tab", scope="logs",
        title="Clear the log", default=("<Control>l",),
    ),
    # ── Inspector tab ────────────────────────────────────────────────────
    ActionSpec(
        id="inspector-refresh", kind="tab", scope="inspector",
        title="Refresh properties", default=("<Control>r",),
    ),
)

SPEC_BY_ID: dict[str, ActionSpec] = {spec.id: spec for spec in CATALOG}


def _default_path() -> Path:
    """Location of ``shortcuts.json`` in the app's data dir. ``gi`` is
    imported lazily so the manager stays unit-testable (with an explicit
    path) where PyGObject is unavailable — see ``SourceRegistry``."""
    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    return Path(GLib.get_user_data_dir()) / "gse-profiler" / "shortcuts.json"


class KeybindingManager:
    """Registry of user-customizable keyboard shortcuts.

    Owns ``app``/``tab`` overrides (persisted to ``shortcuts.json``) and
    caches the last-known ``global`` values reported by the bridge. Uses a
    small hand-rolled observer mechanism rather than GObject signals so this
    class carries no ``gi`` dependency at import time.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else _default_path()
        self._overrides: dict[str, list[str]] = {}
        self._global_cache: dict[str, list[str]] = {}
        self._global_available = False
        self._changed_listeners: dict[int, Callable[[str], None]] = {}
        self._global_edit_listeners: dict[int, Callable[[str, list[str]], None]] = {}
        self._next_handler_id = 1
        self._load()

    # ── Observer registration ───────────────────────────────────────────────

    def connect_changed(self, callback: Callable[[str], None]) -> int:
        """Register a callback for ``changed(action_id)``. ``action_id`` is
        ``""`` for bulk changes (reset-all, bridge availability toggling)
        where listeners should just refresh everything. Returns a handler id
        for :meth:`disconnect`."""
        hid = self._next_handler_id
        self._next_handler_id += 1
        self._changed_listeners[hid] = callback
        return hid

    def connect_global_edit(self, callback: Callable[[str, list[str]], None]) -> int:
        """Register a callback for ``global_edit(settings_key, accels)``,
        fired only when a *user* edit changes a global-kind shortcut (never
        for updates that originated from the bridge itself), so the caller
        can forward it to the bridge without an echo loop."""
        hid = self._next_handler_id
        self._next_handler_id += 1
        self._global_edit_listeners[hid] = callback
        return hid

    def disconnect(self, handler_id: int) -> None:
        self._changed_listeners.pop(handler_id, None)
        self._global_edit_listeners.pop(handler_id, None)

    def _emit_changed(self, action_id: str) -> None:
        for callback in list(self._changed_listeners.values()):
            callback(action_id)

    def _emit_global_edit(self, settings_key: str, accels: list[str]) -> None:
        for callback in list(self._global_edit_listeners.values()):
            callback(settings_key, accels)

    # ── Loading ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            _log.warning("Could not read shortcuts file %s: %s", self._path, exc)
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            _log.warning(
                "Shortcuts file %s is corrupt (%s); starting empty", self._path, exc
            )
            return
        if not isinstance(data, dict):
            return
        for action_id, accels in data.items():
            spec = SPEC_BY_ID.get(action_id)
            # Unknown ids (older/newer catalog) and global ids (persisted by
            # the bridge, not us) are both silently skipped.
            if spec is None or spec.kind == "global":
                continue
            if not isinstance(accels, list) or not accels:
                continue
            if not all(isinstance(a, str) for a in accels):
                continue
            self._overrides[action_id] = list(accels)

    # ── Persisting ───────────────────────────────────────────────────────

    def _persist(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=".shortcuts-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self._overrides, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                os.replace(tmp, self._path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            _log.error("Could not write shortcuts file %s: %s", self._path, exc)

    # ── Public API ───────────────────────────────────────────────────────

    def spec_for(self, action_id: str) -> ActionSpec:
        return SPEC_BY_ID[action_id]

    @property
    def global_available(self) -> bool:
        """Whether the bridge has reported real global-shortcut values.

        False until the first successful ``get_keybindings`` round-trip (or
        after the bridge disconnects) — capture UI for global rows should
        stay disabled while this is False since edits would have nowhere to
        go."""
        return self._global_available

    def get_accels(self, action_id: str) -> list[str]:
        spec = SPEC_BY_ID[action_id]
        if spec.kind == "global":
            return list(self._global_cache.get(action_id, spec.default))
        return list(self._overrides.get(action_id, spec.default))

    def set_accels(self, action_id: str, accels: list[str]) -> None:
        spec = SPEC_BY_ID[action_id]
        accels = list(accels)
        if spec.kind == "global":
            assert spec.settings_key is not None
            self._global_cache[action_id] = accels
            self._emit_changed(action_id)
            self._emit_global_edit(spec.settings_key, accels)
            return
        if accels == list(spec.default):
            self._overrides.pop(action_id, None)
        else:
            self._overrides[action_id] = accels
        self._persist()
        self._emit_changed(action_id)

    def reset(self, action_id: str) -> None:
        spec = SPEC_BY_ID[action_id]
        self.set_accels(action_id, list(spec.default))

    def reset_all(self) -> None:
        self._overrides.clear()
        self._persist()
        for spec in CATALOG:
            if spec.kind == "global":
                assert spec.settings_key is not None
                self._global_cache[spec.id] = list(spec.default)
                self._emit_global_edit(spec.settings_key, list(spec.default))
        self._emit_changed("")

    def find_conflict(self, action_id: str, accel: str) -> str | None:
        """Return the id of another shortcut in ``action_id``'s active
        domain already bound to ``accel``, or None. Domains: global shortcuts
        only ever collide with other global shortcuts (separate input path);
        app shortcuts are always live, so they collide with every other app
        shortcut *and* every tab shortcut (any scope); a tab shortcut only
        collides with app shortcuts and other tab shortcuts in the same
        scope (a different tab's Ctrl+R is never visible at the same time)."""
        spec = SPEC_BY_ID[action_id]
        for other_id in self._domain_ids(spec):
            if other_id == action_id:
                continue
            if accel in self.get_accels(other_id):
                return other_id
        return None

    def update_global_from_bridge(self, bindings: dict[str, list[str]]) -> None:
        """Apply a ``{settings_key: [accel, ...]}`` snapshot reported by the
        bridge. An empty mapping means the bridge couldn't load its schema
        (see ``extension.js`` ``_registerKeybindings``) — treated as
        unavailable rather than "all shortcuts are unbound"."""
        if not bindings:
            self.set_global_available(False)
            return
        self._global_available = True
        changed = False
        for spec in CATALOG:
            if spec.kind != "global" or spec.settings_key not in bindings:
                continue
            accels = list(bindings[spec.settings_key])
            if self._global_cache.get(spec.id) != accels:
                self._global_cache[spec.id] = accels
                changed = True
        if changed:
            self._emit_changed("")

    def set_global_available(self, available: bool) -> None:
        if self._global_available != available:
            self._global_available = available
            self._emit_changed("")

    # ── Internal ─────────────────────────────────────────────────────────

    def _domain_ids(self, spec: ActionSpec) -> list[str]:
        if spec.kind == "global":
            return [s.id for s in CATALOG if s.kind == "global"]
        if spec.kind == "app":
            return [s.id for s in CATALOG if s.kind in ("app", "tab")]
        return [
            s.id for s in CATALOG
            if s.kind == "app" or (s.kind == "tab" and s.scope == spec.scope)
        ]


def populate_shortcut_controller(
    controller: "Gtk.ShortcutController",
    manager: KeybindingManager,
    bindings: list[tuple[str, Callable[..., bool]]],
) -> None:
    """(Re)build a ``Gtk.ShortcutController``'s shortcuts from the manager's
    current accelerator values. Clears any existing shortcuts first, so it is
    safe to call again whenever the manager's ``changed`` signal fires."""
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    n = controller.get_n_items()
    for i in reversed(range(n)):
        controller.remove_shortcut(controller.get_item(i))

    for action_id, callback in bindings:
        for accel in manager.get_accels(action_id):
            try:
                # PyGObject raises rather than returning None on a NULL
                # return here (the gir data doesn't mark it nullable) — a
                # hand-edited shortcuts.json could still contain a garbage
                # string that passed the manager's shape-only validation.
                trigger = Gtk.ShortcutTrigger.parse_string(accel)
            except TypeError:
                trigger = None
            if trigger is None:
                _log.warning("Could not parse accelerator %r for %s", accel, action_id)
                continue
            controller.add_shortcut(
                Gtk.Shortcut.new(trigger, Gtk.CallbackAction.new(callback))
            )
