"""Unit tests for app.core.settings.Settings — requires PyGObject (gi)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("gi", reason="PyGObject (gi) not available in this environment")


def _settings(tmp_path, monkeypatch, namespace: str):
    from app.core import settings as settings_mod

    monkeypatch.setattr(settings_mod, "_config_dir", lambda: tmp_path)
    return settings_mod.Settings(namespace)


def test_load_missing_returns_empty(tmp_path, monkeypatch) -> None:
    s = _settings(tmp_path, monkeypatch, "profiler")
    assert s.load() == {}
    assert s.get("mode", "swimlane") == "swimlane"


def test_set_and_get(tmp_path, monkeypatch) -> None:
    s = _settings(tmp_path, monkeypatch, "profiler")
    s.set("mode", "flamegraph")
    assert s.get("mode") == "flamegraph"
    # Persists to disk — a fresh instance reads the same value.
    s2 = _settings(tmp_path, monkeypatch, "profiler")
    assert s2.get("mode") == "flamegraph"


def test_merge_semantics_preserve_other_keys(tmp_path, monkeypatch) -> None:
    """Independent writes from different call sites must not clobber each other."""
    s = _settings(tmp_path, monkeypatch, "profiler")
    s.set("mode", "swimlane")
    s.set("hide_idle", True)
    s.update({"paned_pos": 300})
    assert s.load() == {"mode": "swimlane", "hide_idle": True, "paned_pos": 300}


def test_update_with_remove(tmp_path, monkeypatch) -> None:
    s = _settings(tmp_path, monkeypatch, "log-viewer")
    s.update({"journal_cmd": "journalctl -f", "other": 1})
    s.update({"capture": {"scope": "user"}}, remove=("journal_cmd",))
    data = s.load()
    assert "journal_cmd" not in data
    assert data["capture"] == {"scope": "user"}
    assert data["other"] == 1


def test_namespaces_are_isolated(tmp_path, monkeypatch) -> None:
    prof = _settings(tmp_path, monkeypatch, "profiler")
    logs = _settings(tmp_path, monkeypatch, "log-viewer")
    prof.set("mode", "histogram")
    logs.set("capture", {"scope": "both"})
    assert "capture" not in prof.load()
    assert "mode" not in logs.load()


def test_corrupt_file_yields_empty(tmp_path, monkeypatch) -> None:
    s = _settings(tmp_path, monkeypatch, "profiler")
    s.path.parent.mkdir(parents=True, exist_ok=True)
    s.path.write_text("{ not valid json", encoding="utf-8")
    assert s.load() == {}
    # A subsequent write still succeeds (overwrites the corrupt file).
    s.set("mode", "swimlane")
    assert s.get("mode") == "swimlane"


def test_non_object_json_ignored(tmp_path, monkeypatch) -> None:
    s = _settings(tmp_path, monkeypatch, "profiler")
    s.path.parent.mkdir(parents=True, exist_ok=True)
    s.path.write_text("[1, 2, 3]", encoding="utf-8")
    assert s.load() == {}
