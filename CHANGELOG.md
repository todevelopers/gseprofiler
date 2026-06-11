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
