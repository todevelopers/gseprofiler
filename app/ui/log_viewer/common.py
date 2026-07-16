"""Shared constants, pure helpers and the row model for the log viewer.

Leaf module of the ``app/ui/log_viewer`` package: it depends only on GTK and
:mod:`app.core.journal_reader`, never on the other log-viewer submodules, so it
can be imported freely by the capture panel, tag bar, factories and main view
without any import cycle.
"""

import hashlib
import re

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GObject, Gtk

from app.core.journal_reader import LogEntry

MAX_ENTRIES = 5000
_CAPTURE_KEY = "capture"

# Capture-panel control mappings: dropdown index ↔ CaptureSpec value.
_SCOPE_BY_INDEX = ("user", "system", "both")
_SOURCE_BY_INDEX = ("gnome-shell", "all", "unit", "identifier")
_PRIORITY_BY_INDEX: tuple[int | None, ...] = (None, 3, 4, 6)

# Priority bucket → stat dot identifier. Buckets group the syslog priorities
# into four user-friendly severities.
_BUCKET_ERROR = "error"   # priority 0-3 (emerg / alert / crit / error)
_BUCKET_WARN = "warn"     # priority 4 (warning)
_BUCKET_INFO = "info"     # priority 5-6 (notice / info)
_BUCKET_DEBUG = "debug"   # priority 7 (debug)

_BUCKET_LABELS: dict[str, str] = {
    _BUCKET_ERROR: "ERROR",
    _BUCKET_WARN: "WARN",
    _BUCKET_INFO: "INFO",
    _BUCKET_DEBUG: "DEBUG",
}

# Hash-derived tag color palette (12 hues defined in style.css as tag-c0..tag-cB)
_TAG_PALETTE_SIZE = 12
_TAG_PALETTE_CHARS = "0123456789AB"
_TAG_CSS_CLASSES = tuple(f"tag-c{c}" for c in _TAG_PALETTE_CHARS)
_LEVEL_PILL_CLASSES = ("lvl-error", "lvl-warn", "lvl-info", "lvl-debug")

# Spacing (px) between elements in the tag bar. Kept in sync with the tag-bar
# layout manager so the chip-fitting math matches the real on-screen gaps.
_TAG_BAR_SPACING = 4

_MSG_TAG_RE = re.compile(r'^(?:JS LOG:\s*)?\[([^\]]+)\]\s*(.*)', re.DOTALL)

# Hover-tooltip help texts (info icons, matching the Profiler view's pattern).
_SEARCH_HELP = (
    "The Logs tab tails the system journal live and groups each line under a "
    "tag, so you can filter by where it came from. Search narrows the visible "
    "lines by text, the severity dots filter by level (ERROR / WARN / INFO / "
    "DEBUG), and the tag chips filter by tag — usually an extension, but with a "
    "broader capture source it can be any log producer (a systemd unit, the "
    "kernel, etc.). All three filters stack.\n\n"
    "A line's tag is resolved most-specific first: an explicit [tag] prefix on "
    "the message, then its GLIB_DOMAIN, then the syslog identifier. The simplest "
    "way to make your extension filterable is to prefix log messages with a "
    "bracketed tag, e.g. console.log('[my-ext] ready') — every such line then "
    "collapses under a single \"my-ext\" chip. Logging through console.* sets "
    "GLIB_DOMAIN automatically, and GLib.log_structured() lets you set that "
    "domain explicitly, so those lines stay grouped under it even without a "
    "prefix."
)
_CAPTURE_HELP = (
    "Source is the capture layer — it defines what journalctl pulls from the "
    "journal before reading starts, like a capture filter. It is separate from "
    "the live Search / severity / tag filters, which only narrow what has "
    "already been captured. Configure it while stopped; it is locked while "
    "reading.\n\n"
    "• Scope — which journal to read: User (--user), System (--system), or "
    "Both.\n"
    "• This boot only — limit to the current boot (-b).\n"
    "• Logs from — the source: GNOME Shell matches the gnome-shell binary and "
    "so captures every extension (they all run inside that one process); "
    "Everything reads the whole journal; or target a custom unit (-u) or "
    "identifier (-t).\n"
    "• Min priority — drop lower-severity entries at the source (-p). Leave it "
    "at All and use the severity dots to filter live instead.\n"
    "• Advanced — type a raw journalctl command for full control."
)


def _make_info_icon(tooltip: str) -> Gtk.Image:
    """Dimmed info icon with a hover tooltip, matching the Profiler view."""
    icon = Gtk.Image.new_from_icon_name("dialog-information-symbolic")
    icon.add_css_class("prof-info-btn")
    icon.set_valign(Gtk.Align.CENTER)
    icon.set_tooltip_text(tooltip)
    return icon


def _priority_bucket(priority: int) -> str:
    if priority <= 3:
        return _BUCKET_ERROR
    if priority == 4:
        return _BUCKET_WARN
    if priority <= 6:
        return _BUCKET_INFO
    return _BUCKET_DEBUG


def _bucket_pill_class(bucket: str) -> str:
    return {
        _BUCKET_ERROR: "lvl-error",
        _BUCKET_WARN: "lvl-warn",
        _BUCKET_INFO: "lvl-info",
        _BUCKET_DEBUG: "lvl-debug",
    }[bucket]


def _bucket_label(bucket: str) -> str:
    return _BUCKET_LABELS[bucket]


def _tag_color_class(tag: str) -> str:
    digest = hashlib.md5(tag.encode("utf-8")).digest()
    idx = digest[0] % _TAG_PALETTE_SIZE
    return _TAG_CSS_CLASSES[idx]


def _tag_display(tag: str) -> str:
    """Human-readable label for a tag chip / popover row. Entries with no tag
    have an empty-string tag; show a placeholder instead of a blank chip.
    (The log table TAG column keeps showing the raw value.)"""
    return tag if tag else "<empty>"


def _extract_log_tag(message: str) -> tuple[str | None, str]:
    m = _MSG_TAG_RE.match(message)
    if m:
        return m.group(1), m.group(2)
    return None, message


def _entry_tag(entry: LogEntry) -> str:
    """Attribute an entry to a tag, most-specific first: an explicit ``[tag]``
    message prefix, then GLIB_DOMAIN (set by console.* / GLib.log_structured),
    then the syslog identifier."""
    tag, _ = _extract_log_tag(entry.message)
    if tag:
        return tag
    if entry.glib_domain:
        return entry.glib_domain
    return entry.identifier


class LogRowItem(GObject.Object):
    """One row in the log column view."""

    __gtype_name__ = "LogRowItem"

    def __init__(self, entry: LogEntry) -> None:
        super().__init__()
        self.entry = entry
        _, body = _extract_log_tag(entry.message)
        self.tag = _entry_tag(entry)
        self.body = body
        self.bucket = _priority_bucket(entry.priority)
        self.time_str = entry.timestamp.strftime("%H:%M:%S.%f")[:-3]
