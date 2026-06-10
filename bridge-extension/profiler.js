'use strict';

import GLib from 'gi://GLib';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import { bridgeLogError, bridgeLogWarning } from './logger.js';

const DEBUG = false;
function _dbg(...args) { if (DEBUG) { bridgeLog(args.join(' ')); } }

// Base classes whose methods we never patch (framework internals).
const _STOP_CLASSES = new Set(['Extension', 'Object']);
// GJS names GObject C types as Namespace_ClassName (e.g. St_Widget, Gio_File).
// Stop the prototype walk when we reach one of these to avoid patching internals.
const _FRAMEWORK_RE = /^(St_|Clutter_|Meta_|Shell_|GObject_|Gio_|GLib_|Mutter_|Gdk_|Gtk_|Pango_|Atk_|Soup_|Json_)/;

function _isStopProto(proto) {
    const name = proto?.constructor?.name ?? '';
    return _STOP_CLASSES.has(name) || _FRAMEWORK_RE.test(name);
}

/**
 * Extension function profiler — monkey-patches a target extension's exported
 * object and records per-call timing events.
 *
 * Emits: { type: "profile_event", extensionUuid, function, start, end, depth }
 */
export class Profiler {
    #running = false;
    #targetUuid = null;
    /** @type {Map<string, {holder: object, name: string, original: Function}>} */
    #patches = new Map();
    #callDepth = 0;
    /** @type {(event: object) => void} */
    #onEvent;

    /** @param {(event: object) => void} onEvent - called for each recorded call */
    constructor(onEvent) {
        this.#onEvent = onEvent;
    }

    get isRunning() {
        return this.#running;
    }

    /**
     * Monkey-patch all functions on the extension's stateObj and its direct
     * object-valued own properties (e.g. _indicator).
     * Walks the full prototype chain (excluding framework base classes).
     * @param {string} uuid
     * @returns {boolean} whether patching succeeded
     */
    startProfiling(uuid) {
        if (this.#running) {
            this.stopProfiling();
        }

        const ext = Main.extensionManager.lookup(uuid);
        _dbg(`startProfiling: lookup=${!!ext} state=${ext?.state} stateObj=${!!ext?.stateObj}`);
        if (!ext?.stateObj) {
            bridgeLogWarning(`startProfiling: no stateObj for ${uuid}`);
            return false;
        }

        this.#targetUuid = uuid;
        this.#callDepth = 0;

        const target = ext.stateObj;
        const ownKeys = Object.getOwnPropertyNames(target);
        _dbg(`stateObj constructor=${target?.constructor?.name} ownKeys=[${ownKeys.join(',')}]`);

        try {
            // Walk the stateObj's prototype chain, stopping at framework base classes.
            let proto = target;
            while (proto) {
                if (_isStopProto(proto)) { break; }
                _dbg(`patching proto level: ${proto.constructor?.name} keys=[${Object.getOwnPropertyNames(proto).join(',')}]`);
                this.#patchObject(target, proto, '');
                proto = Object.getPrototypeOf(proto);
            }

            // Also walk direct object-valued own properties (e.g. _indicator, _fetcher).
            const visited = new Set([target]);
            for (const propKey of ownKeys) {
                let propDesc;
                try {
                    propDesc = Object.getOwnPropertyDescriptor(target, propKey);
                } catch (_e) { continue; }
                const val = propDesc?.value;
                if (!val || typeof val !== 'object' || visited.has(val)) { continue; }
                visited.add(val);

                let sub = val;
                while (sub) {
                    if (_isStopProto(sub)) { break; }
                    _dbg(`patching sub ${propKey} level: ${sub.constructor?.name} keys=[${Object.getOwnPropertyNames(sub).join(',')}]`);
                    this.#patchObject(val, sub, propKey);
                    sub = Object.getPrototypeOf(sub);
                }
            }

            this.#running = true;
        } catch (e) {
            bridgeLogError(e, 'startProfiling failed mid-patch, rolling back');
            this.stopProfiling();
            return false;
        }

        if (this.#patches.size === 0) {
            bridgeLogWarning(`0 functions patched for ${uuid} — extension may use closures or GObject vfuncs`);
        }
        return true;
    }

    /** Restore all original functions and reset state. */
    stopProfiling() {
        if (!this.#running && this.#patches.size === 0) {
            return;
        }
        for (const { holder, name, original } of this.#patches.values()) {
            try {
                holder[name] = original;
            } catch (_e) {
                // Property may have become non-writable — ignore.
            }
        }
        this.#patches.clear();
        this.#running = false;
        this.#targetUuid = null;
        this.#callDepth = 0;
    }

    // ── Private ───────────────────────────────────────────────────────────

    /**
     * Enumerate own function-valued properties of `source` and install
     * timing wrappers on `holder`.
     * Instance properties take precedence — already-patched keys are skipped.
     * @param {object} holder - object to write the patched functions onto
     * @param {object} source - object whose properties are enumerated
     * @param {string} prefix - prepended to the function name in profile events
     *   (e.g. "_indicator" → event function = "_indicator.methodName")
     */
    #patchObject(holder, source, prefix) {
        for (const name of Object.getOwnPropertyNames(source)) {
            if (name === 'constructor') { continue; }
            const patchKey = prefix ? `${prefix}.${name}` : name;
            if (this.#patches.has(patchKey)) { continue; }

            let desc;
            try {
                desc = Object.getOwnPropertyDescriptor(source, name);
            } catch (_e) {
                continue;
            }
            if (!desc || typeof desc.value !== 'function') { continue; }

            const original = desc.value;
            this.#patches.set(patchKey, { holder, name, original });

            const profiler = this;
            const funcName = patchKey;
            holder[name] = function profiled(...args) {
                if (!profiler.#running) {
                    return original.apply(this, args);
                }
                const depth = profiler.#callDepth++;
                // GLib.get_monotonic_time() returns µs — convert to seconds.
                const start = GLib.get_monotonic_time() / 1e6;
                try {
                    return original.apply(this, args);
                } finally {
                    const end = GLib.get_monotonic_time() / 1e6;
                    profiler.#callDepth--;
                    profiler.#onEvent({
                        type: 'profile_event',
                        extensionUuid: profiler.#targetUuid,
                        function: funcName,
                        start,
                        end,
                        depth,
                    });
                }
            };
        }
    }
}
