#!/usr/bin/env bash
# Build VoiceLoop.app from the SPM executable target: compile, assemble the
# bundle (Info.plist + entitlements + the AppleScript resource bundle), and
# ad-hoc sign it. Direct notarized distribution replaces this signature for a
# real release; ad-hoc is enough to run locally and to grant TCC.
set -euo pipefail

cd "$(dirname "$0")"
CONFIG="${1:-release}"
APP="build/VoiceLoop.app"

echo "› swift build -c $CONFIG --product VoiceLoop"
swift build -c "$CONFIG" --product VoiceLoop
BIN_DIR="$(swift build -c "$CONFIG" --show-bin-path)"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp "$BIN_DIR/VoiceLoop" "$APP/Contents/MacOS/VoiceLoop"
cp App/Info.plist "$APP/Contents/Info.plist"

# The SPM resource bundle carries the AppleScript templates; Bundle.module
# resolves it from the app's Resources.
RES_BUNDLE="VoiceLoop_VoiceLoopEngine.bundle"
if [ -d "$BIN_DIR/$RES_BUNDLE" ]; then
  cp -R "$BIN_DIR/$RES_BUNDLE" "$APP/Contents/Resources/$RES_BUNDLE"
fi

echo "› codesign (ad-hoc, hardened runtime, AppleEvents entitlement)"
codesign --force --deep --options runtime \
  --entitlements App/VoiceLoop.entitlements \
  --sign - "$APP"

echo "✓ built $APP"
