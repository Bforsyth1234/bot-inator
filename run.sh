#!/usr/bin/env bash
# Start bot-inator: the daemon in this terminal, then the menu bar app (#92).
# Ctrl-C stops the daemon. Run ./install.sh first.
#
#   ./run.sh
#
# Installs nothing and adds no login item.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON="$ROOT/agent-daemon"
APP="$ROOT/MenuBarUI/MenuBarUI/build/MenuBarUI.app"
# The menu bar app connects to 8000; another port is for testing the daemon.
PORT="${BOT_INATOR_PORT:-8000}"
OPEN_APP="${BOT_INATOR_OPEN_APP:-1}"

fail() { printf 'run.sh: %s\n' "$*" >&2; exit 1; }

[[ -x "$DAEMON/.venv/bin/uvicorn" ]] || fail "not installed yet; run ./install.sh"
if [[ "$OPEN_APP" == "1" && ! -d "$APP" ]]; then
  fail "MenuBarUI.app isn't built; run ./install.sh"
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  holder="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN | awk 'NR==2 {print $1" (pid "$2")"}')"
  fail "port $PORT is already in use by $holder. Stop it, or if it's an earlier bot-inator, just open the app."
fi

cd "$DAEMON"

# Local models download on first start: say how much, and what's missing.
if [[ "${AGENT_EAGER_LOAD:-1}" != "0" ]]; then
  .venv/bin/python - <<'PY' || true
from pathlib import Path
from config import settings

roles = {"main": settings.model_path, "analysis": settings.analysis_model_path,
         "drafting": settings.drafting_model_path}
local = [(r, m) for r, m in roles.items() if settings.engine_provider(r) == "local"]
hub = Path.home() / ".cache" / "huggingface" / "hub"
missing = [m for _, m in local
           if not (hub / ("models--" + m.replace("/", "--"))).is_dir()]
if missing:
    print("==> First start downloads these local models from Hugging Face:")
    for m in missing:
        print("      " + m)
    print("    The stock set is about 12.7 GB on disk and in memory. To use a")
    print("    cloud key instead, pick it in Settings (the brain icon, then ⌘,).")
PY
fi

printf '==> Starting the daemon on 127.0.0.1:%s (Ctrl-C to stop)\n' "$PORT"
.venv/bin/uvicorn main:app --host 127.0.0.1 --port "$PORT" &
daemon=$!
trap 'kill "$daemon" 2>/dev/null; wait "$daemon" 2>/dev/null' INT TERM EXIT

# Wait for /health (model loading can take a few minutes the first time).
for _ in $(seq 1 600); do
  if curl -fsS -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    if [[ "$OPEN_APP" == "1" ]]; then
      printf '==> Daemon is up; opening the menu bar app\n'
      open "$APP"
    else
      printf '==> Daemon is up\n'
    fi
    break
  fi
  kill -0 "$daemon" 2>/dev/null || fail "the daemon exited during start-up (see the log above)"
  sleep 1
done

wait "$daemon"
