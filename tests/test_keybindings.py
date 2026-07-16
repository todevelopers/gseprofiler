"""Unit tests for KeybindingManager — pure Python, no PyGObject required.

The manager never touches ``gi`` when given an explicit path, so these run
everywhere (including Windows pytest in the Stop hook).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.keybindings import CATALOG, SPEC_BY_ID, KeybindingManager


# ─── Defaults / accel round-trip ────────────────────────────────────────────


def test_default_accels_before_any_override(tmp_path: Path) -> None:
    mgr = KeybindingManager(tmp_path / "shortcuts.json")
    assert mgr.get_accels("profiler-run") == ["<Control>r"]
    assert mgr.get_accels("show-shortcuts") == ["<Control>question", "F1"]


def test_global_default_before_bridge_reports(tmp_path: Path) -> None:
    mgr = KeybindingManager(tmp_path / "shortcuts.json")
    assert mgr.get_accels("toggle-profiling") == ["<Super>F5"]
    assert mgr.global_available is False


def test_catalog_ids_are_unique() -> None:
    ids = [spec.id for spec in CATALOG]
    assert len(ids) == len(set(ids))


# ─── app/tab overrides + persistence ────────────────────────────────────────


def test_set_get_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "shortcuts.json"
    mgr = KeybindingManager(path)
    mgr.set_accels("profiler-run", ["<Control>g"])
    assert mgr.get_accels("profiler-run") == ["<Control>g"]


def test_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "shortcuts.json"
    KeybindingManager(path).set_accels("profiler-run", ["<Control>g"])

    reloaded = KeybindingManager(path)
    assert reloaded.get_accels("profiler-run") == ["<Control>g"]


def test_setting_to_default_clears_override(tmp_path: Path) -> None:
    path = tmp_path / "shortcuts.json"
    mgr = KeybindingManager(path)
    mgr.set_accels("profiler-run", ["<Control>g"])
    mgr.set_accels("profiler-run", ["<Control>r"])  # back to default

    assert mgr.get_accels("profiler-run") == ["<Control>r"]
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "profiler-run" not in raw


def test_reset_restores_default(tmp_path: Path) -> None:
    mgr = KeybindingManager(tmp_path / "shortcuts.json")
    mgr.set_accels("profiler-run", ["<Control>g"])
    mgr.reset("profiler-run")
    assert mgr.get_accels("profiler-run") == ["<Control>r"]


def test_reset_all_clears_every_override(tmp_path: Path) -> None:
    mgr = KeybindingManager(tmp_path / "shortcuts.json")
    mgr.set_accels("profiler-run", ["<Control>g"])
    mgr.set_accels("logs-clear", ["<Control>k"])
    mgr.reset_all()

    assert mgr.get_accels("profiler-run") == ["<Control>r"]
    assert mgr.get_accels("logs-clear") == ["<Control>l"]


def test_missing_file_uses_defaults(tmp_path: Path) -> None:
    mgr = KeybindingManager(tmp_path / "nope.json")
    assert mgr.get_accels("profiler-save") == ["<Control>s"]


def test_corrupt_file_degrades_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "shortcuts.json"
    path.write_text("{not valid json", encoding="utf-8")
    mgr = KeybindingManager(path)
    assert mgr.get_accels("profiler-save") == ["<Control>s"]
    # And it can still be written to afterwards.
    mgr.set_accels("profiler-save", ["<Control>d"])
    assert KeybindingManager(path).get_accels("profiler-save") == ["<Control>d"]


def test_ignores_unknown_and_malformed_entries(tmp_path: Path) -> None:
    path = tmp_path / "shortcuts.json"
    path.write_text(
        json.dumps(
            {
                "profiler-save": ["<Control>d"],
                "not-a-real-action": ["<Control>z"],
                "profiler-load": "not-a-list",
                "profiler-clear": [],
            }
        ),
        encoding="utf-8",
    )
    mgr = KeybindingManager(path)
    assert mgr.get_accels("profiler-save") == ["<Control>d"]
    assert mgr.get_accels("profiler-load") == ["<Control>o"]  # malformed, fell back
    assert mgr.get_accels("profiler-clear") == ["<Control>l"]  # empty list, fell back


def test_global_overrides_are_never_written_to_disk(tmp_path: Path) -> None:
    path = tmp_path / "shortcuts.json"
    mgr = KeybindingManager(path)
    mgr.set_accels("toggle-profiling", ["<Super>F6"])
    assert mgr.get_accels("toggle-profiling") == ["<Super>F6"]
    assert not path.exists()  # nothing to persist app-side for a global spec


# ─── Signals ────────────────────────────────────────────────────────────────


def test_changed_signal_fires_on_set(tmp_path: Path) -> None:
    mgr = KeybindingManager(tmp_path / "shortcuts.json")
    seen: list[str] = []
    mgr.connect_changed(seen.append)
    mgr.set_accels("profiler-run", ["<Control>g"])
    assert seen == ["profiler-run"]


def test_disconnect_stops_further_callbacks(tmp_path: Path) -> None:
    mgr = KeybindingManager(tmp_path / "shortcuts.json")
    seen: list[str] = []
    hid = mgr.connect_changed(seen.append)
    mgr.disconnect(hid)
    mgr.set_accels("profiler-run", ["<Control>g"])
    assert seen == []


def test_global_edit_signal_fires_only_for_global_specs(tmp_path: Path) -> None:
    mgr = KeybindingManager(tmp_path / "shortcuts.json")
    edits: list[tuple[str, list[str]]] = []
    mgr.connect_global_edit(lambda key, accels: edits.append((key, accels)))

    mgr.set_accels("profiler-run", ["<Control>g"])  # tab-kind, no global-edit
    assert edits == []

    mgr.set_accels("toggle-profiling", ["<Super>F6"])
    assert edits == [("toggle-profiling", ["<Super>F6"])]


def test_reset_all_emits_global_edit_for_each_global_spec(tmp_path: Path) -> None:
    mgr = KeybindingManager(tmp_path / "shortcuts.json")
    mgr.set_accels("toggle-profiling", ["<Super>F6"])
    edits: list[tuple[str, list[str]]] = []
    mgr.connect_global_edit(lambda key, accels: edits.append((key, accels)))

    mgr.reset_all()

    assert ("toggle-profiling", ["<Super>F5"]) in edits
    assert ("restart-profiling", ["<Super><Shift>F5"]) in edits


# ─── Bridge round-trip (global availability) ────────────────────────────────


def test_update_global_from_bridge_marks_available(tmp_path: Path) -> None:
    mgr = KeybindingManager(tmp_path / "shortcuts.json")
    mgr.update_global_from_bridge(
        {"toggle-profiling": ["<Super>F5"], "restart-profiling": ["<Super><Shift>F5"]}
    )
    assert mgr.global_available is True
    assert mgr.get_accels("toggle-profiling") == ["<Super>F5"]


def test_update_global_from_bridge_reflects_customized_value(tmp_path: Path) -> None:
    mgr = KeybindingManager(tmp_path / "shortcuts.json")
    mgr.update_global_from_bridge({"toggle-profiling": ["<Super>F6"]})
    assert mgr.get_accels("toggle-profiling") == ["<Super>F6"]


def test_empty_bridge_bindings_means_unavailable(tmp_path: Path) -> None:
    mgr = KeybindingManager(tmp_path / "shortcuts.json")
    mgr.update_global_from_bridge({})
    assert mgr.global_available is False


def test_set_global_available_toggles_and_notifies(tmp_path: Path) -> None:
    mgr = KeybindingManager(tmp_path / "shortcuts.json")
    seen: list[str] = []
    mgr.connect_changed(seen.append)

    mgr.set_global_available(True)
    assert mgr.global_available is True
    assert seen == [""]

    mgr.set_global_available(True)  # no-op, no duplicate notification
    assert seen == [""]

    mgr.set_global_available(False)
    assert seen == ["", ""]


# ─── Conflict detection ─────────────────────────────────────────────────────


def test_conflict_within_same_tab_scope_detected(tmp_path: Path) -> None:
    mgr = KeybindingManager(tmp_path / "shortcuts.json")
    # profiler-save defaults to <Control>s; colliding profiler-load into it.
    conflict = mgr.find_conflict("profiler-load", "<Control>s")
    assert conflict == "profiler-save"


def test_no_conflict_across_different_tab_scopes(tmp_path: Path) -> None:
    mgr = KeybindingManager(tmp_path / "shortcuts.json")
    # profiler-run and logs-run both default to <Control>r but live in
    # different tab scopes — never visible at the same time.
    assert mgr.find_conflict("logs-run", "<Control>r") is None


def test_app_shortcut_conflicts_with_any_tab_scope(tmp_path: Path) -> None:
    mgr = KeybindingManager(tmp_path / "shortcuts.json")
    # toggle-sidebar is an app-kind shortcut (always live); colliding it
    # with a tab-scoped default must be caught regardless of which tab.
    conflict = mgr.find_conflict("toggle-sidebar", "<Control>r")
    assert conflict in {"profiler-run", "logs-run", "inspector-refresh"}


def test_tab_shortcut_conflicts_with_app_shortcut(tmp_path: Path) -> None:
    mgr = KeybindingManager(tmp_path / "shortcuts.json")
    conflict = mgr.find_conflict("profiler-save", "<Control>q")  # app.quit's default
    assert conflict == "quit"


def test_global_only_conflicts_with_global(tmp_path: Path) -> None:
    mgr = KeybindingManager(tmp_path / "shortcuts.json")
    # <Control>r is used by several tab specs, but global shortcuts are a
    # separate input path (shell-level), so no conflict is reported.
    assert mgr.find_conflict("toggle-profiling", "<Control>r") is None
    assert mgr.find_conflict("toggle-profiling", "<Super><Shift>F5") == "restart-profiling"


def test_no_self_conflict(tmp_path: Path) -> None:
    mgr = KeybindingManager(tmp_path / "shortcuts.json")
    assert mgr.find_conflict("profiler-run", "<Control>r") is None


def test_spec_for_returns_catalog_entry(tmp_path: Path) -> None:
    mgr = KeybindingManager(tmp_path / "shortcuts.json")
    spec = mgr.spec_for("profiler-run")
    assert spec is SPEC_BY_ID["profiler-run"]
    assert spec.title == "Start / stop profiling"
