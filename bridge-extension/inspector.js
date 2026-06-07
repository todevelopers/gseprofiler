'use strict';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import { bridgeLog, bridgeLogError } from './logger.js';

const _MAX_CHILDREN = 50;
const _MAX_STRING_LEN = 200;

/**
 * Extension state object inspector — enumerates properties and methods on
 * a running extension's exported object.
 *
 * Emits: { type: "inspect_result", extensionUuid, properties: [...] }
 */
export class Inspector {
    /**
     * @param {string} uuid
     * @param {string[]} [path] - property key chain from stateObj to the object to inspect
     * @returns {{ properties: object[] }}
     */
    inspect(uuid, path = []) {
        const ext = Main.extensionManager.lookup(uuid);
        if (!ext?.stateObj) {
            bridgeLog(`inspector: no stateObj for ${uuid}`);
            return { properties: [] };
        }
        try {
            let obj = ext.stateObj;
            for (const key of path) {
                if (obj === null || obj === undefined || typeof obj !== 'object') {
                    bridgeLog(`inspector: path resolution failed at key "${key}"`);
                    return { properties: [] };
                }
                obj = obj[key];
            }
            if (obj === null || obj === undefined || typeof obj !== 'object') {
                bridgeLog(`inspector: resolved path is not an object`);
                return { properties: [] };
            }
            // Arrays are serialized by index so the user sees elements, not prototype methods.
            const properties = Array.isArray(obj)
                ? _serializeArray(obj)
                : _serializeObject(obj);
            return { properties };
        } catch (e) {
            bridgeLogError(e, 'inspector.inspect');
            return { properties: [] };
        }
    }

}

// ── Serialization helpers ────────────────────────────────────────────────────

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
            const item = { name: String(i), type, value };
            if (children) { item.children = children; }
            result.push(item);
        } catch (_) {
            result.push({ name: String(i), type: 'error', value: '[serialization error]' });
        }
    }
    if (arr.length > _MAX_CHILDREN) {
        result.push({ name: '…', type: 'info', value: `${arr.length - _MAX_CHILDREN} more items` });
    }
    return result;
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

    const result = { name, type: type ?? 'error', value: value ?? '' };
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
