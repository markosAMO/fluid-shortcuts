#!/bin/bash
# Installs Shortcuts for the current user: the app under ~/.local/share,
# the `shortcuts` command, the launcher entry and the icon. Safe to run again.
#
# Hyprland's config is not touched here: the app adds its one hook line to
# ~/.config/hypr/hyprland.lua (with a backup) the first time you save a change.

set -euo pipefail

APP_ID="io.github.markosamo.Shortcuts"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.local/share/fluid-shortcuts"
BIN="$HOME/.local/bin/shortcuts"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
DESKTOP="$HOME/.local/share/applications/$APP_ID.desktop"
FILES=(shortcuts.py scan.lua "$APP_ID.svg" install.sh uninstall.sh)

missing=()
command -v lua >/dev/null || missing+=("lua")
command -v hyprctl >/dev/null || missing+=("hyprland")
python3 - <<'PY' 2>/dev/null || missing+=("python-gobject + gtk4 + libadwaita >= 1.7")
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 7)
PY
if (( ${#missing[@]} )); then
  echo "Missing: ${missing[*]}" >&2
  exit 1
fi
[[ -f $HOME/.config/hypr/hyprland.lua ]] || { echo "Cannot find ~/.config/hypr/hyprland.lua (this needs Omarchy with the Lua Hyprland config)." >&2; exit 1; }

mkdir -p "$DEST" "$(dirname "$BIN")" "$ICON_DIR" "$(dirname "$DESKTOP")"
if [[ $SRC != "$DEST" ]]; then
  for f in "${FILES[@]}"; do cp "$SRC/$f" "$DEST/"; done
fi
chmod +x "$DEST/shortcuts.py" "$DEST/install.sh" "$DEST/uninstall.sh"
ln -sf "$DEST/shortcuts.py" "$BIN"
cp "$DEST/$APP_ID.svg" "$ICON_DIR/$APP_ID.svg"

cat >"$DESKTOP" <<DESK
[Desktop Entry]
Type=Application
Name=Shortcuts
GenericName=Keyboard shortcuts
Comment=Manage your Hyprland keyboard shortcuts
Exec=$BIN
Icon=$APP_ID
Terminal=false
Categories=Settings;DesktopSettings;
Keywords=hotkeys;keybindings;keyboard;shortcuts;hyprland;atajos;teclado;
StartupWMClass=$APP_ID
DESK

gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
update-desktop-database -q "$(dirname "$DESKTOP")" 2>/dev/null || true

echo "Installed. Open it from the app launcher (\"Shortcuts\") or run: shortcuts"
