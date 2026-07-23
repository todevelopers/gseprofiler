'use strict';

import { bridgeLogError, bridgeLogWarning } from './logger.js';

const _MAX_CHILDREN = 50;
const _MAX_STRING_LEN = 200;
const _mapEntries = Map.prototype.entries;
const _mapGet = Map.prototype.get;
const _mapSizeGetter = Object.getOwnPropertyDescriptor(Map.prototype, 'size').get;

/**
 * Extension state object inspector — enumerates properties and methods on
 * a running extension's exported object.
 *
 * Emits: { type: "inspect_result", extensionUuid, properties: [...] }
 */
export class Inspector {
    /**
     * @param {(uuid: string) => object|null|undefined} lookupExtension
     */
    constructor(lookupExtension) {
        this._lookupExtension = lookupExtension;
    }

    /**
     * @param {string} uuid
     * @param {Array<string|object>} [path] - typed path from stateObj.
     * Legacy string segments are treated as object properties.
     * @returns {{ properties: object[] }}
     */
    inspect(uuid, path = []) {
        const ext = this._lookupExtension?.(uuid);
        if (!ext?.stateObj) {
            bridgeLogWarning(`inspector: no stateObj for ${uuid}`);
            return { properties: [] };
        }
        try {
            let obj = ext.stateObj;
            for (const segment of path) {
                if (obj === null || obj === undefined || typeof obj !== 'object') {
                    bridgeLogWarning(
                        `inspector: path resolution failed at ${_formatPathSegment(segment)}`
                    );
                    return { properties: [] };
                }
                const resolved = _resolvePathSegment(obj, segment);
                if (!resolved.ok) {
                    bridgeLogWarning(
                        `inspector: path resolution failed at ${_formatPathSegment(segment)}`
                    );
                    return { properties: [] };
                }
                obj = resolved.value;
            }
            if (obj === null || obj === undefined || typeof obj !== 'object') {
                bridgeLogWarning(`inspector: resolved path is not an object`);
                return { properties: [] };
            }
            let properties;
            if (Array.isArray(obj)) {
                // Arrays are serialized by index, not by prototype methods.
                properties = _serializeArray(obj);
            } else if (obj instanceof Map) {
                properties = _serializeTopLevelMap(obj);
            } else {
                properties = _serializeObject(obj);
            }
            return { properties };
        } catch (e) {
            bridgeLogError(e, 'inspector.inspect');
            return { properties: [] };
        }
    }

}

// ── Serialization helpers ────────────────────────────────────────────────────

/**
 * Resolve one inspector path segment.
 *
 * New paths use:
 *   { kind: "property", key: string }
 *   { kind: "map-key", key: string|number|boolean|null }
 *
 * A string remains a supported shorthand for a property segment so requests
 * from older app versions continue to work.
 */
function _resolvePathSegment(obj, segment) {
    if (typeof segment === 'string') {
        return { ok: true, value: obj[segment] };
    }
    if (segment === null || typeof segment !== 'object' || Array.isArray(segment)) {
        return { ok: false };
    }
    if (segment.kind === 'property' && typeof segment.key === 'string') {
        return { ok: true, value: obj[segment.key] };
    }
    if (
        segment.kind === 'map-key' &&
        obj instanceof Map &&
        _isJsonSafeMapKey(segment.key)
    ) {
        return { ok: true, value: _mapGet.call(obj, segment.key) };
    }
    return { ok: false };
}

function _formatPathSegment(segment) {
    if (typeof segment === 'string') {
        return `property ${JSON.stringify(segment)}`;
    }
    if (segment?.kind === 'property' && typeof segment.key === 'string') {
        return `property ${JSON.stringify(segment.key)}`;
    }
    if (segment?.kind === 'map-key' && _isJsonSafeMapKey(segment.key)) {
        return `Map key ${_formatMapKey(segment.key)}`;
    }
    return 'an invalid path segment';
}

function _serializeObject(obj) {
    const seen = new WeakSet();
    seen.add(obj);

    // Collect from prototype chain 1 level up (excluding Object.prototype),
    // then let own properties override prototype entries.
    const propsMap = new Map();
    const proto = Object.getPrototypeOf(obj);
    if (proto && proto !== Object.prototype) {
        for (const name of Object.getOwnPropertyNames(proto)) {
            if (name === 'constructor') { continue; }
            const desc = _safeDescriptor(proto, name);
            if (desc) { propsMap.set(name, desc); }
        }
    }
    for (const name of Object.getOwnPropertyNames(obj)) {
        const desc = _safeDescriptor(obj, name);
        if (desc) { propsMap.set(name, desc); }
    }

    const result = [];
    for (const [name, desc] of propsMap) {
        result.push(_serializeProp(name, desc, obj, seen));
    }
    return result;
}

/** Serialize an array as indexed properties so drilling into it shows its elements. */
function _serializeArray(arr) {
    const seen = new WeakSet();
    seen.add(arr);
    const result = [];
    const limit = Math.min(arr.length, _MAX_CHILDREN);
    for (let i = 0; i < limit; i++) {
        try {
            const [type, value, children] = _describeValue(arr[i], seen);
            const item = {
                name: String(i),
                type,
                value,
                pathSegment: { kind: 'property', key: String(i) },
            };
            if (children) { item.children = children; }
            result.push(item);
        } catch (_) {
            result.push({
                name: String(i),
                type: 'error',
                value: '[serialization error]',
                pathSegment: { kind: 'property', key: String(i) },
            });
        }
    }
    if (arr.length > _MAX_CHILDREN) {
        result.push({ name: '…', type: 'info', value: `${arr.length - _MAX_CHILDREN} more items` });
    }
    return result;
}

