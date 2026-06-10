'use strict';

import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import { bridgeLogError, bridgeLogWarning } from './logger.js';

const SOCKET_SUBDIR = 'gse-profiler';
const SOCKET_NAME = 'gse-profiler.sock';
const RECONNECT_DELAY_MS = 3000;
const PROTOCOL_VERSION = '1';

function _socketPath() {
    return GLib.build_filenamev([GLib.get_user_runtime_dir(), SOCKET_SUBDIR, SOCKET_NAME]);
}

export class SocketClient {
    #uuid;
    #onMessage;
    #connected = false;
    #stopping = false;
    #connection = null;
    #outputStream = null;
    #reconnectSource = null;
    #cancellable = null;
    #encoder = new TextEncoder();

    /**
     * @param {string} uuid - bridge extension UUID for the handshake
     * @param {(msg: object) => void} [onMessage] - callback for incoming messages
     */
    constructor(uuid, onMessage = null) {
        this.#uuid = uuid;
        this.#onMessage = onMessage;
    }

    /** Start connecting; automatically reconnects on disconnect. */
    connect() {
        this.#stopping = false;
        this.#scheduleConnect(0);
    }

    /** Stop connecting / disconnect and cancel any pending reconnect.
     *  Cancels the in-flight read_line_async first so close_async can
     *  complete immediately and the Python side receives EOF promptly.
     */
    disconnect() {
        this.#stopping = true;
        this.#connected = false;
        this.#cancelReconnect();

        // Cancel pending read so close_async is not blocked waiting for it.
        if (this.#cancellable) {
            this.#cancellable.cancel();
            this.#cancellable = null;
        }

        const conn = this.#connection;
        this.#connection = null;
        this.#outputStream = null;
        if (conn) {
            try {
                conn.close_async(GLib.PRIORITY_DEFAULT, null, (obj, result) => {
                    try {
                        obj.close_finish(result);
                    } catch (_e) {
                        // already gone — ignore
                    }
                });
            } catch (_e) {
                // ignore — socket may already be gone
            }
        }
    }

    /**
     * Send a message to the app.  No-op when not connected.
     * @param {object} message
     */
    send(message) {
        if (!this.#connected || !this.#outputStream) {
            return;
        }
        try {
            const line = `${JSON.stringify(message)}\n`;
            const bytes = this.#encoder.encode(line);
            this.#outputStream.write_all(bytes, null);
        } catch (e) {
            bridgeLogError(e, 'socket send failed');
        }
    }

    // ── Private ───────────────────────────────────────────────────────────

    #scheduleConnect(delayMs) {
        this.#cancelReconnect();
        if (delayMs === 0) {
            this.#doConnect();
            return;
        }
        this.#reconnectSource = GLib.timeout_add(
            GLib.PRIORITY_DEFAULT,
            delayMs,
            () => {
                this.#reconnectSource = null;
                this.#doConnect();
                return GLib.SOURCE_REMOVE;
            },
        );
    }

    #cancelReconnect() {
        if (this.#reconnectSource !== null) {
            GLib.source_remove(this.#reconnectSource);
            this.#reconnectSource = null;
        }
    }

    #doConnect() {
        const addr = Gio.UnixSocketAddress.new(_socketPath());
        const client = new Gio.SocketClient();
        client.connect_async(addr, null, (obj, result) => {
            try {
                const conn = obj.connect_finish(result);
                this.#onConnected(conn);
            } catch (_e) {
                if (!this.#stopping) {
                    this.#scheduleConnect(RECONNECT_DELAY_MS);
                }
            }
        });
    }

    #onConnected(connection) {
        this.#connection = connection;
        this.#connected = true;
        this.#cancellable = Gio.Cancellable.new();

        this.#outputStream = new Gio.DataOutputStream({
            base_stream: connection.get_output_stream(),
        });

        this.send({ type: 'hello', version: PROTOCOL_VERSION, uuid: this.#uuid });

        const istream = new Gio.DataInputStream({
            base_stream: connection.get_input_stream(),
        });
        istream.set_newline_type(Gio.DataStreamNewlineType.LF);
        this.#readNextLine(istream);
    }

    #readNextLine(stream) {
        stream.read_line_async(GLib.PRIORITY_DEFAULT, this.#cancellable, (obj, result) => {
            let line;
            try {
                [line] = obj.read_line_finish_utf8(result);
            } catch (_e) {
                // Cancelled means disconnect() was called — no reconnect needed.
                if (!this.#stopping) {
                    this.#onDisconnected();
                }
                return;
            }
            if (line === null) {
                this.#onDisconnected();
                return;
            }
            const trimmed = line.trim();
            if (trimmed) {
                try {
                    const msg = JSON.parse(trimmed);
                    if (this.#onMessage) {
                        this.#onMessage(msg);
                    }
                } catch (_e) {
                    bridgeLogWarning(`invalid JSON from app: ${trimmed}`);
                }
            }
            this.#readNextLine(stream);
        });
    }

    #onDisconnected() {
        this.#connected = false;
        this.#connection = null;
        this.#outputStream = null;
        this.#cancellable = null;
        if (!this.#stopping) {
            this.#scheduleConnect(RECONNECT_DELAY_MS);
        }
    }
}
