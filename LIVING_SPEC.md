# Living Spec — bot-inator

Rolling source-of-truth for the architecture, protocol, and in-flight work.
Update this file as decisions land. Each phase section is append-only; newer
phases supersede older ones where they conflict.

## 1. Goals

A fully on-device macOS agent that observes system events, reasons about them
with a local LLM, and proposes actions that the user approves or rejects from
a MenuBar UI. No cloud inference, no background action without consent.

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
│  Orchestrator ── smolagents(_MLXModel) ── MLXEngine      │
│        │                                      │          │
│        └─ approval-gated tools                │          │
│        └─ subscribers (WebSocket fan-out)     │          │
│                                               ▼          │
│                                       mlx-lm + Metal     │
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

### 3.1 WebSocket protocol

Endpoint: `ws://127.0.0.1:8000/ws/stream`. Every frame is a single JSON object
validated by `schemas/ws_messages.py::WSMessage` (discriminated on `type`).

Server → client: `status`, `thought`, `approval_request`,
`code_approval_request`, `tool_result`, `error`.
Client → server: `approval_response`, `code_approval_response`,
`command` (`pause_listeners`, `resume_listeners`, `reload_model`,
`clear_memory`, `reload_dynamic_tools`).

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

Code-approval flow (Phase 4 meta-tool generation):

1. `PatternRecognizer` spots a repeating workflow, emits a synthetic
   `pattern_detected` event back onto the bus.
2. Orchestrator handles the `pattern_detected` event by emitting a
   normal `approval_request` for a synthetic `generate_custom_tool`
   invocation (`tool_args = {"kwargs": {"tool_name", "description",
   "expected_logic"}}`). The user approves/denies the *idea* first.
3. On approval, the orchestrator drafts the Python module body on the
   main engine, runs the static sanity checks (see §8 Phase 4), and
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
local-only (the daemon binds `127.0.0.1`).

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
removal to the nested git repo (§8 Phase 4.6), and returns 200 with
`{"status":"ok","tool_name":…,"unloaded":…}`.

## 4. Daemon modules (`agent-daemon/`)

| Path                          | Responsibility                                     |
|-------------------------------|----------------------------------------------------|
| `main.py`                     | FastAPI app, `/health`, `/ws/stream`,              |
|                               | `GET /api/tools`, `DELETE /api/tools/{name}`,      |
|                               | lifespan (orchestrator + listeners + memory).      |
| `config.py`                   | `settings` (model path, watch dirs, timeouts).     |
| `ai/mlx_engine.py`            | Async wrapper over `mlx_lm` (load/unload/swap).    |
| `ai/orchestrator.py`          | Event loop, tool gating, subscriber fan-out,       |
|                               | dynamic-tool hot-reload (`load_dynamic_tools`).    |
| `ai/pattern_recognizer.py`    | Rolling 50-event deque fed by the orchestrator;    |
|                               | periodically prompts `analysis_engine` to identify |
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
|                               | LLM). Drafts code → emits `code_approval_request`  |
|                               | → persists to `tools/generated/` → hot-reloads.    |

## 5. MLX inference

Two `MLXEngine` instances are kept warm in unified memory, each with its
own `asyncio.Lock` so load/unload/swap are serialised against generation
within that engine.

| Engine                | Model                                           | Used by                                       |
|-----------------------|-------------------------------------------------|-----------------------------------------------|
| `engine` (main)       | `settings.model_path` = Hermes-3-Llama-3.1-8B   | Orchestrator tool-calling loop (smolagents).  |
| `analysis_engine`     | `settings.analysis_model_path` = Qwen2.5-1.5B   | `_analyse_event` → `evaluate_event` only.     |

Both are constructed in `main.py`'s lifespan and passed to the
`Orchestrator`. When `analysis_engine` is not supplied the orchestrator
falls back to the main engine, so tests and simpler deployments still
work with a single model.

- `generate(prompt, max_tokens)` — the hot path driven by the orchestrator's
  smolagents adapter on the main engine.
- `evaluate_event(event_context, max_tokens=128)` — single-shot plain-text
  analysis of a macOS event, executed on `analysis_engine`. Uses
  `EVENT_ANALYSIS_SYSTEM_PROMPT`, applies the tokenizer chat template
  when available, strips `<think>…</think>` blocks, and returns a
  one-to-two sentence summary. Runs **alongside** the orchestrator tool
  loop, not in place of it.

