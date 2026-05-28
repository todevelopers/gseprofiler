"""Unit tests for github_installer — requires PyGObject (skip on Windows)."""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Skip the whole module when PyGObject is missing — github_installer imports gi.
pytest.importorskip("gi", reason="PyGObject (gi) not available in this environment")


# ─── URL parsing ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("owner/repo", ("owner", "repo")),
        ("octo-org/my.ext-thing", ("octo-org", "my.ext-thing")),
        ("https://github.com/owner/repo", ("owner", "repo")),
        ("https://github.com/owner/repo.git", ("owner", "repo")),
        ("https://github.com/owner/repo/", ("owner", "repo")),
        ("http://github.com/owner/repo", ("owner", "repo")),
        ("  owner/repo  ", ("owner", "repo")),
    ],
)
def test_parse_repo_url_valid(url: str, expected: tuple[str, str]) -> None:
    from app.core.github_installer import parse_repo_url

    assert parse_repo_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "not a url",
        "owner",
        "/repo",
        "owner/",
        "https://gitlab.com/owner/repo",
        "https://example.com/owner/repo",
    ],
)
def test_parse_repo_url_invalid(url: str) -> None:
    from app.core.github_installer import parse_repo_url

    assert parse_repo_url(url) is None


# ─── Tarball helpers ──────────────────────────────────────────────────────


