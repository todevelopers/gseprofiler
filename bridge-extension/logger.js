'use strict';

import GLib from 'gi://GLib';

// All bridge logs go through GLib.log_structured with this domain, so the app
// attributes them to the bridge via GLIB_DOMAIN — no textual "[name]" prefix
// needed. This doubles as the reference logging pattern for extension authors.
const LOG_DOMAIN = 'gse-profiler-bridge';

/** @param {string} message */
export function bridgeLog(message) {
    GLib.log_structured(LOG_DOMAIN, GLib.LogLevelFlags.LEVEL_MESSAGE, {
        MESSAGE: String(message),
    });
}

/** @param {string} message */
export function bridgeLogWarning(message) {
    GLib.log_structured(LOG_DOMAIN, GLib.LogLevelFlags.LEVEL_WARNING, {
        MESSAGE: String(message),
    });
}

/**
 * Structured equivalent of logError(): keeps the message and the error's
 * stack/text, attributed to the bridge domain at critical (non-fatal) level.
 * @param {unknown} error
 * @param {string} message
 */
export function bridgeLogError(error, message) {
    const detail = (error && error.message) ? error.message : String(error);
    const stack = (error && error.stack) ? `\n${error.stack}` : '';
    GLib.log_structured(LOG_DOMAIN, GLib.LogLevelFlags.LEVEL_CRITICAL, {
        MESSAGE: `${message}: ${detail}${stack}`,
    });
}
