#!/usr/bin/env bash
# Build LibGen.app on macOS with PyInstaller. Run this ON a Mac (PyInstaller cannot
# cross-compile; build on the oldest macOS / the CPU arch you want to support).
# lgsearch.py sits next to this file and is imported, so it's bundled automatically.
# The .app can go anywhere (e.g. /Applications): it keeps settings.json / session.json in
# ~/Library/Application Support/LibGen and downloads to ~/Downloads/libgen by default.
set -euo pipefail
cd "$(dirname "$0")"

python3 -m pip install --upgrade pyqt5 pyinstaller

# icon.png -> icon.icns with the macOS built-ins (sips + iconutil); no Pillow needed
if [ -f icon.png ] && [ ! -f icon.icns ]; then
    rm -rf icon.iconset && mkdir icon.iconset
    for s in 16 32 128 256; do
        sips -z $s $s icon.png --out "icon.iconset/icon_${s}x${s}.png" >/dev/null
        sips -z $((s*2)) $((s*2)) icon.png --out "icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
    done
    iconutil -c icns icon.iconset && rm -rf icon.iconset
fi
ICON=(); [ -f icon.icns ] && ICON=(--icon icon.icns)

# always build clean so a stale build/ or .spec can't carry old settings
rm -rf build dist LibGen.spec
pyinstaller --onefile --windowed --name LibGen "${ICON[@]}" libgen_app.py
rm -f dist/LibGen   # keep only the .app bundle, not the bare binary

echo
echo "Done. Your app is at:  dist/LibGen.app   (open it, or drag it to /Applications)"
echo "First launch may need right-click > Open (unsigned app)."
