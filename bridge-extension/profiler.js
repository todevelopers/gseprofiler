'use strict';

import GLib from 'gi://GLib';
import { bridgeLogError, bridgeLogWarning } from './logger.js';

const DEBUG = false;
function _dbg(...args) { if (DEBUG) { log(args.join(' ')); } }

// The root object is depth 0. These limits keep discovery predictable even
// when an extension exposes a very large or cyclic runtime object graph.
const DEFAULT_LIMITS = Object.freeze({
    maxDepth: 6,
    maxVisitedObjects: 2000,
    maxCollectionEntries: 512,
    maxPropertiesPerObject: 2048,
    maxPatchedFunctions: 5000,
    batchDelayMs: 50,
    maxBatchEvents: 256,
});

// Base classes whose methods we never patch (framework/native internals).
const STOP_CLASSES = new Set([
    'Extension',
    'Object',
    'Function',
    'Array',
    'Map',
    'Set',
    'WeakMap',
    'WeakSet',
    'Date',
    'RegExp',
    'Promise',
    'Error',
    'ArrayBuffer',
    'DataView',
    'Int8Array',
    'Uint8Array',
    'Uint8ClampedArray',
    'Int16Array',
    'Uint16Array',
    'Int32Array',
    'Uint32Array',
    'Float32Array',
    'Float64Array',
    'BigInt64Array',
    'BigUint64Array',
]);

// GJS names GObject C types as Namespace_ClassName (e.g. St_Widget, Gio_File).
const FRAMEWORK_RE = /^(St_|Clutter_|Meta_|Shell_|GObject_|Gio_|GLib_|Mutter_|Gdk_|Gtk_|Pango_|Atk_|Soup_|Json_)/;
const HAS_OWN = Object.prototype.hasOwnProperty;
const MAP_ENTRIES = Map.prototype.entries;
const SET_VALUES = Set.prototype.values;
const NATIVE_COLLECTION_PROTOTYPES = new Set([
    Array.prototype,
    Map.prototype,
    Set.prototype,
]);
const MAX_RENDERED_KEY_LENGTH = 80;

function _emptyStats() {
    return {
        patchedFunctions: 0,
        visitedObjects: 0,
        skippedFunctions: 0,
        truncated: false,
    };
}

function _positiveInteger(value, fallback) {
    return Number.isInteger(value) && value > 0 ? value : fallback;
}

function _resolvedLimits(overrides) {
    return {
        maxDepth: Number.isInteger(overrides.maxDepth) && overrides.maxDepth >= 0
            ? overrides.maxDepth
            : DEFAULT_LIMITS.maxDepth,
        maxVisitedObjects: _positiveInteger(
            overrides.maxVisitedObjects,
            DEFAULT_LIMITS.maxVisitedObjects,
        ),
        maxCollectionEntries: _positiveInteger(
            overrides.maxCollectionEntries,
            DEFAULT_LIMITS.maxCollectionEntries,
        ),
        maxPropertiesPerObject: _positiveInteger(
            overrides.maxPropertiesPerObject,
            DEFAULT_LIMITS.maxPropertiesPerObject,
        ),
        maxPatchedFunctions: _positiveInteger(
            overrides.maxPatchedFunctions,
            DEFAULT_LIMITS.maxPatchedFunctions,
        ),
        batchDelayMs: _positiveInteger(overrides.batchDelayMs, DEFAULT_LIMITS.batchDelayMs),
        maxBatchEvents: _positiveInteger(
            overrides.maxBatchEvents,
            DEFAULT_LIMITS.maxBatchEvents,
        ),
    };
}

function _constructorName(proto) {
    try {
        const desc = Object.getOwnPropertyDescriptor(proto, 'constructor');
        return typeof desc?.value === 'function' ? desc.value.name : '';
    } catch (_e) {
        return '';
    }
}

function _isStopProto(proto) {
    const name = _constructorName(proto);
    return STOP_CLASSES.has(name) || FRAMEWORK_RE.test(name);
}

function _collectionKind(value) {
    if (Array.isArray(value)) {
        return 'array';
    }

    // Use captured collection intrinsics as side-effect-free brand checks.
    // Object.prototype.toString is unsuitable because it reads a
    // user-defined Symbol.toStringTag getter during discovery.
    try {
        MAP_ENTRIES.call(value);
        return 'map';
    } catch (_e) { /* not a Map */ }
    try {
        SET_VALUES.call(value);
        return 'set';
    } catch (_e) { /* not a Set */ }
    return null;
}

function _isObject(value) {
    return value !== null && typeof value === 'object';
}