Only `mlx-community/*` repos are supported; vanilla HF PyTorch checkpoints do
not load under `mlx_lm.load`.

## 6. Tools (Phase 1 Sprint final)

Registered in `tools/__init__.py::AVAILABLE_TOOLS` and wired via
`main.py`. Tools listed in `tools/__init__.py::READ_ONLY_TOOL_NAMES`
bypass the approval gate and run inline; all other tools still emit an
`approval_request` and wait on the user.

| Tool               | Read-only | Effect                                            |
|--------------------|-----------|---------------------------------------------------|
| `read_active_tab`  | ✅         | AppleScript → Chrome, then Safari; URL/title.    |
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
| `generate_custom_tool`|         | Meta-tool: drafts a new `@tool` module on the     |
|                    |           | main engine, runs static safety checks, emits a   |
|                    |           | `code_approval_request` for human review, and —   |
|                    |           | on approval — installs it into                    |
|                    |           | `tools/generated/` and hot-loads it.              |

`tool_result` thoughts emitted for read-only invocations are prefixed
with ``"<name> (read-only)"`` so the MenuBar stream still reflects
every call.

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

Built ad-hoc via `swiftc` against macOS 14.0+ (arm64); artefact at
`MenuBarUI/MenuBarUI/build/MenuBarUI.app`.

## 8. Phase status

- **Phase 1 — plumbing.** ✅ WS protocol, event ingestion, smolagents
  adapter, approval round-trip, floating approval window, 7 real tools.
- **Phase 2 Sprint 1 — MLX AI Engine.** ✅ Complete.
  - ✅ `evaluate_event()` added to `MLXEngine`.
  - ✅ Orchestrator calls `evaluate_event` per event and emits an
    `analysis` thought frame between `event_received` and `reasoning`
    (empty returns are skipped). `ThoughtStage` extended in both
    Pydantic (`schemas/ws_messages.py`) and Swift
    (`MenuBarUI/MenuBarUI/Models/WSMessage.swift`).
  - ✅ Two-engine design: `analysis_engine` (Qwen 1.5 B) runs
    `evaluate_event`, main engine (Hermes-3 8 B) runs tool-calling.
    Config exposes `settings.analysis_model_path`.
  - ✅ Eager loading: both engines are loaded concurrently in the
    `main.py` lifespan before the app accepts connections, so the first
    event has no model-load latency. Gated by `AGENT_EAGER_LOAD=0` in
    tests.
  - ✅ Mocked integration tests cover populated-analysis, empty-analysis,
    and engine-distinctness paths (`tests/test_integration.py`).
- **Phase 2 Sprint 2 — Event pipeline hardening + in-app diagnostics.** ✅ Complete.
  - ✅ Diagnosed that NSWorkspace activation notifications are not
    delivered to background threads in the daemon process (uvicorn owns
    the main thread and nothing services the `CFRunLoop` that the
    distributed-notification mach-port source needs). Confirmed via a
    standalone probe.
  - ✅ Rebuilt `MacAppListener` around a dedicated
    `events/_mac_app_helper.py` subprocess whose main thread runs
    `NSRunLoop`. Parent reads JSON lines from stdout on a background
    thread and pushes `ContextEvent`s via `bus.push_threadsafe`.
    End-to-end verified against `osascript` app activations.
  - ✅ In-app Debug tab: new `DebugView.swift` + `DebugStats` struct on
    `WebSocketManager` (frames by type, thoughts by stage, decode-error
    ring buffer, connect attempts, last raw frame, reset button).
  - ✅ Read-only tools (`read_active_tab`, `read_clipboard`,
    `summarize_file`) bypass the approval gate via
    `tools.READ_ONLY_TOOL_NAMES`. `_wrap_tool_for_approval` and
    `_fallback_run` both consult the set; the read-only branch still
    emits a `tool_result` thought prefixed with "(read-only)".
    Covered by `test_read_only_tool_bypasses_approval`.
