"""Typed dispatch over the bridge socket's ``message-received`` stream.

:class:`SocketServer` emits one untyped ``message-received`` signal carrying the
decoded JSON dict. Historically every consumer connected to that signal and ran
its own ``msg.get("type") == "…"`` if/elif ladder. As more message types arrive
(memory profiling, writable inspector, startup profiling) that filtering
duplicates across views and drifts.

``MessageRouter`` wraps a :class:`SocketServer`, owns the single signal
connection, and fans each message out to handlers registered by type::

    router = MessageRouter(socket_server)
    router.on("profile_event", self._on_profile_event)
    router.on("profiling_started", self._on_profiling_started)

Handlers receive just the message dict. Call :meth:`shutdown` when the owner is
destroyed to drop the underlying signal connection.
"""

import logging
from collections.abc import Callable
from typing import Any

from app.core.socket_server import SocketServer

_log = logging.getLogger(__name__)

MessageHandler = Callable[[dict[str, Any]], None]


class MessageRouter:
    """Dispatch bridge messages to per-``type`` handlers."""

    def __init__(self, socket_server: SocketServer) -> None:
        self._server = socket_server
        self._handlers: dict[str, list[MessageHandler]] = {}
        self._signal_id: int = socket_server.connect(
            "message-received", self._on_message_received
        )

    def on(self, msg_type: str, handler: MessageHandler) -> None:
        """Register ``handler`` for messages whose ``type`` equals ``msg_type``.

        Multiple handlers may register for the same type; all are invoked in
        registration order.
        """
        self._handlers.setdefault(msg_type, []).append(handler)

    def off(self, msg_type: str, handler: MessageHandler) -> None:
        """Unregister a previously registered handler (no-op if absent)."""
        handlers = self._handlers.get(msg_type)
        if not handlers:
            return
        try:
            handlers.remove(handler)
        except ValueError:
            return
        if not handlers:
            del self._handlers[msg_type]

    def dispatch(self, msg: dict[str, Any]) -> None:
        """Route ``msg`` to every handler registered for its ``type``."""
        msg_type = msg.get("type")
        handlers = self._handlers.get(msg_type) if isinstance(msg_type, str) else None
        if not handlers:
            _log.debug("No handler for bridge message: type=%s", msg_type)
            return
        for handler in list(handlers):
            handler(msg)

    def shutdown(self) -> None:
        """Drop the ``message-received`` connection and forget all handlers."""
        if self._signal_id:
            self._server.disconnect(self._signal_id)
            self._signal_id = 0
        self._handlers.clear()

    def _on_message_received(self, _server: SocketServer, msg: dict[str, Any]) -> None:
        self.dispatch(msg)
