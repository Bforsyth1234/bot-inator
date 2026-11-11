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
# The app carries its own daemon: bundle-daemon.sh puts a relocatable
# CPython and the daemon's sources in Contents/Resources (BUNDLE_DAEMON=0
# builds the menu bar app alone, as before). Every Mach-O is signed
# inside-out with the hardened runtime, then smoke-test.sh checks that the
# signed Python loads MLX and the daemon answers /health.
#
# Without DEVELOPER_ID it signs ad hoc, verifies and smoke-tests, then stops
# before notarizing (a dry run).
# Output: dist/bot-inator-<VERSION>.zip (stapled app inside).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="$ROOT/MenuBarUI/MenuBarUI"
DIST="$ROOT/dist"
ENTITLEMENTS="$ROOT/scripts/release/MenuBarUI.entitlements"
PY_ENTITLEMENTS="$ROOT/scripts/release/Python.entitlements"
PY_ADHOC_ENTITLEMENTS="$ROOT/scripts/release/Python-adhoc.entitlements"
BUNDLE_DAEMON="${BUNDLE_DAEMON:-1}"
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

if [[ "$BUNDLE_DAEMON" == "1" ]]; then
  "$ROOT/scripts/release/bundle-daemon.sh" "$APP"
fi

# ---- 2. sign inside-out with the hardened runtime ----------------------------
# Every Mach-O in Resources (the bundled Python: ~430 executables, .so and
# .dylib files) gets its own signature, deepest first; the Python binary
# gets Python.entitlements; then the app is sealed with its own.
if [[ -n "${DEVELOPER_ID:-}" ]]; then
  security find-identity -v -p codesigning | grep -qF "$DEVELOPER_ID" \
    || fail "no signing identity \"$DEVELOPER_ID\" in your keychain (see docs/releasing.md)"
  identity="$DEVELOPER_ID"
  sign_flags=(--force --timestamp --options runtime)
  py_entitlements="$PY_ENTITLEMENTS"
  say "Signing with $DEVELOPER_ID (hardened runtime)"
else
  # Ad hoc: a local check of the bundle, never released. Ad-hoc code has no
  # Team ID, so the bundled Python needs disable-library-validation here.
  identity="-"
  sign_flags=(--force --options runtime)
  py_entitlements="$PY_ADHOC_ENTITLEMENTS"
  say "DEVELOPER_ID not set: signing ad hoc (dry run)"
fi
py_bin="$APP/Contents/Resources/python/bin"
libs=()
execs=()
while IFS= read -r -d '' f; do
  if [[ "$(dirname "$f")" == "$py_bin" ]]; then execs+=("$f"); else libs+=("$f"); fi
done < <(/usr/bin/python3 -I "$ROOT/scripts/release/list-macho.py" "$APP/Contents/Resources")
signlog="$(mktemp -t bot-inator-sign)"
# Wheels arrive ad-hoc signed by the linker, so codesign says "replacing
# existing signature" ~280 times; keep its output only if it fails.
if ((${#libs[@]})) && ! printf '%s\0' "${libs[@]}" \
    | xargs -0 -n 50 codesign "${sign_flags[@]}" --sign "$identity" 2>"$signlog"; then
  grep -v 'replacing existing signature' "$signlog" >&2
  fail "signing the bundled Python's libraries failed"
fi
if ((${#execs[@]})) && ! codesign "${sign_flags[@]}" --entitlements "$py_entitlements" \
    --sign "$identity" "${execs[@]}" 2>"$signlog"; then
  grep -v 'replacing existing signature' "$signlog" >&2
  fail "signing the bundled Python failed"
fi
rm -f "$signlog"
codesign "${sign_flags[@]}" --entitlements "$ENTITLEMENTS" --sign "$identity" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
say "Signed $((${#libs[@]} + ${#execs[@]})) nested Mach-O files and the app"

if [[ "$BUNDLE_DAEMON" == "1" && "${SMOKE_TEST:-1}" == "1" ]]; then
  "$ROOT/scripts/release/smoke-test.sh" "$APP"
fi
if [[ -z "${DEVELOPER_ID:-}" ]]; then
  say "Ad-hoc build at $APP ($(du -sh "$APP" | cut -f1)); stopping before notarization"
  exit 0
fi

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
