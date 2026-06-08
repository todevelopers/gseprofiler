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
        ("owner/repo", ("owner", "repo", None, None)),
        ("octo-org/my.ext-thing", ("octo-org", "my.ext-thing", None, None)),
        ("https://github.com/owner/repo", ("owner", "repo", None, None)),
        ("https://github.com/owner/repo.git", ("owner", "repo", None, None)),
        ("https://github.com/owner/repo/", ("owner", "repo", None, None)),
        ("http://github.com/owner/repo", ("owner", "repo", None, None)),
        ("  owner/repo  ", ("owner", "repo", None, None)),
        ("https://github.com/owner/repo?tab=readme", ("owner", "repo", None, None)),
        # Subdirectory (/tree/<ref>/<subpath>) URLs.
        (
            "https://github.com/sakithb/media-controls/tree/main/src",
            ("sakithb", "media-controls", "main", "src"),
        ),
        (
            "https://github.com/o/r/tree/master/a/b/c",
            ("o", "r", "master", "a/b/c"),
        ),
        ("https://github.com/o/r/tree/main/src/", ("o", "r", "main", "src")),
        # /tree/<ref> with no subpath → branch pinned, root extension.
        ("https://github.com/o/r/tree/develop", ("o", "r", "develop", None)),
    ],
)
def test_parse_repo_url_valid(url: str, expected: tuple) -> None:
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
        "https://github.com/o/r/tree/main/../../etc",  # path traversal
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

    uuid, subpath = _do_extract_install(tar, extensions_root=tmp_path)
    assert uuid == "test@ex"
    assert subpath == ""
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


def test_install_leaves_metadata_untouched(tmp_path: Path) -> None:
    """We no longer inject provenance into the upstream metadata.json."""
    from app.core.github_installer import _do_extract_install

    meta_obj = {"uuid": "test@ex", "name": "Test"}
    tar = _make_tarball(
        "repo-cafef00d", {"metadata.json": json.dumps(meta_obj), "extension.js": ""}
    )
    _do_extract_install(tar, extensions_root=tmp_path)

    written = json.loads(
        (tmp_path / "test@ex" / "metadata.json").read_text(encoding="utf-8")
    )
    assert "_gse_profiler_source" not in written
    assert written == meta_obj


def test_install_rejects_missing_metadata(tmp_path: Path) -> None:
    from app.core.github_installer import InstallError, _do_extract_install

    tar = _make_tarball("repo-x", {"extension.js": ""})  # no metadata.json
    with pytest.raises(InstallError, match="metadata.json"):
        _do_extract_install(tar, extensions_root=tmp_path)


def test_install_rejects_metadata_without_uuid(tmp_path: Path) -> None:
    from app.core.github_installer import InstallError, _do_extract_install

    tar = _make_tarball("repo-x", {"metadata.json": json.dumps({"name": "no uuid"})})
    with pytest.raises(InstallError, match="uuid"):
        _do_extract_install(tar, extensions_root=tmp_path)


# ─── Subdirectory installs ────────────────────────────────────────────────


def test_install_from_explicit_subpath(tmp_path: Path) -> None:
    """metadata.json in a subdirectory installs when the subpath is given."""
    from app.core.github_installer import _do_extract_install

    tar = _make_tarball(
        "repo-deadbeef",
        {
            "README.md": "# top level, no extension here",
            "src/metadata.json": json.dumps({"uuid": "sub@ex"}),
            "src/extension.js": "// hi",
        },
    )
    uuid, subpath = _do_extract_install(tar, extensions_root=tmp_path, subpath="src")
    assert uuid == "sub@ex"
    assert subpath == "src"
    # The extension dir contents land directly under <uuid>/, not under src/.
    assert (tmp_path / "sub@ex" / "metadata.json").is_file()
    assert (tmp_path / "sub@ex" / "extension.js").is_file()
    assert not (tmp_path / "sub@ex" / "src").exists()


def test_install_subpath_applies_repo_root_gitignore(tmp_path: Path) -> None:
    """A repo-root .gitignore still filters files inside the subdirectory."""
    pytest.importorskip("pathspec")
    from app.core.github_installer import _do_extract_install

    tar = _make_tarball(
        "repo-x",
        {
            ".gitignore": "*.log\n",
            "src/metadata.json": json.dumps({"uuid": "sub@ex"}),
            "src/debug.log": "noise",
        },
    )
    _do_extract_install(tar, extensions_root=tmp_path, subpath="src")
    assert (tmp_path / "sub@ex" / "metadata.json").is_file()
    assert not (tmp_path / "sub@ex" / "debug.log").exists()


def test_install_rejects_subpath_without_metadata(tmp_path: Path) -> None:
    from app.core.github_installer import InstallError, _do_extract_install

    tar = _make_tarball(
        "repo-x",
        {"src/extension.js": "// no metadata here"},
    )
    with pytest.raises(InstallError, match="metadata.json"):
        _do_extract_install(tar, extensions_root=tmp_path, subpath="src")


