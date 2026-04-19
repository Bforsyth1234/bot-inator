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
│          ◄── FileWatcher   (watchdog)                    │
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

## 3. WebSocket protocol

Endpoint: `ws://127.0.0.1:8000/ws/stream`. Every frame is a single JSON object
validated by `schemas/ws_messages.py::WSMessage` (discriminated on `type`).

Server → client: `status`, `thought`, `approval_request`, `tool_result`, `error`.
Client → server: `approval_response`, `command` (`pause_listeners`,
`resume_listeners`, `reload_model`, `clear_memory`).

Thought stages: `event_received` → `analysis` (optional) → `reasoning` →
`plan` → `tool_result` → `complete`. The `analysis` stage carries the
`MLXEngine.evaluate_event` summary and is skipped when that returns an
empty string.

Approval flow:

1. Orchestrator wraps each registered tool in `_wrap_tool_for_approval`.
2. On invocation, it emits `approval_request` with a fresh `request_id`,
   fans out to every subscriber, and awaits the matching response.
3. Default timeout: 120 s. On timeout or `approved: false`, the wrapper raises
   back into the agent loop which surfaces a `no action` thought.

## 4. Daemon modules (`agent-daemon/`)

| Path                          | Responsibility                                     |
|-------------------------------|----------------------------------------------------|
| `main.py`                     | FastAPI app, `/health`, `/ws/stream`, lifespan.    |
| `config.py`                   | `settings` (model path, watch dirs, timeouts).     |
| `ai/mlx_engine.py`            | Async wrapper over `mlx_lm` (load/unload/swap).    |
| `ai/orchestrator.py`          | Event loop, tool gating, subscriber fan-out.       |
| `ai/memory.py`                | SQLite + `sqlite-vec` short-term memory.           |
| `events/event_bus.py`         | Thread-safe pub/sub bridge to the asyncio loop.    |
| `events/mac_listeners.py`     | Spawns `_mac_app_helper.py` and forwards its JSON  |
|                               | output into the EventBus as `app_activated` events.|
| `events/_mac_app_helper.py`   | Subprocess: main-thread NSRunLoop + NSWorkspace    |
|                               | observer → stdout JSON lines. Never imported by    |
|                               | the daemon; launched via `subprocess.Popen`.       |
| `events/file_watcher.py`      | Watchdog-based FS observer on `settings.watch_dirs`.|
| `schemas/ws_messages.py`      | Pydantic models for every WS frame.                |
| `tools/*.py`                  | `smolagents.Tool` callables; registered via        |
|                               | `AVAILABLE_TOOLS` and passed into the Orchestrator.|

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
`main.py`. Every tool is currently approval-gated.

| Tool               | Effect                                                  |
|--------------------|---------------------------------------------------------|
| `read_active_tab`  | AppleScript → Chrome, then Safari; returns URL/title.   |
| `open_url`         | Opens a URL with `open`.                                |
| `show_notification`| Posts a macOS notification via `osascript`.             |
| `read_clipboard`   | `pbpaste` → string.                                     |
| `write_clipboard`  | `pbcopy` ← string.                                      |
| `summarize_file`   | Metadata + ≤2 KB preview for text files.                |
| `move_file`        | Moves a file into an existing directory.                |

## 7. UI (`MenuBarUI/`)

SwiftUI MenuBar app with an AppKit shell:

- `MenuBarUIApp.swift` — `AppDelegate` launches the WS connection and the
  `ApprovalPresenter` at `applicationDidFinishLaunching`, so thoughts and
  approvals are collected regardless of popover state. `RootPopoverView`
  is a `TabView` with two tabs: "Thoughts" and "Debug".
- `Services/WebSocketManager.swift` — publishes `@Published messages`,
  `pendingApproval`, `lastStatus`, and a `DebugStats` struct (frame
  counts by type, thought counts by stage, decode-error ring buffer,
  connect attempts, last raw frame). Single persistent URLSession WS task.
- `Services/ApprovalPresenter.swift` — subscribes to `pendingApproval` and
  renders `ApprovalAlert` inside a floating `NSWindow` that activates the app.
- `Views/ThoughtStreamView.swift` — scroll-back log inside the popover.
- `Views/DebugView.swift` — in-app diagnostics tab: connection state,
  frame/stage counters, recent decode errors, last raw frame, and a
  placeholder for future tools (daemon log tail, listener health,
  manual event injection). `resetDebugStats()` clears counters.
- `Views/ApprovalAlert.swift` — approve / deny + optional note.

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

## 9. Open questions / follow-ups

1. Should read-only tools (`read_active_tab`, `read_clipboard`,
   `summarize_file`) bypass the approval gate?
2. File-watcher noise: Chrome download churn (`.crdownload`) floods the
   stream. Add debounce + extension filter?
3. Closing the approval window via the red X currently no-ops; should we
   treat it as implicit deny?
4. `MacAppListener` has no watchdog: if `_mac_app_helper` exits or
   crashes, the daemon stops receiving app activations silently. Add a
   supervisor that restarts the helper on exit and exposes liveness in
   the Debug tab?
5. Rapid app switches each trigger a full two-engine LLM pipeline.
   Consider debouncing `app_activated` (e.g. require sustained focus
   ≥ 500 ms) or coalescing bursts before handing off to the orchestrator.
