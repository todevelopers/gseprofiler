# PLAN.md — gse-profiler Implementation Plan

Each phase ends in a working, testable state. **Phases 0–5 are V1 scope** — the app is feature-complete; remaining work is polish and release preparation.
Phases 6–11 go beyond V1 with constructive additions.

---

## Phase 0: Project Setup & CI ✅

**Goal:** Skeleton that launches, CI green from day one.

### App skeleton

- [x] Directory structure per README
- [x] `app/main.py` — `Adw.Application` + `Adw.ApplicationWindow` with `Adw.OverlaySplitView`
- [x] Sidebar navigation (`Adw.OverlaySplitView` + `GtkListBox` with `navigation-sidebar` class)
- [x] Placeholder views for each section (`Adw.StatusPage` per view)
- [x] `bridge-extension/metadata.json` — Bridge extension scaffold
- [x] `app/core/` stubs — `DBusClient`, `SocketServer`, `GitManager`, `JournalReader`
- [x] `api/devtools-api.js` — `DevToolsClient` skeleton with JSDoc
- [x] `scripts/restart-shell.sh` — X11/Wayland aware shell restart
- [x] `scripts/setup-and-run.sh` — Fedora quick-start (no sudo, no prompts)

### GitHub Actions

- [x] **`ci.yml`** — ruff · mypy · pytest · eslint on every push and PR to `main`
- [x] **`release.yml`** — tarball + changelog on `v*` tag push
- [x] **`bridge-test.yml`** — ESLint on changes to `bridge-extension/` or `api/`

---

## Phase 1: Extension Manager ✅

**Goal:** Users can see all installed extensions and toggle them.

- [x] `app/core/dbus_client.py`
  - Async `Gio.DBusProxy` wrapper for `org.gnome.Shell.Extensions`
  - Methods: `list_extensions()`, `enable_extension(uuid)`, `disable_extension(uuid)`
  - Properties per extension: name, UUID, version, state, path, error
- [x] Extension list UI
  - `AdwPreferencesGroup` with `AdwActionRow` per extension, or `GtkListView`
  - State badge: enabled (green) / disabled (grey) / error (red)
  - Toggle switch → D-Bus call
- [x] "Open folder" action — `Gio.AppInfo.launch_default_for_uri("file://...")`
- [x] Refresh button + auto-refresh on D-Bus property change signal

### Bridge extension bootstrap

- [x] On app launch: check whether `gse-profiler-bridge@todevelopers` is installed
  - If not installed → copy `bridge-extension/` to `~/.local/share/gnome-shell/extensions/gse-profiler-bridge@todevelopers/`
  - After copy → run `scripts/restart-shell.sh` in subprocess (prompts user on Wayland: logout required)
- [x] After install (or if already installed but disabled) → call `enable_extension(BRIDGE_UUID)` via D-Bus
- [x] "Install / Reinstall bridge" action in app menu (manual trigger)

### Connection status indicators

- [x] App header bar chip: **Connected** (green) / **Disconnected** (grey) — reflects live Unix socket state
- [x] GNOME panel icon in Bridge extension: `Gio.ThemedIcon` shown when extension is running, hidden on `disable()` (no menu in V1)

---

## Phase 2: Bridge extension + Unix Socket Transport ✅

**Goal:** App and bridge can exchange messages reliably.

### Bridge extension

- [x] `extension.js` — `enable()` / `disable()` lifecycle
- [x] `socket_client.js` — connect to `$XDG_RUNTIME_DIR/gse-profiler.sock`, reconnect loop
- [x] Handshake message `{ type: "hello", version: "1", uuid: BRIDGE_UUID }`
- [x] GNOME panel indicator (simple icon from `Gio.ThemedIcon`, no menu in V1)

### App side

- [x] `app/core/socket_server.py` — async Unix socket server, `Gio.SocketService`
- [x] Message router — dispatch incoming JSON messages to the right subsystem
- [x] Auto-install logic:
  - Copy `bridge-extension/` to install path
  - Run `scripts/restart-shell.sh` in a subprocess
- [x] Connection status indicator in app header bar (connected / disconnected chip)
- [x] "Install / Reinstall bridge" action in app menu
- [x] Reinstall prompts shell restart

---

