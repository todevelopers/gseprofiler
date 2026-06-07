"""Unit tests for JournalReader.

Tests not marked with @needs_systemd run without systemd installed (Windows,
plain WSL). Tests marked @needs_systemd are skipped when systemd.journal is
absent.
"""

import os
import sys
from dataclasses import asdict
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("gi", reason="PyGObject (gi) not available in this environment")

try:
    import systemd.journal  # noqa: F401
    _SD_AVAILABLE = True
except ImportError:
    _SD_AVAILABLE = False

needs_systemd = pytest.mark.skipif(
    not _SD_AVAILABLE, reason="systemd.journal not installed"
)


# ── parse_extra_args ──────────────────────────────────────────────────────────

def test_parse_extra_args_strips_journalctl_prefix():
    from app.core.journal_reader import parse_extra_args
    assert parse_extra_args("journalctl --user") == ["--user"]


def test_parse_extra_args_strips_owned_flags():
    from app.core.journal_reader import parse_extra_args
    result = parse_extra_args("journalctl --follow --no-pager -o json -n 200 --user")
    assert result == ["--user"]


def test_parse_extra_args_strips_after_cursor():
    from app.core.journal_reader import parse_extra_args
    result = parse_extra_args("journalctl --after-cursor=abc123 -u gnome-shell.service")
    assert result == ["-u", "gnome-shell.service"]


def test_parse_extra_args_empty():
    from app.core.journal_reader import parse_extra_args
    assert parse_extra_args("") == []
    assert parse_extra_args("journalctl") == []


def test_parse_extra_args_invalid_quoting():
    from app.core.journal_reader import parse_extra_args
    assert parse_extra_args("journalctl --unit='unclosed") == []


def test_parse_extra_args_preserves_passthrough_flags():
    from app.core.journal_reader import parse_extra_args
    result = parse_extra_args("journalctl --user -t gnome-shell --boot -p 3")
    assert result == ["--user", "-t", "gnome-shell", "--boot", "-p", "3"]


# ── build_journal_cmd (no systemd needed) ────────────────────────────────────

def test_build_journal_cmd_default_is_gnome_shell_capture():
    from app.core.journal_reader import CaptureSpec, build_journal_cmd
    assert build_journal_cmd(CaptureSpec()) == "journalctl --user -b -t gnome-shell"


def test_build_journal_cmd_scope_variants():
    from app.core.journal_reader import CaptureSpec, build_journal_cmd
    assert build_journal_cmd(CaptureSpec(scope="system")) == "journalctl --system -b -t gnome-shell"
    # "both" omits scope flags (reader reads both journals by default)
    assert build_journal_cmd(CaptureSpec(scope="both")) == "journalctl -b -t gnome-shell"


def test_build_journal_cmd_source_all_and_no_boot():
    from app.core.journal_reader import CaptureSpec, build_journal_cmd
    assert build_journal_cmd(CaptureSpec(this_boot=False, source="all")) == "journalctl --user"


def test_build_journal_cmd_custom_unit_and_priority():
    from app.core.journal_reader import CaptureSpec, build_journal_cmd
    spec = CaptureSpec(source="unit", source_value="dbus.service", min_priority=3)
    assert build_journal_cmd(spec) == "journalctl --user -b -u dbus.service -p 3"


def test_build_journal_cmd_custom_identifier():
    from app.core.journal_reader import CaptureSpec, build_journal_cmd
    spec = CaptureSpec(source="identifier", source_value="myext")
    assert build_journal_cmd(spec) == "journalctl --user -b -t myext"


def test_build_journal_cmd_custom_source_blank_value_omitted():
    from app.core.journal_reader import CaptureSpec, build_journal_cmd
    assert build_journal_cmd(CaptureSpec(source="unit", source_value="")) == "journalctl --user -b"


def test_build_journal_cmd_raw_override_wins():
    from app.core.journal_reader import CaptureSpec, build_journal_cmd
    spec = CaptureSpec(raw_override=True, raw_text="journalctl -f custom")
    assert build_journal_cmd(spec) == "journalctl -f custom"


def test_build_journal_cmd_roundtrips_through_parse_extra_args():
    """The generated command must survive parse_extra_args unchanged (minus the
    journalctl prefix)."""
    from app.core.journal_reader import CaptureSpec, build_journal_cmd, parse_extra_args
    cmd = build_journal_cmd(CaptureSpec())
    assert parse_extra_args(cmd) == ["--user", "-b", "-t", "gnome-shell"]


