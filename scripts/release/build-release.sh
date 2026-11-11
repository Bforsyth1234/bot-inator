#!/usr/bin/env bash
# Build, sign, notarize and staple MenuBarUI.app for a GitHub release (#96).
#
# Prep only: the person releasing holds the Apple Developer ID certificate
# and the notarization credentials; this script never asks for or stores
# them. See docs/releasing.md for the one-time setup.
#
#   DEVELOPER_ID="Developer ID Application: Your Name (TEAMID)" \
#   NOTARY_PROFILE=bot-inator-notary \
#   VERSION=0.2.0 \
#   scripts/release/build-release.sh
#
# Without DEVELOPER_ID it stops after an unsigned build (a dry run).
# Output: dist/bot-inator-<VERSION>.zip (stapled app inside).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="$ROOT/MenuBarUI/MenuBarUI"
DIST="$ROOT/dist"
ENTITLEMENTS="$ROOT/scripts/release/MenuBarUI.entitlements"
VERSION="${VERSION:-$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$SRC/Info.plist")}"
BUILD_NUMBER="${BUILD_NUMBER:-$(git -C "$ROOT" rev-list --count HEAD)}"
BUNDLE_ID="${BUNDLE_ID:-com.botinator.MenuBarUI}"
APP="$DIST/bot-inator.app"
ZIP="$DIST/bot-inator-$VERSION.zip"

say() { printf '==> %s\n' "$*"; }
fail() { printf 'build-release.sh: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -m)" == "arm64" ]] || fail "build on Apple Silicon (the app targets arm64)"
if [[ -n "$(git -C "$ROOT" status --porcelain)" && "${ALLOW_DIRTY:-0}" != "1" ]]; then
  fail "the working tree has uncommitted changes; commit them or set ALLOW_DIRTY=1"
fi

# ---- 1. build (release: -O, version stamped) -------------------------------
say "Building bot-inator $VERSION ($BUILD_NUMBER)"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
sources=()
while IFS= read -r -d '' f; do sources+=("$f"); done \
  < <(cd "$SRC" && find . -name '*.swift' -not -path './build/*' -print0)
(cd "$SRC" && xcrun swiftc -O -parse-as-library -target arm64-apple-macos14 \
  -o "$APP/Contents/MacOS/MenuBarUI" "${sources[@]}")
# shellcheck disable=SC2016  # the $(…) below are literal Xcode variables
sed -e 's/$(DEVELOPMENT_LANGUAGE)/en/' -e 's/$(EXECUTABLE_NAME)/MenuBarUI/' \
  -e "s/\$(PRODUCT_BUNDLE_IDENTIFIER)/$BUNDLE_ID/" \
  -e 's/$(PRODUCT_BUNDLE_PACKAGE_TYPE)/APPL/' -e 's/$(PRODUCT_NAME)/bot-inator/' \
  "$SRC/Info.plist" > "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $BUILD_NUMBER" "$APP/Contents/Info.plist"
plutil -lint "$APP/Contents/Info.plist" >/dev/null

if [[ -z "${DEVELOPER_ID:-}" ]]; then
  say "DEVELOPER_ID not set: unsigned build at $APP (dry run, stopping here)"
  exit 0
fi

# ---- 2. sign with Developer ID + hardened runtime ---------------------------
security find-identity -v -p codesigning | grep -qF "$DEVELOPER_ID" \
  || fail "no signing identity \"$DEVELOPER_ID\" in your keychain (see docs/releasing.md)"
say "Signing with $DEVELOPER_ID (hardened runtime)"
codesign --force --timestamp --options runtime \
  --entitlements "$ENTITLEMENTS" --sign "$DEVELOPER_ID" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

# ---- 3. notarize and staple ---------------------------------------------------
[[ -n "${NOTARY_PROFILE:-}" ]] || fail "set NOTARY_PROFILE (xcrun notarytool store-credentials, see docs/releasing.md)"
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"
say "Submitting to Apple's notary service (a few minutes)"
xcrun notarytool submit "$ZIP" --keychain-profile "$NOTARY_PROFILE" --wait
say "Stapling the ticket"
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"
# Re-zip so the download carries the stapled app.
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"

# ---- 4. Gatekeeper check ------------------------------------------------------
spctl --assess --type execute --verbose=2 "$APP"
say "Ready: $ZIP"
shasum -a 256 "$ZIP"
