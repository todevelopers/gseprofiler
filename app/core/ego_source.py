"""The :class:`EgoSource` provenance record.

Lives in its own module so both :mod:`app.core.ego_installer` (which produces
it) and :mod:`app.core.source_registry` (which persists it) can import it
without a circular dependency.  Mirrors :mod:`app.core.github_source`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

EGO_BASE_URL = "https://extensions.gnome.org"


@dataclass
class EgoSource:
    """Metadata about an extension installed from extensions.gnome.org.

    ``version`` is EGO's monotonic per-extension version number (also written
    into the extension's own ``metadata.json``); ``version_tag`` is the id of
    the exact uploaded version we downloaded, used to re-download precisely.
    Persisted by :class:`app.core.source_registry.SourceRegistry`.
    """

    pk: int  # stable EGO extension id
    uuid: str
    version: int  # installed EGO version number
    version_tag: int  # download tag (id of the uploaded version) we installed
    name: str
    installed_at: str  # ISO-8601 UTC
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EgoSource | None:
        try:
            return cls(
                pk=int(d["pk"]),
                uuid=str(d["uuid"]),
                version=int(d["version"]),
                version_tag=int(d["version_tag"]),
                name=str(d.get("name", "")),
                installed_at=str(d.get("installed_at", "")),
                description=str(d.get("description", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def page_url(self) -> str:
        """The extension's page on extensions.gnome.org."""
        return f"{EGO_BASE_URL}/extension/{self.pk}/"

    @property
    def version_label(self) -> str:
        return f"v{self.version}"