def _make_tarball(root_name: str, files: dict[str, str | bytes]) -> bytes:
    """Build an in-memory tar.gz with one top-level dir and the given files."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, content in files.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            info = tarfile.TarInfo(name=f"{root_name}/{rel}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# ─── Filter behaviour ─────────────────────────────────────────────────────


def test_filter_strips_git_meta_and_export_ignore_and_gitignore(tmp_path: Path) -> None:
    from app.core.github_installer import _do_extract_install

    meta = json.dumps({"uuid": "test@ex", "name": "Test", "shell-version": ["48"]})
    tar = _make_tarball(
        "repo-deadbeef",
        {
            "metadata.json": meta,
            "extension.js": "// hello",
            ".gitignore": "screenshot.png\n*.log\n",
            ".gitattributes": "tests/* export-ignore\nbuild.yaml export-ignore\n",
            ".github/workflows/ci.yml": "name: ci",
            "tests/test_foo.js": "// will be stripped via gitattributes",
            "build.yaml": "build",
            "screenshot.png": b"\x89PNG\r\n",
            "debug.log": "ignored",
            "README.md": "# Readme stays",
        },
    )

    uuid = _do_extract_install(
        owner="o",
        repo="r",
        ref="main",
        sha="deadbeefcafe",
        tarball=tar,
        extensions_root=tmp_path,
    )
    assert uuid == "test@ex"
    target = tmp_path / "test@ex"

    # Kept
    assert (target / "metadata.json").is_file()
    assert (target / "extension.js").is_file()
    assert (target / "README.md").is_file()

    # Stripped — git meta
    assert not (target / ".gitignore").exists()
    assert not (target / ".gitattributes").exists()
    assert not (target / ".github").exists()

    # Stripped — .gitattributes export-ignore
    assert not (target / "tests").exists()
    assert not (target / "build.yaml").exists()

    # Stripped — .gitignore patterns
    assert not (target / "screenshot.png").exists()
    assert not (target / "debug.log").exists()


def test_install_records_github_source(tmp_path: Path) -> None:
    from app.core.github_installer import SOURCE_KEY, _do_extract_install

    meta = json.dumps({"uuid": "test@ex", "name": "Test"})
    tar = _make_tarball(
        "repo-cafef00d", {"metadata.json": meta, "extension.js": ""}
    )
    _do_extract_install("owner", "repo", "main", "cafef00d", tar, tmp_path)

    written = json.loads(
        (tmp_path / "test@ex" / "metadata.json").read_text(encoding="utf-8")
    )
    src = written[SOURCE_KEY]
    assert src["owner"] == "owner"
    assert src["repo"] == "repo"
    assert src["ref"] == "main"
    assert src["commit_sha"] == "cafef00d"
    assert src["installed_at"]  # non-empty ISO timestamp


def test_install_rejects_missing_metadata(tmp_path: Path) -> None:
    from app.core.github_installer import InstallError, _do_extract_install

    tar = _make_tarball("repo-x", {"extension.js": ""})  # no metadata.json
    with pytest.raises(InstallError, match="metadata.json"):
        _do_extract_install("o", "r", "main", "x", tar, tmp_path)


def test_install_rejects_metadata_without_uuid(tmp_path: Path) -> None:
    from app.core.github_installer import InstallError, _do_extract_install

    tar = _make_tarball("repo-x", {"metadata.json": json.dumps({"name": "no uuid"})})
    with pytest.raises(InstallError, match="uuid"):
        _do_extract_install("o", "r", "main", "x", tar, tmp_path)


def test_filter_guard_when_metadata_in_gitignore(tmp_path: Path) -> None:
    """If .gitignore would remove metadata.json, install must fail loudly."""
    from app.core.github_installer import InstallError, _do_extract_install

    pathspec = pytest.importorskip(
        "pathspec", reason="pathspec not installed — filter degrades"
    )
    assert pathspec is not None  # silence linter

    tar = _make_tarball(
        "repo-x",
        {
            "metadata.json": json.dumps({"uuid": "x@x"}),
            ".gitignore": "*.json\n",
        },
    )
    with pytest.raises(InstallError, match="metadata.json"):
        _do_extract_install("o", "r", "main", "x", tar, tmp_path)


def test_update_preserves_installed_at(tmp_path: Path) -> None:
    """Re-installing into an existing target keeps the original installed_at."""
    from app.core.github_installer import SOURCE_KEY, _do_extract_install

    meta = json.dumps({"uuid": "test@ex", "name": "Test"})
    tar1 = _make_tarball("repo-aaaaaaa", {"metadata.json": meta})
    _do_extract_install("o", "r", "main", "aaaaaaa", tar1, tmp_path)

    first = json.loads(
        (tmp_path / "test@ex" / "metadata.json").read_text(encoding="utf-8")
    )
    first_installed_at = first[SOURCE_KEY]["installed_at"]

    # Now install a different SHA into the same UUID — installed_at must persist.
    tar2 = _make_tarball("repo-bbbbbbb", {"metadata.json": meta})
    _do_extract_install("o", "r", "main", "bbbbbbb", tar2, tmp_path)

    second = json.loads(
        (tmp_path / "test@ex" / "metadata.json").read_text(encoding="utf-8")
    )
    assert second[SOURCE_KEY]["commit_sha"] == "bbbbbbb"
    assert second[SOURCE_KEY]["installed_at"] == first_installed_at


# ─── read_source / list_github_extensions ─────────────────────────────────


def test_read_source_returns_none_for_plain_extension(tmp_path: Path) -> None:
    from app.core.github_installer import read_source

    (tmp_path / "metadata.json").write_text(
        json.dumps({"uuid": "x@x"}), encoding="utf-8"
    )
    assert read_source(tmp_path) is None


def test_read_source_parses_recorded_source(tmp_path: Path) -> None:
    from app.core.github_installer import SOURCE_KEY, read_source

    src = {
        "owner": "o",
        "repo": "r",
        "ref": "main",
        "commit_sha": "abcdef0",
        "installed_at": "2026-05-28T10:00:00+00:00",
    }
    (tmp_path / "metadata.json").write_text(
        json.dumps({"uuid": "x@x", SOURCE_KEY: src}), encoding="utf-8"
    )
    out = read_source(tmp_path)
    assert out is not None
    assert out.owner == "o"
    assert out.short_sha == "abcdef0"
    assert out.html_url == "https://github.com/o/r"


def test_list_github_extensions_filters_correctly(tmp_path: Path) -> None:
    from app.core.github_installer import SOURCE_KEY, list_github_extensions

    p1 = tmp_path / "a"
    p1.mkdir()
    (p1 / "metadata.json").write_text(json.dumps({"uuid": "a@a"}), encoding="utf-8")

    p2 = tmp_path / "b"
    p2.mkdir()
    (p2 / "metadata.json").write_text(
        json.dumps(
            {
                "uuid": "b@b",
                SOURCE_KEY: {
                    "owner": "o",
                    "repo": "r",
                    "ref": "main",
                    "commit_sha": "deadbeef",
                    "installed_at": "2026-05-28T10:00:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )

    extensions = {
        "a@a": {"path": str(p1)},
        "b@b": {"path": str(p2)},
        "c@c": {"path": ""},  # no path — skipped
    }
    result = list_github_extensions(extensions)
    assert set(result) == {"b@b"}
    assert result["b@b"].owner == "o"