- **Phase 3 Sprint 2 — iMessage watcher + responder.** ✅ Complete.
  - ✅ `events/imessage_watcher.py`: read-only SQLite poll of
    `~/Library/Messages/chat.db` every 10 s. Opens with
    `file:…?mode=ro`, records `MAX(ROWID)` as the baseline at startup,
    then emits a `ContextEvent(event_type="imessage_received",
    metadata={"sender","text","rowid"})` for every new row where
    `is_from_me = 0`. Requires macOS **Full Disk Access** on the
    process that launched the daemon; missing access is logged and the
    watcher idles without crashing.
  - ✅ `tools/send_imessage.py`: smolagents `@tool` that shells out to
    `osascript` with `tell application "Messages"` to deliver a message
    through the iMessage service. Not in `READ_ONLY_TOOL_NAMES`, so
    every send raises an `approval_request` in the MenuBar UI.
  - ✅ Orchestrator specialisation: `_build_prompt` / `_describe_event`
    branch on `event_type == "imessage_received"` and produce a
    dedicated system prompt that instructs the main engine to either
    call `send_imessage` (with `target_number=<sender>`) or reply
    `"no action"`.
  - ✅ `IMessageWatcher` is wired into `main.py` lifespan alongside
    `MacAppListener` and `FileWatcher`; liveness reported as
    `imessage_watcher` in `listener_names`.
  - ✅ Tests: `test_imessage_event_builds_specialized_prompt` (prompt
    shape + tool mention + sender echo) and
    `test_imessage_watcher_emits_inbound_messages` (temp-sqlite baseline,
    inbound vs `is_from_me` filter, sender resolved via `handle`).
  - ✅ Editable approval for `send_imessage`: new
    `ApprovalResponsePayload.edited_args` protocol field carries a
    user-edited `{"args", "kwargs"}` dict back to the daemon;
    `Orchestrator._apply_edits` substitutes it for the agent's proposal
    before the tool is invoked. The SwiftUI `ApprovalAlert` special-cases
    `tool_name == "send_imessage"` with editable "To" + multi-line
    `TextEditor` message fields, a disabled Approve button for empty
    bodies, and an "Approve & Send" label on the primary action.
    Covered by `test_apply_edits_helper` and
    `test_approval_response_with_edited_args_overrides_tool_invocation`.
- **Phase 3 Sprint 3 — Local vector memory.** ✅ Complete.
  - ✅ `ai/memory.py` extended into a real store. `Memory(db_path,
    embedder, dim)` opens SQLite with `check_same_thread=False` so the
    same instance can be used from the asyncio loop and from
    `asyncio.to_thread` callers. `save_memory(text, metadata)` and
    `recall_memory(query, top_k=3) -> list[str]` are the public surface;
    a `search()` helper returns `(id, text, distance)` tuples. Module
    also exposes `get_default_memory()` / `set_default_memory()` and
    top-level `save_memory` / `recall_memory` wrappers that proxy to
    the shared instance.
  - ✅ Embeddings: `default_embedder()` prefers
    `sentence-transformers` (`all-MiniLM-L6-v2`, 384-dim, lazy-loaded
    on first use) and falls back to a deterministic hash embedder when
    the optional dep is missing. Vector search uses `sqlite-vec`'s
    `vec0` virtual table when the Python build supports
    `load_extension`; otherwise the store silently degrades to
    recency-ordered recall. Both failure modes are logged as warnings
    and covered by unit tests.
  - ✅ Persistent DB location is `~/.bot-inator/memory.db`, created
    during `main.py` lifespan via `get_default_memory()` and installed
    with `set_default_memory()` so tools and the orchestrator share a
    single connection.
  - ✅ `tools/remember_preference.py`: smolagents `@tool` calling
    `save_memory(text, metadata={"kind": "preference"})`. Registered
    in `AVAILABLE_TOOLS` **and** in `READ_ONLY_TOOL_NAMES`: writing to
    the user's own local memory bypasses the approval gate and still
    emits a `(read-only)`-prefixed `tool_result` thought.
  - ✅ Orchestrator RAG: `_handle_event` now calls
    `_recall_memories(event)` (off-thread, swallows errors) before
    building the prompt. When recall returns rows the orchestrator
    emits a `memory` thought (`"Recalled: <m1> | <m2> | ..."`) and
    `_build_prompt` / `_build_imessage_prompt` append a
    `"Relevant memories from prior sessions:"` block via
    `_format_memories`. `ThoughtStage` gained `"memory"` in both
    `schemas/ws_messages.py` and `MenuBarUI/Models/WSMessage.swift`;
    `ThoughtStreamView` and `DebugView` render the new stage (pink).
  - ✅ `sentence-transformers` and `sqlite-vec` added to
    `requirements.txt` as optional deps — the daemon runs cleanly
    without them thanks to the fallbacks.
  - ✅ Tests: `test_memory_save_recall_round_trip`,
    `test_remember_preference_tool_writes_to_default_memory`, and
    `test_orchestrator_injects_recalled_memories_into_prompt` (end-to-end:
    seeds the default memory, drives an `app_activated` event, asserts
    a `memory` thought is emitted and the reasoning prompt carries the
    `Relevant memories` block). Whole suite: 20 passing.
