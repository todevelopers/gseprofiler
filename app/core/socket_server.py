import json
import logging
import os
from collections import deque
from typing import Any

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("GObject", "2.0")
from gi.repository import Gio, GLib, GObject

_log = logging.getLogger(__name__)
_SOCKET_SUBDIR = "gse-profiler"
_SOCKET_NAME = "gse-profiler.sock"
_IN_FLATPAK: bool = os.path.exists("/.flatpak-info")


def _socket_path() -> str:
    if _IN_FLATPAK:
        # GLib.get_user_runtime_dir() inside a Flatpak sandbox returns the
        # app-scoped dir (/run/user/<uid>/app/<id>).  Use the uid-based host
        # path directly.  The socket lives in a subdirectory so that the
        # Flatpak --filesystem=xdg-run/gse-profiler:create permission can
        # bind-mount the whole directory — bind-mounting a single socket file
        # only works for files that already exist when the sandbox starts.
        return f"/run/user/{os.getuid()}/{_SOCKET_SUBDIR}/{_SOCKET_NAME}"  # type: ignore[attr-defined]
    return os.path.join(GLib.get_user_runtime_dir(), _SOCKET_SUBDIR, _SOCKET_NAME)


class SocketServer(GObject.Object):
    """Async Unix socket server for bridge extension communication.

    Socket path: $XDG_RUNTIME_DIR/gse-profiler.sock
    Protocol: newline-delimited JSON messages.
    """

    __gtype_name__ = "SocketServer"

    __gsignals__ = {
        "client-connected": (GObject.SignalFlags.RUN_LAST, None, ()),
        "client-disconnected": (GObject.SignalFlags.RUN_LAST, None, ()),
        "message-received": (GObject.SignalFlags.RUN_LAST, None, (object,)),
    }

    def __init__(self) -> None:
        super().__init__()
        self._service: Gio.SocketService | None = None
        self._connection: Gio.SocketConnection | None = None  # keeps streams alive
        self._output: Gio.DataOutputStream | None = None
        self._cancellable: Gio.Cancellable | None = None
        self._istream: Gio.DataInputStream | None = None
        self._send_queue: deque[bytes] = deque()
        self._writing: bool = False

    @property
    def is_client_connected(self) -> bool:
        return self._output is not None

    def start(self) -> None:
        """Start listening on the Unix socket."""
        sock_path = _socket_path()
        os.makedirs(os.path.dirname(sock_path), exist_ok=True)
        _unlink_socket(sock_path)

        self._service = Gio.SocketService.new()
        addr = Gio.UnixSocketAddress.new(sock_path)
        try:
            self._service.add_address(
                addr,
                Gio.SocketType.STREAM,
                Gio.SocketProtocol.DEFAULT,
                None,
            )
        except GLib.Error as exc:
            _log.error("Failed to bind socket at %s: %s", sock_path, exc)
            self._service = None
            return

        self._service.connect("incoming", self._on_incoming)
        self._service.start()
        _log.info("Socket server listening at %s", sock_path)

    def disconnect_client(self) -> None:
        """Proactively cancel the pending read; _on_line_read handles cleanup."""
        if self._cancellable:
            self._cancellable.cancel()

    def _reset_connection(self) -> None:
        """Drop all per-connection state (streams, queue, write flag).

        Shared by the read-error path, the EOF path and ``stop()`` — they all
        need to release the same handles. Does not emit ``client-disconnected``
        or touch the listening service; the callers decide those.
        """
        self._connection = None
        self._output = None
        self._istream = None
        self._send_queue.clear()
        self._writing = False

    def stop(self) -> None:
        """Stop the server and close all connections."""
        if self._cancellable:
            self._cancellable.cancel()
            self._cancellable = None
        if self._service:
            self._service.stop()
            self._service.close()
            self._service = None
        self._reset_connection()
        _unlink_socket(_socket_path())

    def send(self, message: dict[str, Any]) -> None:
        """Send a JSON message to the bridge extension (non-blocking)."""
        if not self._output:
            _log.debug("send() dropped — no client connected: %s", message.get("type"))
            return
        _log.debug("send() → %s", message)
        self._send_queue.append((json.dumps(message) + "\n").encode())
        if not self._writing:
            self._flush_send_queue()

    def _flush_send_queue(self) -> None:
        if not self._send_queue or not self._output:
            self._writing = False
            return
        self._writing = True
        blob = GLib.Bytes.new(self._send_queue.popleft())
        self._output.write_bytes_async(
            blob, GLib.PRIORITY_DEFAULT, None, self._on_write_done, None
        )

    def _on_write_done(
        self,
        stream: Gio.DataOutputStream,
        result: Gio.AsyncResult,
        _user_data: object,
    ) -> None:
        try:
            stream.write_bytes_finish(result)
            _log.debug("send() OK")
        except GLib.Error as exc:
            _log.warning("Failed to send message: %s", exc)
            self._send_queue.clear()
            self._writing = False
            return
        self._flush_send_queue()

    # ── Private ───────────────────────────────────────────────────────────

    def _on_incoming(
        self,
        _service: Gio.SocketService,
        connection: Gio.SocketConnection,
        _source: object,
    ) -> bool:
        _log.info("Bridge connected")
        was_connected = self._output is not None
        if self._cancellable:
            _log.debug("Cancelling previous connection before accepting new one")
            self._cancellable.cancel()

        self._cancellable = Gio.Cancellable.new()
        self._connection = connection  # prevent GC from closing the underlying streams
        self._output = Gio.DataOutputStream.new(connection.get_output_stream())

        istream = Gio.DataInputStream.new(connection.get_input_stream())
        istream.set_newline_type(Gio.DataStreamNewlineType.LF)
        self._istream = istream
        self._read_next(istream)

        if was_connected:
            # Previous connection was replaced without a clean EOF — notify listeners.
            _log.info("Previous bridge connection replaced without clean disconnect")
            self.emit("client-disconnected")

        self.emit("client-connected")
        return True

    def _read_next(self, stream: Gio.DataInputStream) -> None:
        stream.read_line_async(
            GLib.PRIORITY_DEFAULT,
            self._cancellable,
            self._on_line_read,
            None,
        )

    def _on_line_read(
        self,
        stream: Gio.DataInputStream | None,
        result: Gio.AsyncResult,
        _user_data: object,
    ) -> None:
        if stream is None:
            return
        if stream is not self._istream:
            _log.debug("Discarding read from superseded connection")
            try:
                stream.read_line_finish(result)
            except GLib.Error:
                pass
            return

        try:
            line_bytes, _length = stream.read_line_finish(result)  # type: ignore[union-attr]
        except GLib.Error as exc:
            if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                _log.debug("Socket read cancelled")
            else:
                _log.info("Bridge read error: %s", exc)
            self._reset_connection()
            self.emit("client-disconnected")
            return

        if line_bytes is None:
            _log.info("Bridge disconnected (EOF)")
            self._reset_connection()
            self.emit("client-disconnected")
            return

        line = line_bytes.decode("utf-8", errors="replace").strip()
        if line:
            try:
                msg = json.loads(line)
                self._dispatch(msg)
            except json.JSONDecodeError as exc:
                _log.warning("Invalid JSON from bridge: %s — %r", exc, line)

        self._read_next(stream)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
        if msg_type == "hello":
            _log.info(
                "Handshake received: bridge v%s uuid=%s",
                msg.get("version"),
                msg.get("uuid"),
            )
        self.emit("message-received", msg)


def _unlink_socket(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        _log.warning("Could not remove socket file %s: %s", path, exc)