function _propertyPrefix(prefix, name) {
    return prefix ? `${prefix}.${name}` : name;
}

function _shortString(value, index) {
    if (value.length <= MAX_RENDERED_KEY_LENGTH) {
        return value;
    }
    const suffix = `…#${index}`;
    return `${value.slice(0, MAX_RENDERED_KEY_LENGTH - suffix.length)}${suffix}`;
}

function _mapEntryPrefix(prefix, key, index) {
    let rendered;
    switch (typeof key) {
    case 'string':
        rendered = JSON.stringify(_shortString(key, index));
        break;
    case 'number':
        if (Number.isNaN(key)) {
            rendered = 'NaN';
        } else if (Object.is(key, -0)) {
            rendered = '-0';
        } else {
            rendered = String(key);
        }
        break;
    case 'bigint':
        rendered = `${String(key)}n`;
        break;
    case 'boolean':
    case 'undefined':
        rendered = String(key);
        break;
    case 'symbol': {
        let description = '';
        try {
            description = key.description ?? '';
        } catch (_e) {
            description = '';
        }
        rendered = `Symbol(${JSON.stringify(_shortString(description, index))})#${index}`;
        break;
    }
    default:
        rendered = key === null ? 'null' : `<key:${index}>`;
        break;
    }
    return `${prefix}[${rendered}]`;
}

function _isArrayIndex(name) {
    if (!/^(0|[1-9]\d*)$/.test(name)) {
        return false;
    }
    const value = Number(name);
    return value >= 0 && value < 4294967295 && Number.isInteger(value);
}

/**
 * Extension function profiler. It walks the target extension's reachable
 * runtime object graph, installs reversible timing wrappers, and emits bounded
 * batches of legacy profile_event objects.
 */
export class Profiler {
    #running = false;
    #targetUuid = null;
    /**
     * @type {Array<{
     *   holder: object,
     *   name: string,
     *   hadOwn: boolean,
     *   originalDescriptor: PropertyDescriptor | null,
     *   wrapper: Function
     * }>}
     */
    #patches = [];
    #patchedMembers = new WeakMap();
    #discoveryStopped = false;
    #callDepth = 0;
    #sessionId = 0;
    #clientSessionId = null;
    #eventQueue = [];
    #flushSource = null;
    #stats = _emptyStats();
    #limits;
    /** @type {(event: object) => void} */
    #onEvent;
    /** @type {(uuid: string) => object | null | undefined} */
    #lookupExtension;

    /**
     * @param {(event: object) => void} onEvent - receives profile_batch
     * @param {(uuid: string) => object | null | undefined} lookupExtension
     * @param {object} [limitOverrides] - primarily useful for focused tests
     */
    constructor(onEvent, lookupExtension, limitOverrides = {}) {
        this.#onEvent = onEvent;
        this.#lookupExtension = lookupExtension;
        this.#limits = _resolvedLimits(limitOverrides);
    }

    get isRunning() {
        return this.#running;
    }

