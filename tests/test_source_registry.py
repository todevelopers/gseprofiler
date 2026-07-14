"""Unit tests for SourceRegistry — pure Python, no PyGObject required.

The registry never touches ``gi`` when given an explicit path, so these
run everywhere (including Windows pytest in the Stop hook).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.ego_source import EgoSource
from app.core.github_source import GitHubSource
from app.core.source_registry import SourceRegistry


def _src(sha: str = "abc1234", owner: str = "o", repo: str = "r") -> GitHubSource:
    return GitHubSource(
        owner=owner,
        repo=repo,
        ref="main",
        commit_sha=sha,
        installed_at="2026-05-28T10:00:00+00:00",
    )


def _ego(pk: int = 307, uuid: str = "d@d", version: int = 105) -> EgoSource:
    return EgoSource(
        pk=pk,
        uuid=uuid,
        version=version,
        version_tag=69959,
        name="Dash to Dock",
        installed_at="2026-07-14T10:00:00+00:00",
    )


def test_set_get_roundtrip(tmp_path: Path) -> None:
    reg = SourceRegistry(tmp_path / "sources.json")
    reg.set("a@a", _src("deadbeef"))

    out = reg.get("a@a")
    assert out is not None
    assert out.commit_sha == "deadbeef"
    assert reg.get("missing@x") is None


def test_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "sources.json"
    SourceRegistry(path).set("a@a", _src("cafef00d"))

    # A fresh instance reads what the first one wrote.
    reloaded = SourceRegistry(path).get("a@a")
    assert reloaded is not None
    assert reloaded.commit_sha == "cafef00d"


def test_missing_file_is_empty(tmp_path: Path) -> None:
    reg = SourceRegistry(tmp_path / "nope.json")
    assert reg.all() == {}


def test_corrupt_file_degrades_to_empty(tmp_path: Path) -> None:
    path = tmp_path / "sources.json"
    path.write_text("{not valid json", encoding="utf-8")
    reg = SourceRegistry(path)
    assert reg.all() == {}
    # And it can still be written to afterwards.
    reg.set("a@a", _src())
    assert SourceRegistry(path).get("a@a") is not None


def test_ignores_malformed_entries(tmp_path: Path) -> None:
    path = tmp_path / "sources.json"
    path.write_text(
        json.dumps(
            {
                "good@x": _src("1111111").to_dict(),
                "not-a-dict@x": "nope",
                "incomplete@x": {"owner": "o"},  # missing required keys
            }
        ),
        encoding="utf-8",
    )
    reg = SourceRegistry(path)
    assert set(reg.all()) == {"good@x"}


def test_remove(tmp_path: Path) -> None:
    reg = SourceRegistry(tmp_path / "sources.json")
    reg.set("a@a", _src())
    assert reg.remove("a@a") is True
    assert reg.get("a@a") is None
    assert reg.remove("a@a") is False  # already gone


def test_reconcile_prunes_when_dir_missing(tmp_path: Path) -> None:
    ext_root = tmp_path / "extensions"
    (ext_root / "present@x").mkdir(parents=True)  # gone@x has no dir
    path = tmp_path / "sources.json"
    reg = SourceRegistry(path)
    reg.set("present@x", _src())
    reg.set("gone@x", _src())

    changed = reg.reconcile(ext_root)
    assert changed is True
    assert set(reg.all()) == {"present@x"}
    # Persisted.
    assert set(SourceRegistry(path).all()) == {"present@x"}


def test_reconcile_keeps_dir_that_exists(tmp_path: Path) -> None:
    """A freshly installed extension (dir on disk) must not be pruned."""
    ext_root = tmp_path / "extensions"
    (ext_root / "present@x").mkdir(parents=True)
    reg = SourceRegistry(tmp_path / "sources.json")
    reg.set("present@x", _src())
    assert reg.reconcile(ext_root) is False
    assert reg.get("present@x") is not None


def test_all_returns_copy(tmp_path: Path) -> None:
    reg = SourceRegistry(tmp_path / "sources.json")
    reg.set("a@a", _src())
    snapshot = reg.all()
    snapshot.clear()
    assert reg.get("a@a") is not None  # internal state untouched


# ─── EGO sources + mixed kinds ─────────────────────────────────────────────


def test_ego_set_get_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "sources.json"
    SourceRegistry(path).set("d@d", _ego(version=105))

    out = SourceRegistry(path).get("d@d")
    assert isinstance(out, EgoSource)
    assert out.version == 105
    assert out.pk == 307


def test_mixed_kinds_load(tmp_path: Path) -> None:
    path = tmp_path / "sources.json"
    reg = SourceRegistry(path)
    reg.set("gh@x", _src("deadbeef"))
    reg.set("ego@x", _ego(uuid="ego@x"))

    reloaded = SourceRegistry(path)
    assert isinstance(reloaded.get("gh@x"), GitHubSource)
    assert isinstance(reloaded.get("ego@x"), EgoSource)


def test_kindless_entry_loads_as_github(tmp_path: Path) -> None:
    """Pre-EGO sources.json entries have no ``kind`` and are GitHub sources."""
    path = tmp_path / "sources.json"
    path.write_text(
        json.dumps({"gh@x": _src("cafef00d").to_dict()}),  # no "kind"
        encoding="utf-8",
    )
    out = SourceRegistry(path).get("gh@x")
    assert isinstance(out, GitHubSource)
    assert out.commit_sha == "cafef00d"


def test_malformed_ego_entry_dropped(tmp_path: Path) -> None:
    path = tmp_path / "sources.json"
    path.write_text(
        json.dumps(
            {
                "good@x": {**_ego(uuid="good@x").to_dict(), "kind": "ego"},
                "bad@x": {"kind": "ego", "uuid": "bad@x"},  # missing version/pk
            }
        ),
        encoding="utf-8",
    )
    reg = SourceRegistry(path)
    assert set(reg.all()) == {"good@x"}


def test_persist_tags_kind(tmp_path: Path) -> None:
    path = tmp_path / "sources.json"
    reg = SourceRegistry(path)
    reg.set("gh@x", _src())
    reg.set("ego@x", _ego(uuid="ego@x"))

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["gh@x"]["kind"] == "github"
    assert raw["ego@x"]["kind"] == "ego"