# ── capture_from_settings (migration, no systemd needed) ─────────────────────

def test_capture_from_settings_empty_is_default():
    from app.core.journal_reader import CaptureSpec, capture_from_settings
    assert capture_from_settings({}) == CaptureSpec()


def test_capture_from_settings_upgrades_legacy_default():
    """Existing users on the old free-text default get the new structured one."""
    from app.core.journal_reader import CaptureSpec, capture_from_settings
    assert capture_from_settings({"journal_cmd": "journalctl --user -f"}) == CaptureSpec()


def test_capture_from_settings_preserves_legacy_custom_as_raw():
    from app.core.journal_reader import capture_from_settings
    spec = capture_from_settings({"journal_cmd": "journalctl -u sshd.service"})
    assert spec.raw_override is True
    assert spec.raw_text == "journalctl -u sshd.service"


def test_capture_from_settings_roundtrips_capture_dict():
    from app.core.journal_reader import CaptureSpec, capture_from_settings
    spec = CaptureSpec(scope="both", source="unit", source_value="x.service", min_priority=4)
    assert capture_from_settings({"capture": asdict(spec)}) == spec


def test_capture_from_settings_ignores_unknown_keys():
    from app.core.journal_reader import CaptureSpec, capture_from_settings
    spec = capture_from_settings({"capture": {"scope": "system", "bogus": 1}})
    assert spec == CaptureSpec(scope="system")


# ── _parse_entry (no systemd needed) ─────────────────────────────────────────

def _make_reader():
    from app.core.journal_reader import JournalReader
    return JournalReader()


def test_parse_entry_basic():
    reader = _make_reader()
    entry = {
        "__REALTIME_TIMESTAMP": datetime(2024, 1, 1, 12, 0, 0),
        "PRIORITY": 6,
        "SYSLOG_IDENTIFIER": "gnome-shell",
        "MESSAGE": "Extension loaded",
        "__CURSOR": "s=abc123",
    }
    result = reader._parse_entry(entry)
    assert result is not None
    assert result.priority == 6
    assert result.priority_name == "INFO"
    assert result.identifier == "gnome-shell"
    assert result.message == "Extension loaded"
    assert result.raw["__CURSOR"] == "s=abc123"


def test_parse_entry_bytes_message_and_identifier():
    reader = _make_reader()
    entry = {
        "__REALTIME_TIMESTAMP": datetime(2024, 1, 1),
        "PRIORITY": 3,
        "SYSLOG_IDENTIFIER": b"dbus-daemon",
        "MESSAGE": b"Binary \xff data",
        "__CURSOR": "s=xyz",
    }
    result = reader._parse_entry(entry)
    assert result is not None
    assert result.identifier == "dbus-daemon"
    assert "Binary" in result.message
    assert result.priority == 3
    assert result.priority_name == "ERROR"


def test_parse_entry_integer_usec_timestamp():
    reader = _make_reader()
    ts_usec = 1_704_067_200_000_000  # 2024-01-01 00:00:00 UTC
    entry = {
        "__REALTIME_TIMESTAMP": str(ts_usec),
        "PRIORITY": "7",
        "SYSLOG_IDENTIFIER": "test",
        "MESSAGE": "hello",
    }
    result = reader._parse_entry(entry)
    assert result is not None
    assert result.priority == 7
    assert result.priority_name == "DEBUG"


@pytest.mark.parametrize("bad_prio", [-1, 8, 100, "bogus", None])
def test_parse_entry_clamps_priority(bad_prio):
    reader = _make_reader()
    entry = {
        "__REALTIME_TIMESTAMP": datetime(2024, 1, 1),
        "PRIORITY": bad_prio,
        "MESSAGE": "test",
    }
    result = reader._parse_entry(entry)
    assert result is not None
    assert 0 <= result.priority <= 7


def test_parse_entry_missing_fields_yields_defaults():
    reader = _make_reader()
    result = reader._parse_entry({})
    assert result is not None
    assert result.message == ""
    assert result.identifier == ""
    assert result.priority == 6  # default INFO
    assert isinstance(result.timestamp, datetime)


# ── _configure_reader (MockReader — no systemd needed) ───────────────────────

class _MockReader:
    def __init__(self):
        self.matches: list[tuple] = []
        self.boot_filtered = False
        self.log_level_set: int | None = None

    def add_match(self, **kwargs):
        self.matches.extend(kwargs.items())

    def this_boot(self):
        self.boot_filtered = True

    def log_level(self, level: int):
        self.log_level_set = level


