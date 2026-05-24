# CLAUDE.md — gse-profiler

## Project Overview

GTK4 desktop application for **managing, debugging, and profiling GNOME Shell extensions**.
Targets GNOME Shell extension developers. Internally installs a bridge GJS extension that acts as a bridge into the `gnome-shell` process.

**Target platform:** GNOME 48+  
**Languages:** Python 3.11+ (app), GJS / ES6 (bridge extension + developer API)

---

## Architecture

```
GTK4 App (Python/PyGObject)
         │
         ├─ D-Bus ──────────────► org.gnome.Shell.Extensions  (list / enable / disable)
         │
         └─ Unix Socket (JSON) ──► Bridge Extension (GJS)
                                         │
                                         ├── Target extension   (monkey-patch, inspect)
                                         ├── Core gnome-shell   (optional)
                                         └── opt-in API bridge  (DevToolsClient)
```

Communication split:

- **D-Bus** — standard GNOME Shell APIs (extensions list, enable/disable)  
- **Unix socket** — all custom high-frequency communication with the bridge

Socket path: `$XDG_RUNTIME_DIR/gse-profiler.sock`  
Protocol: newline-delimited JSON messages

---

## Directory Layout

```
gse-profiler/
├── app/                        # GTK4 Python application
│   ├── main.py
│   ├── ui/                     # UI view modules
│   │   ├── extension_manager.py
│   │   ├── log_viewer.py
│   │   ├── profiler_view.py
│   │   └── inspector_view.py
│   ├── core/                   # Core logic
│   │   ├── dbus_client.py
│   │   ├── socket_server.py
│   │   ├── git_manager.py
│   │   └── journal_reader.py
│   └── data/ui/                # Glade .ui files (if needed)
├── bridge-extension/        # GJS GNOME Shell extension
│   ├── extension.js
│   ├── profiler.js
│   ├── inspector.js
│   ├── socket_client.js
│   └── metadata.json
├── api/
│   └── devtools-api.js         # opt-in developer API
├── scripts/
│   └── restart-shell.sh
└── tests/                      # pytest unit tests
```

---

## Bridge Extension

- **UUID:** `gse-profiler-bridge@todevelopers`
- **Install path:** `~/.local/share/gnome-shell/extensions/gse-profiler-bridge@todevelopers/`
- Auto-installed by the app on first launch
- Shows a minimal status indicator in the GNOME panel (no menu needed in V1)
- After installation, `scripts/restart-shell.sh` is run to reload gnome-shell

### Shell Restart Logic

| Session type | Restart method                                    |
| ------------ | ------------------------------------------------- |
| X11          | `Meta.restart()` via `org.gnome.Shell Eval` D-Bus |
| Wayland      | `gnome-session-quit --logout --no-prompt`         |

---

## Coding Conventions

### Python (`app/`)

- Python 3.11+, PEP 8, type hints throughout
- Use GObject property system (`GObject.Property`) where appropriate
- D-Bus calls: use `Gio.DBusProxy` async variants — never block the main loop
- Unix socket I/O: async via `GLib.IOChannel` or `asyncio` with GLib event loop integration
- No hardcoded paths — use `GLib.get_user_data_dir()`, `GLib.get_runtime_dir()`, etc.

### GJS (`bridge-extension/`, `api/`)

- ES6 module syntax (`import` / `export`), strict mode (`'use strict'`)
- Use GNOME GJS bindings (`imports.gi.*` or `gi://` depending on GNOME version)
- Always disconnect signals in `disable()` — no memory leaks
- Never use `org.gnome.Shell Eval` on Wayland — all introspection goes through the socket

### General

- All user-facing strings and identifiers in English
- No `console.log` left in production GJS code — use `log()` / `logError()`
- Profile event JSON schema: `{ type, extensionUuid, function, start, end, depth }`

---

## D-Bus Interfaces Used

| Interface                    | Purpose                         |
| ---------------------------- | ------------------------------- |
| `org.gnome.Shell.Extensions` | List extensions, enable/disable |
| `org.gnome.Shell`            | Shell eval for X11 restart      |
| `org.freedesktop.DBus`       | Introspection                   |

---

## Headless Smoke Testing on Windows (WSL)

The repo lives on Windows but the app targets GNOME/Linux. Full UI testing
needs a real GNOME 48+ box (D-Bus, Wayland, gnome-shell). For everything
short of that — syntax, imports, widget construction, draw functions —
use WSL. PyGObject 3.48+, GTK4, and libadwaita-1 are typically already
installed on a recent Ubuntu WSL.

> **These checks run automatically via the Claude Code Stop hook**
> (`.claude/run-tests.ps1`): `ruff check app/`, `eslint bridge-extension/ api/`,
> syntax check, WSL headless tests, and `pytest`. If any check fails, Claude
> is blocked from finishing and must fix the errors first. Manual runs below
> are for debugging only.

**1. Syntax check (Windows Python is fine here, no `gi` needed):**

```bash
py -c "
import ast
for p in [
    'C:/GitHubRepos/gse-profiler/app/ui/profiler_view.py',
    # …add the files you touched
]:
    with open(p, encoding='utf-8') as f: ast.parse(f.read())
    print('OK:', p)
"
```

**2. Import check — every module under WSL** (catches missing names, bad
relative imports, signal-type mismatches at class-construction time):

```bash
wsl -- bash -c "cd /mnt/c/GitHubRepos/gse-profiler && python3 -c '
import sys, os; sys.path.insert(0, os.getcwd())
from app.ui.profiler_view import ProfilerView
# …import every module touched
print(\"imports OK\")
'"
```