- **Phase 4 Sprint 1 — Autonomous meta-tool generation.** ✅ Complete.
  - 🎯 Goal: the daemon observes a repeating manual workflow, drafts a
    `smolagents` `@tool` that automates it, submits the code for
    strict human review, and hot-loads the approved module into the
    live agent — no daemon restart, no cloud.
  - ✅ New files:
    `agent-daemon/ai/pattern_recognizer.py`,
    `agent-daemon/tools/meta_tool_generator.py`,
    `agent-daemon/tools/generated/.gitkeep` (the directory is a
    nested git repo at runtime; generated modules are loaded by
    path rather than imported as a package), and
    `MenuBarUI/MenuBarUI/Views/CodeReviewAlert.swift`.
  - ✅ Pattern recognizer: owned by the `Orchestrator` and fed by
    `_handle_event` so it sees exactly the events the agent
    processes — no second bus subscriber and no race with the
    queue. Rolling `collections.deque(maxlen=50)` of short
    one-line event descriptions. Detection fires on **10 observed
    events or 60 s**, whichever comes first (class attributes
    `trigger_every` / `trigger_interval`), followed by a 300 s
    cooldown and a "same tool name as last time" dedupe guard so
    duplicate suggestions don't flood the user. Detection calls
    `analysis_engine.evaluate_event` with a dedicated prompt that
    requires either the literal string `NO_PATTERN` or a one-line
    JSON object `{"tool_name","description","expected_logic"}`.
    A match is published as
    `ContextEvent(event_type="pattern_detected", metadata={…})`
    via the supplied `publish` coroutine (default:
    `event_bus.push`), so it re-enters the same orchestrator loop
    on the next iteration.
  - ✅ Suggestion flow (reuses the existing approval machinery):
    `Orchestrator._handle_event` branches on
    `event_type == "pattern_detected"` and runs the agent with a
    specialised prompt from `_build_pattern_prompt` that names the
    proposed tool and instructs the agent to invoke
    `generate_custom_tool` exactly once. Because
    `generate_custom_tool` is a normal gated tool, the existing
    `_wrap_tool_for_approval` wrapper emits the **idea-gate**
    `approval_request` automatically; the user approves the call,
    then the meta-tool runs and emits a second `code_approval_request`
    for the **code gate**. The pattern recognizer avoids re-entering
    its own detection when it sees a `pattern_detected` event in
    `observe`, preventing self-amplification.
  - ✅ `generate_custom_tool(tool_name, description, expected_logic)`:
    a smolagents `@tool` registered in `AVAILABLE_TOOLS`. Module-level
    globals (`_ORCHESTRATOR`, `_ENGINE`, `_GENERATED_DIR`) are wired
    by `tools.meta_tool_generator.set_meta_tool_context` during the
    `main.py` lifespan, so the tool can reach the orchestrator and
    the main engine without taking them as function arguments (which
    smolagents would forward to the LLM). Pipeline:
    1. `_validate_identifier` — `tool_name` must match
       `[a-z_][a-z0-9_]*`.
    2. `_draft_with_engine` — drives `engine.generate` with a
       strict system prompt that demands `from smolagents import
       tool`, exactly one `@tool` function matching `tool_name`,
       typed parameters, a docstring, and only stdlib +
       `smolagents` imports. Fenced markdown is stripped via
       `_strip_fences`.
    3. `_validate_source` — AST-based: every `Import` /
       `ImportFrom` root module must appear in
       `_ALLOWED_STDLIB` (ast, base64, collections, contextlib,
       csv, datetime, difflib, enum, functools, glob, hashlib,
       html, http, io, itertools, json, logging, math, os,
       pathlib, random, re, shlex, shutil, socket, sqlite3,
       statistics, string, subprocess, sys, tempfile, textwrap,
       threading, time, typing, urllib[.parse/.request], uuid,
       zipfile) or `_ALLOWED_THIRD_PARTY` (`smolagents`); source
       must not contain any `_BANNED_SUBSTRINGS` (`__import__(`,
       `eval(`, `exec(`, `compile(`, `os.system(`, `os.popen(`,
       `pty.spawn(`); there must be exactly one `FunctionDef`
       named `tool_name` with a `tool` / `smolagents.tool`
       decorator. Relative imports are rejected.
    4. `_request_code_approval` — crosses the async boundary via
       `asyncio.run_coroutine_threadsafe` into
       `Orchestrator.request_code_approval`, which puts a future
       on `_pending_code` keyed by `request_id` and emits the
       `code_approval_request` frame. Timeout is
       `Orchestrator.code_approval_timeout` (default 300 s);
       expiration returns a synthetic denial.
    5. On `approved: true`, if the user supplied `edited_code` we
       re-run `_validate_source` against the edits before
       persisting; a failed edit aborts the install with a
       surfaced error.
    6. `_atomic_write` — `tempfile.mkstemp` in the target dir +
       `os.replace` so partial writes are never visible to the
       loader.
    7. `_git_commit` — nested-repo scheme from Phase 4.6.
    8. `Orchestrator.load_dynamic_tools()` — re-imports the newly
       persisted module.
    Returns `{"status": "ok" | "denied" | "error", "tool_name",
    "path"?, "git"?, "git_ok"?, "error"?, "user_note"?}`; the
    agent renders the result as a `tool_result` thought.
  - ✅ `Orchestrator.load_dynamic_tools(directory=None)`: walks the
    configured `settings.generated_tools_dir` (overridable for
    tests), skipping dotfiles (`.gitkeep`), directories (including
    the nested `.git/`), and non-`.py` files. For each file uses
    `importlib.util.spec_from_file_location(
      f"tools.generated.{stem}", path)` → `module_from_spec` →
    `loader.exec_module`. Picks the first module attribute that is
    either a `smolagents.Tool` instance or a callable whose `name`
    attribute matches the filename stem. Deduplicates against the
    built-in names captured at construction in
    `_builtin_tool_names` (collisions are logged and skipped).
    Records every successfully-loaded name in
    `_dynamic_tool_names` so `unload_dynamic_tool` and
    `GET /api/tools` can distinguish agent-authored entries.
    Re-loading refreshes the cached module and replaces the
    previous tool instance in `self.tools`; after the walk the
    agent is rebuilt so the running loop sees the new callable.
    Called once during `main.py` lifespan startup and again from
    `generate_custom_tool` after every successful install. Also
    reachable via the WS `command` action `reload_dynamic_tools`
    for manual re-scan when the user edits a file on disk.
  - ✅ `Orchestrator.unload_dynamic_tool(name) -> bool`: removes
    the tool from `self.tools`, discards it from
    `_dynamic_tool_names`, pops
    `sys.modules[f"tools.generated.{name}"]`, and rebuilds
    `self._agent`. Returns `False` when the name is not a
    currently-loaded dynamic tool (built-ins or unknown names).
  - 🔒 Security posture: no runtime sandbox. HITL review of the
    literal source is the sole gate; the stdlib allow-list and
    banned-substring scan are defence-in-depth, not a guarantee.
  - ✅ UI: `CodeReviewAlert.swift` is presented by
    `ApprovalPresenter` through a second published slot
    (`pendingCodeApproval`) on `WebSocketManager`. Floating
    resizable window (620×560), monospaced `TextEditor` bound to
    a `@State` buffer pre-filled with `payload.code`; primary
    action **Approve & Install**, secondary **Deny**. An
    `edited_code` field is sent back only when the buffer
    diverges from the original. Debug tab counters cover
    `code_approval_request` and `code_approval_response` frames.
  - ✅ Tests (`tests/test_integration.py`):
    - `test_pattern_recognizer_publishes_on_match` — fake eval
      returns a parseable suggestion; assert a `pattern_detected`
      event lands on the bus.
    - `test_pattern_recognizer_ignores_no_pattern` — a
      `NO_PATTERN` reply leaves the bus empty.
    - `test_load_dynamic_tools_skips_dotfiles` — `.gitkeep` + a
      nested `.git/` are present alongside a valid module; only
      the module is loaded.
    - (The Phase 4.5 / 4.6 tests below also exercise the install
      path end-to-end.)
