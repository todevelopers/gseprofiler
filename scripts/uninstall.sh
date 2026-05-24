#!/usr/bin/env bash
# GSE Profiler — uninstall script.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/todevelopers/gse-profiler/main/scripts/uninstall.sh | bash
#
# Removes everything installed by setup-and-run.sh:
#   - application repository  (~/$INSTALL_DIR)
#   - desktop entry
#   - app icon
#   - bridge GNOME Shell extension
set -euo pipefail

INSTALL_DIR="${GSE_PROFILER_DIR:-$HOME/gse-profiler}"
BRIDGE_UUID="gse-profiler-bridge@todevelopers"

info()    { echo -e "\033[1;34m[gse-profiler]\033[0m $*"; }
success() { echo -e "\033[1;32m[gse-profiler]\033[0m $*"; }

# ── Bridge extension ───────────────────────────────────────────────────────────
BRIDGE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$BRIDGE_UUID"
if [[ -d "$BRIDGE_DIR" ]]; then
    info "Removing bridge extension…"
    rm -rf "$BRIDGE_DIR"
    success "Bridge extension removed."
else
    info "Bridge extension not found — skipping."
fi

# ── Desktop entry ──────────────────────────────────────────────────────────────
DESKTOP_FILE="${XDG_DATA_HOME:-$HOME/.local/share}/applications/gse-profiler.desktop"
if [[ -f "$DESKTOP_FILE" ]]; then
    info "Removing desktop entry…"
    rm -f "$DESKTOP_FILE"
    command -v update-desktop-database &>/dev/null && \
        update-desktop-database "${XDG_DATA_HOME:-$HOME/.local/share}/applications"
    success "Desktop entry removed."
else
    info "Desktop entry not found — skipping."
fi

# ── App icon ───────────────────────────────────────────────────────────────────
ICON_FILE="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps/io.github.todevelopers.GseProfiler.svg"
if [[ -f "$ICON_FILE" ]]; then
    info "Removing app icon…"
    rm -f "$ICON_FILE"
    command -v gtk-update-icon-cache &>/dev/null && \
        gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
    success "Icon removed."
else
    info "Icon not found — skipping."
fi

# ── Repository ─────────────────────────────────────────────────────────────────
if [[ -d "$INSTALL_DIR" ]]; then
    info "Removing repository at $INSTALL_DIR…"
    rm -rf "$INSTALL_DIR"
    success "Repository removed."
else
    info "Repository not found at $INSTALL_DIR — skipping."
fi

success "GSE Profiler has been fully uninstalled."
