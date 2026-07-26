"""Headless protocol regressions for typed Inspector paths."""

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from app.ui.inspector_view import _paths_equal


def test_map_path_comparison_keeps_boolean_and_number_keys_distinct() -> None:
    boolean_path = [{"kind": "map-key", "key": True}]
    numeric_path = [{"kind": "map-key", "key": 1}]

    assert _paths_equal(boolean_path, boolean_path)
    assert _paths_equal(numeric_path, numeric_path)
    assert not _paths_equal(boolean_path, numeric_path)


def test_property_path_comparison_accepts_legacy_string_segments() -> None:
    assert _paths_equal(
        ["manager"],
        [{"kind": "property", "key": "manager"}],
    )
