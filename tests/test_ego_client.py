"""Unit tests for ego_client parsing helpers — requires PyGObject (gi)."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ego_client imports gi (Soup); skip the whole module when it is unavailable.
pytest.importorskip("gi", reason="PyGObject (gi) not available in this environment")


# ─── Search parsing ────────────────────────────────────────────────────────


def test_parse_search_extracts_rows() -> None:
    from app.core.ego_client import _parse_search

    data = {
        "extensions": [
            {
                "uuid": "a@x",
                "name": "Alpha",
                "creator": "Alice",
                "description": "First",
                "pk": 11,
                "shell_version_map": {"48": {"pk": 100, "version": 3}},
            }
        ],
        "total": 1,
        "numpages": 1,
    }
    rows = _parse_search(data)
    assert len(rows) == 1
    row = rows[0]
    assert row["uuid"] == "a@x"
    assert row["name"] == "Alpha"
    assert row["creator"] == "Alice"
    assert row["pk"] == 11
    assert row["shell_version_map"] == {"48": {"pk": 100, "version": 3}}


def test_parse_search_skips_items_without_identifiers() -> None:
    from app.core.ego_client import _parse_search

    data = {
        "extensions": [
            {"name": "no uuid or pk"},
            {"uuid": "b@x"},  # missing pk
            {"uuid": "c@x", "pk": 5},  # valid
        ]
    }
    rows = _parse_search(data)
    assert [r["uuid"] for r in rows] == ["c@x"]


def test_parse_search_empty() -> None:
    from app.core.ego_client import _parse_search

    assert _parse_search({}) == []
    assert _parse_search({"extensions": None}) == []


# ─── Info parsing ──────────────────────────────────────────────────────────


def test_parse_info_valid() -> None:
    from app.core.ego_client import _parse_info

    data = {
        "uuid": "dash-to-dock@micxgx.gmail.com",
        "name": "Dash to Dock",
        "creator": "michele_g",
        "description": "A dock",
        "pk": 307,
        "version": 105,
        "version_tag": 69959,
        "download_url": "/download-extension/dash-to-dock@micxgx.gmail.com.shell-extension.zip?version_tag=69959",
        "shell_version_map": {"48": {"pk": 69959, "version": 105}},
    }
    info = _parse_info(data)
    assert info is not None
    assert info["uuid"] == "dash-to-dock@micxgx.gmail.com"
    assert info["pk"] == 307
    assert info["version"] == 105
    assert info["version_tag"] == 69959
    assert info["download_url"].startswith("/download-extension/")


def test_parse_info_missing_version_tag_returns_none() -> None:
    from app.core.ego_client import _parse_info

    assert _parse_info({"uuid": "x@x", "pk": 1, "version": 2}) is None


def test_parse_info_missing_uuid_returns_none() -> None:
    from app.core.ego_client import _parse_info

    assert _parse_info({"pk": 1, "version": 2, "version_tag": 3}) is None


# ─── Compatibility ─────────────────────────────────────────────────────────


def test_is_compatible() -> None:
    from app.core.ego_client import is_compatible

    svm = {"47": {"pk": 1, "version": 3}, "48": {"pk": 2, "version": 4}}
    assert is_compatible(svm, "48") is True
    assert is_compatible(svm, "49") is False
    # Unknown shell version → assume compatible rather than hide everything.
    assert is_compatible(svm, None) is True
    assert is_compatible({}, "48") is False


# ─── Direct input parsing ──────────────────────────────────────────────────


def test_parse_ego_input_url() -> None:
    from app.core.ego_client import parse_ego_input

    assert parse_ego_input(
        "https://extensions.gnome.org/extension/307/dash-to-dock/"
    ) == ("pk", 307)
    assert parse_ego_input("extensions.gnome.org/extension/42/foo") == ("pk", 42)


def test_parse_ego_input_uuid() -> None:
    from app.core.ego_client import parse_ego_input

    assert parse_ego_input("dash-to-dock@micxgx.gmail.com") == (
        "uuid",
        "dash-to-dock@micxgx.gmail.com",
    )
    assert parse_ego_input("  myext@me  ") == ("uuid", "myext@me")


def test_parse_ego_input_search_term() -> None:
    from app.core.ego_client import parse_ego_input

    # Free-text queries and GitHub-style owner/repo are not direct references.
    assert parse_ego_input("dash to dock") is None
    assert parse_ego_input("clipboard") is None
    assert parse_ego_input("owner/repo") is None
    assert parse_ego_input("") is None