def test_configure_reader_identifier_short():
    from app.core.journal_reader import _configure_reader
    r = _MockReader()
    _configure_reader(r, ["-t", "gnome-shell"])
    assert ("SYSLOG_IDENTIFIER", "gnome-shell") in r.matches


def test_configure_reader_identifier_long():
    from app.core.journal_reader import _configure_reader
    r = _MockReader()
    _configure_reader(r, ["--identifier=gnome-shell"])
    assert ("SYSLOG_IDENTIFIER", "gnome-shell") in r.matches


def test_configure_reader_identifier_space_form():
    from app.core.journal_reader import _configure_reader
    r = _MockReader()
    _configure_reader(r, ["--identifier", "gnome-shell"])
    assert ("SYSLOG_IDENTIFIER", "gnome-shell") in r.matches


def test_configure_reader_unit_short():
    from app.core.journal_reader import _configure_reader
    r = _MockReader()
    _configure_reader(r, ["-u", "gnome-shell.service"])
    assert ("_SYSTEMD_UNIT", "gnome-shell.service") in r.matches


def test_configure_reader_unit_long():
    from app.core.journal_reader import _configure_reader
    r = _MockReader()
    _configure_reader(r, ["--unit=gnome-shell.service"])
    assert ("_SYSTEMD_UNIT", "gnome-shell.service") in r.matches


def test_configure_reader_boot_short():
    from app.core.journal_reader import _configure_reader
    r = _MockReader()
    _configure_reader(r, ["-b"])
    assert r.boot_filtered


def test_configure_reader_boot_long():
    from app.core.journal_reader import _configure_reader
    r = _MockReader()
    _configure_reader(r, ["--boot"])
    assert r.boot_filtered


def test_configure_reader_priority_short():
    from app.core.journal_reader import _configure_reader
    r = _MockReader()
    _configure_reader(r, ["-p", "3"])
    assert r.log_level_set == 3


def test_configure_reader_priority_long():
    from app.core.journal_reader import _configure_reader
    r = _MockReader()
    _configure_reader(r, ["--priority=4"])
    assert r.log_level_set == 4


def test_configure_reader_combined():
    from app.core.journal_reader import _configure_reader
    r = _MockReader()
    _configure_reader(r, ["-b", "-t", "dbus", "--unit=dbus.service", "-p", "6"])
    assert r.boot_filtered
    assert ("SYSLOG_IDENTIFIER", "dbus") in r.matches
    assert ("_SYSTEMD_UNIT", "dbus.service") in r.matches
    assert r.log_level_set == 6


def test_configure_reader_ignores_user_system_flags():
    """--user and --system are handled by _reader_flags, not _configure_reader."""
    from app.core.journal_reader import _configure_reader
    r = _MockReader()
    _configure_reader(r, ["--user", "--system"])
    assert not r.boot_filtered
    assert r.matches == []
    assert r.log_level_set is None


# ── _reader_flags (requires systemd.journal) ──────────────────────────────────

@needs_systemd
def test_reader_flags_default_is_local_only():
    from app.core.journal_reader import _reader_flags
    from systemd import journal
    flags = _reader_flags([])
    assert flags & journal.LOCAL_ONLY
    assert not (flags & journal.CURRENT_USER)
    assert not (flags & journal.SYSTEM)


@needs_systemd
def test_reader_flags_user_adds_current_user():
    from app.core.journal_reader import _reader_flags
    from systemd import journal
    flags = _reader_flags(["--user"])
    assert flags & journal.LOCAL_ONLY
    assert flags & journal.CURRENT_USER
    assert not (flags & journal.SYSTEM)


@needs_systemd
def test_reader_flags_system_adds_system():
    from app.core.journal_reader import _reader_flags
    from systemd import journal
    flags = _reader_flags(["--system"])
    assert flags & journal.LOCAL_ONLY
    assert flags & journal.SYSTEM
    assert not (flags & journal.CURRENT_USER)


@needs_systemd
def test_reader_flags_both_user_and_system_uses_default():
    """When both --user and --system are given, fall back to LOCAL_ONLY only."""
    from app.core.journal_reader import _reader_flags
    from systemd import journal
    flags = _reader_flags(["--user", "--system"])
    assert flags & journal.LOCAL_ONLY
    assert not (flags & journal.CURRENT_USER)
    assert not (flags & journal.SYSTEM)
