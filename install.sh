#!/usr/bin/env bash
# Install bot-inator from this clone: the daemon's Python environment and
# the menu bar app (#92). Safe to run again: it only (re)does what's needed.
#
#   ./install.sh
#
# Nothing is downloaded here beyond Python packages. With the local
# provider, the models download when the daemon first starts (./run.sh).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON="$ROOT/agent-daemon"
APP_SRC="$ROOT/MenuBarUI/MenuBarUI"
APP="$APP_SRC/build/MenuBarUI.app"

say() { printf '==> %s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

# ---- 1. requirements, each missing one named on its own line -------------
missing=()
if [[ "$(uname -s)" != "Darwin" ]]; then
  missing+=("macOS 14 or later (this is $(uname -s))")
else
  major="$(sw_vers -productVersion | cut -d. -f1)"
  ((major >= 14)) || missing+=("macOS 14 or later (this Mac runs $(sw_vers -productVersion))")
  [[ "$(uname -m)" == "arm64" ]] || missing+=("an Apple Silicon Mac (this is $(uname -m); local models use MLX)")
  xcode-select -p >/dev/null 2>&1 || missing+=("Xcode command line tools: run  xcode-select --install")
fi
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  missing+=("Python 3.11 or later: e.g.  brew install python")
elif ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
  missing+=("Python 3.11 or later ($PYTHON is $("$PYTHON" -V 2>&1))")
fi
if ((${#missing[@]})); then
  printf 'install.sh: missing:\n' >&2
  printf '  - %s\n' "${missing[@]}" >&2
  exit 1
fi

# ---- 2. the daemon's virtualenv and packages ------------------------------
cd "$DAEMON"
if [[ ! -x .venv/bin/python ]]; then
  say "Creating agent-daemon/.venv"
  "$PYTHON" -m venv .venv
fi
say "Installing Python packages (agent-daemon/requirements.txt)"
if [[ -x .venv/bin/pip ]]; then
  .venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt
elif command -v uv >/dev/null 2>&1; then
  # A venv made by uv has no pip.
  uv pip install --quiet --python .venv/bin/python -r requirements.txt
else
  fail "agent-daemon/.venv has no pip and uv isn't installed; delete agent-daemon/.venv and run again"
fi

if [[ ! -f .env ]]; then
  say "Creating agent-daemon/.env from .env.example"
  cp .env.example .env
fi

# ---- 3. the menu bar app ----------------------------------------------------
cd "$APP_SRC"
say "Building MenuBarUI.app (swiftc, about a minute)"
mkdir -p "$APP/Contents/MacOS"
sources=()
while IFS= read -r -d '' f; do sources+=("$f"); done \
  < <(find . -name '*.swift' -not -path './build/*' -print0)
xcrun swiftc -O -parse-as-library -target arm64-apple-macos14 \
  -o "$APP/Contents/MacOS/MenuBarUI" "${sources[@]}"
# Info.plist uses Xcode build variables; fill them in.
# shellcheck disable=SC2016  # the $(…) below are literal Xcode variables
sed -e 's/$(DEVELOPMENT_LANGUAGE)/en/' -e 's/$(EXECUTABLE_NAME)/MenuBarUI/' \
  -e 's/$(PRODUCT_BUNDLE_IDENTIFIER)/com.botinator.MenuBarUI/' \
  -e 's/$(PRODUCT_BUNDLE_PACKAGE_TYPE)/APPL/' -e 's/$(PRODUCT_NAME)/MenuBarUI/' \
  Info.plist > "$APP/Contents/Info.plist"
codesign --force --sign - "$APP" 2>/dev/null
codesign --verify "$APP"

say "Installed. Start it with:  ./run.sh"
