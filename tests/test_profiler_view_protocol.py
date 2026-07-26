"""Headless protocol regressions for the profiler view.

PyGObject is unavailable in the Windows-only unit-test environment, so this
module is skipped there and runs on Linux/WSL where GTK4 is installed.
"""

from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

Gtk.init_check()

from app.core.dbus_client import DBusClient, ExtensionState
from app.core.keybindings import KeybindingManager
from app.core.socket_server import SocketServer
from app.ui.profiler_view import ProfilerView


def _new_view(tmp_path: Path) -> ProfilerView:
    return ProfilerView(
        DBusClient(),
        SocketServer(),
        KeybindingManager(str(tmp_path / "shortcuts.json")),
    )


def _button_label(button: Gtk.Button) -> str:
    box = button.get_child()
    assert isinstance(box, Gtk.Box)
    label = box.get_last_child()
    assert isinstance(label, Gtk.Label)
    return label.get_label()


def test_accepts_legacy_events_and_batches(tmp_path: Path) -> None:
    view = _new_view(tmp_path)

    view._router.dispatch(
        {
            "type": "profile_event",
            "function": "foo",
            "start": 0.0,
            "end": 0.01,
            "depth": 0,
        }
    )
    view._router.dispatch(
        {
            "type": "profile_batch",
            "events": [
                {"function": "foo", "start": 0.02, "end": 0.03, "depth": 0},
                "not an event",
                {"function": "bar", "start": 0.04, "end": 0.06, "depth": 0},
            ],
        }
    )
    view._flush_refresh()

    assert len(view._raw_events) == 3
    assert view._stats["foo"].count == 2
    assert view._stats["bar"].count == 1
    assert view._inner_stack.get_visible_child_name() == "data"


def test_stale_start_acks_do_not_change_newer_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = _new_view(tmp_path)
    sent: list[dict[str, object]] = []
    view._target_uuid = "x@x"
    monkeypatch.setattr(
        view._dbus,
        "get_extension_state",
        lambda _uuid: ExtensionState.ENABLED,
    )
    view._socket._output = object()
    monkeypatch.setattr(view._socket, "send", sent.append)

    # Two starts are in flight. ACKs are ordered on the socket, so the first
    # one belongs to the superseded generation.
    assert view._start_profiling() is True
    assert view._start_profiling() is True
    assert _button_label(view._start_stop_btn) == "Stop"
    assert _button_label(view._empty_start_btn) == "Stop"
    assert view._empty_page.get_title() == "Recording"

    view._router.dispatch(
        {
            "type": "profiling_started",
            "uuid": "x@x",
            "ok": False,
            "patchedFunctions": 1,
        }
    )
    assert view._profiling is True
    assert view._instrumented_functions is None

    view._router.dispatch(
        {
            "type": "profiling_started",
            "uuid": "x@x",
            "ok": True,
            "patchedFunctions": 247,
            "visitedObjects": 83,
            "skippedFunctions": 4,
            "truncated": True,
        }
    )
    assert view._instrumented_functions == 247
    assert (
        "0 observed total · 247 instrumented last scan"
        in view._fn_caption.get_text()
    )

    # With no pending request this is stale/unsolicited and must not replace
    # the current discovery totals.
    view._router.dispatch(
        {
            "type": "profiling_started",
            "uuid": "x@x",
            "ok": True,
            "patchedFunctions": 1,
        }
    )
    assert view._instrumented_functions == 247

    view._stop_profiling()
    assert _button_label(view._start_stop_btn) == "Start"
    assert _button_label(view._empty_start_btn) == "Start"
    view._stop_rec_timer()


def test_legacy_start_ack_without_stats_remains_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = _new_view(tmp_path)
    view._target_uuid = "x@x"
    monkeypatch.setattr(
        view._dbus,
        "get_extension_state",
        lambda _uuid: ExtensionState.ENABLED,
    )
    view._socket._output = object()
    monkeypatch.setattr(view._socket, "send", lambda _msg: None)

    assert view._start_profiling() is True
    view._router.dispatch(
        {"type": "profiling_started", "uuid": "x@x", "ok": True}
    )

    assert view._profiling is True
    assert view._instrumented_functions is None
    view._stop_profiling()
    view._stop_rec_timer()


def test_session_ids_reject_events_from_a_superseded_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = _new_view(tmp_path)
    view._target_uuid = "x@x"
    monkeypatch.setattr(
        view._dbus,
        "get_extension_state",
        lambda _uuid: ExtensionState.ENABLED,
    )
    view._socket._output = object()
    monkeypatch.setattr(view._socket, "send", lambda _msg: None)

    assert view._start_profiling() is True
    old_session = view._accepted_event_generation
    assert view._start_profiling() is True
    current_session = view._accepted_event_generation
    assert current_session != old_session

    stale_event = {
        "type": "profile_event",
        "sessionId": old_session,
        "function": "stale",
        "start": 0.0,
        "end": 0.01,
        "depth": 0,
    }
    current_event = {
        "type": "profile_event",
        "sessionId": current_session,
        "function": "current",
        "start": 0.02,
        "end": 0.03,
        "depth": 0,
    }
    view._router.dispatch(
        {
            "type": "profile_batch",
            "sessionId": old_session,
            "events": [stale_event],
        }
    )
    view._router.dispatch(
        {
            "type": "profile_batch",
            "sessionId": current_session,
            "events": [current_event],
        }
    )
    view._flush_refresh()

    assert "stale" not in view._stats
    assert view._stats["current"].count == 1
    view._stop_profiling()

    # Clear after Stop invalidates the just-finished generation, so a delayed
    # final flush cannot repopulate the profile the user explicitly cleared.
    view._on_clear(None)
    view._router.dispatch(
        {
            "type": "profile_batch",
            "sessionId": current_session,
            "events": [current_event],
        }
    )
    view._flush_refresh()
    assert not view._stats
    view._stop_rec_timer()
