#!/bin/sh
# Build a thin Debian package for GSE Profiler.
#
# "Thin" means the package ships only the application code and declares
# runtime dependencies on distro-provided GTK4/libadwaita/PyGObject stacks.
# It is directly installable on GNOME 48+ hosts (Ubuntu 25.10+, Fedora is
# rpm-based so the deb targets Debian-family only) and doubles as the
# extra-data payload for the flatpark.org Flatpak manifest, where the
# declared dependencies are ignored and the org.gnome.Platform runtime
# provides them instead.
#
# Usage: build-deb.sh <source-root> <version> <output-dir>
set -eu

SRC=$(cd "$1" && pwd)
VERSION=$2
OUT=$(cd "$3" && pwd)
PKG=gse-profiler

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
ROOT="$STAGE/$PKG"

install -d \
    "$ROOT/DEBIAN" \
    "$ROOT/usr/bin" \
    "$ROOT/usr/share/$PKG" \
    "$ROOT/usr/share/applications" \
    "$ROOT/usr/share/metainfo" \
    "$ROOT/usr/share/icons/hicolor/scalable/apps" \
    "$ROOT/usr/share/doc/$PKG"

# Same payload layout as the Flatpak build (build-aux/*.yml), rooted at
# /usr/share instead of /app/share.
cp -r "$SRC/app" "$SRC/bridge-extension" "$ROOT/usr/share/$PKG/"
find "$ROOT/usr/share/$PKG" -type d -name '__pycache__' -exec rm -rf {} +

cat > "$ROOT/usr/bin/$PKG" <<'EOF'
#!/bin/sh
exec python3 /usr/share/gse-profiler/app/main.py "$@"
EOF

install -m 644 "$SRC/data/io.github.todevelopers.GseProfiler.desktop" \
    "$ROOT/usr/share/applications/"
install -m 644 "$SRC/data/io.github.todevelopers.GseProfiler.metainfo.xml" \
    "$ROOT/usr/share/metainfo/"
install -m 644 \
    "$SRC/app/data/icons/hicolor/scalable/apps/io.github.todevelopers.GseProfiler.svg" \
    "$ROOT/usr/share/icons/hicolor/scalable/apps/"
install -m 644 "$SRC/LICENSE" "$ROOT/usr/share/doc/$PKG/copyright"

# Normalise permissions: staging may inherit 777 from Windows-mounted
# filesystems (WSL /mnt/c); dpkg records the staged modes verbatim.
find "$ROOT/usr" -type d -exec chmod 755 {} +
find "$ROOT/usr" -type f -exec chmod 644 {} +
chmod 755 "$ROOT/usr/bin/$PKG"

INSTALLED_SIZE=$(du -ks "$ROOT/usr" | cut -f1)

cat > "$ROOT/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Section: devel
Priority: optional
Architecture: all
Depends: python3 (>= 3.11), python3-gi (>= 3.48), python3-gi-cairo, gir1.2-gtk-4.0 (>= 4.18), gir1.2-adw-1 (>= 1.7), python3-systemd, python3-pathspec, gnome-shell (>= 48)
Installed-Size: $INSTALLED_SIZE
Maintainer: Tomas Gazovic <gazovic.todevelopers@gmail.com>
Homepage: https://github.com/todevelopers/gseprofiler
Description: Debug, profile and manage GNOME Shell extensions
 GTK4 desktop application for GNOME Shell extension developers.
 Lists, enables and profiles installed extensions, shows their
 journal output, and inspects live extension state through a
 bridge extension injected into the gnome-shell process.
EOF

# Explicit xz: the flatpark apply_extra script unpacks data.tar.xz with
# bsdtar, so the compression must not drift with the dpkg default (zstd
# on current Ubuntu).
dpkg-deb --build -Zxz --root-owner-group "$ROOT" "$OUT/${PKG}_${VERSION}_all.deb"
