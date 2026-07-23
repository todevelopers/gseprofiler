'use strict';

import GLib from 'gi://GLib';
import { Profiler } from '../bridge-extension/profiler.js';

const HAS_OWN = Object.prototype.hasOwnProperty;

function assert(condition, message) {
    if (!condition) {
        throw new Error(message);
    }
}

function assertEqual(actual, expected, message) {
    if (!Object.is(actual, expected)) {
        throw new Error(`${message}: expected ${String(expected)}, got ${String(actual)}`);
    }
}

function makeProfiler(stateObj, messages, limits = {}) {
    return new Profiler(
        message => messages.push(message),
        uuid => uuid === 'test@example.com' ? { stateObj } : null,
        {
            // Keep scheduled sources dormant until each test explicitly stops.
            batchDelayMs: 60000,
            ...limits,
        },
    );
}

function eventNames(messages) {
    return messages
        .filter(message => message.type === 'profile_batch')
        .flatMap(message => message.events)
        .map(event => event.function);
}

function testCollectionTraversal() {
    class MapModule {
        run() {
            return 'map';
        }
    }
    class SetModule {
        run() {
            return 'set';
        }
    }
    class ArrayModule {
        run() {
            return 'array';
        }
    }
    class ModuleMap extends Map {
        refreshContainer() {
            return 'map-container';
        }
    }
    class ModuleSet extends Set {
        refreshContainer() {
            return 'set-container';
        }
    }
    class ModuleArray extends Array {
        refreshContainer() {
            return 'array-container';
        }
    }

    const mapModule = new MapModule();
    const setModule = new SetModule();
    const arrayModule = new ArrayModule();
    const modules = new ModuleMap([['dock', mapModule]]);
    let toStringTagReads = 0;
    Object.defineProperty(modules, Symbol.toStringTag, {
        configurable: true,
        get() {
            toStringTagReads++;
            return 'CustomModuleMap';
        },
    });
    const group = new ModuleSet([setModule]);
    const list = new ModuleArray();
    list.push(arrayModule);
    const stateObj = { modules, group, list };
    stateObj.self = stateObj;

    const messages = [];
    const profiler = makeProfiler(stateObj, messages);
    assert(profiler.startProfiling('test@example.com'), 'collection profiling should start');
    assertEqual(
        toStringTagReads,
        0,
        'collection discovery must not invoke Symbol.toStringTag getters',
    );

    assertEqual(
        profiler.stats.patchedFunctions,
        6,
        'module methods and custom collection-subclass methods should be patched',
    );
    assert(!HAS_OWN.call(modules, 'get'), 'Map.get must not be shadow-patched');
    assert(!HAS_OWN.call(group, 'add'), 'Set.add must not be shadow-patched');
    assert(!HAS_OWN.call(list, 'push'), 'Array.push must not be shadow-patched');

    assertEqual(mapModule.run(), 'map', 'Map module result should be preserved');
    assertEqual(setModule.run(), 'set', 'Set module result should be preserved');
    assertEqual(arrayModule.run(), 'array', 'Array module result should be preserved');
    assertEqual(
        modules.refreshContainer(),
        'map-container',
        'Map subclass result should be preserved',
    );
    assertEqual(
        group.refreshContainer(),
        'set-container',
        'Set subclass result should be preserved',
    );
    assertEqual(
        list.refreshContainer(),
        'array-container',
        'Array subclass result should be preserved',
    );
    profiler.stopProfiling();

    const names = eventNames(messages);
    assert(names.includes('modules["dock"].run'), 'Map value path should include its key');
    assert(names.includes('group[0].run'), 'Set value path should include its index');
    assert(names.includes('list[0].run'), 'Array value path should include its index');
    assert(names.includes('modules.refreshContainer'), 'Map subclass method should be profiled');
    assert(names.includes('group.refreshContainer'), 'Set subclass method should be profiled');
    assert(names.includes('list.refreshContainer'), 'Array subclass method should be profiled');
    assert(
        names.every(name => !/\.(?:get|set|add|push|values|entries)$/.test(name)),
        'native collection methods must never produce events',
    );
}

