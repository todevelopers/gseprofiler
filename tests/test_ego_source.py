"""Unit tests for EgoSource — pure Python, no PyGObject required."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.ego_source import EgoSource


def _src(**overrides: object) -> EgoSource:
    kwargs: dict[str, object] = {
        "pk": 307,
        "uuid": "dash-to-dock@micxgx.gmail.com",
        "version": 105,
        "version_tag": 69959,
        "name": "Dash to Dock",
        "installed_at": "2026-07-14T10:00:00+00:00",
        "description": "A dock for the Gnome Shell.",
    }
    kwargs.update(overrides)
    return EgoSource(**kwargs)  # type: ignore[arg-type]


def test_to_from_dict_roundtrip() -> None:
    src = _src()
    out = EgoSource.from_dict(src.to_dict())
    assert out == src


def test_from_dict_coerces_numeric_strings() -> None:
    out = EgoSource.from_dict(
        {
            "pk": "307",
            "uuid": "x@x",
            "version": "105",
            "version_tag": "69959",
            "name": "X",
            "installed_at": "",
        }
    )
    assert out is not None
    assert out.pk == 307
    assert out.version == 105
    assert out.version_tag == 69959


def test_from_dict_missing_keys_returns_none() -> None:
    assert EgoSource.from_dict({"pk": 1, "uuid": "x@x"}) is None


def test_from_dict_non_numeric_returns_none() -> None:
    assert (
        EgoSource.from_dict(
            {"pk": "abc", "uuid": "x@x", "version": 1, "version_tag": 2}
        )
        is None
    )


def test_page_url_and_version_label() -> None:
    src = _src(pk=307, version=105)
    assert src.page_url == "https://extensions.gnome.org/extension/307/"
    assert src.version_label == "v105"