## Phase 3: Log Viewer ✅

**Goal:** Live, filterable log stream from journalctl.

- [x] `app/core/journal_reader.py`
  - Spawn `journalctl --follow -o json` subprocess
  - Parse JSON lines: `SYSLOG_IDENTIFIER`, `MESSAGE`, `PRIORITY`, `__REALTIME_TIMESTAMP`
  - Emit GObject signal per parsed entry
- [x] Log Viewer UI
  - `GtkTextView` with monospace font
  - Auto-scroll to bottom (toggle button to lock/unlock)
- [x] Filter bar
  - UUID dropdown (populated from Extension Manager)
  - Log level filter (DEBUG / INFO / WARNING / ERROR / CRITICAL)
  - Full-text search with match highlighting
- [x] Toolbar actions: copy selected lines, export visible log to `.txt` file, clear

---

## Phase 4: Profiler V1 ✅

**Goal:** Live function timing for a selected extension with flame graph visualization.

### Bridge side

- [x] `bridge-extension/profiler.js`
  - `startProfiling(uuid)` — monkey-patch all functions on extension's exported object
  - Record: function name, call depth, start timestamp (µs), end timestamp
  - Emit events via socket: `{ type: "profile_event", extensionUuid, function, start, end, depth }`
  - `stopProfiling()` — restore original functions

### App side

- [x] Profiler UI
  - Start / stop profiling controls (select target extension from dropdown)
  - Call table (`GtkColumnView`): function name, call count, total ms, avg ms, max ms — sortable
  - Flame graph (`Gtk.DrawingArea` + Cairo): horizontal axis = time, vertical axis = call depth
    - Each bar labeled with function name (clipped to fit)
    - Zoom (mouse wheel), pan (click-drag), hover tooltip
    - Click to filter call table to selected function
    - Hide-idle toggle with log-scale gap compression
- [x] Save profile to JSON file (`Gio.File`)
- [x] Load profile from JSON file + same visualization
- [x] Clear / reset profiling data

---

## Phase 5: Inspector ✅

**Goal:** Live access to extension `stateObj` properties and methods.

### Bridge side

- [x] `bridge-extension/inspector.js`
  - `inspect(uuid, path)` — resolve `stateObj` down the path one level at a time
  - Enumerate own properties + prototype chain (1 level)
  - Serialize: `{ name, type, value }` — handle functions, circular refs, symbols
  - Respond with `{ type: "inspect_result", extensionUuid, path, properties: [...] }`

### App side

- [x] Inspector UI
  - `GtkColumnView`: property name | type | value (read-only in V1)
  - Inline expand chevron for object/array values (depth 0)
  - Drill-in chevron + monospace breadcrumb for nested navigation
  - Refresh button
  - Copy selected row (name + type + value) to clipboard
  - Type pills color-coded by JS type (string / number / boolean / object / array / null / error)

> **Descoped from V1:** inline property editing was prototyped but cut because
> the bridge would have needed full path-aware writes plus `Gio.Settings`
> support to be useful. See Phase 11 for the full plan.

---

## Pre-release

**Goal:** Polish, packaging, and distribution prep before tagging `v1.0.0`.

### Polish & UX

- [x] Visual review pass — spacing, colours, icon consistency across all views
- [x] Keyboard shortcuts (`Gtk.ShortcutController`): `Ctrl+R` refresh, `Ctrl+F` search, `Ctrl+S` save. Note: Partially, full shortcuts will be added post v1
- [x] Onboarding flow for first launch (bridge not installed → step-by-step dialog)
- [x] Error states and empty states reviewed in every view

### GitHub repository prep

- [x] README with screenshots, feature list, and quick-start instructions
- [x] AppStream metadata (`data/io.github.todevelopers.GseProfiler.metainfo.xml`)
- [x] `.desktop` entry (`data/io.github.todevelopers.GseProfiler.desktop`)
- [x] Icon set: SVG master (`app/data/icons/hicolor/scalable/apps/io.github.todevelopers.GseProfiler.svg`)
- [x] `CHANGELOG.md` for v1.0.0

## Packaging & distribution

