#!/usr/bin/env python3
"""Guardrail: verify the assets hot-linked by the external landing page.

The GSE Profiler landing page in the `todevelopers/flatpaks` repo (served on
GitHub Pages at https://todevelopers.github.io/flatpaks/gseprofiler/) references
the screenshots and the app icon directly from this repo's `main` branch via
raw.githubusercontent.com URLs. Renaming, moving, or deleting any of those files
silently breaks the live page.

This script fails if any required file is missing, empty, or obviously corrupt.
The required paths are listed in `.github/landing-page-assets.txt`. If you change
one on purpose, update the landing page too, then update that manifest.

Run locally:  python3 scripts/check-landing-page-assets.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / ".github" / "landing-page-assets.txt"
LANDING_PAGE = "https://todevelopers.github.io/flatpaks/gseprofiler/"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def load_manifest() -> list[str]:
    if not MANIFEST.exists():
        sys.exit(f"manifest not found: {MANIFEST}")
    paths: list[str] = []
    for raw in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        paths.append(line)
    return paths


def check(rel: str) -> str | None:
    """Return an error description if the asset is bad, else None."""
    p = REPO_ROOT / rel
    if not p.exists():
        return "missing"
    if not p.is_file():
        return "not a file"
    data = p.read_bytes()
    if not data:
        return "empty"
    suffix = p.suffix.lower()
    if suffix == ".png" and not data.startswith(PNG_MAGIC):
        return "not a valid PNG (bad magic bytes)"
    if suffix == ".svg" and b"<svg" not in data[:4096]:
        return "not a valid SVG (no <svg> tag)"
    return None


def main() -> int:
    paths = load_manifest()
    if not paths:
        sys.exit("manifest is empty — nothing to check")

    errors: list[tuple[str, str]] = []
    for rel in paths:
        err = check(rel)
        print(f"{'OK  ' if err is None else 'FAIL'} {rel}" + (f"  ({err})" if err else ""))
        if err:
            errors.append((rel, err))

    if errors:
        print()
        print("ERROR: Landing-page asset guard failed.")
        print(f"  These files are hot-linked by the landing page ({LANDING_PAGE})")
        print("  and must keep their exact paths and names:")
        for rel, err in errors:
            print(f"    - {rel}  [{err}]")
        print()
        print("  If you renamed/moved/removed one on purpose, update the landing page")
        print("  in todevelopers/flatpaks (gseprofiler/index.html), then update")
        print("  .github/landing-page-assets.txt to match.")
        return 1

    print()
    print(f"OK: all {len(paths)} landing-page assets present and valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
