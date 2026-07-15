'use strict';

import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';
import { SocketClient } from './socket_client.js';
import { Profiler } from './profiler.js';
import { Inspector } from './inspector.js';
import { bridgeLog, bridgeLogError, bridgeLogWarning } from './logger.js';

const DEBUG = false;
function _dbg(...args) { if (DEBUG) { bridgeLog(args.join(' ')); } }

const COMPANION_UUID = 'gse-profiler-bridge@todevelopers';
const SETTINGS_SCHEMA = 'org.gnome.shell.extensions.gse-profiler-bridge';

// Global keybindings registered in gnome-shell. Each maps a GSettings key
// (see schemas/…gschema.xml) to a message forwarded to the app over the
// socket. Because they are registered with the shell's window manager they
// fire regardless of which window is focused — the whole point of Phase 13.
const KEYBINDINGS = {
    'toggle-profiling': { type: 'toggle_profiling' },
    'restart-profiling': { type: 'restart_profiling' },
};

export default class GSEProfilerBridge extends Extension {
    /** @type {SocketClient | null} */
    _socketClient = null;

    /** @type {Profiler | null} */
    _profiler = null;

    /** @type {Inspector | null} */
    _inspector = null;

    /** @type {import('gi://Gio').Gio.Settings | null} */
    _settings = null;

    /** @type {string[]} */
    _boundKeys = [];

    enable() {
        this._profiler = new Profiler(event => {
            this._socketClient?.send(event);
        });

        this._inspector = new Inspector();

        this._socketClient = new SocketClient(COMPANION_UUID, msg => this._onMessage(msg));
        this._socketClient.connect();

        this._registerKeybindings();
    }

    disable() {
        this._unregisterKeybindings();

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

    /** Register the global profiling keybindings with the shell's window
     *  manager. Failures (e.g. an uncompiled schema) are logged but never
     *  break the rest of the bridge — profiling still works from the app UI. */
    _registerKeybindings() {
        try {
            this._settings = this.getSettings(SETTINGS_SCHEMA);
        } catch (e) {
            bridgeLogError(e, 'could not load settings schema — global shortcuts disabled');
            this._settings = null;
            return;
        }

        for (const [key, message] of Object.entries(KEYBINDINGS)) {
            try {
                Main.wm.addKeybinding(
                    key,
                    this._settings,
                    Meta.KeyBindingFlags.IGNORE_AUTOREPEAT,
                    // ActionMode.ALL so the shortcut still fires while a panel
                    // menu / popup grab is open (POPUP mode) — the whole point
                    // is that the app need not be focused. NORMAL|OVERVIEW alone
                    // silently dropped the key whenever any panel widget was up.
                    Shell.ActionMode.ALL,
                    () => this._socketClient?.send(message),
                );
                this._boundKeys.push(key);
            } catch (e) {
                bridgeLogError(e, `could not register keybinding '${key}'`);
            }
        }
    }

    _unregisterKeybindings() {
        for (const key of this._boundKeys) {
            Main.wm.removeKeybinding(key);
        }
        this._boundKeys = [];
        this._settings = null;
    }

    /** @param {object} msg */
    _onMessage(msg) {
        _dbg(`_onMessage: type=${msg.type}`);
        switch (msg.type) {
        case 'start_profiling': {
            _dbg(`start_profiling: uuid=${msg.uuid}`);
            const ok = this._profiler?.startProfiling(msg.uuid) ?? false;
            _dbg(`start_profiling result: ok=${ok}`);
            this._socketClient?.send({ type: 'profiling_started', uuid: msg.uuid, ok });
            break;
        }
        case 'stop_profiling':
            _dbg('stop_profiling received');
            this._profiler?.stopProfiling();
            this._socketClient?.send({ type: 'profiling_stopped' });
            break;
        case 'inspect': {
            const path = msg.path ?? [];
            _dbg(`inspect: uuid=${msg.uuid} path=[${path.join(',')}]`);
            const result = this._inspector?.inspect(msg.uuid, path) ?? { properties: [] };
            this._socketClient?.send({ type: 'inspect_result', extensionUuid: msg.uuid, path, ...result });
            break;
        }
        default:
            bridgeLogWarning(`unhandled message type: ${msg.type}`);
        }
    }
}