def test_install_rejects_missing_subpath(tmp_path: Path) -> None:
    from app.core.github_installer import InstallError, _do_extract_install

    tar = _make_tarball("repo-x", {"metadata.json": json.dumps({"uuid": "x@x"})})
    with pytest.raises(InstallError, match="not found"):
        _do_extract_install(tar, extensions_root=tmp_path, subpath="nope")


def test_install_autodetects_single_nested_metadata(tmp_path: Path) -> None:
    """With no subpath and none at root, a unique nested metadata is found."""
    from app.core.github_installer import _do_extract_install

    tar = _make_tarball(
        "repo-x",
        {
            "README.md": "# monorepo",
            "extension/metadata.json": json.dumps({"uuid": "auto@ex"}),
            "extension/extension.js": "// hi",
        },
    )
    uuid, subpath = _do_extract_install(tar, extensions_root=tmp_path)
    assert uuid == "auto@ex"
    assert subpath == "extension"
    assert (tmp_path / "auto@ex" / "metadata.json").is_file()


def test_install_rejects_ambiguous_metadata_at_same_depth(tmp_path: Path) -> None:
    """Two extensions at the same depth must be rejected, not guessed."""
    from app.core.github_installer import InstallError, _do_extract_install

    tar = _make_tarball(
        "repo-x",
        {
            "one/metadata.json": json.dumps({"uuid": "one@ex"}),
            "two/metadata.json": json.dumps({"uuid": "two@ex"}),
        },
    )
    with pytest.raises(InstallError, match="Multiple extensions"):
        _do_extract_install(tar, extensions_root=tmp_path)


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
        _do_extract_install(tar, extensions_root=tmp_path)


def test_install_compiles_gsettings_schemas(tmp_path: Path) -> None:
    """Extensions with schemas/*.gschema.xml must end up with gschemas.compiled."""
    import shutil as _shutil

    from app.core.github_installer import _do_extract_install

    if _shutil.which("glib-compile-schemas") is None:
        pytest.skip("glib-compile-schemas is not available in this environment")

    schema_xml = (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<schemalist>\n"
        "  <schema id='org.example.test' path='/org/example/test/'>\n"
        "    <key name='enabled' type='b'><default>true</default></key>\n"
        "  </schema>\n"
        "</schemalist>\n"
    )
    tar = _make_tarball(
        "repo-schema",
        {
            "metadata.json": json.dumps({"uuid": "schemas@ex"}),
            "extension.js": "// hi",
            "schemas/org.example.test.gschema.xml": schema_xml,
        },
    )
    _do_extract_install(tar, extensions_root=tmp_path)
    assert (tmp_path / "schemas@ex" / "schemas" / "gschemas.compiled").is_file()


def test_install_fails_on_invalid_schema(tmp_path: Path) -> None:
    """If a schema is malformed, the install must fail loudly (not silently)."""
    import shutil as _shutil

    from app.core.github_installer import InstallError, _do_extract_install

    if _shutil.which("glib-compile-schemas") is None:
        pytest.skip("glib-compile-schemas is not available in this environment")

    tar = _make_tarball(
        "repo-bad",
        {
            "metadata.json": json.dumps({"uuid": "bad@ex"}),
            "schemas/org.example.bad.gschema.xml": "<not-valid-xml>",
        },
    )
    with pytest.raises(InstallError, match="schemas"):
        _do_extract_install(tar, extensions_root=tmp_path)


# ─── Provenance recording (registry) ───────────────────────────────────────


def test_finish_install_records_source_in_registry(tmp_path: Path) -> None:
    from app.core.github_installer import GitHubInstaller
    from app.core.source_registry import SourceRegistry

    reg = SourceRegistry(tmp_path / "sources.json")
    inst = GitHubInstaller(reg)

    inst._finish_install("ext@x", "owner", "repo", "main", "cafef00d", "src", None)

    src = reg.get("ext@x")
    assert src is not None
    assert src.owner == "owner"
    assert src.repo == "repo"
    assert src.ref == "main"
    assert src.commit_sha == "cafef00d"
    assert src.subpath == "src"
    assert src.installed_at  # non-empty ISO timestamp


def test_finish_install_preserves_installed_at_on_update(tmp_path: Path) -> None:
    """Re-installing the same UUID keeps the original installed_at."""
    from app.core.github_installer import GitHubInstaller
    from app.core.source_registry import SourceRegistry

    reg = SourceRegistry(tmp_path / "sources.json")
    inst = GitHubInstaller(reg)

    inst._finish_install("ext@x", "o", "r", "main", "aaaaaaa", "", None)
    first = reg.get("ext@x")
    assert first is not None
    first_installed_at = first.installed_at

    inst._finish_install("ext@x", "o", "r", "main", "bbbbbbb", "", None)
    second = reg.get("ext@x")
    assert second is not None
    assert second.commit_sha == "bbbbbbb"
    assert second.installed_at == first_installed_at