- **Phase 4.5 — Tool manager UI + auto-versioning.** ✅ Complete.
  - 🎯 Goal: give the user first-class visibility and control over
    every tool the agent can call, and snapshot each agent-authored
    module into git so a bad generation is one `git revert` away.
  - ✅ Files shipped:
    `MenuBarUI/MenuBarUI/Views/ToolManagerView.swift` (view + its
    lightweight `ToolManagerViewModel`),
    `agent-daemon/main.py` (two new routes + `reload_dynamic_tools`
    command),
    `agent-daemon/tools/meta_tool_generator.py` (stdlib allow-list
    prompt + AST-based validator + nested git commit),
    `agent-daemon/ai/orchestrator.py` (`unload_dynamic_tool`),
    `MenuBarUI/MenuBarUI/MenuBarUIApp.swift` (`Window(id:
    "tool-manager")` scene + `⌘⇧T` shortcut), and
    `MenuBarUI/MenuBarUI/Views/RootPopoverView.swift` (gear button
    that calls `openWindow(id: "tool-manager")`).
  - ✅ Draft-prompt hardening: the system prompt in
    `meta_tool_generator._SYSTEM_PROMPT` explicitly requires stdlib
    + `smolagents` only and enumerates the allowed modules. The
    AST-based `_validate_source` rejects any `Import` / `ImportFrom`
    whose root is outside `_ALLOWED_STDLIB ∪ _ALLOWED_THIRD_PARTY`,
    any relative import, and any hit against `_BANNED_SUBSTRINGS`
    (see Phase 4 Sprint 1 bullet for the complete lists).
  - ✅ Git auto-commit lives in `meta_tool_generator._git_commit`;
    see Phase 4.6 below for the nested-repo scheme.
  - ✅ `GET /api/tools`: implemented in `main.py`. Walks
    `orchestrator.tools` (live built-ins **and** currently-loaded
    generated modules, deduplicated by `name`) then scans
    `settings.generated_tools_dir` for any cold `.py` files that
    aren't loaded yet. Live descriptions come from the
    `smolagents.Tool.description` / `__doc__` attribute; cold
    descriptions come from `ast.get_docstring` on the first
    function in the module. Returns a plain JSON array
    `[{"name","description","is_generated"}, …]`. `is_generated` is
    derived from `Orchestrator._dynamic_tool_names`.
  - ✅ `DELETE /api/tools/{tool_name}`: validates the identifier
    (400 on mismatch), refuses any built-in name by checking
    `Orchestrator._builtin_tool_names` (400), returns 404 when no
    matching file exists, else calls
    `Orchestrator.unload_dynamic_tool(tool_name)`, unlinks the
    `.py`, best-effort commits the removal to the nested repo
    (errors logged, never fatal), and returns **200** with
    `{"status":"ok","tool_name":…,"unloaded":…}`.
  - ✅ `Orchestrator.unload_dynamic_tool(name) -> bool`: drops the
    tool from `self.tools` by `name`, evicts
    `sys.modules[f"tools.generated.{name}"]`, discards from
    `_dynamic_tool_names`, and rebuilds `self._agent`. Returns
    `False` when the name is not a currently-loaded dynamic tool
    (built-in or unknown); the route still unlinks the file in
    that case so a stale on-disk module can always be cleared.
  - ✅ `ToolManagerView.swift`: rendered in a `Window(id:
    "tool-manager")` scene with `⌘⇧T` keyboard shortcut.
    `ToolManagerViewModel` owns the `URLSession` calls against
    `http://127.0.0.1:8000`. View body: title bar with refresh
    button + tool count, transient error banner, SwiftUI `List`.
    Each row shows the name (monospaced), description
    (line-limited to 2), and a `hammer` (built-in) or `sparkles`
    (generated) SF Symbol badge. Generated rows get a trailing
    destructive `trash` button that presents a
    `confirmationDialog` before issuing `DELETE` and refreshing
    the list. Opened from a gear button in the
    `RootPopoverView` tab bar.
  - ✅ Tests (`tests/test_integration.py`):
    - `test_list_tools_includes_builtins_and_generated` — live
      built-ins + a cold stub in an isolated generated dir, with
      descriptions sourced from both live tools and `ast.parse`.
    - `test_delete_tool_rejects_builtins` — built-in name, invalid
      identifier, and missing file all produce the expected 400 /
      404 errors.
    - `test_delete_tool_removes_file_and_unloads` — writes a
      real module into a `tmp_path` generated dir, loads it,
      asserts the DELETE path unlinks it and detaches it from
      the orchestrator.
