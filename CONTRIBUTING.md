# Contributing to GSE Profiler

Thank you for your interest in contributing! This document covers the development
workflow, local checks, and all automations that run in CI so you know what to
expect before opening a pull request.

---

## Table of Contents

- [Development setup](#development-setup)
- [Local quality checks](#local-quality-checks)
- [Bridge hash](#bridge-hash)
- [Scripts reference](#scripts-reference)
- [GitHub Actions workflows](#github-actions-workflows)
- [Making a release](#making-a-release)
- [Pull request checklist](#pull-request-checklist)

---

## Development setup

### 1. Clone

```bash
git clone https://github.com/todevelopers/gse-profiler.git
cd gse-profiler
```

### 2. Install system dependencies

```bash
# Fedora / RHEL
sudo dnf install python3-gobject gtk4 libadwaita

# Ubuntu / Debian (24.04+)
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
```

### 3. Install dev tools

```bash
pip install ruff mypy pytest
npm install        # installs eslint and config from package.json
```

### 4. Run

```bash
python3 -m app.main
```

On first launch the app will offer to install the bridge extension and restart GNOME Shell.

---

## Local quality checks

These are the exact commands that CI runs. All of them must pass before a PR
can be merged.

| Command | What it checks |
|---|---|
| `ruff check app/` | Python style and common errors |
| `mypy app/` | Python type correctness |
| `pytest tests/ -v --tb=short` | Unit tests |
| `npm run lint` | ESLint on `bridge-extension/` and `api/` |

Run them all at once:

```bash
ruff check app/ && mypy app/ && pytest tests/ -v --tb=short && npm run lint
```

---

## Bridge hash

`bridge-extension/metadata.json` contains a `bundle-hash` field — a SHA-256
digest over all `*.js` files in `bridge-extension/`. The app uses this hash to
detect whether the installed bridge is out of date and needs a reinstall.

**Whenever you change any `.js` file in `bridge-extension/`, regenerate the hash:**

```bash
python3 scripts/update-bridge-hash.py
```

The script prints whether the hash changed and overwrites `metadata.json` in
place. Commit the updated file together with your JS changes.

> **Note:** CI also runs this script automatically (see [Bridge hash sync](#bridge-hash-sync-job)
> below) and commits the result if you forget, but it is cleaner to do it yourself.

---

## Scripts reference

### `scripts/setup-and-run.sh`

One-line install for end users (Fedora / GNOME):

```bash
curl -fsSL https://raw.githubusercontent.com/todevelopers/gse-profiler/main/scripts/setup-and-run.sh | bash
```

What it does:
1. Checks that GTK4 / libadwaita Python bindings are installed.
2. Clones the repo to `~/gse-profiler` (or pulls if it already exists).
3. Installs a `.desktop` entry and the app icon into `~/.local/share/`.
4. Launches the app in debug mode.

Re-running the same command on an existing install pulls the latest changes and
restarts the app. The installation directory can be overridden with the
`GSE_PROFILER_DIR` environment variable.

### `scripts/uninstall.sh`

Removes everything `setup-and-run.sh` installed:

```bash
curl -fsSL https://raw.githubusercontent.com/todevelopers/gse-profiler/main/scripts/uninstall.sh | bash
```

Removes: repository directory, `.desktop` entry, app icon, and the bridge
GNOME Shell extension from `~/.local/share/gnome-shell/extensions/`.

### Shell restart after bridge install/uninstall

Done directly via D-Bus from [`app/core/bridge_manager.py`](app/core/bridge_manager.py):

| Session | D-Bus call |
|---|---|
| **Wayland** | `org.gnome.SessionManager.Logout(1)` (no confirmation; user logs back in) |
| **X11** | `org.gnome.Shell.Eval("Meta.restart(…)")` (restarts in place, no logout) |

This works identically inside and outside Flatpak — no `flatpak-spawn` or
host shell access required.

### `scripts/update-bridge-hash.py`

Recomputes the `bundle-hash` in `bridge-extension/metadata.json`. Run it after
any change to `bridge-extension/*.js`. See [Bridge hash](#bridge-hash) above.

---

## GitHub Actions workflows

### CI (`ci.yml`)

**Triggers:** every push to any branch; every pull request targeting `main`.

Contains three parallel jobs:

#### `python` job

- Sets up Python 3.11.
- Runs `ruff check app/` — fails on style or lint errors.
- Runs `mypy app/` — fails on type errors.
- Runs `pytest tests/ -v --tb=short` — fails on test failures.

#### `bridge-hash-sync` job

- Runs **only on direct pushes** (not on PRs from forks, because those cannot
  push back to the repository).
- Checks whether any `bridge-extension/*.js` file changed in the last commit.
- If yes, runs `python3 scripts/update-bridge-hash.py` and, if `metadata.json`
  was actually changed, commits it back to the branch with the message
  `chore(bridge): update bundle-hash [skip ci]`.
- The `[skip ci]` suffix prevents an infinite CI loop.

#### `javascript` job

- Sets up Node.js 20.
- Installs npm dependencies.
- Runs `npm run lint` (ESLint on `bridge-extension/` and `api/`).

---

### Flathub lint (`flathub-lint.yml`)

**Triggers:** manual only — run it from **Actions → Flathub lint → Run workflow**.

Runs `flatpak-builder-lint` (via `org.flatpak.Builder`) against both the
manifest and the built repo:

1. Installs `flatpak`, `flatpak-builder`, and `org.flatpak.Builder` from Flathub.
2. **Manifest lint** — validates `build-aux/io.github.todevelopers.GseProfiler.yml`
   against Flathub submission rules.
3. Installs the GNOME 50 runtime / SDK and builds the Flatpak into a local `repo/`.
4. **Repo lint** — validates the built repo against Flathub submission rules.

Use this before opening a Flathub submission PR to check for issues. The lint
output is visible in the job log even when there are known exceptions pending.

---

### Release (`release.yml`)

**Triggers:** any tag matching `v*`.

The workflow classifies the tag as **stable** or **prerelease** based on its
shape and runs different jobs accordingly.

| Tag shape | Channel |
|---|---|
| `v1.2.3` (plain semver) | **stable** |
| `v1.2.3-rc1`, `v1.2.3-beta`, `v1.2.3-test`, … (semver with suffix) | **prerelease** |

#### Stable path (`v1.2.3`)

| Job | What it does |
|---|---|
| `detect` | Classifies the tag as stable. |
| `guard-stable` | Refuses if the GitHub Release for this tag already exists — prevents accidental watcher-notification spam from re-running an existing tag. |
| `version-bump` | Patches `_BASE_VERSION` in `main.py`, injects a `<release>` entry into `metainfo.xml` from CHANGELOG.md, commits both to `main`, and force-moves the tag to the new commit. |
| `github-release` | Builds the source tarball `gseprofiler-X.Y.Z.tar.gz` and creates the GitHub Release with notes extracted from CHANGELOG.md. |
| `flatpak` | Updates manifest source URL + `sha256` to point at the new tarball, commits it back to `main`, builds the Flatpak bundle, attaches it to the GitHub Release, and pushes the OSTree commit to `todevelopers/flatpaks` (stable repo). |

#### Prerelease path (`v1.2.3-rc1`)

| Job | What it does |
|---|---|
| `detect` | Classifies the tag as prerelease. |
| `flatpak` | Patches `_BASE_VERSION` to `X.Y.Z-rcN+<short-sha>` **in build env only** (no commit), injects a `type="development"` `<release>` entry into metainfo (also no commit), overrides the `gse-profiler` source in the manifest to a local-directory checkout, builds the Flatpak, and pushes the OSTree commit to `todevelopers/flatpaks` (testing repo). |

Prerelease builds do **not** modify `main`, do **not** create a GitHub
Release, and do **not** notify watchers. The committed state of `main`
stays at the previous stable.

**How to cut releases:**

```bash
# Production release for everyone
git tag v1.2.3
git push origin v1.2.3

# Testing build for early validation
git tag v1.2.3-rc1
git push origin v1.2.3-rc1
```

### Self-hosted Flatpak remote (`todevelopers/flatpaks`)

The `flatpak` job pushes built OSTree commits to the
[`todevelopers/flatpaks`](https://github.com/todevelopers/flatpaks)
repository's `gh-pages` branch, served via GitHub Pages at
<https://todevelopers.github.io/flatpaks/>.

Two separate OSTree repos and two `.flatpakrepo` files mirror Fedora's
"Fedora Flatpaks" + "Fedora Flatpaks (testing)" split:

| Channel | Remote name | URL |
|---|---|---|
| Testing | `todevelopers-testing` | <https://todevelopers.github.io/flatpaks/todevelopers-testing.flatpakrepo> |

> Stable channel is not yet available — use the testing remote until the first stable release.

Users add the remote once and install via the normal flatpak CLI or
GNOME Software:

```bash
flatpak remote-add --user todevelopers-testing \
  https://todevelopers.github.io/flatpaks/todevelopers-testing.flatpakrepo
flatpak install --user todevelopers-testing io.github.todevelopers.GseProfiler
```

Builds are GPG-signed; the `.flatpakrepo` file embeds the public key so
`--no-gpg-verify` is not needed.

---

## Making a release

Two channels: **stable** (full release for everyone) and **prerelease** (test
build, no GitHub Release, no source commits).

### Stable release (`v1.2.3`)

Cutting a stable release requires **one manual step** — everything else is
automated.

#### Step 1 — Update `CHANGELOG.md` (manual)

Add a new section at the top of `CHANGELOG.md` for the version you are
releasing. The release workflow reads this section to generate the GitHub
Release body and the AppStream `<description>` in `metainfo.xml`.

```markdown
## [1.2.3] - YYYY-MM-DD

Optional introductory paragraph.

### Fixed
- Short description of a bug fix.
- Another fix.

### Changed
- What changed and why.
```

Rules:
- The heading must be exactly `## [X.Y.Z]` (with optional ` - date` suffix).
- Use `### Section` sub-headings and `- bullet` items for structured output.
- Inline markdown (`` `code` ``, `**bold**`) is stripped automatically when
  converting to AppStream XML.
- Do **not** add a `<release>` entry to `metainfo.xml` manually — the workflow
  injects it from CHANGELOG.md.

#### Step 2 — Push the tag

```bash
git tag v1.2.3
git push origin v1.2.3
```

The `release.yml` workflow then runs `detect` → `guard-stable` → `version-bump`
→ `github-release` → `flatpak`. After it completes:

- A new GitHub Release `v1.2.3` exists with notes from CHANGELOG.md and the
  `.flatpak` bundle attached.
- The OSTree commit is published to `todevelopers/flatpaks` (stable repo).
- Users get the update via `flatpak update`.

> If a GitHub Release for `v1.2.3` already exists, `guard-stable` refuses to
> proceed. To re-publish you must delete the existing GitHub Release first.

### Prerelease (`v1.2.3-rc1`, `v1.2.3-beta`, …)

Use a prerelease tag to publish a test build to the **testing** Flatpak remote
without touching `main` or notifying GitHub watchers. The workflow patches
`_BASE_VERSION` to `X.Y.Z-rcN+<sha>` in-memory and builds from the tagged
commit's local source.

```bash
git tag v1.2.3-rc1
git push origin v1.2.3-rc1
```

Testers install with:

```bash
flatpak install todevelopers-testing io.github.todevelopers.GseProfiler
```

Prerelease tags do **not** require a CHANGELOG.md entry. They can be created
and deleted freely — there is no GitHub Release to clean up afterwards.

### Flathub publish

This project does **not** submit to Flathub. Distribution is via the
self-hosted Flatpak remote at <https://todevelopers.github.io/flatpaks/>.

---

## Pull request checklist

Before opening a PR, make sure:

- [ ] `ruff check app/` passes with no errors.
- [ ] `mypy app/` passes with no errors.
- [ ] `pytest tests/` passes.
- [ ] `npm run lint` passes.
- [ ] If you changed `bridge-extension/*.js`: ran `python3 scripts/update-bridge-hash.py` and committed the updated `metadata.json`.
- [ ] New behaviour is covered by tests in `tests/`.
- [ ] The PR targets the `main` branch.