/** Serialize a Map as bounded entries rather than Map.prototype methods. */
function _serializeTopLevelMap(map) {
    const seen = new WeakSet();
    seen.add(map);
    return _serializeMap(map, seen);
}

function _serializeMap(map, seen) {
    const result = [];
    const size = _mapSizeGetter.call(map);
    const iterator = _mapEntries.call(map);
    let count = 0;

    while (count < _MAX_CHILDREN) {
        const next = iterator.next();
        if (next.done) { break; }

        const [key, entryValue] = next.value;
        const keyInfo = _describeMapKey(key, count);
        let type, value, children;
        try {
            [type, value, children] = _describeValue(entryValue, seen);
        } catch (_) {
            type = 'error';
            value = '[serialization error]';
        }

        const item = {
            name: keyInfo.label,
            type: type ?? 'error',
            value: value ?? '',
            pathSegment: keyInfo.pathSegment,
        };
        if (children) { item.children = children; }
        result.push(item);
        count++;
    }

    if (size > count) {
        result.push({ name: '…', type: 'info', value: `${size - count} more entries` });
    }
    return result;
}

function _describeMapKey(key, index) {
    if (_isJsonSafeMapKey(key)) {
        return {
            label: _formatMapKey(key),
            pathSegment: { kind: 'map-key', key },
        };
    }

    const keyType = key === undefined ? 'undefined' : typeof key;
    return {
        label: `[unsupported ${keyType} key #${index + 1}]`,
        pathSegment: null,
    };
}

function _isJsonSafeMapKey(key) {
    if (key === null || typeof key === 'string' || typeof key === 'boolean') {
        return true;
    }
    return typeof key === 'number' && Number.isFinite(key);
}

function _formatMapKey(key) {
    if (typeof key === 'string') {
        const display = key.length > _MAX_STRING_LEN
            ? `${key.slice(0, _MAX_STRING_LEN)}…`
            : key;
        return JSON.stringify(display);
    }
    return String(key);
}

function _serializeProp(name, desc, holder, seen) {
    let type, value, children;

    try {
        if (typeof desc.get === 'function') {
            const v = desc.get.call(holder);
            [type, value, children] = _describeValue(v, seen);
        } else {
            [type, value, children] = _describeValue(desc.value, seen);
        }
    } catch (e) {
        type = 'error';
        value = `[serialization error: ${e.message}]`;
    }

    const result = {
        name,
        type: type ?? 'error',
        value: value ?? '',
        pathSegment: { kind: 'property', key: name },
    };
    if (children) { result.children = children; }
    return result;
}

function _describeValue(v, seen) {
    if (v === null) { return ['null', 'null', null]; }
    if (v === undefined) { return ['undefined', 'undefined', null]; }

    const t = typeof v;
    if (t === 'function') { return ['function', `function ${v.name || '?'}() { … }`, null]; }
    if (t === 'symbol') { return ['symbol', v.toString(), null]; }
    if (t === 'number') { return ['number', String(v), null]; }
    if (t === 'boolean') { return ['boolean', String(v), null]; }
    if (t === 'string') {
        const s = v.length > _MAX_STRING_LEN ? `${v.slice(0, _MAX_STRING_LEN)}…` : v;
        return ['string', s, null];
    }

    if (seen.has(v)) { return ['object', '[Circular]', null]; }

    if (Array.isArray(v)) {
        seen.add(v);
        const children = [];
        const limit = Math.min(v.length, _MAX_CHILDREN);
        for (let i = 0; i < limit; i++) {
            try {
                const [ct, cv] = _describeValue(v[i], seen);
                children.push({ name: String(i), type: ct, value: String(cv) });
            } catch (_) {
                children.push({ name: String(i), type: 'error', value: '[serialization error]' });
            }
        }
        if (v.length > _MAX_CHILDREN) {
            children.push({ name: '…', type: 'info', value: `${v.length - _MAX_CHILDREN} more` });
        }
        seen.delete(v);
        return ['array', `Array(${v.length})`, children.length > 0 ? children : null];
    }

    if (v instanceof Map) {
        seen.add(v);
        const children = _serializeMap(v, seen);
        seen.delete(v);
        const size = _mapSizeGetter.call(v);
        return ['map', `Map(${size})`, children.length > 0 ? children : null];
    }

    // Plain object / GObject instance.
    seen.add(v);
    const children = [];
    try {
        const keys = Object.getOwnPropertyNames(v).slice(0, _MAX_CHILDREN);
        for (const k of keys) {
            if (k === '__proto__') { continue; }
            const desc = _safeDescriptor(v, k);
            if (!desc) { continue; }
            let ct, cv;
            try {
                if (typeof desc.get === 'function') {
                    ct = 'getter';
                    [, cv] = _describeValue(desc.get.call(v), seen);
                } else {
                    [ct, cv] = _describeValue(desc.value, seen);
                }
            } catch (_) {
                ct = 'error';
                cv = '[serialization error]';
            }
            children.push({ name: k, type: ct, value: String(cv) });
        }
    } catch (_) { /* skip on enumeration errors */ }
    seen.delete(v);

    const ctorName = v.constructor?.name ?? '';
    const label = ctorName && ctorName !== 'Object' ? `[${ctorName}]` : '{…}';
    return ['object', label, children.length > 0 ? children : null];
}

function _safeDescriptor(obj, name) {
    try {
        return Object.getOwnPropertyDescriptor(obj, name) ?? null;
    } catch (_) {
        return null;
    }
}
