"""Async client for the extensions.gnome.org (EGO) web API.

Two endpoints are used:

- ``extension-query`` — full-text search, returns a page of matching
  extensions (used by the install dialog's search-as-you-type).
- ``extension-info`` — full metadata for one extension, including the
  ``download_url`` and the version compatible with a given shell version
  (used by the installer to download and to detect updates).

Network I/O is async via :class:`Soup.Session` on the GTK main loop; the
pure JSON→dict parsing is split into module-level functions so it can be
unit-tested without a network or PyGObject event loop.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Soup", "3.0")
from gi.repository import Gio, GLib, Soup

from app.core.ego_source import EGO_BASE_URL
from app.core.extension_install import http_error

_log = logging.getLogger(__name__)

_QUERY_URL = f"{EGO_BASE_URL}/extension-query/"
_INFO_URL = f"{EGO_BASE_URL}/extension-info/"
_USER_AGENT = "gse-profiler"

#: ``on_search(results, error)`` — results is a list of search-result dicts.
SearchCallback = Callable[[list[dict[str, Any]] | None, str | None], None]
#: ``on_info(info, error)`` — info is a single parsed info dict.
InfoCallback = Callable[[dict[str, Any] | None, str | None], None]


class EgoClient:
    """Thin async wrapper over the EGO search / info endpoints."""

    def __init__(self) -> None:
        self._session = Soup.Session()
        self._session.set_user_agent(_USER_AGENT)

    # ── Search ────────────────────────────────────────────────────────────

    def search(
        self,
        term: str,
        shell_version: str | None,
        on_done: SearchCallback,
        cancellable: Gio.Cancellable | None = None,
    ) -> None:
        """Search EGO for ``term`` (async).

        ``shell_version`` (major, e.g. ``"48"``) filters to compatible
        extensions when given.  ``cancellable`` lets the caller drop a
        stale in-flight search when the user keeps typing; a cancelled
        request never calls ``on_done``.
        """
        params: dict[str, str] = {"search": term, "page": "1", "sort": "popularity"}
        if shell_version:
            params["shell_version"] = shell_version
        url = f"{_QUERY_URL}?{urlencode(params)}"
        msg = Soup.Message.new("GET", url)
        msg.get_request_headers().append("Accept", "application/json")
        self._session.send_and_read_async(
            msg,
            GLib.PRIORITY_DEFAULT,
            cancellable,
            self._on_search_done,
            (msg, on_done),
        )

    def _on_search_done(
        self,
        session: Soup.Session,
        result: Gio.AsyncResult,
        user_data: tuple[Soup.Message, SearchCallback],
    ) -> None:
        msg, on_done = user_data
        try:
            body = bytes(session.send_and_read_finish(result).get_data() or b"")
        except GLib.Error as exc:
            if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                return  # Superseded by a newer search — stay silent.
            on_done(None, f"Network error: {exc.message}")
            return
        if msg.get_status() != Soup.Status.OK:
            on_done(None, http_error(msg, body, "search extensions.gnome.org"))
            return
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            on_done(None, f"Bad response from extensions.gnome.org: {exc}")
            return
        on_done(_parse_search(data), None)

    # ── Info ──────────────────────────────────────────────────────────────

    def fetch_info(
        self,
        uuid: str | None,
        shell_version: str | None,
        on_done: InfoCallback,
        pk: int | None = None,
    ) -> None:
        """Fetch full metadata for an extension (async).

        Looked up by ``uuid`` or, when ``pk`` is given (e.g. parsed from an
        extension URL), by primary key.  With ``shell_version`` set, the
        returned ``version`` / ``version_tag`` / ``download_url`` reflect the
        newest version compatible with that shell; without it, the absolute
        latest.
        """
        params: dict[str, str] = {}
        if pk is not None:
            params["pk"] = str(pk)
        elif uuid:
            params["uuid"] = uuid
        if shell_version:
            params["shell_version"] = shell_version
        url = f"{_INFO_URL}?{urlencode(params)}"
        msg = Soup.Message.new("GET", url)
        msg.get_request_headers().append("Accept", "application/json")
        self._session.send_and_read_async(
            msg,
            GLib.PRIORITY_DEFAULT,
            None,
            self._on_info_done,
            (msg, on_done),
        )

    def _on_info_done(
        self,
        session: Soup.Session,
        result: Gio.AsyncResult,
        user_data: tuple[Soup.Message, InfoCallback],
    ) -> None:
        msg, on_done = user_data
        try:
            body = bytes(session.send_and_read_finish(result).get_data() or b"")
        except GLib.Error as exc:
            on_done(None, f"Network error: {exc.message}")
            return
        status = msg.get_status()
        if status == Soup.Status.NOT_FOUND:
            on_done(None, "Extension not found on extensions.gnome.org.")
            return
        if status != Soup.Status.OK:
            on_done(None, http_error(msg, body, "look up extension"))
            return
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            on_done(None, f"Bad response from extensions.gnome.org: {exc}")
            return
        info = _parse_info(data)
        if info is None:
            on_done(None, "extensions.gnome.org returned incomplete data.")
            return
        on_done(info, None)


# ─── Pure parsing helpers (unit-testable, no network) ─────────────────────


def _parse_search(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the lightweight result rows from an ``extension-query`` reply."""
    results: list[dict[str, Any]] = []
    for item in data.get("extensions", []) or []:
        if not isinstance(item, dict):
            continue
        uuid = item.get("uuid")
        pk = item.get("pk")
        if not uuid or pk is None:
            continue
        results.append(
            {
                "uuid": str(uuid),
                "name": str(item.get("name", uuid)),
                "creator": str(item.get("creator", "")),
                "description": str(item.get("description", "")),
                "pk": int(pk),
                "shell_version_map": item.get("shell_version_map") or {},
            }
        )
    return results


