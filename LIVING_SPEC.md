# Living Spec — bot-inator

Rolling source-of-truth for the goals, architecture and protocol. Open work
lives in [todo.md](todo.md); finished work, with its full write-up, lives in
[DONE.md](DONE.md). Update this file as decisions land.

## 1. Goals

A local-first macOS agent that builds agent workflows to help the user
wherever it can. It observes system events, reasons about them with an
LLM, and proposes tools and actions that the user approves or rejects
from a MenuBar UI. No background action without consent.

**Standing rules (amendment to "no background action without consent",
user decision 2026-11-10, item #78).** Consent is given per action, on an
approval card, or by a standing rule. A standing rule is the only way a
side-effecting action may run without a card, and only within these
limits:

1. The user creates it. The app may offer a rule only after the user has
   approved the same tool for the same kind of trigger at least 5 times
   with no edits and no denials. A model can never create, change or
   enable a rule.
2. It is narrow: one trigger kind, one tool, one fixed argument pattern.
3. It can never cover: sending or drafting messages or email,
   `generate_custom_tool`, generated tools, `run_shortcut`, deleting
   anything, or any action that uses the network.
4. It runs without a model: matching and arguments are deterministic, so
   text in a file, email or web page cannot trigger or steer it.
5. The user can see and revoke it: every rule is listed in Settings with
   on/off and delete; every automatic run posts a notification with Undo
   and is logged; a rule that is undone twice switches itself off.

Everything outside these limits still needs an approval card. Until #78
is built, no standing rules exist and every side-effecting call is gated.

**Guiding question.** For every event, or window of events, the LLM asks:
*"How could I possibly help the user here?"* That question is always
asked. It is never gated on repetition or on a fixed list of event kinds.

**Ask always, surface rarely.** Open-ended generation from a small model
over-proposes (Phase 5.2: "add a screenshot to the calendar"). So the
pipeline has two separate stages:

1. **Generate.** Ask the guiding question freely and produce candidate
   ideas. Generating is cheap and its output is never shown directly.
2. **Filter.** A candidate reaches the user only if it passes a quality
   bar: critic review, static draft checks and a dry run
   (`tools/draft_critic.py`, `tools/draft_checks.py`,
   `tools/draft_dry_run.py`). When in doubt, drop it.
3. **Learn.** Approvals and rejections tune what passes the bar, so
   suggestions fit this user over time.

Known-good follow-ups (`FOLLOW_UPS` in `ai/idea_sources.py`) and the
repeated-workflow detector (`ai/pattern_recognizer.py`) are seed sources
of high-confidence ideas. They are not the only ideas allowed.

Inference runs on-device (MLX) by default. Groq cloud inference is an
opt-in alternative (`AGENT_PROVIDER=groq`, Phase 4.7). With Groq selected,
event summaries, chat and tool drafts leave the machine; nothing else
about the consent model changes.

## 2. Topology

```
┌──────────── MenuBarUI.app (SwiftUI + AppKit) ────────────┐
│  AppDelegate → WebSocketManager → {ThoughtStreamView,    │
│                                    DebugView,            │
│                                    ApprovalPresenter}    │
└──────────────────────────┬───────────────────────────────┘
                           │ ws://127.0.0.1:8000/ws/stream
┌──────────────────────────▼───────────────────────────────┐
│  agent-daemon (FastAPI, uvicorn on 127.0.0.1:8000)       │
│                                                          │
│  EventBus ◄── MacAppListener ──► _mac_app_helper (child) │
│          ◄── FileWatcher     (watchdog)                  │
│          ◄── IMessageWatcher (sqlite ro poll of chat.db) │
│     │                                                    │
│     ▼                                                    │
│  Orchestrator ── smolagents adapter ── engine triplet    │
│        │          (_MLXModel |         (main, analysis,  │
│        │           OpenAIServerModel)   drafting)        │
│        └─ approval-gated tools                │          │
│        └─ subscribers (WebSocket fan-out)     │          │
│        └─ idea sources → pattern_detected     ▼          │
│                              AGENT_PROVIDER=local: mlx-lm│
│                              AGENT_PROVIDER=groq:  Groq  │
└──────────────────────────────────────────────────────────┘
```

`_mac_app_helper` is a dedicated Python child process whose main thread
runs ``NSRunLoop``; it observes
``NSWorkspaceDidActivateApplicationNotification`` and emits one JSON line
per activation on stdout. The daemon never touches ``NSWorkspace`` in-process
because uvicorn owns its main thread — see §4 for the rationale.

## 3. Daemon protocol

The daemon speaks two transports on `127.0.0.1:8000`: a persistent
WebSocket (real-time event stream + approvals, §3.1) and a small
read/write HTTP surface for operations that do not fit a broadcast
model — tool management today, diagnostics later (§3.2).

**Auth (#27).** Every HTTP request and WS handshake passes two checks, in
this order (`agent-daemon/auth.py`, pure ASGI middleware):
1. *Origin.* Any request carrying an `Origin` header is refused (HTTP 403;
   WS handshake closed with 1008, HTTP 403 on the wire), unless the origin
   is listed exactly in `AGENT_ALLOWED_ORIGINS` (comma separated, empty by
   default, no wildcard). Browsers always send `Origin` on cross-site
   requests and WebSockets; URLSession, curl and `websockets` send none.
   This also applies to `GET /health`.
2. *Token.* `Authorization: Bearer <token>` with the per-install secret in
   `~/.bot-inator/token` (`secrets.token_urlsafe(32)`, file 0600, dir 0700,
   created at daemon start if missing, `AGENT_TOKEN_PATH` overrides; never
   logged). Compared with `hmac.compare_digest`. Missing/wrong → 401 with
   `WWW-Authenticate: Bearer`; on the WS, close 1008 before accept. Only
   `GET /health` is exempt. The Swift clients (`DaemonAuth.swift`) re-read
   the file on every connect/request and send it as a header, including on
   the `URLSessionWebSocketTask` handshake (built from a `URLRequest`).
   `/api/restart` re-execs with the same token file, so clients keep
   working.

### 3.1 WebSocket protocol

Endpoint: `ws://127.0.0.1:8000/ws/stream`. Every frame is a single JSON object
validated by `schemas/ws_messages.py::WSMessage` (discriminated on `type`).

Server → client: `status`, `thought`, `approval_request`,
`code_approval_request`, `tool_result`, `error`.
Client → server: `approval_response`, `code_approval_response`,
`command` (`pause_listeners`, `resume_listeners`, `reload_model`,
`clear_memory`, `reload_dynamic_tools`).
Calendar (#60): server → client `calendar_request`; client → server
`calendar_response`, `calendar_status` (see the Calendar flow below).

Thought stages: `event_received` → `analysis` (optional) → `memory`
(optional) → `reasoning` → `plan` → `tool_result` → `complete`. The
`analysis` stage carries the `MLXEngine.evaluate_event` summary and is
skipped when that returns an empty string. The `memory` stage carries
the pipe-joined text of the top-k recalled prior memories and is
skipped when recall returns nothing.

Approval flow:

1. Orchestrator wraps each registered tool in `_wrap_tool_for_approval`.
2. On invocation, it emits `approval_request` with a fresh `request_id`
   and a `tool_args` payload shaped like `{"args": [...], "kwargs": {...}}`,
   fans out to every subscriber, and awaits the matching response.
3. `approval_response` may optionally include an `edited_args` field with
   the same shape as `tool_args`. When present, the orchestrator calls
   the tool with the edited values instead of the agent's proposal
   (see `Orchestrator._apply_edits`).
4. Default timeout: 120 s. On timeout or `approved: false`, the wrapper
   returns `{"skipped": True, "reason": "user_denied"}` back into the
   agent loop which surfaces a `no action` thought.

Calendar flow (#60): the menu bar app owns EventKit (the daemon's Python
is silently denied it), and the daemon asks it over this WebSocket.

1. On connect (after the hello `status`) and whenever it changes (checked
   every 30 s and after each request), the app sends
   **`calendar_status`** `{authorization}` (EKAuthorizationStatus name:
   `not_determined | restricted | denied | full_access | write_only`, read
   without prompting). Only a connection that sent it can serve Calendar.
2. `tools/calendar_access.py::AppCalendarBridge` sends
   **`calendar_request`** `{request_id, op: list | create | request_access,
   args, timeout_seconds}` to ONE connection (the one that most recently
   sent `calendar_status`), from the calling worker thread via
   `run_coroutine_threadsafe`, and blocks on a future keyed by `request_id`.
   Timeout 15 s; 120 s while the status is `not_determined` (the first
   request shows the macOS prompt) and for `request_access`.
3. The app answers **`calendar_response`** `{request_id, ok, events? |
   event_id? + calendar?, error?, authorization?}`; `main.py` routes it to
   the bridge. Events are #42 dicts. A disconnect fails pending requests.
4. `ready()` (watcher / setup-scan gate) = the reported status is
   `full_access`; the Permissions `calendars` row reads the same status,
   and its Request click sends `request_access`. No app connected → the
   tools answer "open the bot-inator menu bar app to use Calendar".

Code-approval flow (Phase 4 meta-tool generation):

1. `PatternRecognizer` spots a repeating workflow, emits a synthetic
   `pattern_detected` event back onto the bus.
2. Orchestrator handles the `pattern_detected` event by emitting a
   normal `approval_request` for a synthetic `generate_custom_tool`
   invocation (`tool_args = {"kwargs": {"tool_name", "description",
   "expected_logic"}}`). The user approves/denies the *idea* first.
3. On approval, the orchestrator drafts the Python module body on the
   main engine, runs the static sanity checks (see DONE.md #6), and
   emits a **`code_approval_request`** frame:
   `{request_id, tool_name, description, code, timeout_seconds}`.
4. The MenuBar UI surfaces the code in `CodeReviewAlert` and replies
   with a **`code_approval_response`**:
   `{request_id, approved, edited_code?, user_note?}`. `edited_code`
   lets the user tweak the script before it is installed.
5. On `approved: true`, the meta-tool writes
   `agent-daemon/tools/generated/<tool_name>.py` (via tempfile +
   `os.replace`), then invokes `Orchestrator.load_dynamic_tools()` to
   hot-load it. Denial or timeout: the draft is discarded and a
   `tool_result` thought records the decision.

### 3.2 HTTP API

Non-streaming, request/response operations live on plain HTTP so they
can be driven by ordinary `URLSession` calls from the MenuBar app and
by `curl` during debugging. All responses are JSON; all routes are
local-only (the daemon binds `127.0.0.1`) and, except `GET /health`, need
the bearer token (see Auth above):
`curl -H "Authorization: Bearer $(cat ~/.bot-inator/token)" http://127.0.0.1:8000/api/tools`.

| Method | Path                      | Purpose                                          |
|--------|---------------------------|--------------------------------------------------|
| GET    | `/health`                 | Liveness probe: `{"status":"ok","version":...}`. |
| GET    | `/api/tools`              | List of every tool the agent knows about.        |
| DELETE | `/api/tools/{tool_name}`  | Uninstall an agent-authored tool.                |

`GET /api/tools` returns a plain JSON array `[{"name","description",
"is_generated"}, …]`. The handler walks the orchestrator's live tools
list first (built-ins and currently-loaded generated modules together,
deduplicated by `name`), then scans `agent-daemon/tools/generated/`
for any cold `.py` files that aren't yet loaded so the Manage Tools
view surfaces pending installs too. Descriptions come from the live
`smolagents.Tool.description` when the module is currently loaded;
for cold files the handler `ast.parse`s the module and reads the
first function's docstring.

`DELETE /api/tools/{tool_name}` validates that `tool_name` is a
Python identifier (400 otherwise), refuses any name that belongs to
a built-in (400), and returns 404 when no file exists at
`tools/generated/<tool_name>.py`. On success it calls
`Orchestrator.unload_dynamic_tool(tool_name)` (drops the tool from
the active agent list, evicts `sys.modules[f"tools.generated.{name}"]`,
rebuilds `self._agent`), unlinks the file, best-effort commits the
removal to the nested git repo (DONE.md #8), and returns 200 with
`{"status":"ok","tool_name":…,"unloaded":…}`.

`GET` / `PUT /api/daily-brief` (`routes/daily_brief.py`, #65) reads and
saves `{enabled, time "HH:MM", locked_fields}`; it applies live, no
restart. `POST /api/daily-brief/run` publishes a `daily_brief` event with
`trigger: "manual"`.

## 4. Daemon modules (`agent-daemon/`)

| Path                          | Responsibility                                     |
|-------------------------------|----------------------------------------------------|
| `main.py`                     | FastAPI app, `/health`, `/ws/stream`,              |
|                               | `GET /api/tools`, `DELETE /api/tools/{name}`,      |
|                               | lifespan (orchestrator + listeners + memory).      |
| `config.py`                   | `settings` (provider, model paths, Groq models,    |
|                               | watch dirs). Loads `agent-daemon/.env` via         |
|                               | `python-dotenv`; shell exports win.                |
| `ai/mlx_engine.py`            | Async wrapper over `mlx_lm` (load/unload/swap).    |
| `ai/groq_engine.py`           | `GroqEngine`: same duck-typed surface as           |
|                               | `MLXEngine`, backed by Groq's OpenAI-compatible    |
|                               | chat API. load/unload/swap are bookkeeping only.   |
| `ai/idea_sources.py`          | `NextStepSuggester` (fixed `FOLLOW_UPS` list) and  |
|                               | `ChatGapDetector`; both publish `pattern_detected` |
|                               | with `metadata.source` (DONE.md #12).              |
| `ai/open_help.py`             | `OpenHelpSuggester`: asks "how could I help?"      |
|                               | over recent events, judge-scores ideas 0-10, keeps |
|                               | approve/reject feedback (DONE.md #13).             |
| `ai/setup_scan.py`            | `SetupScanner`: once-per-install scan (apps, shell |
|                               | history, Downloads, calendar) + starter catalog    |
|                               | (`ai/starter_catalog.json`); ≤3 judged ideas.      |
| `ai/friction.py`              | `FrictionDetector`: copy in A → paste in B loops   |
|                               | and app ping-pong → judged ideas.                  |
| `events/mail_watcher.py`      | Mail.app Envelope Index (read-only) → mail_received|
| `events/calendar_watcher.py`  | Upcoming meetings → event_starting_soon.           |
| `events/clipboard_watcher.py` | Clipboard changes → clipboard_changed (app + kind, |
|                               | never content).                                    |
| `events/daily_brief.py`       | One `daily_brief` event a day at                   |
|                               | AGENT_DAILY_BRIEF_TIME (08:00) unless              |
|                               | AGENT_DAILY_BRIEF=0; same-day catch-up; state in   |
|                               | `~/.bot-inator/daily_brief.json`. Gathers calendar,|
|                               | last-24 h mail headers (no bodies; subjects        |
|                               | redacted for cloud engines) and due reminders,     |
|                               | read-only; names a source it can't read; never     |
|                               | prompts for a permission (#65).                    |
| `ai/privacy.py`               | Replaces personal text (iMessage, mail) in cloud   |
|                               | engines' prompts unless AGENT_CLOUD_PERSONAL_TEXT=1|
| `ai/permissions.py`           | Permission table + never-prompting checks (#59);   |
|                               | `GET /api/permissions` in `routes/permissions.py`. |
| `ai/idea_match.py`            | Declined-idea matching (names, model2vec, judge);  |
|                               | review/remove via `routes/ideas.py` (#56).         |
| `routes/tool_detail.py`       | `GET /api/tools/{name}`: summary, when it runs,    |
|                               | gate, inputs, sandbox, source (#61).               |
| `ai/orchestrator.py`          | Event loop, tool gating, subscriber fan-out,       |
|                               | dynamic-tool hot-reload (`load_dynamic_tools`).    |
| `ai/pattern_recognizer.py`    | Rolling 50-event deque fed by the orchestrator;    |
|                               | periodically prompts the main engine (configurable,|
|                               | `AGENT_PATTERN_ENGINE`) to identify                |
|                               | a repeating manual workflow and, when found, emits |
|                               | a `pattern_detected` event back onto the bus.      |
| `ai/memory.py`                | Long-term memory: SQLite + optional `sqlite-vec`   |
|                               | vector store at `~/.bot-inator/memory.db`.         |
|                               | `Memory.save_memory`/`recall_memory`,              |
|                               | module-level `save_memory`/`recall_memory` wrappers|
|                               | and `get_default_memory`/`set_default_memory` for  |
|                               | sharing one instance between orchestrator + tools. |
| `events/event_bus.py`         | Thread-safe pub/sub bridge to the asyncio loop.    |
| `events/mac_listeners.py`     | Spawns `_mac_app_helper.py` and forwards its JSON  |
|                               | output into the EventBus as `app_activated` events.|
| `events/_mac_app_helper.py`   | Subprocess: main-thread NSRunLoop + NSWorkspace    |
|                               | observer → stdout JSON lines. Never imported by    |
|                               | the daemon; launched via `subprocess.Popen`.       |
| `events/file_watcher.py`      | Watchdog-based FS observer on `settings.watch_dirs`.|
| `events/imessage_watcher.py`  | Read-only SQLite poll of `~/Library/Messages/chat.db`;|
|                               | emits `imessage_received` events for inbound texts.|
| `schemas/ws_messages.py`      | Pydantic models for every WS frame.                |
| `tools/*.py`                  | `smolagents.Tool` callables; registered via        |
|                               | `AVAILABLE_TOOLS` and passed into the Orchestrator.|
| `tools/generated/*.py`        | Agent-authored tools. Each file defines exactly    |
|                               | one `@tool` function whose name matches the        |
|                               | filename stem. Loaded via `importlib.util.spec_    |
|                               | from_file_location` on lifespan startup and after  |
|                               | `generate_custom_tool` writes a new module.        |
| `tools/meta_tool_generator.py`| Orchestrator-private `generate_custom_tool`        |
|                               | (not in `AVAILABLE_TOOLS`; never exposed to the    |
|                               | LLM). Drafts code → checks → critic → repair (≤3)  |
|                               | → emits `code_approval_request`                    |
|                               | → persists to `tools/generated/` → hot-reloads.    |
| `tools/draft_checks.py`       | `check_draft`: staged static + import checks on a  |
|                               | drafted module (DONE.md #11).                      |
| `tools/draft_critic.py`       | Grounded logic review of a draft against its spec; |
|                               | advisory, `AGENT_DRAFT_CRITIC=0` disables.         |

## 5. Inference engines

`main._build_engines()` builds three engines from `settings.provider`
(`AGENT_PROVIDER`): `local` (default) → `MLXEngine`, `groq` →
`GroqEngine`. Each has its own `asyncio.Lock`. Eager-loading is
serialised to avoid `huggingface_hub` lock contention.

| Engine             | Local (MLX)                         | Groq (`GROQ_*_MODEL`)       | Used by                                         |
|--------------------|-------------------------------------|-----------------------------|-------------------------------------------------|
| `engine` (main)    | Hermes-3-Llama-3.1-8B-4bit          | `openai/gpt-oss-120b`       | smolagents tool loop; pattern detection and idea sources by default. |
| `analysis_engine`  | Qwen2.5-1.5B-Instruct-4bit          | `openai/gpt-oss-20b`        | `_analyse_event` → `evaluate_event`.            |
| `drafting_engine`  | Qwen2.5-Coder-14B-Instruct-4bit     | `openai/gpt-oss-120b`       | `generate_custom_tool` drafts + `draft_critic`. |

The detection engine is picked by `main._select_detection_engine` from
`AGENT_PATTERN_ENGINE` (`main` / `analysis` / `drafting`).

smolagents adapter: MLX engines use the custom `_MLXModel` with the
`<tool_call>` parser; Groq engines plug into `OpenAIServerModel`.

When `analysis_engine` is not supplied the orchestrator falls back to
the main engine, so tests and simpler deployments still work with a
single model.

`app_activated` events no longer start the tool-calling agent
(`_AGENT_TRIGGER_EXCLUDED_EVENT_TYPES`); they still feed pattern
detection.

- `generate(prompt, max_tokens)` — the hot path driven by the orchestrator's
  smolagents adapter on the main engine.
- `evaluate_event(event_context, max_tokens=128)` — single-shot plain-text
  analysis of a macOS event, executed on `analysis_engine`. Uses
  `EVENT_ANALYSIS_SYSTEM_PROMPT`, applies the tokenizer chat template
  when available, strips `<think>…</think>` blocks, and returns a
  one-to-two sentence summary. Runs **alongside** the orchestrator tool
  loop, not in place of it.

Only `mlx-community/*` repos are supported locally; vanilla HF PyTorch
checkpoints do not load under `mlx_lm.load`. `GroqEngine` implements the
same methods over HTTP.

Config lives in `agent-daemon/.env` (template: `.env.example`):
`AGENT_PROVIDER`, `AGENT_PATTERN_ENGINE`, `AGENT_DRAFT_CRITIC`,
`GROQ_API_KEY`, `GROQ_API_BASE`, `GROQ_MAIN_MODEL`,
`GROQ_ANALYSIS_MODEL`, `GROQ_DRAFTING_MODEL`.

## 6. Tools (Phase 1 Sprint final)

Registered in `tools/__init__.py::AVAILABLE_TOOLS` and wired via
`main.py`. Tools listed in `tools/__init__.py::READ_ONLY_TOOL_NAMES`
bypass the approval gate and run inline; all other tools still emit an
`approval_request` and wait on the user.

| Tool               | Read-only | Effect                                            |
|--------------------|-----------|---------------------------------------------------|
| `read_active_tab`  | ✅         | AppleScript; most recently active browser, then  |
|                    |           | Chrome, then Safari; URL/title.                   |
| `read_active_tab_text`| ✅      | AppleScript JS → innerText of the active tab,     |
|                    |           | truncated; sensitive sites refused; cloud         |
|                    |           | placeholder per #31.                              |
| `open_url`         |           | Opens a URL with `open`.                          |
| `show_notification`|           | Posts a macOS notification via `osascript`.       |
| `read_clipboard`   | ✅         | `pbpaste` → string.                              |
| `write_clipboard`  |           | `pbcopy` ← string.                                |
| `summarize_file`   | ✅         | Metadata + ≤2 KB preview for text files.         |
| `move_file`        |           | Moves a file into an existing directory.          |
| `send_imessage`    |           | AppleScript `tell application "Messages"` → send  |
|                    |           | to a buddy on the iMessage service.               |
| `remember_preference`| ✅       | Persists a user preference string into the shared |
|                    |           | `Memory` at `~/.bot-inator/memory.db`.            |
| `find_files`       | ✅         | Spotlight (`mdfind`) by name or content, optional |
|                    |           | folder and kind; newest first, ≤50 results.       |
| `list_shortcuts`   | ✅         | `shortcuts list` → the user's Shortcut names.     |
| `run_shortcut`     |           | `shortcuts run <exact name>`, optional text input |
|                    |           | (temp file via `--input-path`); returns stdout.   |
| `create_note`      |           | AppleScript → Notes.app: new note (text via argv, |
|                    |           | HTML-escaped).                                    |
| `append_note`      |           | Appends to the one note with that exact title.    |
| `list_windows`     | ✅         | Quartz window list (app + title; titles need      |
|                    |           | Screen Recording).                                |
| `open_app`         |           | `open -a` on an `.app` found in the Applications  |
|                    |           | folders.                                          |
| `read_email`       | ✅         | Body of one Mail.app message by id (`.emlx`).     |
| `draft_email`      |           | AppleScript → Mail.app: opens an UNSENT draft     |
|                    |           | (text via argv, recipients validated);            |
|                    |           | `reply_to_message_id` makes a threaded reply.     |
|                    |           | Drafts only: no send tool exists (#63).           |
| `brief_me`         | ✅         | Today's brief on demand: meetings, last-24 h mail |
|                    |           | headers, reminders due (#65).                     |
| `list_events`      | ✅         | Calendar events in a range, via the menu bar      |
|                    |           | app's EventKit (#60; app must be running).        |
| `create_event`     |           | Adds a Calendar event (app's EventKit; default    |
|                    |           | calendar unless one is named).                    |
| `list_reminders`   | ✅         | Reminders (EventKit/AppleScript), soonest first.  |
| `create_reminder`  |           | Adds a reminder (optional due date, list, notes). |
| `generate_custom_tool`|         | Meta-tool: drafts a new `@tool` module on the     |
|                    |           | main engine, runs static safety checks, emits a   |
|                    |           | `code_approval_request` for human review, and —   |
|                    |           | on approval — installs it into                    |
|                    |           | `tools/generated/` and hot-loads it.              |

`tool_result` thoughts emitted for read-only invocations are prefixed
with ``"<name> (read-only)"`` so the MenuBar stream still reflects
every call.

**Generated tools run sandboxed (#22).** `load_dynamic_tools` never imports
a generated module in the daemon. `tools/tool_sandbox.py` imports it in a
`sandbox-exec` child (`describe`) and registers a `SandboxedTool` proxy with
the same name, description, inputs and output type; each `forward` runs the
real tool in a fresh child (JSON args in, JSON result out, timeout
`AGENT_TOOL_SANDBOX_TIMEOUT`, default 60 s; background threads may keep the
child alive up to `AGENT_TOOL_SANDBOX_LINGER`, default 1 h). Profile: writes
under `$HOME` except `~/Library`, `~/.ssh`, `~/.bot-inator`, shell start-up
files and the repo (incl. `tools/generated`); reads everywhere except
`~/.ssh`, `~/.bot-inator`, `agent-daemon/.env`; network only when the AST
shows a network-module import (`network_allowed`, `sandbox_note` on the
proxy); exec only Python, `osascript`, `open`, `shortcuts`, `mdfind`. The
child has a minimal env and `dotenv` disabled. The approval gate and
`tool_result` thoughts wrap the proxy unchanged. A built-in the generated
tool calls runs in the child with no second approval: the user approved the
source and approves each call of the generated tool. A sandbox that is
missing or fails its self-test means no generated tool loads (a
`tool_result` thought per tool, files left in place); `AGENT_TOOL_SANDBOX=0`
restores the in-process import. Residual risk: `open` and `osascript` can
ask other apps (Terminal, Script Editor) to run code outside the sandbox.

## 7. UI (`MenuBarUI/`)

SwiftUI MenuBar app with an AppKit shell:

- `MenuBarUIApp.swift` — `AppDelegate` launches the WS connection and the
  `ApprovalPresenter` at `applicationDidFinishLaunching`, so thoughts and
  approvals are collected regardless of popover state. `RootPopoverView`
  is a `TabView` with two tabs: "Thoughts" and "Debug". A `WindowGroup`
  scene named `tool-manager` hosts `ToolManagerView`; the status-bar
  popover shows a gear button that calls `openWindow(id: "tool-manager")`
  to bring it forward.
- `Services/WebSocketManager.swift` — publishes `@Published messages`,
  `pendingApproval`, `lastStatus`, and a `DebugStats` struct (frame
  counts by type, thought counts by stage, decode-error ring buffer,
  connect attempts, last raw frame). Single persistent URLSession WS task.
- `Services/ApprovalPresenter.swift` — subscribes to `pendingApproval`
  **and** `pendingCodeApproval` and renders `ApprovalAlert` /
  `CodeReviewAlert` inside a floating `NSWindow` that activates the app.
- `Views/ThoughtStreamView.swift` — scroll-back log inside the popover.
- `Views/DebugView.swift` — in-app diagnostics tab: connection state,
  frame/stage counters, recent decode errors, last raw frame, and a
  placeholder for future tools (daemon log tail, listener health,
  manual event injection). `resetDebugStats()` clears counters.
- `Views/ApprovalAlert.swift` — approve / deny + optional note. For
  `send_imessage` requests, the generic JSON args view is replaced with
  an editable "To" field and multi-line message `TextEditor`; on
  Approve the view builds an `edited_args` payload
  (`{"args": [], "kwargs": {"target_number", "message"}}`) that the
  daemon substitutes into the actual tool call.
- `Views/CodeReviewAlert.swift` — strict-HITL window for
  `code_approval_request`. Displays `tool_name` and `description` at
  the top and the proposed Python module body in a monospaced,
  scrollable `TextEditor` bound to a `@State` buffer so the user can
  edit before installing. Primary action **Approve & Install**, secondary
  **Deny**. On approve the view sends a `code_approval_response` with
  `edited_code` only when the buffer diverges from the original.
- `Views/ToolManagerView.swift` — standalone resizable macOS window
  (default 520×420). On appear, hits `GET /api/tools` via
  `URLSession.shared.data(for:)` and renders a SwiftUI `List` of
  `ToolRow` items. Built-in rows show a `hammer` SF Symbol; agent-
  authored rows show a `sparkles` badge and a trailing destructive
  trash button. Trash tap presents a confirmation dialog; on
  confirm, sends `DELETE /api/tools/{name}` and reloads the list
  (on 204 or 404). A top-right refresh button re-fetches. Errors
  surface inline via a banner.
- `Views/DailyBriefSettingsView.swift` — Settings section "Morning brief":
  on/off toggle, time of day, **Brief me now** (#65). Saves through
  `/api/daily-brief` as soon as a value changes.
- `Views/ApprovalAlert.swift` has a `draft_email` card (#63): To, Cc,
  Subject, Body editable; states that only a draft is created.

Idea sources (wave 10, #66): open-help asks the model only after a new
content event (`file`, `mail_received`, `imessage_received`,
`event_starting_soon`); an app name or window title no longer grounds an
idea; ideas whose whole job is switching, toggling, listing or counting
apps are dropped deterministically in open-help, friction and the pattern
recognizer. A new watcher's event type must be added to
`CONTENT_EVENT_TYPES` in `ai/open_help.py` before open-help reacts to it.

Tool summaries (wave 11, #64): `approval_request` for
`generate_custom_tool` and `code_approval_request` carry an optional
`summary` (plain sentences: what it does, when it runs, what it touches,
that each use asks first), built by `ai/tool_summary.py` (one
analysis-engine call bounded at 5 s; deterministic fallback). For code
review the "touches" sentence is derived from the drafted source, not the
model. Provenance stores `summary`; `GET /api/tools/{name}` returns it as
`plain_summary` (null for built-ins and older tools). The summary prompt
is scrubbed with `privacy.scrub_idea_text`.

Built ad-hoc via `swiftc` against macOS 14.0+ (arm64); artefact at
`MenuBarUI/MenuBarUI/build/MenuBarUI.app`.

## 8. Phase status

Moved to [DONE.md](DONE.md) (finished phases 1–5.2 and the open-help
source, items #1–#13) and [todo.md](todo.md) (open work). Phase 5,
tool-idea quality, is still open.

## 9. Open questions / follow-ups

Moved to [todo.md](todo.md), items #14–#32 (old question numbers kept in
brackets). New open questions go there, not here.
