#!/bin/bash
# Build the JarvisContext helper app (Calendar/Reminders/Mail snapshotter).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/build/JarvisContext.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$DIR/JarvisContext/Info.plist" "$APP/Contents/Info.plist"
swiftc -O -o "$APP/Contents/MacOS/JarvisContext" "$DIR/JarvisContext/main.swift"
codesign --force --sign - "$APP"
echo "$APP"