function testExactDescriptorRestoration() {
    const ownOriginal = function ownOriginal() {
        return 'own';
    };
    const ownHolder = {};
    Object.defineProperty(ownHolder, 'run', {
        value: ownOriginal,
        writable: true,
        enumerable: false,
        configurable: false,
    });
    const ownDescriptor = Object.getOwnPropertyDescriptor(ownHolder, 'run');

    const inheritedOriginal = function inheritedOriginal() {
        return 'inherited';
    };
    const proto = {};
    Object.defineProperty(proto, 'walk', {
        value: inheritedOriginal,
        writable: false,
        enumerable: false,
        configurable: false,
    });
    const inheritedHolder = Object.create(proto);
    const replacement = function replacement() {
        return 'replacement';
    };
    const replacedOwnHolder = {
        change() {
            return 'old-own';
        },
    };
    const replacedInheritedHolder = Object.create(proto);

    const messages = [];
    const profiler = makeProfiler(
        {
            ownHolder,
            inheritedHolder,
            replacedOwnHolder,
            replacedInheritedHolder,
        },
        messages,
    );
    assert(profiler.startProfiling('test@example.com'), 'descriptor profiling should start');
    assert(ownHolder.run !== ownOriginal, 'own function should be wrapped');
    assert(HAS_OWN.call(inheritedHolder, 'walk'), 'inherited wrapper should be an own shadow');
    replacedOwnHolder.change = replacement;
    replacedInheritedHolder.walk = replacement;

    const wrappedOwnDescriptor = Object.getOwnPropertyDescriptor(ownHolder, 'run');
    assertEqual(
        wrappedOwnDescriptor.configurable,
        ownDescriptor.configurable,
        'own configurable flag must be preserved while patched',
    );
    assertEqual(
        wrappedOwnDescriptor.enumerable,
        ownDescriptor.enumerable,
        'own enumerable flag must be preserved while patched',
    );
    assertEqual(
        wrappedOwnDescriptor.writable,
        ownDescriptor.writable,
        'own writable flag must be preserved while patched',
    );

    profiler.stopProfiling();
    const restored = Object.getOwnPropertyDescriptor(ownHolder, 'run');
    assertEqual(restored.value, ownOriginal, 'own function value must be restored');
    assertEqual(restored.writable, ownDescriptor.writable, 'own writable flag must be restored');
    assertEqual(
        restored.enumerable,
        ownDescriptor.enumerable,
        'own enumerable flag must be restored',
    );
    assertEqual(
        restored.configurable,
        ownDescriptor.configurable,
        'own configurable flag must be restored',
    );
    assert(
        !HAS_OWN.call(inheritedHolder, 'walk'),
        'inherited wrapper shadow must be deleted on stop',
    );
    assertEqual(inheritedHolder.walk, inheritedOriginal, 'inherited method must resolve unchanged');
    assertEqual(
        replacedOwnHolder.change,
        replacement,
        'stop must preserve an own method replaced by the extension',
    );
    assertEqual(
        replacedInheritedHolder.walk,
        replacement,
        'stop must preserve an inherited wrapper replaced by the extension',
    );
    assert(
        HAS_OWN.call(replacedInheritedHolder, 'walk'),
        'replacement of an inherited wrapper must remain an own method',
    );
}

function testDistinctSymbolMapPaths() {
    class Module {
        run() {}
    }

    const first = new Module();
    const second = new Module();
    const modules = new Map([
        [Symbol('same'), first],
        [Symbol('same'), second],
    ]);
    const messages = [];
    const profiler = makeProfiler({ modules }, messages);
    assert(profiler.startProfiling('test@example.com'), 'symbol-keyed Map should start');
    first.run();
    second.run();
    profiler.stopProfiling();

    const names = eventNames(messages);
    assertEqual(new Set(names).size, 2, 'equal Symbol descriptions need distinct paths');
    assert(names.some(name => name.includes('Symbol("same")#0')), 'first Symbol needs index');
    assert(names.some(name => name.includes('Symbol("same")#1')), 'second Symbol needs index');
}

function testUnpatchableFunctionIsSkipped() {
    const locked = {};
    const original = function original() {
        return 7;
    };
    Object.defineProperty(locked, 'run', {
        value: original,
        writable: false,
        enumerable: false,
        configurable: false,
    });

    const messages = [];
    const profiler = makeProfiler({ locked }, messages);
    assert(profiler.startProfiling('test@example.com'), 'one locked method must not abort start');
    assertEqual(profiler.stats.patchedFunctions, 0, 'locked method must not be counted as patched');
    assertEqual(profiler.stats.skippedFunctions, 1, 'locked method must be counted as skipped');
    assertEqual(locked.run, original, 'locked method must remain untouched');
    profiler.stopProfiling();
}

