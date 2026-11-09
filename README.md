# bot-inator

A local-first macOS agent that watches what you do and offers to help.

It notices activity on your Mac (app switches, new files in Downloads,
incoming iMessages), asks an LLM *"how could I help here?"*, and proposes
actions or brand-new tools it writes itself. **Nothing runs without your
approval**: every action and every line of generated code goes through a
menu bar prompt first.

## How it fits together

| Part | What it is |
|---|---|
| `agent-daemon/` | Python service (FastAPI on `127.0.0.1:8000`). Listens to system events, runs the LLM, holds the tools. |
| `MenuBarUI/` | SwiftUI menu bar app. Shows the agent's thoughts, asks for approvals, chat, tool manager, settings. |

The two talk over a WebSocket (`/ws/stream`) plus a small HTTP API. See
[ARCHITECTURE.md](ARCHITECTURE.md) for diagrams and
[LIVING_SPEC.md](LIVING_SPEC.md) for the full protocol and design history.

## Requirements

- macOS 14+ on Apple Silicon (local models use MLX)
- Python 3 (developed and tested on 3.14)
- Xcode command line tools (`xcode-select --install`) to build the app
- Optional: a [Groq API key](https://console.groq.com/keys) to run models
  in the cloud instead of on your Mac

## Quick start

### 1. Start the daemon

```bash
cd agent-daemon
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
```

With the default local provider, the first start downloads three MLX models
from Hugging Face (several GB) and can take a few minutes.

### 2. Build and launch the menu bar app

There is no Xcode project in the repo; build it straight from the sources:

```bash
cd MenuBarUI/MenuBarUI
mkdir -p build/MenuBarUI.app/Contents/MacOS
xcrun swiftc -O -parse-as-library -target arm64-apple-macos14 \
  -o build/MenuBarUI.app/Contents/MacOS/MenuBarUI $(find . -name '*.swift')
sed -e 's/$(DEVELOPMENT_LANGUAGE)/en/' -e 's/$(EXECUTABLE_NAME)/MenuBarUI/' \
  -e 's/$(PRODUCT_BUNDLE_IDENTIFIER)/com.botinator.MenuBarUI/' \
  -e 's/$(PRODUCT_BUNDLE_PACKAGE_TYPE)/APPL/' -e 's/$(PRODUCT_NAME)/MenuBarUI/' \
  Info.plist > build/MenuBarUI.app/Contents/Info.plist
codesign --force --sign - build/MenuBarUI.app
open build/MenuBarUI.app
```

A brain icon appears in the menu bar. It connects to the daemon on its own
and reconnects if the daemon restarts.

### 3. Pick a model provider

Click the brain icon → **Settings…** (⌘,):

- **Local (MLX)**: everything stays on your Mac. Default.
- **Groq (cloud)**: faster and stronger models. Paste your API key, click
  **Save**, then **Restart Daemon**. With Groq, event summaries, chat and
  tool drafts are sent to Groq.

Settings are saved to `~/.bot-inator/settings.json` (readable only by you).
They override `.env`, and variables exported in the daemon's shell override
both.

| Engine | Local default | Groq default |
|---|---|---|
| Main (agent loop, idea detection) | Hermes-3-Llama-3.1-8B-4bit | `openai/gpt-oss-120b` |
| Analysis (event summaries) | Qwen2.5-1.5B-Instruct-4bit | `openai/gpt-oss-20b` |
| Drafting (writing new tools) | Qwen2.5-Coder-14B-Instruct-4bit | `openai/gpt-oss-120b` |

## macOS permissions

- **Full Disk Access** for the terminal or process running the daemon,
  so the iMessage watcher can read `~/Library/Messages/chat.db`. Without
  it, that listener logs an error and the rest keeps working.
- Notification and Automation prompts may appear the first time a tool
  that needs them runs.

## Using it

- **Thoughts** tab: a live log of what the agent noticed and decided.
- **Approvals**: a window pops up before any tool runs. Approve, deny, or
  add a note.
- **Code review**: when the agent writes a new tool, you see the full
  source and approve it before it's installed.
- **Chat…** (⇧⌘C): ask for things directly.
- **Manage Tools…** (⇧⌘T): see built-in and generated tools; delete
  generated ones.

Generated tools live in `agent-daemon/tools/generated/`, which is its own
nested git repo so every installed tool is versioned.

## Configuration

Everything else is set in `agent-daemon/.env`. See
[`.env.example`](agent-daemon/.env.example) for all options with comments.
Useful ones:

| Variable | Default | Effect |
|---|---|---|
| `AGENT_PROVIDER` | `local` | `local` or `groq` (also settable in the UI) |
| `AGENT_OPEN_HELP_INTERVAL` | `600` | Seconds between open-ended "how could I help?" checks |
| `AGENT_OPEN_HELP_MIN_SCORE` | `7` | Judge score (0-10) an idea needs before you see it |
| `AGENT_DRAFT_CRITIC` | `1` | Review each drafted tool against its spec |
| `AGENT_DRAFT_DRY_RUN` | `1` | Dry-run each drafted tool in a sandbox |
| `AGENT_DISABLE_LISTENERS` | unset | `1` turns off all system event listeners |
| `AGENT_LOG_LEVEL` | `INFO` | Daemon log level |

## Development

```bash
cd agent-daemon
.venv/bin/python -m pytest -q
```

Tests run with listeners off and the local provider pinned, and never read
your real `.env` provider or `~/.bot-inator/settings.json`.