**3. Headless instantiation** — construct the view with real
`DBusClient`/`SocketServer` (their `__init__` is non-blocking; no actual
bus or socket is opened until `start()` or main-loop iteration), exercise
the data flow, check state transitions:

```bash
wsl -- bash -c "cd /mnt/c/GitHubRepos/gse-profiler && python3 -c '
import sys, os; sys.path.insert(0, os.getcwd())
import gi
gi.require_version(\"Gtk\", \"4.0\"); gi.require_version(\"Adw\", \"1\")
from gi.repository import Gtk
Gtk.init_check()  # returns True without a display

from app.core.dbus_client import DBusClient
from app.core.socket_server import SocketServer
from app.ui.profiler_view import ProfilerView

view = ProfilerView(DBusClient(), SocketServer())
# Feed events directly through the ingest path (bypasses socket I/O)
view._ingest_event({\"function\": \"foo\", \"start\": 0.0, \"end\": 0.01, \"depth\": 0}, schedule_refresh=False)
view._flush_refresh()
assert view._inner_stack.get_visible_child_name() == \"data\"
print(\"ok\")
'"
```

**4. Headless draw** — `Gtk.DrawingArea` draw functions never fire
without a display, so call them directly against a `cairo.ImageSurface`.
This catches index errors, off-by-ones, and hit-test rect generation:

```bash
wsl -- bash -c "cd /mnt/c/GitHubRepos/gse-profiler && python3 -c '
import sys, os, cairo; sys.path.insert(0, os.getcwd())
import gi; gi.require_version(\"Gtk\", \"4.0\"); gi.require_version(\"Adw\", \"1\")
from gi.repository import Gtk
Gtk.init_check()
from app.ui.profiler.flamegraph import FlamegraphView

fg = FlamegraphView()
fg.set_events([{\"function\":\"foo\",\"start\":0.0,\"end\":0.01,\"depth\":0}])
cr = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 800, 300))
fg._draw(fg, cr, 800, 300)
assert len(fg._bar_rects) == 1
print(\"draw ok\")
'"
```

### What WSL cannot test
- **Live D-Bus** — no `gnome-shell` running, so `DBusClient` never reaches
  the `proxy ready` callback. Code paths gated on `_proxy is not None`
  will not execute. Test these on a real GNOME box.
- **Bridge socket** — `SocketServer.start()` creates the socket but no
  bridge will connect to it.
- **Hover / click / keyboard events** — `Gtk.EventControllerMotion` and
  `Gtk.GestureClick` only fire under a real display. Call the handler
  methods directly if you need to test them headless.
- **`Adw.StyleManager.get_default().get_dark()`** — works headless but
  always returns the default theme. Theme-switch behaviour is GNOME-only.

### Final check: real GNOME session
For UI changes always also run `python3 -m app.main` on a real GNOME 48+
machine before reporting the work as done. Headless covers code paths;
only the live app covers visual layout, colour, animation, popover
placement, and interaction timing.

---

## Release Process

**One manual step, everything else automated.**

### Step 1 — CHANGELOG.md (manual, always first)

Add a `## [X.Y.Z]` section at the top of `CHANGELOG.md`. Use `### Fixed` /
`### Changed` / `### Added` sub-headings with `- bullet` items.

> **IMPORTANT for the agent:** Before writing anything to `CHANGELOG.md`,
> always show the user a preview of the exact text to be written and wait
> for explicit confirmation before proceeding.

The release workflow reads this section to produce:
- The GitHub Release body
- The AppStream `<release><description>` in `metainfo.xml` (auto-converted
  from Markdown: `### Heading` → `<p>`, `- item` → `<ul><li>`, inline
  markup stripped)

Do **not** manually add a `<release>` entry to `metainfo.xml` — the
workflow injects it from CHANGELOG.md automatically.

### Step 2 — Push tag (triggers full automation)

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

`release.yml` then runs three jobs automatically:

| Job | What it does |
|---|---|
| `version-bump` | Patches `_BASE_VERSION` in `main.py`, injects `<release>` into `metainfo.xml`, commits + re-tags |
| `release` | Builds source tarball, creates GitHub Release with CHANGELOG notes |
| `flatpak` | Updates manifest URL + sha256, builds `.flatpak` bundle, attaches to Release |

### Step 3 — Flathub (manual, separate)

After the GitHub Release exists, open a PR in
`github.com/flathub/io.github.todevelopers.GseProfiler` with the updated
manifest URL + sha256. Run **Actions → Flathub lint** first to catch issues.

---

## Key Rules for the Agent

1. **Never block the GTK main loop** — all I/O must be async or run in a thread.
2. **Wayland first** — never assume X11; shell eval path is a fallback, not the default.
3. **opt-in API must be side-effect free when not connected** — all `DevToolsClient` methods must silently no-op when the bridge socket is unavailable.
4. **Bridge lifecycle** — always call `disable()` cleanup: disconnect signals, close socket, remove monkey-patches.
5. **Tests live in `tests/`** — use `pytest`; mock D-Bus and subprocess in unit tests.
6. **Checks run automatically** — the Stop hook runs `ruff check`, syntax check, WSL headless tests, and `pytest` after every response. Do not report work as done if the hook is still failing.
7. **CHANGELOG.md — always preview first** — before writing any content to `CHANGELOG.md`, show the user the exact text to be written and wait for explicit approval.
