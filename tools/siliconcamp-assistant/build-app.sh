#!/bin/bash
#
# Build the local SiliconCamp Assistant.app.  The app is a window around the
# existing loopback server; it does not embed Python or the repository.
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
APP="$HERE/build/SiliconCamp Assistant.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"

rm -rf "$APP"
mkdir -p "$MACOS" "$RESOURCES"

swiftc \
  -O \
  -whole-module-optimization \
  -framework Cocoa \
  -framework WebKit \
  -o "$MACOS/SiliconCampAssistant" \
  "$HERE/mac/SiliconCampAssistant.swift"

cp "$HERE/mac/Info.plist" "$CONTENTS/Info.plist"

if [ -f "$HERE/mac/AppIcon.icns" ]; then
  cp "$HERE/mac/AppIcon.icns" "$RESOURCES/AppIcon.icns"
elif [ -f "$HERE/mac/AppIcon.png" ]; then
  ICONSET="$HERE/build/AppIcon.iconset"
  rm -rf "$ICONSET"
  mkdir -p "$ICONSET"
  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$HERE/mac/AppIcon.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z "$double" "$double" "$HERE/mac/AppIcon.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$RESOURCES/AppIcon.icns"
  rm -rf "$ICONSET"
fi

# Ad-hoc signing keeps the local bundle coherent for WKWebView and Gatekeeper.
codesign --force --sign - "$APP" >/dev/null 2>&1 || true

echo "$APP"