function testLimitsAndStatsSnapshot() {
    class Module {
        first() {}
        second() {}
    }

    const collectionMessages = [];
    const collectionProfiler = makeProfiler(
        [new Module(), new Module(), new Module()],
        collectionMessages,
        { maxCollectionEntries: 2 },
    );
    assert(collectionProfiler.startProfiling('test@example.com'), 'limited collection should start');
    assertEqual(
        collectionProfiler.stats.patchedFunctions,
        4,
        'only two array entries should be traversed',
    );
    assert(collectionProfiler.stats.truncated, 'collection cap must set truncated');
    collectionProfiler.stopProfiling();

    const depthMessages = [];
    const depthProfiler = makeProfiler(
        { child: { nested: new Module() } },
        depthMessages,
        { maxDepth: 1 },
    );
    assert(depthProfiler.startProfiling('test@example.com'), 'depth-limited profiling should start');
    assertEqual(depthProfiler.stats.patchedFunctions, 0, 'object beyond depth cap must not be patched');
    assert(depthProfiler.stats.truncated, 'depth cap must set truncated');
    depthProfiler.stopProfiling();

    const objectMessages = [];
    const objectProfiler = makeProfiler(
        { first: {}, second: new Module() },
        objectMessages,
        { maxVisitedObjects: 2 },
    );
    assert(objectProfiler.startProfiling('test@example.com'), 'object-limited profiling should start');
    assertEqual(objectProfiler.stats.visitedObjects, 2, 'visited object cap must be exact');
    assert(objectProfiler.stats.truncated, 'object cap must set truncated');
    objectProfiler.stopProfiling();

    const functionMessages = [];
    const functionProfiler = makeProfiler(
        { module: new Module() },
        functionMessages,
        { maxPatchedFunctions: 1 },
    );
    assert(functionProfiler.startProfiling('test@example.com'), 'function-limited profiling should start');
    assertEqual(functionProfiler.stats.patchedFunctions, 1, 'function cap must be exact');
    assert(functionProfiler.stats.truncated, 'function cap must set truncated');
    const snapshot = functionProfiler.stats;
    snapshot.patchedFunctions = 999;
    assertEqual(functionProfiler.stats.patchedFunctions, 1, 'stats getter must return a copy');
    functionProfiler.stopProfiling();

    const propertyMessages = [];
    const wideObject = {
        first: {},
        second: {},
        lateMethod() {},
    };
    const propertyProfiler = makeProfiler(
        wideObject,
        propertyMessages,
        { maxPropertiesPerObject: 2 },
    );
    assert(propertyProfiler.startProfiling('test@example.com'), 'wide object should start');
    assertEqual(
        propertyProfiler.stats.patchedFunctions,
        0,
        'properties beyond the per-object cap must not be patched',
    );
    assert(propertyProfiler.stats.truncated, 'property cap must set truncated');
    propertyProfiler.stopProfiling();
}

function testBatchBoundAndStopFlush() {
    class Module {
        run() {
            return true;
        }
    }

    const module = new Module();
    const messages = [];
    const profiler = makeProfiler(
        { module },
        messages,
        { maxBatchEvents: 2 },
    );
    assert(profiler.startProfiling('test@example.com', 42), 'batch profiling should start');
    module.run();
    module.run();
    assertEqual(messages.length, 1, 'event-count cap should flush immediately');
    assertEqual(messages[0].type, 'profile_batch', 'profiler should emit profile_batch');
    assertEqual(messages[0].sessionId, 42, 'batch should carry the app session ID');
    assertEqual(messages[0].events.length, 2, 'first batch should contain two events');
    assert(
        messages[0].events.every(
            event => event.type === 'profile_event' && event.sessionId === 42,
        ),
        'batch members should retain the legacy event schema',
    );

    module.run();
    profiler.stopProfiling();
    assertEqual(messages.length, 2, 'explicit stop should flush the pending event');
    assertEqual(messages[1].events.length, 1, 'stop-flushed batch should contain pending event');
}

function testTimedBatchFlush() {
    class Module {
        run() {}
    }

    const module = new Module();
    const messages = [];
    const profiler = makeProfiler(
        { module },
        messages,
        { batchDelayMs: 10 },
    );
    assert(profiler.startProfiling('test@example.com'), 'timed batch profiling should start');
    module.run();

    const loop = GLib.MainLoop.new(null, false);
    GLib.timeout_add(GLib.PRIORITY_DEFAULT, 100, () => {
        loop.quit();
        return GLib.SOURCE_REMOVE;
    });
    loop.run();

    assertEqual(messages.length, 1, 'batch timer should flush a pending event');
    assertEqual(messages[0].events.length, 1, 'timed batch should contain the pending event');
    profiler.stopProfiling();
}

function testRestartDiscardsOldQueue() {
    class Module {
        run() {}
    }

    const module = new Module();
    const messages = [];
    const profiler = makeProfiler({ module }, messages);
    assert(profiler.startProfiling('test@example.com', 1), 'first session should start');
    module.run();
    const staleBoundWrapper = module.run.bind(module);
    assertEqual(messages.length, 0, 'first event should still be queued');

    assert(profiler.startProfiling('test@example.com', 2), 'restart session should start');
    assertEqual(messages.length, 0, 'restart must discard, not flush, the old queue');
    staleBoundWrapper();
    module.run();
    profiler.stopProfiling();
    assertEqual(messages.length, 1, 'only the new session should be flushed');
    assertEqual(messages[0].sessionId, 2, 'new batch should identify the fresh session');
    assertEqual(
        messages[0].events.length,
        1,
        'old queued and externally bound wrapper events must not contaminate restart',
    );
}

const TESTS = [
    testCollectionTraversal,
    testExactDescriptorRestoration,
    testDistinctSymbolMapPaths,
    testUnpatchableFunctionIsSkipped,
    testLimitsAndStatsSnapshot,
    testBatchBoundAndStopFlush,
    testTimedBatchFlush,
    testRestartDiscardsOldQueue,
];

let failures = 0;
for (const test of TESTS) {
    try {
        test();
        print(`ok - ${test.name}`);
    } catch (e) {
        failures++;
        printerr(`not ok - ${test.name}: ${e.stack ?? e}`);
    }
}

if (failures > 0) {
    throw new Error(`${failures} profiler bridge test(s) failed`);
}

print('profiler bridge tests OK');