- [x] Flatpak manifest (`build-aux/io.github.todevelopers.GseProfiler.yml`)
  
  * Runtime: `org.gnome.Platform//48` + `org.gnome.Sdk//48` (GTK4, libadwaita, PyGObject included — no SDK extensions needed)
  * Permissions:
    * `--talk-name=org.gnome.Shell.Extensions` — D-Bus access for extension manager
    * `--talk-name=org.freedesktop.Flatpak` — required for `flatpak-spawn --host` (journalctl)
    * `--filesystem=~/.local/share/gnome-shell/extensions:create` — bridge extension install
  * No `git` module — clone feature is excluded from V1 Flatpak build
  * `journal_reader.py` detects Flatpak environment (`/.flatpak-info`) and prefixes journalctl with `["flatpak-spawn", "--host"]` ✓ (already implemented)

- [x] Helper files (required for valid Flatpak):
  
  * `data/io.github.todevelopers.GseProfiler.metainfo.xml`
  * `data/io.github.todevelopers.GseProfiler.desktop`
  * `app/data/icons/hicolor/scalable/apps/io.github.todevelopers.GseProfiler.svg`

- [x] `release.yml` extended: on `git tag v*`, build Flatpak bundle via `flatpak-builder` and attach `gse-profiler-{version}-x86_64.flatpak` to GitHub Release

---

## 🚀 V1 Release ✅

> **Released:** v1.0.0 (2026-05-21) · v1.0.1 (2026-05-24, Flathub publish fix — replaced `flatpak-spawn journalctl` with `systemd.journal.Reader`).

---

## Phase 6: Install from GitHub ✅

**Goal:** Install GNOME Shell extensions directly from a GitHub URL.

> **Implementation note:** Diverged from original spec — uses tarball download (`Soup.Session`) instead of `git clone`, so no `git` dependency is required. Provenance is tracked in a local registry (`source_registry.py`) rather than in `metadata.json`.

- [x] `app/core/github_installer.py` — download default-branch tarball, validate `metadata.json`, extract, compile GSettings schemas, record source commit SHA
- [x] `app/core/github_source.py` + `app/core/source_registry.py` — per-extension provenance (repo URL, installed SHA, subdirectory)
- [x] Install dialog (`app/ui/github_install_dialog.py`) — GitHub URL input, per-stage progress display, show extension name + UUID after success, prompt logout
- [x] Subdirectory URL support — install from `github.com/user/repo/tree/branch/subdir`
- [x] "Check for Updates" action in `app/ui/details_view.py` — compares installed SHA with latest default-branch SHA, auto-reinstalls if newer
- [x] "Uninstall" action for all user extensions in `details_view.py`
- [x] Update indicator folded into status dot in `app/ui/extension_list.py`
- [x] Error handling: invalid URL, network failure, UUID conflict, missing `metadata.json`

---

## Phase 7: Memory Profiling (V2)

**Goal:** Heap snapshots and allocation tracking.

- [ ] Bridge: expose SpiderMonkey heap stats via GJS `System.gc()` + memory counters
- [ ] Memory timeline chart — heap size over time (`Gtk.DrawingArea` + Cairo)
- [ ] Object count table by constructor name
- [ ] Snapshot diff: compare two snapshots, highlight growth
- [ ] Leak candidates: objects that grew monotonically between snapshots

---

## Phase 8: Extension Health & Linting (V2+)

**Goal:** Automated quality checks surfaced in the UI.

- [ ] ESLint integration — run ESLint on extension source directory, show inline errors
  - Use `eslint --format=json`, parse output, display in a `GtkListBox`
- [ ] `metadata.json` validator
  - Required fields: `uuid`, `name`, `description`, `shell-version`
  - Warn on missing `url`, invalid `shell-version` range
- [ ] Shell error scanner — parse journal for uncaught JS exceptions tagged to the extension
- [ ] Performance regression detection — compare saved profiles, flag functions that regressed > 20%
- [ ] Extension health summary card in Extension Manager (green / yellow / red)

---

## Phase 9: Settings & Preferences (V2+)

- [ ] `AdwPreferencesWindow`
  - Theme: follow system / force dark / force light
  - Log viewer: max lines buffer, font size
  - Socket path override (advanced)
  - Auto-connect bridge on launch
- [ ] Session persistence via GSettings — remember last selected extension, filter state
- [ ] i18n scaffold (gettext / `_()`) — English only initially, structure ready for translators

