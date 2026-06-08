"""The :class:`GitHubSource` provenance record.

Lives in its own module so both :mod:`app.core.github_installer` (which
produces it) and :mod:`app.core.source_registry` (which persists it) can
import it without a circular dependency.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class GitHubSource:
    """Metadata about where a GitHub-sourced extension came from.

    Persisted by :class:`app.core.source_registry.SourceRegistry`.
    """

    owner: str
    repo: str
    ref: str  # branch name we tracked (default branch in V1)
    commit_sha: str
    installed_at: str  # ISO-8601 UTC
    subpath: str = ""  # subdirectory within the repo that holds the extension

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GitHubSource | None:
        try:
            return cls(
                owner=str(d["owner"]),
                repo=str(d["repo"]),
                ref=str(d["ref"]),
                commit_sha=str(d["commit_sha"]),
                installed_at=str(d.get("installed_at", "")),
                subpath=str(d.get("subpath", "")),
            )
        except (KeyError, TypeError):
            return None

    @property
    def html_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"

    @property
    def tree_url(self) -> str:
        """Repo page, or the subdirectory's tree page when installed from one."""
        if self.subpath:
            return f"{self.html_url}/tree/{self.ref}/{self.subpath}"
        return self.html_url

    @property
    def short_sha(self) -> str:
        return self.commit_sha[:7] if self.commit_sha else ""
