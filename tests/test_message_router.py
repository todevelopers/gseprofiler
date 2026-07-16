"""Unit tests for app.core.message_router.MessageRouter — requires PyGObject."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("gi", reason="PyGObject (gi) not available in this environment")


def _make_router():
    from app.core.message_router import MessageRouter
    from app.core.socket_server import SocketServer

    server = SocketServer()
    return server, MessageRouter(server)


def test_routes_by_type_via_signal() -> None:
    """A message emitted by the server reaches the handler registered for its type."""
    server, router = _make_router()
    got: list[dict] = []
    router.on("profile_event", got.append)

    server._dispatch({"type": "profile_event", "x": 1})
    server._dispatch({"type": "inspect_result"})  # no handler → ignored

    assert got == [{"type": "profile_event", "x": 1}]


def test_dispatch_direct() -> None:
    _server, router = _make_router()
    got: list[dict] = []
    router.on("toggle_profiling", got.append)
    router.dispatch({"type": "toggle_profiling"})
    assert got == [{"type": "toggle_profiling"}]


def test_fanout_to_multiple_handlers() -> None:
    _server, router = _make_router()
    a: list[dict] = []
    b: list[dict] = []
    router.on("keybindings", a.append)
    router.on("keybindings", b.append)
    router.dispatch({"type": "keybindings"})
    assert len(a) == 1 and len(b) == 1


def test_off_unregisters() -> None:
    _server, router = _make_router()
    got: list[dict] = []
    router.on("profile_event", got.append)
    router.off("profile_event", got.append)
    router.dispatch({"type": "profile_event"})
    assert got == []
    # Removing an unknown handler is a no-op (must not raise).
    router.off("profile_event", got.append)
    router.off("never_registered", got.append)


def test_shutdown_stops_dispatch() -> None:
    server, router = _make_router()
    got: list[dict] = []
    router.on("profile_event", got.append)
    router.shutdown()
    # After shutdown the signal is disconnected — server emissions do nothing.
    server._dispatch({"type": "profile_event"})
    assert got == []


def test_untyped_message_ignored() -> None:
    _server, router = _make_router()
    got: list[dict] = []
    router.on("profile_event", got.append)
    router.dispatch({"no_type_key": 1})  # must not raise
    assert got == []
