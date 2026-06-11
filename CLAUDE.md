# CLAUDE.md — gse-profiler

## Project Overview

GTK4 desktop application for **managing, debugging, and profiling GNOME Shell extensions**.
Targets GNOME Shell extension developers. Internally installs a bridge GJS extension that acts as a bridge into the `gnome-shell` process.

**Target platform:** GNOME 48+ (Wayland only)
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
└── tests/                      # pytest unit tests
```

---

## Bridge Extension

- **UUID:** `gse-profiler-bridge@todevelopers`
- **Install path:** `~/.local/share/gnome-shell/extensions/gse-profiler-bridge@todevelopers/`
- Auto-installed by the app on first launch
- Shows a minimal status indicator in the GNOME panel (no menu needed in V1)
- After installation, the app triggers a shell restart via D-Bus (see below)

### Shell Restart Logic

Performed via a single session-bus D-Bus call:
`org.gnome.SessionManager.Logout(1)` (no-confirm logout). Works
identically inside and outside Flatpak.

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
- Never use `org.gnome.Shell.Eval` — all introspection goes through the socket

### General

- All user-facing strings and identifiers in English
- No `console.log` left in production GJS code — use `log()` / `logError()`
- Profile event JSON schema: `{ type, extensionUuid, function, start, end, depth }`

---

## D-Bus Interfaces Used

| Interface                    | Purpose                            |
| ---------------------------- | ---------------------------------- |
| `org.gnome.Shell`            | List extensions, enable/disable (via the `org.gnome.Shell.Extensions` interface) |
| `org.gnome.SessionManager`   | Logout after bridge install/remove |
| `org.freedesktop.DBus`       | Introspection                      |

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

**Two channels: stable (full release) and prerelease (test build).** Tag pattern
decides which path runs.

| Tag pattern | Channel | Effect |
|---|---|---|
| `v1.2.3` (clean semver) | **stable** | source commits, GitHub Release with notes, `.flatpak` bundle, push to `todevelopers/flatpaks` → **stable** OSTree repo |
| `v1.2.3-rc1`, `v1.2.3-beta`, `v1.2.3-test`, … | **prerelease** | no source commits, no GitHub Release, push to `todevelopers/flatpaks` → **testing** OSTree repo |

### Step 1 — CHANGELOG.md (manual, stable only)

Add a `## [X.Y.Z]` section at the top of `CHANGELOG.md`. Use `### Fixed` /
`### Changed` / `### Added` sub-headings with `- bullet` items.

> **Keep every bullet on ONE physical line — do not wrap bullets across
> multiple lines.** The metainfo converter (Step 2 `version-bump`) treats
> only lines starting with `- `/`* ` as `<li>`; a wrapped continuation line
> becomes a stray standalone `<p>` between `</ul>` and `<ul>`, producing
> broken sentence-fragment output in GNOME Software. This bit the v1.1.0
> release. Lines may be long; that's fine.

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

Skip this step for prerelease tags — they do not generate release notes
or modify CHANGELOG.

### Step 2 — Push tag

**Stable:**

```bash
git tag v1.2.3
git push origin v1.2.3
```

`release.yml` runs:

| Job | What it does |
|---|---|
| `detect` | Classifies tag as stable |
| `guard-stable` | Refuses if GitHub Release for this tag already exists (prevents watcher-notification spam from accidental re-runs) |
| `version-bump` | Patches `_BASE_VERSION` in `main.py`, injects `<release>` into `metainfo.xml`, commits + re-tags |
| `github-release` | Builds source tarball, creates GitHub Release with CHANGELOG notes |
| `flatpak` | Updates manifest URL + sha256, builds `.flatpak` bundle (attached to Release), pushes to `todevelopers/flatpaks` → stable OSTree repo |

**Prerelease (for testing):**

```bash
git tag v1.2.3-rc1
git push origin v1.2.3-rc1
```

`release.yml` runs:

| Job | What it does |
|---|---|
| `detect` | Classifies tag as prerelease |
| `flatpak` | Patches `_BASE_VERSION` in-memory to `1.2.3-rc1+<short-sha>`, injects a `type="development"` `<release>` entry into metainfo (also in-memory), overrides the gse-profiler source to local checkout, builds Flatpak, pushes to `todevelopers/flatpaks` → testing OSTree repo |

Prerelease builds **do not commit anything back** to `main` and **do not
create a GitHub Release** — watchers are not notified. The committed
state of `main` stays at the previous stable.

### Self-hosted Flatpak remote

Builds are published to `todevelopers/flatpaks` (gh-pages branch, served
via GitHub Pages at https://todevelopers.github.io/flatpaks/). Two
separate remotes:

| Remote | URL | Channel |
|---|---|---|
| `todevelopers` | `.../todevelopers.flatpakrepo` | stable releases (not yet available) |
| `todevelopers-testing` | `.../todevelopers-testing.flatpakrepo` | prereleases (rc/beta) |

Until the first stable release, use the testing remote:

```bash
flatpak remote-add --user todevelopers-testing \
  https://todevelopers.github.io/flatpaks/todevelopers-testing.flatpakrepo
flatpak install --user todevelopers-testing io.github.todevelopers.GseProfiler
```

Builds are GPG-signed; the `.flatpakrepo` file embeds the public key so
`--no-gpg-verify` is no longer needed.

---

## Key Rules for the Agent

1. **Never block the GTK main loop** — all I/O must be async or run in a thread.
2. **Wayland only** — X11 support was dropped; never use `org.gnome.Shell.Eval` or assume an X server.
3. **opt-in API must be side-effect free when not connected** — all `DevToolsClient` methods must silently no-op when the bridge socket is unavailable.
4. **Bridge lifecycle** — always call `disable()` cleanup: disconnect signals, close socket, remove monkey-patches.
5. **Tests live in `tests/`** — use `pytest`; mock D-Bus and subprocess in unit tests.
6. **Checks run automatically** — the Stop hook runs `ruff check`, syntax check, WSL headless tests, and `pytest` after every response. Do not report work as done if the hook is still failing.
7. **CHANGELOG.md — always preview first** — before writing any content to `CHANGELOG.md`, show the user the exact text to be written and wait for explicit approval.
