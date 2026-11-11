#!/usr/bin/env bash
# Check a signed bot-inator.app's bundled daemon (#96): the signed Python
# loads MLX, PyObjC, torch and sqlite-vec under the hardened runtime, and
# the daemon starts and answers /health.
#
#   scripts/release/smoke-test.sh dist/bot-inator.app
#
# Runs against a throwaway HOME on a free port (never 8000), with listeners
# off and no model loading, so it touches nothing of yours and downloads
# nothing. The daemon command line matches DaemonLauncher.swift.
set -euo pipefail

APP="${1:?usage: smoke-test.sh <path to bot-inator.app>}"
RES="$(cd "$APP/Contents/Resources" && pwd)"
PY="$RES/python/bin/python3"
DAEMON="$RES/agent-daemon"

say() { printf '==> %s\n' "$*"; }
fail() { printf 'smoke-test.sh: %s\n' "$*" >&2; exit 1; }

[[ -x "$PY" && -f "$DAEMON/main.py" ]] || fail "$APP has no bundled daemon"

scratch="$(mktemp -d -t bot-inator-smoke)"
daemon_pid=""
# shellcheck disable=SC2329  # called by the EXIT trap
cleanup() {
  if [[ -n "$daemon_pid" ]]; then
    kill "$daemon_pid" 2>/dev/null || true
    wait "$daemon_pid" 2>/dev/null || true
  fi
  rm -rf "$scratch"
}
trap cleanup EXIT

say "Signed Python loads its native modules"
(cd "$scratch" && HOME="$scratch" "$PY" -I -B -c '
import ctypes, sqlite3
import mlx.core as mx
assert (mx.array([1.0, 2.0]) * 2).sum().item() == 6.0
import objc, Foundation, EventKit, sqlite_vec, torch, uvicorn, fastapi, smolagents, mlx_lm
c = sqlite3.connect(":memory:"); c.enable_load_extension(True); sqlite_vec.load(c)
cb = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)(lambda x: x + 1)
assert cb(1) == 2
print("    mlx", mx.__version__, "on", mx.default_device(), "| torch", torch.__version__)
') || fail "the signed Python could not load its modules (library validation or entitlements)"

port="$("$PY" -I -B -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
[[ "$port" != "8000" ]] || fail "refusing port 8000"
say "Starting the bundled daemon on 127.0.0.1:$port (throwaway HOME)"
mkdir -p "$scratch/data"
touch "$scratch/started"
(cd "$scratch/data" && exec env HOME="$scratch" AGENT_DATA_DIR="$scratch/data" \
  AGENT_DISABLE_LISTENERS=1 AGENT_EAGER_LOAD=0 PYTHONDONTWRITEBYTECODE=1 \
  "$PY" -I -B -m uvicorn main:app --app-dir "$DAEMON" \
  --host 127.0.0.1 --port "$port" >"$scratch/daemon.log" 2>&1) &
daemon_pid=$!
for _ in $(seq 1 120); do
  if curl -fsS -m 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
    say "/health answered: $(curl -fsS -m 2 "http://127.0.0.1:$port/health")"
    [[ -d "$scratch/data/tools/generated" ]] || fail "the daemon did not make its generated-tools dir in AGENT_DATA_DIR"
    written="$(find "$RES" -newer "$scratch/started" | head -3)"
    if [[ -n "$written" ]]; then
      fail "the daemon wrote inside the app bundle: $written"
    fi
    say "Smoke test passed"
    exit 0
  fi
  kill -0 "$daemon_pid" 2>/dev/null || { cat "$scratch/daemon.log" >&2; fail "the daemon exited during start-up"; }
  sleep 1
done
cat "$scratch/daemon.log" >&2
fail "no /health answer within 120 s"