- **Phase 4.6 — Source-control isolation for generated tools.** ✅ Complete.
  - 🎯 Goal: keep the main project history free of AI-authored
    churn, while still giving every generated tool a local,
    per-project rollback history the user can inspect with plain
    `git log` / `git revert`.
  - ✅ Main `.gitignore`:
    - `*.db`, `*.db-shm`, `*.db-wal` — covers the local SQLite
      memory store plus any scratch `.db` fixtures tests may
      write inside the tree.
    - `agent-daemon/tools/generated/*` with
      `!agent-daemon/tools/generated/.gitkeep`. The **glob form
      is load-bearing**: the literal directory-ignore
      `generated/` would prevent git from descending into the
      folder, so the `.gitkeep` re-include never fires. The
      committed `.gitkeep` is a zero-byte placeholder that keeps
      the directory on fresh clones. Confirmed via
      `git check-ignore -v` on both the keepfile (re-included)
      and an arbitrary `foo.py` inside the folder (ignored).
  - ✅ Nested git repo, implemented in
    `tools/meta_tool_generator._git_commit`:
    - Before the first commit, probes for `tools/generated/.git`;
      missing → runs `git init --quiet` with `cwd=tools/generated/`.
    - Identity is derived from the **host's global git config**
      (`user.name` / `user.email`) and passed via
      `git -c user.name=… -c user.email=… …` on every invocation.
      When the host has no global identity set, the helper falls
      back to a synthetic `bot-inator` / `bot-inator@local` pair so
      the commit still lands on a fresh machine or in CI. No
      `git config …` writes touch the nested repo's on-disk
      state, which keeps the identity decision transient and
      local to each commit.
    - After the atomic `tempfile`+`os.replace` write, runs:
      ```
      git add <tool_name>.py
      git commit -m "Auto-generated tool: <tool_name>"
      ```
      both with `cwd=tools/generated/`, via
      `subprocess.run([...], check=False, timeout=10,
      capture_output=True)`. Failures are **non-fatal** and return
      `(False, summary)` from `_git_commit`; the meta-tool folds
      the summary into its returned dict (`{"git": …, "git_ok":
      false}`) so the MenuBar UI surfaces the failure in the
      `tool_result` thought stream.
    - `DELETE /api/tools/<name>` (Phase 4.5) commits the removal
      with the same cwd and message
      `"Removed generated tool: <name>"`.
    - `Orchestrator.load_dynamic_tools()` skips dotfiles and any
      directory entries, so the nested `.git/` is invisible to
      the loader. Covered by
      `test_load_dynamic_tools_skips_dotfiles`.
  - 🚫 Removed: the earlier Phase 4.5 design that committed to
    the main project repo. No `git -C <repo_root>` calls exist in
    the daemon; the main repo never sees an agent commit.
  - ✅ Tests exercising the isolation rules:
    - `test_load_dynamic_tools_skips_dotfiles` — `.gitkeep` and a
      nested `.git/` sit beside a valid module; only the module is
      loaded.
    - `test_delete_tool_removes_file_and_unloads` — drives the
      DELETE route against a `tmp_path` generated dir and verifies
      the nested-repo removal commit is best-effort (missing git
      binary or un-initialised repo never blocks the response).
    - A dedicated "nested git first-use" regression is deferred
      to follow-up Q17 below; the current suite covers the
      isolation + loader guarantees but not the live `git init`
      transition (which requires a real `git` on PATH and is
      slower than the 1 s test budget we hold the daemon suite
      to). Live exercise happens as soon as the user approves the
      first generated tool.