def _parse_info(data: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the fields the installer needs from an ``extension-info`` reply.

    Returns ``None`` when the reply lacks the identifiers required to install
    (uuid / pk / version / version_tag).
    """
    uuid = data.get("uuid")
    pk = data.get("pk")
    version = data.get("version")
    version_tag = data.get("version_tag")
    if not uuid or pk is None or version is None or version_tag is None:
        return None
    try:
        return {
            "uuid": str(uuid),
            "name": str(data.get("name", uuid)),
            "creator": str(data.get("creator", "")),
            "description": str(data.get("description", "")),
            "pk": int(pk),
            "version": int(version),
            "version_tag": int(version_tag),
            "download_url": str(data.get("download_url", "")),
            "shell_version_map": data.get("shell_version_map") or {},
        }
    except (TypeError, ValueError):
        return None


def is_compatible(shell_version_map: dict[str, Any], shell_version: str | None) -> bool:
    """Whether an extension supports ``shell_version``.

    When the running shell version is unknown (``None``), we can't tell, so
    assume compatible rather than hiding everything.
    """
    if not shell_version:
        return True
    return shell_version in (shell_version_map or {})


_EGO_URL_RE = re.compile(r"extensions\.gnome\.org/extension/(\d+)")


def parse_ego_input(text: str) -> tuple[str, str | int] | None:
    """Classify install-dialog input as a direct EGO reference, or ``None``.

    Returns ``("pk", <int>)`` for an extensions.gnome.org extension URL,
    ``("uuid", <str>)`` for a bare extension UUID (``name@domain``), or
    ``None`` when the text should be treated as a free-text search query.
    """
    text = (text or "").strip()
    if not text:
        return None
    m = _EGO_URL_RE.search(text)
    if m:
        return ("pk", int(m.group(1)))
    # A UUID looks like ``foo@bar`` with no whitespace or path separators.
    if "@" in text and " " not in text and "/" not in text:
        return ("uuid", text)
    return None