---

## Phase 11: Inspector V2 — Writable Properties (V2+)

**Goal:** Bring back inline editing of extension state in a way that actually
works across the whole tree, not just the root level.

### Why this is its own phase

The V1 prototype only wrote to `stateObj[name]`, ignoring the active drill path,
and could never edit GSettings-backed values (which is where most extensions
keep their configurable state). Doing it properly means cooperating with both
nested object paths and `Gio.Settings`, so it belongs in V2.

### Bridge side

- [ ] `setProperty(uuid, path, name, value)` — walk `stateObj` down `path`, then assign to `target[name]`. Honour both data descriptors with `writable: true` and accessor descriptors with a setter.

- [ ] Detect `Gio.Settings` instances during serialization; expose their keys as writable children with their declared schema type (`b`, `i`, `d`, `s`, enums).

- [ ] Re-introduce a `writable` flag in `inspect_result` for each property — only `true` when the property is actually assignable on the current `holder` (own data prop, accessor with setter, or known GSettings key).

- [ ] Validate `set_property` values against the property's reported type before assigning; reject with a typed error instead of throwing.

### App side

- [ ] Render a "writable" affordance on rows that can be edited (e.g. an edit pencil icon that appears on hover, mirroring the drill-in chevron).

- [ ] Adwaita `AlertDialog` for edit, with a control matched to the type:
  
  - String → `Gtk.Entry`
  - Number → `Gtk.SpinButton` with min/max from GSettings schema where known
  - Boolean → `Gtk.Switch`
  - Enum (GSettings choice key) → `Gtk.DropDown` populated with allowed values

- [ ] Send `set_property` with the current navigation `path` and the row `name`.

- [ ] On `set_property_result.ok` → re-issue `inspect` at the current path and flash the affected row briefly to confirm the write.

- [ ] On `set_property_result.error` → `Adw.Toast` with the bridge's error message.

- [ ] Drop stale `set_property_result`s where `extensionUuid` / `path` no longer match the active navigation (same pattern as stale `inspect_result`s).

### Protocol additions

```
→ { type: "set_property", uuid, path, name, value }
← { type: "set_property_result", extensionUuid, path, name, ok, error? }
```

`inspect_result.properties[*].writable` returns as a boolean — absent or `false`
means read-only for V1 clients.

---

## Phase 12: Startup Profiling — disabled → enabled (V2+)

**Goal:** Capture function calls during an extension's `enable()` startup, not just during steady-state runtime.

### Why it's non-trivial

Disabled extensions have no `stateObj` — it's created inside GNOME Shell's internal
`_callExtensionEnable()` immediately before `enable()` is called. There is no public
signal between "stateObj assigned" and "enable() invoked", so monkey-patching must
happen via one of two strategies:

**Variant A — post-enable patching (simpler, ~80 lines)**
Bridge connects to `extensionManager`'s `extension-state-changed` signal, patches
`stateObj` the moment state transitions to ENABLED. Misses `enable()` itself but
catches every callback, timer, and D-Bus reply fired after enable completes —
sufficient for most startup bottleneck analysis.

**Variant B — full enable() capture (~130 lines, riskier)**
Bridge monkey-patches `extensionManager.enableExtension()` (public) or the private
`_callExtensionEnable` async method to intercept before `enable()` is called.
Catches `enable()` itself, but depends on GNOME Shell internals and may need
adjustment across major GNOME versions.

### Planned scope (Variant A)

**Bridge side**

- [ ] New message handler `enable_and_profile { uuid }` in `extension.js`

- [ ] `Profiler.armForEnable(uuid)` — connects to `extensionManager`'s `extension-state-changed`, patches `stateObj` on first ENABLED transition, then disconnects the signal handler

- [ ] Teardown: if enable fails or takes > 10 s, disarm and emit `profiling_error`

**App side**

- [ ] "Profile startup" button on the "Extension Disabled" status page in `profiler_view.py`
- [ ] On click: send `enable_and_profile`, then call `dbus_client.enable_extension(uuid)`
- [ ] Handle `profiling_started { ok: false }` — show toast "Extension failed to enable"
- [ ] Edge cases: bridge disconnects during enable, user clicks twice, extension stays disabled

