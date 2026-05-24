import logging
import shlex
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib, GObject

try:
    from systemd import journal as _sd_journal
    _HAVE_SYSTEMD: bool = True
except ImportError:
    _sd_journal = None  # type: ignore[assignment]
    _HAVE_SYSTEMD = False

_log = logging.getLogger(__name__)

PRIORITY_NAMES: dict[int, str] = {
    0: "EMERG",
    1: "ALERT",
    2: "CRIT",
    3: "ERROR",
    4: "WARNING",
    5: "NOTICE",
    6: "INFO",
    7: "DEBUG",
}

# Flags that JournalReader controls internally — strip from user command strings
_OWNED_FLAGS = frozenset({"--follow", "-f", "--no-pager", "--output", "-o", "--lines", "-n"})
_OWNED_PREFIXES = ("--output=", "--lines=", "--after-cursor=")


def parse_extra_args(cmd_str: str) -> list[str]:
    """Extract pass-through journalctl-compatible args from a user command string.

    Strips 'journalctl' and flags owned by JournalReader (--follow/-f,
    --output/-o, --lines/-n, --after-cursor, --no-pager).
    Everything else (e.g. --user, -t, -u, --boot, -p) is kept and forwarded.
    """
    try:
        parts = shlex.split(cmd_str)
    except ValueError:
        return []

    if parts and parts[0].split("/")[-1] == "journalctl":
        parts = parts[1:]

    result: list[str] = []
    skip_next = False
    for part in parts:
        if skip_next:
            skip_next = False
            continue
        if part in _OWNED_FLAGS:
            if part in ("--output", "-o", "--lines", "-n"):
                skip_next = True
            continue
        if any(part.startswith(p) for p in _OWNED_PREFIXES):
            continue
        result.append(part)

    return result


def _reader_flags(extra_args: list[str]) -> int:
    """Compute journal.Reader flags from journalctl-style extra_args."""
    flags: int = _sd_journal.LOCAL_ONLY
    has_user = "--user" in extra_args
    has_system = "--system" in extra_args
    if has_user and not has_system:
        flags |= _sd_journal.CURRENT_USER
    elif has_system and not has_user:
        flags |= _sd_journal.SYSTEM
    # Default (neither): LOCAL_ONLY reads both system and current-user journals
    return flags


def _configure_reader(reader: Any, extra_args: list[str]) -> None:
    """Apply matches, boot filter, and log level to an open journal.Reader."""
    i = 0
    while i < len(extra_args):
        arg = extra_args[i]
        if arg in ("-b", "--boot"):
            reader.this_boot()
        elif arg in ("-t", "--identifier") and i + 1 < len(extra_args):
            i += 1
            reader.add_match(SYSLOG_IDENTIFIER=extra_args[i])
        elif arg.startswith("--identifier="):
            reader.add_match(SYSLOG_IDENTIFIER=arg.split("=", 1)[1])
        elif arg in ("-u", "--unit") and i + 1 < len(extra_args):
            i += 1
            reader.add_match(_SYSTEMD_UNIT=extra_args[i])
        elif arg.startswith("--unit="):
            reader.add_match(_SYSTEMD_UNIT=arg.split("=", 1)[1])
        elif arg in ("-p", "--priority") and i + 1 < len(extra_args):
            i += 1
            try:
                reader.log_level(int(extra_args[i]))
            except (ValueError, AttributeError):
                pass
        elif arg.startswith("--priority="):
            try:
                reader.log_level(int(arg.split("=", 1)[1]))
            except (ValueError, AttributeError):
                pass
        i += 1


@dataclass
class LogEntry:
    timestamp: datetime
    priority: int
    priority_name: str
    identifier: str
    message: str
    raw: dict[str, Any]


