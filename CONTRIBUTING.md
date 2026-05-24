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

### `scripts/restart-shell.sh`

Restarts GNOME Shell after the bridge extension is installed or updated.

| Session | Method |
|---|---|
| **Wayland** | `gnome-session-quit --logout --no-prompt` (prompts the user to log back in) |
| **X11** | `Meta.restart()` via `org.gnome.Shell` D-Bus (restarts in place, no logout) |

When running inside Flatpak on Wayland the script uses `flatpak-spawn --host`
to call `gnome-session-quit` on the host system.

The app calls this script automatically after installing the bridge; you
generally do not need to run it by hand.

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

### Bridge Tests (`bridge-test.yml`)

**Triggers:** pushes and pull requests that touch `bridge-extension/**`,
`.eslintrc.json`, or `package.json`.

Runs a single `lint` job — same ESLint check as the `javascript` job in CI.
This workflow fires faster than full CI because it is scoped to bridge-only
changes.

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

**Triggers:** any tag matching `v*` (e.g. `v1.2.0`, `v2.0.0-beta.1`).

The workflow runs three jobs in sequence:

#### 1. `version-bump` job

1. Checks out `main` (not the tag commit).
2. Extracts the version number from the tag name (strips the leading `v`).
3. Patches `_BASE_VERSION` in `app/main.py` to the new version.
4. Commits the change as `chore: bump version to X.Y.Z [skip ci]`, pushes it
   to `main`, and force-moves the tag to the new commit.
5. Outputs the final commit SHA for the downstream jobs.

> If `_BASE_VERSION` is already correct (e.g. you patched it manually), the job
> detects no diff and skips the commit.

#### 2. `release` job (needs `version-bump`)

1. Checks out the commit produced by `version-bump`.
2. Builds a source tarball using `git archive`: `gse-profiler-X.Y.Z.tar.gz`
3. Extracts release notes from `CHANGELOG.md`:
   - Looks for a `## [X.Y.Z]` section and copies everything up to the next
     `## [` heading.
   - Falls back to `git log --oneline` from the previous tag if the section is
     missing.
4. Creates a GitHub Release with:
   - The extracted release notes as the body.
   - The source tarball as a release asset.
   - `prerelease: true` if the tag name contains a `-` (e.g. `-beta`, `-rc`).

#### 3. `flatpak` job (needs `version-bump`, `release`)

1. Checks out the commit produced by `version-bump`.
2. Updates the manifest source URL and `sha256` to match the new release tarball.
3. Commits the updated manifest back to `main`.
4. Installs `flatpak`, `flatpak-builder`, and the GNOME 50 runtime / SDK from Flathub.
5. Builds the Flatpak bundle: `gse-profiler-X.Y.Z-x86_64.flatpak`
6. Runs `flatpak-builder-lint` against the manifest and the built repo
   (non-blocking — lint output is visible in logs).
7. Attaches the `.flatpak` bundle to the GitHub Release.

**How to cut a release:**

```bash
git tag v1.2.3
git push origin v1.2.3
```

That is all. The workflow handles the version bump commit, tarball, release
notes, and Flatpak bundle automatically.

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