    /** Return a snapshot; callers cannot mutate the profiler's counters. */
    get stats() {
        return { ...this.#stats };
    }

    /**
     * Discover and monkey-patch reachable functions on an extension stateObj.
     * @param {string} uuid
     * @param {number|null} [clientSessionId] app-provided recording generation
     * @returns {boolean} whether discovery completed and profiling is running
     */
    startProfiling(uuid, clientSessionId = null) {
        if (this.#running || this.#patches.length > 0 || this.#eventQueue.length > 0) {
            // The UI treats start-while-running as a fresh recording and has
            // already cleared its data. Do not leak a pending old batch into
            // the new session (the UUID can be identical).
            this.#finishProfiling(false);
        }

        this.#stats = _emptyStats();
        this.#patchedMembers = new WeakMap();
        this.#discoveryStopped = false;
        this.#clientSessionId = null;

        let ext;
        try {
            ext = this.#lookupExtension?.(uuid);
        } catch (e) {
            bridgeLogError(e, `startProfiling: lookup failed for ${uuid}`);
            return false;
        }

        _dbg(`startProfiling: lookup=${!!ext} state=${ext?.state} stateObj=${!!ext?.stateObj}`);
        if (!ext?.stateObj) {
            bridgeLogWarning(`startProfiling: no stateObj for ${uuid}`);
            return false;
        }

        this.#targetUuid = uuid;
        this.#clientSessionId = Number.isSafeInteger(clientSessionId)
            && clientSessionId >= 0
            ? clientSessionId
            : null;
        this.#callDepth = 0;
        this.#sessionId++;

        try {
            this.#patchGraph(ext.stateObj);
            this.#running = true;
        } catch (e) {
            // Object/proxy-level failures are handled locally. This is only a
            // last-resort rollback so the target extension is never left half
            // instrumented.
            bridgeLogError(e, 'startProfiling failed mid-patch, rolling back');
            this.#finishProfiling(false);
            this.#stats.patchedFunctions = 0;
            return false;
        }

        if (this.#patches.length === 0) {
            bridgeLogWarning(
                `0 functions patched for ${uuid} — extension may use closures or GObject vfuncs`,
            );
        }
        return true;
    }

    /**
     * Flush queued events, restore exact own descriptors, and delete wrapper
     * shadows that were created for inherited methods.
     */
    stopProfiling() {
        this.#finishProfiling(true);
    }

    #finishProfiling(flushQueuedEvents) {
        if (
            !this.#running
            && this.#patches.length === 0
            && this.#eventQueue.length === 0
            && this.#flushSource === null
        ) {
            return;
        }

        this.#running = false;
        if (flushQueuedEvents) {
            this.#flushEvents();
        } else {
            this.#discardEvents();
        }

        for (let index = this.#patches.length - 1; index >= 0; index--) {
            const {
                holder,
                name,
                hadOwn,
                originalDescriptor,
                wrapper,
            } = this.#patches[index];
            try {
                const currentDescriptor = Object.getOwnPropertyDescriptor(holder, name);
                // Preserve a replacement or deletion made by the extension
                // itself while profiling. Only undo our still-installed
                // wrapper.
                if (!currentDescriptor || currentDescriptor.value !== wrapper) {
                    continue;
                }
                const restored = hadOwn
                    ? Reflect.defineProperty(holder, name, originalDescriptor)
                    : Reflect.deleteProperty(holder, name);
                if (!restored) {
                    bridgeLogWarning(`could not restore profiled property '${name}'`);
                }
            } catch (e) {
                bridgeLogError(e, `could not restore profiled property '${name}'`);
            }
        }

        this.#patches = [];
        this.#patchedMembers = new WeakMap();
        this.#targetUuid = null;
        this.#clientSessionId = null;
        this.#callDepth = 0;
    }

    // ── Discovery ─────────────────────────────────────────────────────────

    /**
     * Breadth-first scan of the reachable graph.
     *
     * Order matters as much as the limits do. Methods sit near the root while
     * bulk data (cached items, parsed entries) sits deeper, so a depth-first
     * walk lets one data-heavy sibling consume the whole visit budget and
     * starve every sibling after it — the extension's real behaviour then
     * never gets instrumented at all. Level order spends the budget on the
     * shallow objects first and truncates the deep data instead.
     */
    #patchGraph(root) {
        /** @type {Array<{obj: object, prefix: string, depth: number}>} */
        const queue = [];
        let head = 0;
        // Claim objects when they are queued so the same object is never
        // scheduled twice, even when several parents reference it.
        const visited = new Set();
        const enqueue = (value, prefix, depth) => {
            if (!_isObject(value) || visited.has(value)) {
                return;
            }
            if (depth > this.#limits.maxDepth) {
                this.#stats.truncated = true;
                return;
            }
            visited.add(value);
            queue.push({ obj: value, prefix, depth });
        };

        enqueue(root, '', 0);

        while (head < queue.length) {
            if (this.#stats.visitedObjects >= this.#limits.maxVisitedObjects) {
                this.#stats.truncated = true;
                break;
            }
            const { obj, prefix, depth } = queue[head++];
            this.#stats.visitedObjects++;
            this.#visitObject(obj, prefix, depth, enqueue);
            if (this.#discoveryStopped) {
                break;
            }
        }