### Protocol additions

```
→ { type: "enable_and_profile", uuid }
← { type: "profiling_started",  uuid, ok }   (reuses existing message)
← { type: "profiling_error",    uuid, reason } (new)
```

---

## Phase 13: Global Keyboard Shortcuts (V2+)

**Goal:** Start/stop profiling via a keyboard shortcut even when the app has lost focus — e.g. while clicking around in the extension under test.

### Why it works here but not in GTK4 alone

A standard GTK4 app cannot capture keys while it has no focus — and on Wayland `XGrabKey` (the old X11 hack) is simply unavailable. The bridge, however, runs inside `gnome-shell` and can call `global.display.add_keybinding()`, which is a first-class Mutter API that works on both X11 and Wayland. The shortcut press fires in the shell process, the bridge sends a `toggle_profiling` message over the existing Unix socket, and the app reacts exactly as if the Start/Stop button was clicked.

**Implemented (v1 of Phase 13):** shortcuts are always active — the
rebind-via-preferences work is deferred to a later pass (see Settings
integration below). A companion `restart-profiling` shortcut and a full set of
tab-scoped in-app shortcuts plus a help dialog were added on top of the
original toggle-only design.

### Bridge side

- [x] Add a GSettings schema to the bridge extension (`schemas/org.gnome.shell.extensions.gse-profiler-bridge.gschema.xml`)
  
  - Key: `toggle-profiling` — type `as`, default `['<Super>F5']`
  - Key: `restart-profiling` — type `as`, default `['<Super><Shift>F5']`
  - Compiled with `glib-compile-schemas` on bridge install (`BridgeManager._compile_schemas`)

- [x] In `extension.js` `enable()`: register keybindings via `Main.wm.addKeybinding()` (registration wrapped in try/catch so an uncompiled schema disables the shortcuts without breaking the bridge)

- [x] In `extension.js` `disable()`: remove keybindings via `Main.wm.removeKeybinding()`

- [x] Guard: `SocketClient.send()` already no-ops when the socket is disconnected

### App side

