# bot-inator

A local-first macOS agent that watches what you do and offers to help.
Nothing runs without your approval.

<!-- #91: a 30–60 s demo (a PDF lands in Downloads → card → Approve → it is
moved) goes here as docs/demo.gif or .mp4, recorded with no personal text
on screen. -->

It notices activity on your Mac (new files in Downloads, incoming mail and
iMessages, upcoming meetings, app switches), asks an LLM *"how could I help
here?"*, and offers an action, or a small tool it writes itself, as a card
in the menu bar.

What you can check for yourself, in this repo:

- **Every action is a card.** Tools that change something (move a file,
  create a reminder or event, draft an email) ask first; only read-only
  tools skip the card. See `READ_ONLY_TOOL_NAMES` in
  `agent-daemon/tools/__init__.py`.
- **Generated tools are reviewed, then sandboxed.** You read the full
  source before a tool is installed, and each call runs in a short-lived
  child under macOS `sandbox-exec`
  ([How generated tools run](#how-generated-tools-run)).
- **Local by default.** Models run on your Mac with MLX. A cloud provider
  is opt-in, and email and message text stay on the Mac unless you also
  allow them to leave. Settings → Privacy ledger lists what left today.

MIT licensed ([LICENSE](LICENSE)).

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

If the venv was made with `uv` (it has no `pip`), install with
`uv pip install --python .venv/bin/python -r requirements.txt` instead.
Vector memory search needs `sqlite-vec` (in requirements). If the log says
"Vector memory search disabled (sqlite3 built without load_extension
support)", also install `pysqlite3-binary`. Without either, recall falls
back to the most recent memories.

With the default local provider, the first start downloads three MLX models
from Hugging Face (several GB) and can take a few minutes.

The first start also creates `~/.bot-inator/token` (readable only by you).
Every route except `GET /health`, and the WebSocket, needs it as a bearer
token; the menu bar app reads it automatically. Requests that carry a
browser `Origin` header are refused, so web pages can't reach the daemon.
For `curl`:

```bash
curl -H "Authorization: Bearer $(cat ~/.bot-inator/token)" http://127.0.0.1:8000/api/tools
```

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
- **Calendars** belong to the menu bar app, not the daemon: Calendar tools
  and the meeting watcher work only while the app is running and
  connected (otherwise they answer "open the bot-inator menu bar app to use
  Calendar"). The app asks for Calendar access the first time the agent
  needs it. An ad-hoc signed rebuild may ask again.
  `AGENT_CALENDAR_BACKEND=applescript` (or `eventkit`) pins the old
  in-daemon paths.
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

### How generated tools run

An installed generated tool does not run inside the daemon. Each call
starts a short-lived Python child under macOS `sandbox-exec`:

- it can write under your home folder, but not in `~/Library`, `~/.ssh`,
  `~/.bot-inator`, your shell start-up files, or this repo;
- it can't read `~/.ssh`, `~/.bot-inator` (API token, memory) or
  `agent-daemon/.env`, and gets no API keys in its environment;
- it has no network unless its source imports a network module
  (`urllib`, `http`, `socket`, `ssl`, …). The tool's `sandbox_note` says
  which;
- it can only run `osascript`, `open`, `shortcuts`, `mdfind` and Python;
- a call that takes longer than `AGENT_TOOL_SANDBOX_TIMEOUT` seconds
  (default 60) is stopped.

You still approve every call. Built-in tools a generated tool calls
(e.g. `show_notification`) run inside the same sandbox without a second
approval. If `sandbox-exec` is missing or fails its self-test, generated
tools are not loaded. `AGENT_TOOL_SANDBOX=0` turns the sandbox off and
runs them in the daemon process as before.

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
| `AGENT_TOOL_SANDBOX` | `1` | `0` runs installed generated tools in the daemon process, unsandboxed |
| `AGENT_DISABLE_LISTENERS` | unset | `1` turns off all system event listeners |
| `AGENT_LOG_LEVEL` | `INFO` | Daemon log level |

## Development

```bash
cd agent-daemon
.venv/bin/python -m pytest -q
```

Tests run with listeners off and the local provider pinned, and never read
your real `.env` provider or `~/.bot-inator/settings.json`.