        if (head < queue.length) {
            this.#stats.truncated = true;
        }
    }

    /**
     * Patch one object's methods and queue its object-valued children.
     * @param {(value: unknown, prefix: string, depth: number) => void} enqueue
     */
    #visitObject(obj, prefix, depth, enqueue) {
        const collectionKind = _collectionKind(obj);

        // Own methods can be extension code even on a collection instance.
        this.#patchObject(obj, obj, prefix);
        if (this.#discoveryStopped) {
            return;
        }

        // Real Array/Map/Set subclasses may define extension methods on their
        // custom prototypes. Walk those, but stop before the captured native
        // collection prototypes.
        let proto;
        try {
            proto = Object.getPrototypeOf(obj);
        } catch (_e) {
            proto = null;
        }
        while (
            proto
            && !NATIVE_COLLECTION_PROTOTYPES.has(proto)
            && !_isStopProto(proto)
        ) {
            this.#patchObject(obj, proto, prefix);
            if (this.#discoveryStopped) {
                return;
            }
            try {
                proto = Object.getPrototypeOf(proto);
            } catch (_e) {
                break;
            }
        }

        const handledOwnProperties = new Set();
        if (collectionKind === 'map') {
            this.#walkMap(obj, prefix, depth, enqueue);
        } else if (collectionKind === 'set') {
            this.#walkSet(obj, prefix, depth, enqueue);
        } else if (collectionKind === 'array') {
            this.#walkArray(obj, prefix, depth, handledOwnProperties, enqueue);
        }

        // Collection instances may also carry custom own properties.
        this.#walkOwnObjectProperties(
            obj,
            prefix,
            depth,
            handledOwnProperties,
            enqueue,
        );
    }

    #boundedOwnPropertyNames(obj) {
        let names;
        try {
            names = Object.getOwnPropertyNames(obj);
        } catch (_e) {
            return [];
        }
        if (names.length > this.#limits.maxPropertiesPerObject) {
            this.#stats.truncated = true;
            return names.slice(0, this.#limits.maxPropertiesPerObject);
        }
        return names;
    }

    #walkMap(map, prefix, depth, enqueue) {
        let iterator;
        try {
            iterator = MAP_ENTRIES.call(map);
        } catch (_e) {
            return;
        }

        let index = 0;
        while (index < this.#limits.maxCollectionEntries) {
            let step;
            try {
                step = iterator.next();
            } catch (_e) {
                return;
            }
            if (step.done) {
                return;
            }

            const entry = step.value;
            if (Array.isArray(entry) && entry.length >= 2) {
                const childPrefix = _mapEntryPrefix(prefix, entry[0], index);
                enqueue(entry[1], childPrefix, depth + 1);
            }
            index++;
        }

        try {
            if (!iterator.next().done) {
                this.#stats.truncated = true;
            }
        } catch (_e) {
            // The already-consumed entries remain valid; no session failure.
        }

    }

    #walkSet(set, prefix, depth, enqueue) {
        let iterator;
        try {
            iterator = SET_VALUES.call(set);
        } catch (_e) {
            return;
        }

        let index = 0;
        while (index < this.#limits.maxCollectionEntries) {
            let step;
            try {
                step = iterator.next();
            } catch (_e) {
                return;
            }
            if (step.done) {
                return;
            }

            enqueue(step.value, `${prefix}[${index}]`, depth + 1);
            index++;
        }

        try {
            if (!iterator.next().done) {
                this.#stats.truncated = true;
            }
        } catch (_e) {
            // The already-consumed entries remain valid; no session failure.
        }
    }

    #walkArray(array, prefix, depth, handledOwnProperties, enqueue) {
        const names = this.#boundedOwnPropertyNames(array);

        let visitedEntries = 0;
        for (const name of names) {
            if (!_isArrayIndex(name)) {
                continue;
            }
            // Ignore every numeric own property in the generic follow-up walk,
            // including entries beyond the collection cap.
            handledOwnProperties.add(name);
            if (visitedEntries >= this.#limits.maxCollectionEntries) {
                this.#stats.truncated = true;
                continue;
            }

            let desc;
            try {
                desc = Object.getOwnPropertyDescriptor(array, name);
            } catch (_e) {
                continue;
            }
            enqueue(desc?.value, `${prefix}[${name}]`, depth + 1);
            visitedEntries++;
        }
    }

    #walkOwnObjectProperties(obj, prefix, depth, ignoredNames, enqueue) {
        const names = this.#boundedOwnPropertyNames(obj);

        for (const name of names) {
            if (ignoredNames.has(name)) {
                continue;
            }
            let desc;
            try {
                desc = Object.getOwnPropertyDescriptor(obj, name);
            } catch (_e) {
                continue;
            }
            if (!_isObject(desc?.value)) {
                continue;
            }
            enqueue(desc.value, _propertyPrefix(prefix, name), depth + 1);
        }
    }

    // ── Patching ──────────────────────────────────────────────────────────

    #patchObject(holder, source, prefix) {
        const names = this.#boundedOwnPropertyNames(source);

        let patchedNames = this.#patchedMembers.get(holder);
        if (!patchedNames) {
            patchedNames = new Set();
            this.#patchedMembers.set(holder, patchedNames);
        }

        for (const name of names) {
            if (name === 'constructor' || patchedNames.has(name)) {
                continue;
            }

            // An own non-function/accessor shadows an inherited method and
            // must take precedence; never overwrite it while walking a proto.
            if (source !== holder) {
                let ownDesc;
                try {
                    ownDesc = Object.getOwnPropertyDescriptor(holder, name);
                } catch (_e) {
                    continue;
                }
                if (ownDesc) {
                    continue;
                }
            }

            let sourceDesc;
            try {
                sourceDesc = Object.getOwnPropertyDescriptor(source, name);
            } catch (_e) {
                continue;
            }
            if (!sourceDesc || typeof sourceDesc.value !== 'function') {
                continue;
            }

            if (this.#stats.patchedFunctions >= this.#limits.maxPatchedFunctions) {
                this.#stats.truncated = true;
                this.#discoveryStopped = true;
                return;
            }

            const original = sourceDesc.value;
            const funcName = _propertyPrefix(prefix, name);
            const profiler = this;
            const sessionId = this.#sessionId;
            const clientSessionId = this.#clientSessionId;
            const wrapper = function profiled(...args) {
                if (!profiler.#running || profiler.#sessionId !== sessionId) {
                    return Reflect.apply(original, this, args);
                }
                const callDepth = profiler.#callDepth++;
                const start = GLib.get_monotonic_time() / 1e6;
                try {
                    return Reflect.apply(original, this, args);
                } finally {
                    const end = GLib.get_monotonic_time() / 1e6;
                    profiler.#callDepth--;
                    const event = {
                        type: 'profile_event',
                        extensionUuid: profiler.#targetUuid,
                        function: funcName,
                        start,
                        end,
                        depth: callDepth,
                    };
                    if (clientSessionId !== null) {
                        event.sessionId = clientSessionId;
                    }
                    profiler.#queueEvent(event);
                }
            };

            let originalDescriptor = null;
            let hadOwn = false;
            try {
                hadOwn = HAS_OWN.call(holder, name);
                if (hadOwn) {
                    originalDescriptor = Object.getOwnPropertyDescriptor(holder, name);
                    if (!originalDescriptor) {
                        this.#stats.skippedFunctions++;
                        continue;
                    }
                }

                const wrapperDescriptor = hadOwn
                    ? { ...originalDescriptor, value: wrapper }
                    : {
                        value: wrapper,
                        writable: true,
                        enumerable: sourceDesc.enumerable,
                        configurable: true,
                    };
                if (!Reflect.defineProperty(holder, name, wrapperDescriptor)) {
                    this.#stats.skippedFunctions++;
                    continue;
                }
            } catch (_e) {
                this.#stats.skippedFunctions++;
                continue;
            }

            this.#patches.push({
                holder,
                name,
                hadOwn,
                originalDescriptor,
                wrapper,
            });
            patchedNames.add(name);
            this.#stats.patchedFunctions++;
        }
    }

    // ── Event batching ────────────────────────────────────────────────────

    #queueEvent(event) {
        this.#eventQueue.push(event);
        if (this.#eventQueue.length >= this.#limits.maxBatchEvents) {
            this.#flushEvents();
            return;
        }
        if (this.#flushSource !== null) {
            return;
        }

        try {
            this.#flushSource = GLib.timeout_add(
                GLib.PRIORITY_DEFAULT,
                this.#limits.batchDelayMs,
                () => {
                    this.#flushSource = null;
                    this.#emitBatch();
                    return GLib.SOURCE_REMOVE;
                },
            );
        } catch (e) {
            bridgeLogError(e, 'could not schedule profile batch flush');
            this.#emitBatch();
        }
    }

    #flushEvents() {
        if (this.#flushSource !== null) {
            try {
                GLib.source_remove(this.#flushSource);
            } catch (_e) {
                // The source may already have been dispatched.
            }
            this.#flushSource = null;
        }
        this.#emitBatch();
    }

    #discardEvents() {
        if (this.#flushSource !== null) {
            try {
                GLib.source_remove(this.#flushSource);
            } catch (_e) {
                // The source may already have been dispatched.
            }
            this.#flushSource = null;
        }
        this.#eventQueue = [];
    }

    #emitBatch() {
        if (this.#eventQueue.length === 0) {
            return;
        }

        const events = this.#eventQueue;
        this.#eventQueue = [];
        try {
            const batch = {
                type: 'profile_batch',
                extensionUuid: events[0].extensionUuid,
                events,
            };
            if (HAS_OWN.call(events[0], 'sessionId')) {
                batch.sessionId = events[0].sessionId;
            }
            this.#onEvent?.(batch);
        } catch (e) {
            // Profiling must never change the behaviour of the target method.
            bridgeLogError(e, 'profile batch callback failed');
        }
    }
}
