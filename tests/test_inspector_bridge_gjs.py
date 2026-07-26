import shutil
import subprocess
from pathlib import Path

import pytest


def test_inspector_bridge_with_gjs() -> None:
    """Exercise the real GJS Inspector module when GJS is available."""
    gjs = shutil.which("gjs")
    if gjs is None:
        pytest.skip("gjs is not installed")

    repo_root = Path(__file__).resolve().parent.parent
    script = repo_root / "tests" / "test_inspector_bridge.js"
    result = subprocess.run(
        [gjs, "-m", str(script)],
        cwd=repo_root,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "inspector bridge tests OK" in result.stdout