## 9. Open questions / follow-ups

1. File-watcher noise: Chrome download churn (`.crdownload`) floods the
   stream. Add debounce + extension filter?
2. Closing the approval window via the red X currently no-ops; should we
   treat it as implicit deny?
3. `MacAppListener` has no watchdog: if `_mac_app_helper` exits or
   crashes, the daemon stops receiving app activations silently. Add a
   supervisor that restarts the helper on exit and exposes liveness in
   the Debug tab?
4. Rapid app switches each trigger a full two-engine LLM pipeline.
   Consider debouncing `app_activated` (e.g. require sustained focus
   ≥ 500 ms) or coalescing bursts before handing off to the orchestrator.
5. Extend the editable-approval pattern to other tools: `open_url` (edit
   the URL), `show_notification` (edit title/body), `move_file` (edit
   destination). Current UI falls back to a read-only JSON view.
6. `IMessageWatcher` relies on Full Disk Access being pre-granted.
   Should the daemon detect the `OperationalError` and surface a
   first-run permission prompt through the MenuBar UI instead of a log
   line?
7. Vector memory requires `sqlite-vec` *and* a Python/SQLite build
   compiled with `load_extension` support. The Homebrew default on
   macOS typically lacks it, so most installs currently run the
   recency fallback. Document (or auto-install) a `pysqlite3-binary`
   path, or ship a prebuilt SQLite with the extension enabled?