- [x] Handle `toggle_profiling` / `restart_profiling` messages in `profiler_view.py` — same logic as clicking Start/Stop
- [x] Show `Adw.Toast` (via `MainWindow`'s `Adw.ToastOverlay`) to confirm the action; the Profiler tab is also brought forward

### In-app tab-scoped shortcuts (added beyond original scope)

- [x] Per-tab `Gtk.ShortcutController` (MANAGED scope) — only live while that tab is selected, since `Gtk.Stack` unmaps hidden pages
- [x] `Ctrl+1…4` tab switch, `Ctrl+?`/`F1` help, `Ctrl+Q` quit (window-level)
- [x] `Ctrl+R` primary action (Profiler/Logs start-stop, Inspector refresh), `Ctrl+F` search, `Ctrl+S` save/export, `Ctrl+O` load, `Ctrl+L` clear
- [x] Keyboard-shortcuts help dialog (`app/ui/shortcuts_dialog.py`), reachable from the menu

### Settings integration (ties into Phase 9) — deferred

- [ ] Expose the `toggle-profiling` / `restart-profiling` GSettings keys in `AdwPreferencesWindow` (Phase 9) so users can rebind them without editing schema files
- [ ] Sync a displayed shortcut badge next to the Start/Stop button with the current GSettings value

### Protocol additions

```
→ (none — bridge-initiated)
← { type: "toggle_profiling" }    (bridge → app, existing transport)
← { type: "restart_profiling" }   (bridge → app, existing transport)
```

### Notes

- Defaults `<Super>F5` / `<Super><Shift>F5` avoid conflicts with common app shortcuts (and with the bare `F9` sidebar toggle); document them in the README
- On Wayland this requires `gnome-shell` 45+ (Mutter API stable); the bridge already targets shell 46+
- User must restart the bridge (or log out/in) after installing the schema for the global shortcuts to register

---

## Phase 14: Bridge Hardening & Event Batching (V2 prerequisite)

**Goal:** Fix latent bridge bugs and make the profiling pipeline robust enough for the
higher event volume of Phase 7. Should land **before** memory profiling — it builds on
the same socket pipeline.

### Latent bugs (from 2026-06 code review)

- [ ] `profiler.js` — `_dbg()` calls `bridgeLog`, which is not imported (only `bridgeLogError` / `bridgeLogWarning` are); a `ReferenceError` waits for the first `DEBUG = true` session
- [ ] `Profiler.stopProfiling()` — when the original function came from the prototype, restore with `delete holder[name]` instead of assigning an own property; the patch currently leaves an own-property shadow on the instance after restore
- [ ] `inspector.js` — getters are invoked eagerly during serialization; GObject getters can have side effects, so opening the Inspector can mutate the inspected extension. Report getters lazily (`type: "getter"`) and evaluate only on an explicit "invoke getter" action

### Event batching

- [ ] Bridge: buffer profile events and flush as `{ type: "profile_batch", events: [...] }` every ~50 ms or after N events — today every wrapped call does its own socket write from inside `gnome-shell`, so profiling a hot path floods the shell's main loop and skews the measurement
- [ ] App: handle `profile_batch` in `profiler_view.py` (keep accepting single `profile_event` for backward compatibility)

### Async function profiling

- [ ] Wrapper: detect a `Promise` return value and tag the event `async: true` — today the event closes in `finally` when the synchronous part returns, so async methods report setup cost, not end-to-end latency
- [ ] Track settle time: attach `.finally()` to the returned promise and record an `asyncEnd` timestamp (extended event schema or a follow-up event)
- [ ] UI: distinguish async events visually (badge in tooltip / hatched bar)
- [x] README: documented that only the synchronous portion is measured

### Protocol versioning

- [ ] App validates `hello.version` on handshake; on mismatch the connection chip shows **Bridge outdated** instead of Connected (covers the "user skipped the logout, old bridge still running" case the install-time bundle-hash check cannot catch)

---

## Phase 15: Refactoring Pass

**Goal:** Pay down structural debt before phases 7 / 11 / 12 grow the codebase further.

- [ ] Split `app/ui/log_viewer.py` (~1450 lines) into a package `app/ui/log_viewer/`, mirroring the `app/ui/profiler/` split — natural seams: capture panel, tag bar + chip fitting, list-view factories, main view
- [ ] Unify settings persistence — `_settings_path` / `_load_settings` / `_save_settings` are duplicated in `profiler_view.py` and `log_viewer.py` with diverging merge semantics → single `app/core/settings.py` (becomes the seam for the Phase 9 GSettings backend)
- [ ] Proper Python packaging — add a `[project]` table + `console_scripts` entry point to `pyproject.toml`, drop the `sys.path.insert` hack in `main.py` (also simplifies the Flatpak manifest and the Phase 10 RPM spec)
- [ ] `socket_server.py` — extract `_reset_connection()`; the connection-teardown block is copy-pasted 3× (error path, EOF path, `stop()`)
- [ ] Typed message router — replace per-view `message-received` filtering with `router.on("profile_event", cb)`-style registration before phases 7 / 11 / 12 add more message types
- [ ] Delete `tests/test_placeholder.py` (Phase 0 leftover)

---

## Phase 16: Profile Export & Analysis

**Goal:** Get more answers out of the event data the profiler already records.

- [x] Export to **speedscope** JSON (and/or Firefox Profiler format) — just an alternate serializer over existing events; users get a powerful external viewer for free. Cheapest high-value item in the backlog
- [ ] Aggregated flame graph view — merged stacks answering *"where does time go overall"*, complementing the existing time-axis views
- [ ] Percentile columns (p95 / p99) in the call table — raw events are already retained
- [ ] Shell crash detection during profiling — distinguish a clean disconnect from "shell died mid-recording"; offer to save the collected data and re-arm automatically after reconnect

---

## Phase 17: Install from extensions.gnome.org ✅

**Goal:** Support the place where GNOME extensions actually live, not just GitHub.

- [x] Download via the EGO REST API (zip), reuse the provenance registry from the GitHub installer
- [x] Unified "Add extension" flow — one dialog accepting a GitHub URL or an EGO URL / search query

---

## Backlog (unscheduled)

- Bridge D-Bus control interface + socket fd-passing — keep the Unix socket as the
  data plane (D-Bus is wrong for the high-frequency `profile_event` stream: every
  message is double-copied through `dbus-broker`, and the sender is `gnome-shell`
  itself, so flooding the bus would skew the very measurements we take), but let the
  bridge export a minimal D-Bus interface on the session bus for the control plane.
  The app then gets: presence detection via `Gio.bus_watch_name()` (replaces the 3 s
  reconnect polling loop in `socket_client.js`), and socket fd handover via a single
  D-Bus call with fd-passing — which removes the socket path from the protocol
  entirely, so the Flatpak `--filesystem=xdg-run/gse-profiler:create` permission and
  the `/run/user/<uid>/…` path workaround in `socket_server.py` collapse into one
  `--talk-name=`. Same control-plane/data-plane split GNOME itself uses
  (`org.gnome.Sysprof3`, Mutter screencast → PipeWire fd). Pairs well with the
  Phase 14 event batching, which helps the socket and any future transport alike.
- Periodic GitHub update re-check — today the check runs once at startup
  (`main.py`, `_on_ready_for_update_check`); a `GLib.timeout_add_seconds` re-check every
  few hours is a trivial add
- Log ↔ profiler time correlation — both views share a time axis; clicking a profile
  event could jump to the journal lines from the same instant. No other GNOME tool has
  this

---

## Deferred — opt-in Developer API

> **Status: deferred indefinitely.** The core tooling covers the developer use-case well enough for V1 and V2. Revisit only if there is concrete demand.

**Original goal:** Extension developers integrate `DevToolsClient` for custom profiling marks, counters, and property watches.

- `api/devtools-api.js` — `DevToolsClient` with `connect`, `mark`, `measure`, `counter`, `watch`
- Bridge routes `devtools_*` message types to profiler subsystem
- App displays API-originated events in a distinct colour
- All methods: silent no-op when not connected

---

## Recommended Ordering (2026-06 review)

1. **Phase 14** — bridge bug fixes + event batching; prerequisite for Phase 7, which
   pushes much more data through the same socket
2. **Phase 15** — log_viewer split + settings unification, while the surface is still small
3. **Phase 16** — speedscope export first (cheapest, highest leverage), then the rest
4. **Phases 9 and 12** from the existing plan
5. **Phase 7** (memory profiling) only after Phase 14 has landed

---

## Milestone Summary

| Phase | Milestone            | Scope                                                      | Status                 |
| ----- | -------------------- | ---------------------------------------------------------- | ---------------------- |
| 0     | Skeleton + CI        | Project setup                                              | ✅ done                 |
| 1     | Extension Manager    | List, enable/disable                                       | ✅ done                 |
| 2     | Bridge + Socket      | App ↔ Shell IPC                                            | ✅ done                 |
| 3     | Log Viewer           | Live filtered logs                                         | ✅ done                 |
| 4     | Profiler V1          | Timing table + flame graph                                 | ✅ done                 |
| 5     | Inspector            | stateObj live view (R/O)                                   | ✅ done                 |
| —     | Pre-release          | Polish, GitHub, Flatpak                                    | ✅ done                 |
| —     | **V1 Release**       | **v1.0.0 + v1.0.1**                                        | ✅ done                 |
| 6     | GitHub install       | Install extensions from GitHub                             | ✅ done                 |
| 7     | Memory profiling     | Heap analysis (V2)                                         | planned                |
| 8     | Health checks        | Linting + validation (V2+)                                 | planned                |
| 9     | Settings             | Preferences window (V2+)                                   | planned                |
| 10    | Extended packaging   | RPM + Flathub full (V2+)                                   | planned                |
| 11    | Inspector writable   | Full property editing (V2+)                                | planned                |
| 12    | Startup profiling    | Profile enable() ramp-up (V2+)                             | planned                |
| 13    | Global shortcuts     | Toggle/restart profiling via keybinding + in-app shortcuts | done (rebind deferred) |
| 14    | Bridge hardening     | Bug fixes, batching, async profiling                       | planned                |
| 15    | Refactoring pass     | log_viewer split, settings, packaging                      | planned                |
| 16    | Export & analysis    | speedscope, merged flame graph                             | planned                |
| 17    | EGO install          | Install from extensions.gnome.org                          | ✅ done                 |
| —     | opt-in Developer API | Extension author integration                               | deferred ∞             |
