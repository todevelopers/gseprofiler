#!/usr/bin/env bash
# GSE Profiler — quick-start script for Fedora (including Hyper-V VMs).
#
# Usage (no sudo, no prompts):
#   curl -fsSL https://raw.githubusercontent.com/todevelopers/gse-profiler/main/scripts/setup-and-run.sh | bash
#
# Run inside an active GNOME session, not over bare SSH.
set -euo pipefail

REPO_URL="https://github.com/todevelopers/gse-profiler.git"
INSTALL_DIR="${GSE_PROFILER_DIR:-$HOME/gse-profiler}"

# ── Helpers ────────────────────────────────────────────────────────────────────
info()    { echo -e "\033[1;34m[gse-profiler]\033[0m $*"; }
success() { echo -e "\033[1;32m[gse-profiler]\033[0m $*"; }
die()     { echo -e "\033[1;31m[gse-profiler] ERROR:\033[0m $*" >&2; exit 1; }

# ── Pre-flight: display ────────────────────────────────────────────────────────
if [[ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    die "No display found. Run inside a GNOME session, not over bare SSH."
fi

# ── Pre-flight: Python / GTK4 / libadwaita ─────────────────────────────────────
# On Fedora with GNOME these packages are pre-installed — no sudo needed.
# If they are missing, print the one-time install command and exit.
if ! python3 - <<'EOF' 2>/dev/null
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: F401
EOF
then
    die "GTK4 / libadwaita Python bindings not found.
Run this once to install them, then re-run the script:
  sudo dnf install python3-gobject gtk4 libadwaita"
fi

success "Dependencies OK."

# ── Clone or update ────────────────────────────────────────────────────────────
# GIT_TERMINAL_PROMPT=0 prevents git from opening /dev/tty to ask for credentials.
# The repo is public so no credentials are needed; this suppresses any keyring prompt.
export GIT_TERMINAL_PROMPT=0

if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Updating existing installation at $INSTALL_DIR…"
    git -C "$INSTALL_DIR" fetch origin
    git -C "$INSTALL_DIR" reset --hard origin/main
else
    info "Cloning to $INSTALL_DIR…"
    git clone -c credential.helper= "$REPO_URL" "$INSTALL_DIR"
fi

success "Repository is up to date."

# ── Desktop integration ────────────────────────────────────────────────────────
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
mkdir -p "$DESKTOP_DIR" "$ICON_DIR"

sed "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
    "$INSTALL_DIR/app/data/gse-profiler.desktop.in" \
    > "$DESKTOP_DIR/gse-profiler.desktop"

cp "$INSTALL_DIR/app/data/icons/hicolor/scalable/apps/io.github.todevelopers.GseProfiler.svg" \
   "$ICON_DIR/io.github.todevelopers.GseProfiler.svg"

command -v update-desktop-database &>/dev/null && \
    update-desktop-database "$DESKTOP_DIR"
command -v gtk-update-icon-cache &>/dev/null && \
    gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"

success "Desktop entry and icon installed — GSE Profiler is now in the app launcher."

# ── Launch ─────────────────────────────────────────────────────────────────────
info "Starting GSE Profiler in debug mode…"
exec python3 "$INSTALL_DIR/app/main.py" --debug
