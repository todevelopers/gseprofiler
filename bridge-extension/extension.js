'use strict';

import GLib from 'gi://GLib';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';
import { SocketClient } from './socket_client.js';
import { Profiler } from './profiler.js';
import { Inspector } from './inspector.js';

const DEBUG = false;
function _dbg(...args) { if (DEBUG) { log(...args); } }

const COMPANION_UUID = 'gse-profiler-bridge@todevelopers';
const LOG_DOMAIN = 'gse-profiler-bridge';

// Log via GLib.log_structured so the entry carries GLIB_DOMAIN; the app
// attributes it to this extension without a textual "[name]" prefix. This is
// the recommended logging pattern for extensions that want their logs grouped.
function _log(message) {
    GLib.log_structured(LOG_DOMAIN, GLib.LogLevelFlags.LEVEL_MESSAGE, { MESSAGE: message });
}

export default class GSEProfilerBridge extends Extension {
    /** @type {SocketClient | null} */
    _socketClient = null;

    /** @type {Profiler | null} */
    _profiler = null;

    /** @type {Inspector | null} */
    _inspector = null;

    enable() {
        _log('Enabled');

        this._profiler = new Profiler(event => {
            this._socketClient?.send(event);
        });

        this._inspector = new Inspector();

        this._socketClient = new SocketClient(COMPANION_UUID, msg => this._onMessage(msg));
        this._socketClient.connect();
    }

    disable() {
        _log('Disabled');

        if (this._profiler) {
            this._profiler.stopProfiling();
            this._profiler = null;
        }

        this._inspector = null;

        if (this._socketClient) {
            this._socketClient.disconnect();
            this._socketClient = null;
        }

    }

    /** @param {object} msg */
    _onMessage(msg) {
        _dbg(`[gse-profiler-bridge] _onMessage: type=${msg.type}`);
        switch (msg.type) {
        case 'start_profiling': {
            _dbg(`[gse-profiler-bridge] start_profiling: uuid=${msg.uuid}`);
            const ok = this._profiler?.startProfiling(msg.uuid) ?? false;
            _dbg(`[gse-profiler-bridge] start_profiling result: ok=${ok}`);
            this._socketClient?.send({ type: 'profiling_started', uuid: msg.uuid, ok });
            break;
        }
        case 'stop_profiling':
            _dbg('[gse-profiler-bridge] stop_profiling received');
            this._profiler?.stopProfiling();
            this._socketClient?.send({ type: 'profiling_stopped' });
            break;
        case 'inspect': {
            const path = msg.path ?? [];
            _dbg(`[gse-profiler-bridge] inspect: uuid=${msg.uuid} path=[${path.join(',')}]`);
            const result = this._inspector?.inspect(msg.uuid, path) ?? { properties: [] };
            this._socketClient?.send({ type: 'inspect_result', extensionUuid: msg.uuid, path, ...result });
            break;
        }
        default:
            _log(`unhandled message type: ${msg.type}`);
        }
    }
}
