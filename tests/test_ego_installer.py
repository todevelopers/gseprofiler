"""Unit tests for the EGO zip install body — requires PyGObject (gi)."""

from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ego_installer imports gi (Soup); skip the whole module when unavailable.
pytest.importorskip("gi", reason="PyGObject (gi) not available in this environment")


def _make_zip(files: dict[str, str]) -> bytes:
    """Build an in-memory EGO-style zip (files at the archive root)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_extract_install_lands_uuid_dir(tmp_path: Path) -> None:
    from app.core.ego_installer import _do_extract_install_zip

    data = _make_zip(
        {
            "metadata.json": json.dumps({"uuid": "y@y", "name": "Y", "version": 3}),
            "extension.js": "// hi",
        }
    )
    uuid = _do_extract_install_zip(data, extensions_root=tmp_path)
    assert uuid == "y@y"
    assert (tmp_path / "y@y" / "metadata.json").is_file()
    assert (tmp_path / "y@y" / "extension.js").is_file()


def test_extract_install_from_nested_metadata(tmp_path: Path) -> None:
    """A wrapped archive (files under one dir) is still located."""
    from app.core.ego_installer import _do_extract_install_zip

    data = _make_zip(
        {
            "y@y/metadata.json": json.dumps({"uuid": "y@y"}),
            "y@y/extension.js": "// hi",
        }
    )
    uuid = _do_extract_install_zip(data, extensions_root=tmp_path)
    assert uuid == "y@y"
    assert (tmp_path / "y@y" / "metadata.json").is_file()


def test_extract_install_rejects_missing_metadata(tmp_path: Path) -> None:
    from app.core.ego_installer import _do_extract_install_zip
    from app.core.extension_install import InstallError

    data = _make_zip({"extension.js": "// no metadata"})
    with pytest.raises(InstallError, match="metadata.json"):
        _do_extract_install_zip(data, extensions_root=tmp_path)


def test_extract_install_rejects_metadata_without_uuid(tmp_path: Path) -> None:
    from app.core.ego_installer import _do_extract_install_zip
    from app.core.extension_install import InstallError

    data = _make_zip({"metadata.json": json.dumps({"name": "no uuid"})})
    with pytest.raises(InstallError, match="uuid"):
        _do_extract_install_zip(data, extensions_root=tmp_path)


def test_extract_install_rejects_bad_zip(tmp_path: Path) -> None:
    from app.core.ego_installer import _do_extract_install_zip
    from app.core.extension_install import InstallError

    with pytest.raises(InstallError, match="archive"):
        _do_extract_install_zip(b"not a zip", extensions_root=tmp_path)


def test_install_rejects_incompatible_shell(tmp_path: Path) -> None:
    """An extension without the running shell in its map is refused pre-download."""
    from app.core.ego_client import EgoClient
    from app.core.ego_installer import EgoInstaller
    from app.core.source_registry import SourceRegistry

    inst = EgoInstaller(
        SourceRegistry(tmp_path / "sources.json"), EgoClient()
    )
    captured: dict[str, object] = {}
    info = {
        "uuid": "x@x",
        "pk": 1,
        "version": 2,
        "version_tag": 3,
        "download_url": "/download-extension/x.zip?version_tag=3",
        "shell_version_map": {"47": {"pk": 3, "version": 2}},
    }
    inst._on_info_for_install(
        info,
        None,
        "48",
        lambda uuid, err: captured.update(uuid=uuid, err=err),
        None,
    )
    assert captured["uuid"] is None
    assert "not compatible" in str(captured["err"]).lower()
