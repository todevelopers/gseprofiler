## [1.0.1] - 2026-05-24

Flathub publish release.

### Fixed
- Replace `flatpak-spawn --host journalctl` with `systemd.journal.Reader` for direct journal access — removes the `finish-args-flatpak-spawn-access` Flathub blocker
- Fix `skip_previous` API error on startup (`get_previous(N)` is the correct call)

### Changed
- Log Viewer no longer spawns a `journalctl` subprocess — reads the systemd journal directly via `systemd.journal.Reader` with `reader.wait()` for live tailing
- Flatpak sandbox permissions: replaced `--talk-name=org.freedesktop.Flatpak` with `--filesystem=/run/log/journal:ro` and `--filesystem=/var/log/journal:ro`
- Log Viewer filter field accepts journalctl-compatible flags (`--user`, `--system`, `-t`, `-u`, `-b`, `-p`) mapped directly to the journal reader API

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
