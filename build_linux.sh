#!/usr/bin/env bash
# Build a standalone LibGen binary on Linux with PyInstaller, and (unless --no-install)
# install it to ~/.local/bin and register a desktop launcher + icon, so it works both
# as a menu app and as the terminal command `libgen`.
#
# PyInstaller cannot cross-compile: run this ON the distro you want to target
# (oldest glibc you need to support). lgsearch.py sits next to this file and is
# imported, so PyInstaller bundles it automatically. Everything is user-level (~/.local),
# no sudo. Pass --no-install to only build the binary.
set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"

python3 -m pip install --upgrade pyqt5 pyinstaller
pyinstaller --onefile --windowed --name libgen libgen_app.py
BUILT="$HERE/dist/libgen"
echo
echo "Built: $BUILT"

if [ "${1:-}" = "--no-install" ]; then
    echo "Skipped install (--no-install). Run it with: $BUILT"
    exit 0
fi

# install the binary onto PATH so it's both a menu app and a terminal command
BINDIR="$HOME/.local/bin"
mkdir -p "$BINDIR"
install -m 755 "$BUILT" "$BINDIR/libgen"
BIN="$BINDIR/libgen"
echo "Installed binary:   $BIN"
case ":$PATH:" in
    *":$BINDIR:"*) ;;
    *) echo "NOTE: $BINDIR is not on your PATH — add it (e.g. in ~/.profile) to run 'libgen' from a terminal." ;;
esac

# --- desktop integration (user-level) ---
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"

# install the icon into the hicolor theme if we have one
if [ -f icon.png ]; then
    ICONDIR="$HOME/.local/share/icons/hicolor/256x256/apps"
    mkdir -p "$ICONDIR"
    cp icon.png "$ICONDIR/libgen.png"
    ICON_LINE="Icon=libgen"
    command -v gtk-update-icon-cache >/dev/null 2>&1 && \
        gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
else
    ICON_LINE="Icon=applications-other"   # generic fallback
fi

cat > "$APPS/libgen.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=LibGen
Comment=Search and download books from Library Genesis
Exec=$BIN
$ICON_LINE
Terminal=false
Categories=Utility;Network;
DESKTOP
chmod +x "$APPS/libgen.desktop"
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$APPS" 2>/dev/null || true

echo
echo "Installed launcher: $APPS/libgen.desktop"
[ -f icon.png ] && echo "Installed icon:     $ICONDIR/libgen.png"
echo "LibGen should now appear in your applications menu (you may need to log out/in once),"
echo "and run from a terminal as:  libgen"
