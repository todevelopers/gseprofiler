"""Remove an installed GNOME Shell extension from disk.

Standalone helper so callers that only need to delete an extension
directory do not have to depend on :class:`GitHubInstaller`.  Uninstall
applies to any user-installed extension, not just GitHub-sourced ones.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

_log = logging.getLogger(__name__)


def remove_extension(extension_path: Path) -> bool:
    """Remove an installed extension directory.

    Returns ``True`` on success (including when the directory is already
    gone).  Caller is responsible for disabling the extension first.
    """
    try:
        shutil.rmtree(extension_path)
    except FileNotFoundError:
        return True
    except OSError as exc:
        _log.error("Uninstall failed: %s", exc)
        return False
    _log.info("Removed %s", extension_path)
    return True