class JournalReader(GObject.Object):
    """Journal reader using systemd.journal.Reader.

    Opens the journal in a background thread, seeks to the last 200 entries
    on start, then waits for new entries via reader.wait(). Cursor-based
    position tracking ensures no entries are missed or duplicated across
    stop/start cycles.
    """

    __gtype_name__ = "JournalReader"

    __gsignals__ = {
        "log-entry": (GObject.SignalFlags.RUN_LAST, None, (object,)),
    }

    _INITIAL_ENTRIES = 200
    _WAIT_USEC = 1_000_000  # 1 second in microseconds

    def __init__(self) -> None:
        super().__init__()
        self._cursor: str | None = None
        self._running = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._extra_args: list[str] = []
        # Generation counter lets idle callbacks discard stale batches after
        # stop+start without needing a join on the main thread.
        self._generation = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self, extra_args: list[str] | None = None) -> None:
        if self._running:
            return
        if not _HAVE_SYSTEMD:
            _log.error("systemd.journal not available; JournalReader disabled")
            return
        self._extra_args = extra_args or []
        self._cursor = None
        self._generation += 1
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        _log.info("JournalReader started (extra=%s)", self._extra_args)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        # Do not join here — avoids freezing the GTK main loop.
        # The thread is daemon=True and will exit within ≤ 1 second on its own.
        self._thread = None
        _log.info("JournalReader stop requested")

    # ── Poll loop (background thread) ─────────────────────────────────────────

    def _poll_loop(self) -> None:
        gen = self._generation
        try:
            reader = _sd_journal.Reader(flags=_reader_flags(self._extra_args))
        except Exception as exc:
            _log.error("Failed to open journal reader: %s", exc)
            return

        try:
            _configure_reader(reader, self._extra_args)
            self._seek_initial(reader)

            # Drain entries already in the journal before entering the wait loop.
            initial: list[LogEntry] = []
            for entry in reader:
                e = self._parse_entry(entry)
                if e is not None:
                    c = entry.get("__CURSOR")
                    if c:
                        self._cursor = c
                    initial.append(e)
            if initial:
                GLib.idle_add(self._emit_batch, initial, gen)

            # Wait for new entries appended after the initial drain.
            while not self._stop_event.is_set():
                change = reader.wait(self._WAIT_USEC)
                if change == _sd_journal.APPEND:
                    batch: list[LogEntry] = []
                    for entry in reader:
                        e = self._parse_entry(entry)
                        if e is not None:
                            c = entry.get("__CURSOR")
                            if c:
                                self._cursor = c
                            batch.append(e)
                    if batch:
                        GLib.idle_add(self._emit_batch, batch, gen)
        finally:
            reader.close()

    def _seek_initial(self, reader: Any) -> None:
        if self._cursor:
            try:
                reader.seek_cursor(self._cursor)
                # get_next() positions AT the cursor entry; calling it once
                # consumes it so the first iteration yields entries after it.
                reader.get_next()
                return
            except Exception:
                pass
        reader.seek_tail()
        reader.skip_previous(self._INITIAL_ENTRIES)

    def _emit_batch(self, entries: list[LogEntry], gen: int) -> bool:
        if self._running and gen == self._generation:
            for entry in entries:
                self.emit("log-entry", entry)
        return bool(GLib.SOURCE_REMOVE)

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse_entry(self, entry: dict) -> "LogEntry | None":
        ts_raw = entry.get("__REALTIME_TIMESTAMP")
        try:
            if isinstance(ts_raw, datetime):
                # systemd.journal returns timezone-aware datetimes; drop tz for
                # consistency with the rest of the app (naive local time).
                timestamp = ts_raw.replace(tzinfo=None)
            elif ts_raw is not None:
                timestamp = datetime.fromtimestamp(int(ts_raw) / 1_000_000)
            else:
                timestamp = datetime.now()
        except (ValueError, OSError):
            timestamp = datetime.now()

        prio_raw = entry.get("PRIORITY", 6)
        try:
            priority = max(0, min(7, int(prio_raw)))
        except (ValueError, TypeError):
            priority = 6

        message = entry.get("MESSAGE", "")
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="replace")

        identifier = entry.get("SYSLOG_IDENTIFIER", "")
        if isinstance(identifier, bytes):
            identifier = identifier.decode("utf-8", errors="replace")

        return LogEntry(
            timestamp=timestamp,
            priority=priority,
            priority_name=PRIORITY_NAMES.get(priority, "INFO"),
            identifier=str(identifier),
            message=str(message),
            raw=dict(entry),
        )
