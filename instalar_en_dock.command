#!/bin/bash
# Doble clic UNA VEZ: copia la app a ~/Applications, la pone en el Dock y reinicia el Dock.
set -euo pipefail
SRC="/Users/alvaro/Uncensored-LLM-appdata/UncensoredBuilder.app"
DEST="$HOME/Applications/UncensoredBuilder.app"
URL="file://$HOME/Applications/UncensoredBuilder.app/"

mkdir -p "$HOME/Applications"
ditto "$SRC" "$DEST"
chmod +x "$DEST/Contents/MacOS/Uncensored Builder"

# Evitar duplicados: quitar entradas viejas del mismo bundle id (best effort)
/usr/libexec/PlistBuddy -c "Print persistent-apps" "$HOME/Library/Preferences/com.apple.dock.plist" 2>/dev/null | true

defaults write com.apple.dock persistent-apps -array-add "<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>${URL}</string><key>_CFURLStringType</key><integer>15</integer></dict></dict></dict>"

killall Dock 2>/dev/null || true
open "$DEST"
echo "Listo: app en ~/Applications y tile añadido al Dock (Dock se reinició un segundo)."
exit 0