8. Memory has no eviction, namespacing, or TTL. Over time
   `memory.db` will accumulate every event-related preference we save.
   Add a retention policy (LRU, decayed importance, or manual
   `clear_memory` wired to the existing WS command) before Sprint 4.
9. Generated tools run unsandboxed in the daemon process. The static
   sanity checks (`exec`, `eval`, `__import__`, `shell=True`, …) are
   easy to bypass. If we ever want to relax the HITL gate we will
   need a real sandbox: subprocess isolation with
   `sandbox-exec`/`seatbelt`, a capability-restricted `RestrictedPython`
   interpreter, or running generated code inside a subordinate
   helper like `_mac_app_helper.py`.
10. ~~No uninstall / disable path for generated tools.~~ Addressed by
    Phase 4.5 (`ToolManagerView` + `DELETE /api/tools/<name>`).
    Follow-up: add a *disable without deleting* toggle so the user
    can suspend a suspect tool without losing the source in case
    they want to tweak and re-install it.
11. Tool-name collisions: the design rejects a generated tool whose
    name collides with a built-in. What about collisions between two
    generated tools across daemon restarts (e.g. the agent picks
    `summarize_pdf` twice with different logic)? Current plan: reject
    the second with a surfaced error; alternative is to version the
    file (`summarize_pdf.v2.py`) and let both coexist.
12. Pattern detection cost: every 10 events we hit the analysis
    engine with a 50-event prompt, which is ~1-2 s of Metal time.
    Worth it for a demo, but we may want an adaptive cadence (back
    off when no new patterns are found for N cycles) before this
    ships as a daily-driver feature.
13. ~~Git scope for auto-commit.~~ Resolved by Phase 4.6: commits
    land in a nested repo rooted at `tools/generated/`, never in
    the main project repo, so scope can safely be the whole
    nested working tree without touching anything else.
14. HTTP API surface has no authentication. The daemon binds
    `127.0.0.1`, so any local process can hit `DELETE /api/tools`
    and wipe the agent's learned behaviour. Same threat model as
    the WS channel; deferred unless we add a process-local token
    shared via keychain.
15. Stdlib allow-list is advisory. A generated tool that imports a
    non-stdlib module will fail at `load_dynamic_tools` time with
    an `ImportError`; the static scan catches the obvious cases up
    front but a user-edited `edited_code` could still slip past.
    Should the loader quarantine the file (move to
    `generated/disabled/`) when `exec_module` raises?
16. Tool-manager refresh: the view fetches once on appear and on
    manual refresh. Should it also listen for a new
    `tools_changed` WS broadcast emitted by the orchestrator after
    `load_dynamic_tools` / `unload_dynamic_tool`, so multiple
    open windows stay consistent without polling?
17. Nested-repo first-use regression test: add a slower integration
    case that drives `_git_commit` against a real on-disk `tmp_path`
    so we cover the `git init` transition + identity derivation
    (host global → `bot-inator` fallback). Requires marking the case
    with `@pytest.mark.slow` or a `git`-on-PATH skip so the fast
    suite stays under a second.
