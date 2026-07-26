'use strict';

import { Inspector } from '../bridge-extension/inspector.js';

function assert(condition, message) {
    if (!condition) {
        throw new Error(message);
    }
}

function assertJsonEqual(actual, expected, message) {
    assert(
        JSON.stringify(actual) === JSON.stringify(expected),
        `${message}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`
    );
}

function findProperty(result, name) {
    return result.properties.find(property => property.name === name);
}

const objectKey = { id: 'not-json-safe' };
const modules = new Map([
    ['dock', { start() {}, config: { edge: 'bottom' } }],
    [7, { numeric: true }],
    [true, { boolean: true }],
    [null, { nullable: true }],
    [objectKey, { hiddenBehindObjectKey: true }],
]);
const stateObj = {
    manager: { modules },
    legacy: { value: 42 },
};
const inspector = new Inspector(uuid => uuid === 'aurora@test' ? { stateObj } : null);

const rootResult = inspector.inspect('aurora@test');
const managerRow = findProperty(rootResult, 'manager');
assertJsonEqual(
    managerRow.pathSegment,
    { kind: 'property', key: 'manager' },
    'ordinary objects expose a typed property segment'
);

const mapResult = inspector.inspect('aurora@test', [
    { kind: 'property', key: 'manager' },
    { kind: 'property', key: 'modules' },
]);
assert(mapResult.properties.length === 5, 'Map entries replace Map prototype methods');

const dockRow = findProperty(mapResult, '"dock"');
assert(dockRow.type === 'object', 'string-keyed Map value is described');
assertJsonEqual(
    dockRow.pathSegment,
    { kind: 'map-key', key: 'dock' },
    'string Map key gets a typed path segment'
);
assertJsonEqual(
    findProperty(mapResult, '7').pathSegment,
    { kind: 'map-key', key: 7 },
    'number Map key remains a number'
);
assertJsonEqual(
    findProperty(mapResult, 'true').pathSegment,
    { kind: 'map-key', key: true },
    'boolean Map key remains a boolean'
);
assertJsonEqual(
    findProperty(mapResult, 'null').pathSegment,
    { kind: 'map-key', key: null },
    'null Map key remains null'
);

const unsupportedRow = mapResult.properties.find(
    property => property.name.includes('unsupported object key')
);
assert(unsupportedRow !== undefined, 'object Map key is shown as unsupported');
assert(unsupportedRow.pathSegment === null, 'object Map key cannot be navigated');
assert(
    JSON.stringify(mapResult).includes('hiddenBehindObjectKey'),
    'value behind unsupported key remains visible in the preview'
);

const dockResult = inspector.inspect('aurora@test', [
    { kind: 'property', key: 'manager' },
    { kind: 'property', key: 'modules' },
    { kind: 'map-key', key: 'dock' },
]);
assert(findProperty(dockResult, 'start').type === 'function', 'typed Map path drills into value');
assert(findProperty(dockResult, 'config').type === 'object', 'drilled value is fully inspected');

const legacyResult = inspector.inspect('aurora@test', ['legacy']);
assert(findProperty(legacyResult, 'value').value === '42', 'legacy string paths still work');

const largeMap = new Map();
for (let i = 0; i < 55; i++) {
    largeMap.set(`item-${i}`, i);
}
stateObj.largeMap = largeMap;
const boundedResult = inspector.inspect('aurora@test', [
    { kind: 'property', key: 'largeMap' },
]);
assert(boundedResult.properties.length === 51, 'Map serialization is bounded to 50 entries');
assertJsonEqual(
    boundedResult.properties.at(-1),
    { name: '…', type: 'info', value: '5 more entries' },
    'bounded Map reports omitted entries'
);

print('inspector bridge tests OK');
