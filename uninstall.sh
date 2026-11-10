#!/bin/bash
# Removes Shortcuts and its hook line from ~/.config/hypr/hyprland.lua.
# Your shortcuts.json / shortcuts.lua stay in ~/.config/hypr unless you pass
# --purge, so reinstalling brings your shortcuts back.

set -euo pipefail

APP_ID="io.github.markosamo.Shortcuts"
HYPR="$HOME/.config/hypr"
CONFIG="$HYPR/hyprland.lua"
MARKER="-- fluid-shortcuts (remove with uninstall.sh)"

if [[ -f $CONFIG ]] && grep -qF -- "$MARKER" "$CONFIG"; then
  cp "$CONFIG" "$CONFIG.bak.$(date +%s)"
  # Drop the marker and the hook line right after it.
  awk -v m="$MARKER" '$0 == m { skip = 1; next } skip { skip = 0; next } { print }' "$CONFIG" >"$CONFIG.tmp"
  mv "$CONFIG.tmp" "$CONFIG"
  echo "Removed the hook from $CONFIG (backup saved next to it)."
fi

if [[ ${1:-} == "--purge" ]]; then
  rm -f "$HYPR/shortcuts.lua" "$HYPR/shortcuts.json"
  echo "Deleted shortcuts.json and shortcuts.lua."
fi

rm -f "$HOME/.local/bin/shortcuts" \
      "$HOME/.local/share/applications/$APP_ID.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/$APP_ID.svg"
rm -rf "$HOME/.local/share/fluid-shortcuts"

command -v hyprctl >/dev/null && hyprctl reload >/dev/null || true
echo "Shortcuts uninstalled."
