#!/usr/bin/env bash
# Put the daemon and its own Python inside bot-inator.app (#96), so the app
# can start it and macOS permissions belong to the app, not a terminal.
#
#   scripts/release/bundle-daemon.sh dist/bot-inator.app
#
# Called by build-release.sh after the Swift build and before signing; it
# signs nothing itself. Layout it makes:
#
#   Contents/Resources/python/        relocatable CPython (python-build-standalone)
#                                     plus the daemon's packages in site-packages
#   Contents/Resources/agent-daemon/  the daemon's sources (no tests, no evals)
#
# Python: python-build-standalone's "install_only_stripped" CPython, pinned
# by release tag and SHA-256 below and cached in dist/cache/. It is the
# smallest option that loads MLX (see #96 Results in todo.md): 69 MB
# unpacked, self-contained (links only system libraries), and pip installs
# the same wheels as the dev venv into it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP="${1:?usage: bundle-daemon.sh <path to bot-inator.app>}"
RES="$APP/Contents/Resources"
PY="$RES/python"
DAEMON="$RES/agent-daemon"
CACHE="$ROOT/dist/cache"

# Pinned CPython. To move to a newer build, change all four lines; the
# SHA-256 is the "digest" GitHub lists for the asset (also in SHA256SUMS).
PBS_TAG="20261026"
PBS_PY="3.14.10"
PBS_SHA256="95eb8c87d16af37c54e9765e31d8c56c0d8ffa9968ea0f704f73a9b83f84e9f9"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG/cpython-$PBS_PY%2B$PBS_TAG-aarch64-apple-darwin-install_only_stripped.tar.gz"
PY_MINOR="${PBS_PY%.*}"
SITE="$PY/lib/python$PY_MINOR/site-packages"

# requirements.txt lines left out of the bundle: nothing in the daemon
# imports them (checked with grep over agent-daemon/, tests excluded), and
# playwright alone is ~160 MB plus browsers it would download.
EXCLUDE_REQS='^(browser-use|playwright)([<>=! ;\[]|$)'

say() { printf '==> %s\n' "$*"; }
fail() { printf 'bundle-daemon.sh: %s\n' "$*" >&2; exit 1; }

[[ -d "$APP/Contents" ]] || fail "$APP is not an app bundle (build it first)"

# ---- 1. CPython, downloaded once and checked ---------------------------------
tarball="$CACHE/cpython-$PBS_PY+$PBS_TAG-aarch64-apple-darwin-install_only_stripped.tar.gz"
mkdir -p "$CACHE"
if [[ ! -f "$tarball" ]]; then
  say "Downloading CPython $PBS_PY ($PBS_TAG) from python-build-standalone"
  curl -fL --retry 3 -o "$tarball.part" "$PBS_URL"
  mv "$tarball.part" "$tarball"
fi
actual="$(shasum -a 256 "$tarball" | awk '{print $1}')"
if [[ "$actual" != "$PBS_SHA256" ]]; then
  rm -f "$tarball"
  fail "SHA-256 mismatch for the CPython download (got $actual); deleted it, try again"
fi

rm -rf "$PY" "$DAEMON"
mkdir -p "$PY" "$DAEMON"
tar -xzf "$tarball" -C "$PY" --strip-components 1
[[ -x "$PY/bin/python3" ]] || fail "unexpected archive layout (no python/bin/python3)"

# ---- 2. the daemon's packages ---------------------------------------------------
reqs="$(mktemp -t bot-inator-reqs)"
trap 'rm -f "$reqs"' EXIT
grep -v -E "$EXCLUDE_REQS" "$ROOT/agent-daemon/requirements.txt" > "$reqs"
say "Installing the daemon's packages into the bundled Python (several minutes the first time)"
# --prefer-binary: watchdog 5.x has no cp314 wheel and builds from source
# (needs the Xcode command line tools on the build Mac, not the user's).
"$PY/bin/python3" -I -m pip install --quiet --disable-pip-version-check \
  --no-warn-script-location --no-compile --prefer-binary -r "$reqs"

# ---- 3. the daemon's sources (tracked files only: never .env or generated tools)
say "Copying agent-daemon sources"
(cd "$ROOT/agent-daemon" && git ls-files -z -- . ':!:tests/**' ':!:evals/**' \
  ':!:tools/generated/**' ':!:screenshot.png' \
  | rsync -a --from0 --files-from=- ./ "$DAEMON/")
[[ -f "$DAEMON/main.py" ]] || fail "daemon sources missing from the bundle"
[[ ! -e "$DAEMON/.env" ]] || fail "a .env reached the bundle"

# ---- 4. prune what the daemon never uses -----------------------------------------
# pip/ensurepip (no installs at run time), Tk and IDLE, headers, and bin/
# scripts whose #! lines point at this build machine.
rm -rf "$SITE/pip" "$SITE"/pip-*.dist-info "$SITE/PyObjCTest" \
  "$PY/lib/python$PY_MINOR"/{ensurepip,idlelib,tkinter,turtledemo} \
  "$PY/lib/python$PY_MINOR"/lib-dynload/_tkinter.*.so \
  "$PY"/lib/{itcl*,tcl*,tk*,thread*,libtcl*} "$PY/include" "$PY/share"
find "$PY/bin" -mindepth 1 ! -name 'python3*' ! -name python -delete
find "$PY" "$DAEMON" -name __pycache__ -type d -prune -exec rm -rf {} +

# ---- 5. bytecode, compiled now so the signed bundle is never written to --------
# unchecked-hash .pyc files are used without comparing source mtimes (a zip
# or copy can change those); the app starts Python with -B, so it never
# writes new ones into the bundle and breaks the seal.
say "Compiling bytecode"
"$PY/bin/python3" -I -m compileall -q -j 0 --invalidation-mode unchecked-hash \
  "$PY/lib" "$DAEMON" >/dev/null 2>&1 || say "compileall skipped some files (test data with old syntax); fine"

# ---- 6. it loads ------------------------------------------------------------------
"$PY/bin/python3" -I -B -c 'import mlx.core as mx; assert (mx.array([2.0]) * 3).item() == 6.0' \
  || fail "the bundled Python cannot import mlx.core"
say "Bundled Python $PBS_PY: $(du -sh "$PY" | cut -f1), daemon sources: $(du -sh "$DAEMON" | cut -f1)"
