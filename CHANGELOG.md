## [1.4.0] - 2026-08-23

### Added

- **GNOME 51 support** — the bridge now declares shell-version 51, so GNOME 51 loads it instead of refusing it as out of date.
- **Measured instrumentation overhead** — every recording starts by timing the profiler's own wrapper against an empty function; rows whose average per-call time falls at or below that cost, or below the microsecond clock floor, are dimmed with an explanation on hover, and the measured figure is shown with the scan details. Dimmed rows keep their call counts, which remain exact.

### Fixed

- Profiler discovery now traverses Arrays, `Map` values, and `Set` values, so extensions that keep their modules in a collection — `Map<string, Module>` and similar — are instrumented instead of skipped (#23).
- Profiler discovery walks the object graph breadth-first, so a first sibling holding bulk cached data no longer consumes the whole visit budget and starves the objects carrying the extension's actual behaviour.
- Reaching the visited-objects limit now only marks the scan as truncated instead of stopping discovery outright; the hard stop is reserved for the instrumented-function limit.
- The bridge's `bundle-hash` now covers `metadata.json`, so a change to `shell-version`, `uuid`, or `settings-schema` triggers the reinstall prompt instead of leaving an outdated copy on disk.

## [1.3.0] - 2026-07-19

### Added

- **Install extensions from extensions.gnome.org (EGO)** — the install dialog now has a GitHub tab and an extensions.gnome.org tab; search as you type or paste an extension URL/UUID, with shell-version compatibility checks and update detection alongside the existing GitHub path.
- **Customizable keyboard shortcuts** — global (Super+F5 toggle, Super+Shift+F5 restart), window, and tab-scoped shortcuts, editable from a new shortcuts dialog with click-to-rebind, per-row/all reset, and in-scope conflict detection; overrides persist across restarts.

### Fixed

- Restarting profiling while a session was running no longer hides the Recording pill and its live timer/event counter.
- "Check for Updates" no longer auto-installs a new version — it only reveals the Update action; the download happens on demand when you click it.

## [1.2.0] - 2026-07-09

### Added

- **Export profiles to speedscope and Chrome trace-event formats** — the profiler Save button is now a split button: primary click keeps the JSON save (Ctrl+S unchanged), the dropdown adds "Export for speedscope…" and "Export for Firefox Profiler / Perfetto…".

### Fixed

- Profiler now recursively patches nested target properties instead of only one level deep, so calls on deeper holders are captured during profiling instead of running invisibly.
- Total wall time stat card now sums self time instead of total time per function, so nested calls are no longer double-counted.

## [1.1.0] - 2026-06-11

### Added

- **Install extensions from GitHub** — install a GNOME Shell extension directly from a GitHub repository URL, including repos where the extension lives in a subdirectory, with per-stage progress during install and update.
- **Check for Updates** — on-demand update check per extension with automatic reinstall; the update indicator is now folded into the status dot.
- **Uninstall** action available for all user-installed extensions.
- Installed commit row links straight to the commit on GitHub.
- **Log Viewer** — structured capture controls (defaulting to gnome-shell), log attribution by `GLIB_DOMAIN`, a tag-chip filter bar replacing the old Selected filter, and help tooltips on the filters.

### Changed

- **Wayland only** — X11 support dropped to shrink the Flatpak permission surface.
- Shell restart after bridge install/remove now goes through D-Bus instead of a subprocess script.
- Profiler stat cards reworked with a demand-weighted layout.
- Extension provenance is now tracked in a dedicated registry instead of `metadata.json`.
- Bridge logging unified through `GLib.log_structured`; noisy lifecycle logs removed (errors and warnings only).

### Fixed

- GitHub installs now reconcile the registry against what is on disk rather than the live D-Bus list.
- GSettings schemas are compiled after extracting a GitHub-installed extension.
- Log Viewer: tag bar no longer overflows the window, re-fits chips when counts grow, captures gnome-shell by executable path, and fixes the tag popover background in dark mode.
- Details panel: "Homepage" no longer wraps character-by-character.

## [1.0.0] - 2026-05-21

First public release.

### Features

- **Extension Manager** — list installed GNOME Shell extensions, enable/disable
  with state badges (enabled, disabled, error, out-of-date), open the source
  folder in your editor.
- **Log Viewer** — live `journalctl` stream filtered by extension UUID and log
  level with full-text search.
- **Profiler** — live function timing via runtime monkey-patching, visualized
  in three interchangeable modes:
  - **Flamegraph** — nested call stack with per-function highlighting.
  - **Swimlane** — chronological per-function lanes.
  - **Histogram** — duration distribution across calls.
  Includes a per-function call table and the ability to save/load profile
  sessions as JSON (Ctrl+S).
- **Inspector** — read-only live view into a running extension's `stateObj`,
  browse properties and methods of the JS object.
- **Bridge Extension** — `gse-profiler-bridge@todevelopers` is auto-installed
  into `~/.local/share/gnome-shell/extensions/` on first launch. Communicates
  with the app over a Unix socket using newline-delimited JSON. Compatible
  with GNOME Shell 46–50.

### Distribution

- **Flatpak** bundle attached to each GitHub release (built against the GNOME
  50 runtime).
- **Source install** via `setup-and-run.sh` for any GNOME 46+ system.
- Companion `uninstall.sh` removes the app, desktop entry, icon, and bridge
  extension cleanly.

### Platform support

- GNOME Shell **46, 47, 48, 49, 50** (X11 and Wayland sessions).
- Python **3.11+**, GTK **4**, libadwaita **1**.
