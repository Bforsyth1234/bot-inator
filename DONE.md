# bot-inator — Done

Full write-ups of finished work, moved here from LIVING_SPEC.md §8 and
todo.md (todo.md rule 1). Newest phase last. Write-ups are kept as they
were when the work closed; later phases supersede earlier ones where they
conflict.

## Phases 1–3 — closed 2026-04-19

#1 — Plumbing [x]
Phase: 1 · Done: 2026-04-19 · Commit: de20698…0ebc0f8 · Source: LIVING_SPEC §8
WS protocol, event ingestion, smolagents adapter, approval round-trip,
floating approval window, 7 real tools.

#2 — MLX AI engine: analysis stage and two-engine design [x]
Phase: 2 Sprint 1 · Done: 2026-04-19 · Commit: 53e292b · Source: LIVING_SPEC §8
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

---

#3 — Event pipeline hardening and in-app diagnostics [x]
Phase: 2 Sprint 2 · Done: 2026-04-19 · Commit: 53e292b, 268139a · Source: LIVING_SPEC §8
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

---

#4 — iMessage watcher and responder [x]
Phase: 3 Sprint 2 · Done: 2026-04-19 · Commit: 91e78eb · Source: LIVING_SPEC §8
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

---

#5 — Local vector memory [x]
Phase: 3 Sprint 3 · Done: 2026-04-19 · Commit: 91e78eb · Source: LIVING_SPEC §8
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

## Phase 4 — closed 2026-05-07

#6 — Autonomous meta-tool generation [x]
Phase: 4 Sprint 1 · Done: 2026-04-19 · Commit: 91e78eb · Source: LIVING_SPEC §8
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
  duplicate suggestions don't flood the user. Before any model
  call, a **repeat gate** (`_has_repetition`) requires some event
  signature (`event_type|app_name|metadata.action`, ignoring window
  titles and paths) to occur `min_repeats` (default 3) times in the
  window; otherwise detection is skipped, because models invent
  patterns from unrelated events. Detection calls
  `generate_chat_sync` on the engine named by
  `settings.pattern_detection_engine` (`AGENT_PATTERN_ENGINE`:
  `main` by default, or `analysis` / `drafting`; chosen by
  `main._select_detection_engine`) via `make_detection_evaluator`, with `DETECTION_SYSTEM_PROMPT` as the
  system role and a 512-token cap (`DETECTION_MAX_TOKENS`). It must
  **not** go through `evaluate_event`: that wraps input in the
  event-summary system prompt ("do not propose actions") and caps
  replies at 128 tokens, which truncated proposals. The reply is
  either the literal string `NO_PATTERN` or one JSON object
  `{"tool_name","description","expected_logic"}`.
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
  7. `_git_commit` — nested-repo scheme from #8.
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
  - (The #7 / #8 tests also exercise the install
    path end-to-end.)

---

#7 — Tool manager UI and auto-versioning [x]
Phase: 4.5 · Done: 2026-04-19 · Commit: 91e78eb · Source: LIVING_SPEC §8
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
  (see #6 for the complete lists).
- ✅ Git auto-commit lives in `meta_tool_generator._git_commit`;
  see #8 for the nested-repo scheme.
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

---

#8 — Source-control isolation for generated tools [x]
Phase: 4.6 · Done: 2026-04-19 · Commit: 91e78eb · Source: LIVING_SPEC §8
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
    to todo.md #30; the current suite covers the
    isolation + loader guarantees but not the live `git init`
    transition (which requires a real `git` on PATH and is
    slower than the 1 s test budget we hold the daemon suite
    to). Live exercise happens as soon as the user approves the
    first generated tool.

---

#9 — Groq cloud provider [x]
Phase: 4.7 · Done: 2026-05-07 · Commit: e0f91ca · Source: LIVING_SPEC §8
- `AGENT_PROVIDER=groq` swaps all three engines for `GroqEngine`
  (`ai/groq_engine.py`); default stays `local`. See LIVING_SPEC §5.
- Secrets come from `agent-daemon/.env` via `python-dotenv`; the
  file is git-ignored and `.env.example` is the template.
- Orchestrator duck-types engines and picks the smolagents adapter
  per provider (`_MLXModel` vs `OpenAIServerModel`).
- `app_activated` no longer starts the tool-calling agent (noise);
  it still feeds pattern detection.
- Tests: `tests/test_groq_engine.py`; integration tests use `file`
  events instead of `app_activated` to start the agent.
- Supersedes §1's original "no cloud inference" goal: cloud is
  opt-in, local is the default.

## Phase 5 — Tool-idea quality (open; finished parts below)

Goal: the agent suggests a useful tool *before* the user thinks to
ask for one, and the drafted code works the first time it runs.
Review finding that started this phase: generated tools were poor
because (a) the idea stage was broken (detection ran through the
event-summary prompt with a 128-token cap on a 1.5B model) and
(b) drafting was single-shot with no feedback.

#10 — Detection fixes [x]
Phase: 5.0 · Done: 2026-11-09 · Commit: 8451553 · Source: LIVING_SPEC §8
- Detection has its own `DETECTION_SYSTEM_PROMPT` and a 512-token
  cap via `make_detection_evaluator` (no longer `evaluate_event`).
- Detection runs on the main engine by default
  (`AGENT_PATTERN_ENGINE`).
- `_parse` uses `json.loads(strict=False)`: models put raw
  newlines in `expected_logic`.
- Repeat gate: no model call unless an event signature repeats
  `min_repeats` (3) times. Real-model A/B (Hermes-3 8B): invoice
  workflow ×3 → `invoice_processor`; 10 random app switches →
  0 calls (previously a hallucinated `daily_rituals`).

---

#11 — Draft → check → repair loop, critic pass, sandboxed dry run [x]
Phase: 5.1 · Done: 2026-11-09 · Commit: 8451553 · Source: LIVING_SPEC §8
`meta_tool_generator._draft_until_clean` drafts, runs
`tools/draft_checks.check_draft`, and on failure re-prompts the
drafter with its previous draft plus the exact problems, up to
`MAX_DRAFT_ATTEMPTS` (3). Each failed draft is dumped to
`generated/_rejected/` and reported to the UI as a `tool_result`
thought. Draft cap raised 1200 → 2048 tokens. User-edited code
goes through the same checks (no repair). Checks, cheapest first,
stopping at the first failing stage:
1. `ast.parse` (syntax, with line number).
2. The existing `_validate_source` safety validator.
3. Inert top level: only imports, a docstring, literal constants,
   and functions with the bare `@tool` decorator and literal
   defaults. This is what makes step 5 safe.
4. Tool signature: type hints on every parameter and the return,
   and an `Args:` entry per parameter (smolagents rejects these).
5. Undefined names via `symtable` (the "called `show_notification`
   without importing it" bug).
6. Import in a fresh subprocess and confirm the attribute is a
   smolagents `Tool`. The tool body is never called: its side
   effects are the user's call.
Real-model check (Qwen2.5-Coder-14B): 4 of 5 specs passed on the
first draft; a spec that tempted `requests` + scraping failed and
was repaired on attempt 2 (stdlib `urllib`, env-var API key).
**Critic pass** (`tools/draft_critic.py`, on by default,
`AGENT_DRAFT_CRITIC=0` to disable). After a draft passes the
structural checks, the drafting engine reviews it against
`description` + `expected_logic`; problems go back into the same
repair loop. Design points, each from a failed real-model run:
- **Grounded problems only.** Each problem must quote the spec
  sentence and the code it violates; `_grounded` drops any problem
  whose quotes are not really in the spec and the code. Without
  this the critic flagged every draft with invented "does not
  handle permission errors" items and repairs never converged
  (3 attempts, 61 s, for drafts that were fine).
- **Comments stripped** (`ast.unparse`) before review. A repair
  added `# Case-insensitive search` above unchanged code, and the
  critic believed the comment.
- **Literal pattern check** in the prompt: a glob that matches more
  files than the spec names is a problem.
- **Advisory.** A critic error or unparseable reply counts as a
  pass. If attempts run out with critic problems left, the last
  structurally clean draft goes to review with the problems as a
  `# REVIEW NOTES` comment header.
Real-model result (Qwen2.5-Coder-14B as drafter and critic):
`remind_me_in` and `tidy_dmg_downloads` passed review first time
(no false alarms). `file_invoices`: the critic caught `*.pdf`
(every PDF) on attempt 2 and the attempt-3 draft globbed
`inv*.pdf` + `*invoice*.pdf` as specified, about 60 s in total.
**Still missed:** that draft is case-sensitive, and a file matching
both patterns (`invoice1.pdf`) is moved twice, so the second
`shutil.move` raises. Human review is still the real gate.

**Sandboxed dry run** (`tools/draft_dry_run.py`, on by default,
`AGENT_DRAFT_DRY_RUN=0` to disable). Runs between the structural
checks and the critic, so the tool is actually executed before the
user sees it.
- **Plan** (`plan_dry_run`, once per tool, reused across attempts so
  every attempt faces the same test): the drafting model writes up
  to 8 fixture files for a fake home (optionally with `age_days` to
  set mtime), 1-3 calls (kwargs), and expectations
  (`expect_status`, `expect_exists`, `expect_missing`).
  `expect_exists` is kept only for fixture files: names of files the
  tool creates were guessed wrong (`inv1041_copy_1.pdf` with no name
  clash).
- **Isolation**, because the code is not approved yet:
  `sandbox-exec` profile (no network, no writes outside the per-call
  temp dir, no exec except Python itself; child processes inherit
  it); `HOME` = fake home; minimal environment (no API keys);
  `subprocess` replaced by a recorder that reports success, and
  `urlopen` refused. A self-test (write inside allowed, write to
  `/tmp` blocked) runs once; if `sandbox-exec` is missing or the
  self-test fails, the dry run is skipped. Unapproved code never
  runs unsandboxed. Tested: writes to the real home, `/tmp`, running
  `osascript`/`sh`, network, and reading `GROQ_API_KEY` are all
  blocked.
- **Hard vs soft** (`DryRunResult`). Hard = observed facts:
  exception (with the draft line), background-thread error, timeout,
  or a return value that is not a status dict. Hard problems drive
  repairs. Soft = mismatch with the planner's expectations; the
  planner was wrong in about half the `file_invoices` runs (expected
  `inv1041_copy.pdf` to stay although `inv*.pdf` matches it), so
  soft problems only become `# REVIEW NOTES` for the human.
- Long timers do not block: the probe reports, flushes, and
  `os._exit`s after joining threads for up to 3 s.
Real-model results (Qwen2.5-Coder-14B):
- Caught the double move: `FileNotFoundError` from the second
  `shutil.move` of a file matching both `inv*.pdf` and
  `*invoice*.pdf`.
- **Found a bug in the drafting prompt itself.** The prompt's
  examples called `show_notification(title=..., message=...)`, but
  the real argument is `body`. Every generated tool that notified
  raised `TypeError` inside a background thread, so the
  notification silently never appeared. That is likely a large part
  of why generated tools seemed broken. Fixed in the prompt, and
  the AVAILABLE TOOLS list now shows each tool's exact arguments
  (`call: show_notification(title: string, body: string)`).
- Planner degenerated into a numbered series (`inv1041.dmg` …
  `inv1070.dmg`) until the token cap; fixed by "at most 8 files,
  never a numbered series".
- Final run: `file_invoices`, `remind_me_in`, `tidy_dmg_downloads`
  each passed on attempt 1 (20-40 s); `file_invoices` carried one
  soft note from a wrong planner expectation.
Also fixed on the way: `_validate_source` banned plain substrings,
so `re.compile(` and `ast.literal_eval(` were rejected as
`compile(` / `eval(`. Banned calls are now matched on the AST.

---

#12 — Idea sources: next step and chat capability gaps [x]
Phase: 5.2 · Done: 2026-11-09 · Commit: 8451553 · Source: LIVING_SPEC §8
Repetition only finds what the user already
does by hand, after three rounds. Other sources, ranked by value
for effort. Each publishes the same `pattern_detected` event, so
the approval → draft → review flow is shared; `metadata.source`
records which source produced the idea.

| # | Source | Signal | Example | Status |
|---|--------|--------|---------|--------|
| 1 | Next step | One high-signal event with an obvious follow-up; no repetition needed. | `.dmg` downloaded → install + delete. Message with a tracking number → track it. | ✅ Done |
| 2 | Chat capability gaps | The agent could not do what the user asked in chat. | "What's on my calendar?" → calendar tool. | ✅ Done |

Rows 3–5 (setup scan, friction, starter catalog) are still ideas: see
todo.md #33–#35.

The repeat gate applies only to source 0 (repetition). Other
sources bypass it.

**Implementation** (`ai/idea_sources.py`, wired in `main.py`, fed
by the orchestrator; both use the detection engine):
- `NextStepSuggester.observe(event)`: a deterministic `classify`
  maps an event to a trigger kind, and only kinds listed in
  `FOLLOW_UPS` qualify (finished `.dmg`/`.pkg`, `.zip`, `.ics`;
  iMessages with a tracking number or a plan with a time). The
  known follow-up is passed to the model, which only writes the
  spec. Per-kind cooldown: 24 h, in memory.
  Why a fixed list: with an open "what's the obvious next step?"
  prompt, Hermes-3 8B proposed a tool for 7/7 events, several
  nonsense ("add a screenshot to the calendar"), and prompt
  tightening made it worse. With `FOLLOW_UPS`, 5/5 qualifying
  events gave sensible specs and 3 non-qualifying events made no
  model call. Adding a kind = adding a `FOLLOW_UPS` entry and a
  classifier rule (merges into todo.md #35, the starter catalog).
- `ChatGapDetector.observe_chat(user_text, reply, tools_called)`:
  runs after each chat turn; triggers when the reply admits it
  could not help (`_GAP_RE`) and `generate_custom_tool` was not
  already tried this turn (`Orchestrator._turn_tool_calls`). The
  model turns the request into a proposal or `NO_SUGGESTION`.
  Cooldown: 1 h per request (bag of words). Real-model check: 4/4
  correct (calendar and disk-space gaps → tools; a trivia question
  and a poem → `NO_SUGGESTION`).
- Both publish `pattern_detected` with `metadata.source`
  (`next_step` / `chat_gap`) and `metadata.trigger`;
  `_build_pattern_prompt` words the agent prompt per source.
- `FileWatcher` now handles `on_moved` (`action: "renamed"`, path
  = destination). Browsers finish downloads with a rename from
  `.crdownload`/`.download`, so finished downloads were previously
  invisible.

---

#13 — Open-help idea source: ask, filter, learn [x]
Phase: 5.3 · Done: 2026-11-09 · Commit: 707e813 · Source: LIVING_SPEC §1
Implemented by `ai/open_help.py` (`OpenHelpSuggester`, source
`open_help`): it asks over a rolling window of events at most once per
`AGENT_OPEN_HELP_INTERVAL`, a judge call scores each idea 0-10 against
`AGENT_OPEN_HELP_MIN_SCORE`, and approve/reject answers on
`generate_custom_tool` are kept in `~/.bot-inator/idea_feedback.json`.
Live run (2026-11-09): with the interval at 60 s, 3 test files in
~/Downloads produced judged ideas (`csv_merge`, `csv_diff`,
`recent_apps`); `csv_diff` went idea → approval → draft → review →
installed and committed in the nested repo.

## Wave 1 — closed 2026-11-09

Wave-close run: full suite 228 passed, 3 runs in a row. No Swift changes.
Registered by the runner: `find_files`, `list_shortcuts`, `run_shortcut`,
`create_note`, `append_note`, `list_windows`, `open_app` (read-only:
`find_files`, `list_shortcuts`, `list_windows`). #43, #44, #46 and #47 are
built and wait in todo.md as `[~] live`.

#39 — Groq engine tests fail on Python 3.14 [x]
Wave: 1 · Agent: _ · Added: 2026-11-09 · Source: assistant (test run)
Ask: 6 tests in `tests/test_groq_engine.py` fail with "There is no
current event loop in thread 'MainThread'": they call
`asyncio.get_event_loop().run_until_complete(...)`, which Python 3.14 no
longer supports without a running loop. Fails on the untouched baseline
too.
Approach: use `asyncio.run(...)` in the tests.
Verify: `pytest tests/test_groq_engine.py` 8/8.
Results (wave 1, 2026-11-09):
⚬ The 6 `asyncio.get_event_loop().run_until_complete(...)` calls in
  `tests/test_groq_engine.py` are now `asyncio.run(...)`. 8/8 pass; the old
  file gives 6 failed.
Deviations: none.

---

#40 — test_dry_run fails depending on test order [x]
Wave: 1 · Agent: _ · Added: 2026-11-09 · Source: assistant (test run)
Ask: `tests/test_dry_run.py::test_parse_plan_keeps_exists_checks_only_for_fixtures`
failed in one full-suite run and passed alone and in later full runs.
Approach: find the shared state (module globals or env) another test
leaves behind; run the suite with `-p randomly` or in reverse order.
Verify: 5 full runs in random order, no failure.
Results (wave 1, 2026-11-09):
⚬ Not reproducible: 5 fully shuffled runs (seeds 1–5), reverse file order,
  and `test_dry_run.py` after every other file all passed. `parse_plan` is
  pure, nothing patches or reloads `tools.draft_dry_run`, and the autouse
  fixture only resets `_SANDBOX_OK`. No code changed.
⚬ After #48 the full suite passed 3 runs in a row (228 passed).
Deviations: closed without a code change: no shared state was found. The likeliest cause
of the single failure is a run against a half-edited `draft_dry_run.py`
(unproven). Reopen as a new item if it recurs.

---

#30 — Nested-repo first-use regression test [x] [Q17]
Wave: 1 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q17)
Ask: Add a slower integration case that drives `_git_commit` against a
real on-disk `tmp_path`, covering the `git init` transition and identity
derivation (host global → `bot-inator` fallback). Mark it
`@pytest.mark.slow` or skip when `git` is not on PATH, so the fast suite
stays under a second.
Verify:
Results (wave 1, 2026-11-09):
⚬ `tests/test_git_commit.py`, 7 tests (~1 s): first commit runs `git init`
  with the fallback identity; identity from the global config; partial
  global identity falls back per key; second commit; nothing-to-commit and
  missing-git are non-fatal; missing directory is non-fatal. Isolated HOME,
  `GIT_CONFIG_GLOBAL` to a tmp file, `GIT_CONFIG_NOSYSTEM=1`, repos only
  under `tmp_path`; skipped when `git` is missing.
⚬ Non-vacuity: 4 mutations of `_git_commit` (fallback email, ignoring the
  global name, skipping `git init`, dropping the `FileNotFoundError` handler)
  each failed 1–4 tests.
Deviations: no `@pytest.mark.slow`: the marker isn't registered, so only the skip-if-no-git
is used. Three `_git_commit` bugs found and handed to #24.

---

#48 — Approval wrapping stacked on every agent rebuild [x]
Wave: 1 (runner, found at wave close) · Added: 2026-11-09 · Source: assistant (#40's test runs)
Ask: The full suite failed 17 tests with "Event loop is closed", with
`gated_forward` nested 13 deep in one traceback.
Cause: `Orchestrator._wrap_tool_for_approval` replaced `forward` on the
shared tool objects in `AVAILABLE_TOOLS`, and `_build_agent` runs again
after every dynamic-tool load. Each rebuild wrapped the previous wrapper,
so one tool call raised one approval request per layer, and stale layers
stayed bound to old orchestrators and closed loops. In the daemon this
means repeated approval prompts for a single call after tool installs
(likely related to #38's 4× prompts).
Fix (`ai/orchestrator.py`): wrap a shallow copy of the tool; the original
`forward` is kept as `_ungated_forward`, so re-wrapping a wrapper still
gates once. The shared tool objects are never changed.
Results (wave 1, 2026-11-09):
⚬ `tests/test_tool_wrapping.py`: 3 rebuilds (including wrapping a wrapper)
  → exactly 1 approval request per call; the shared tool stays ungated;
  a wrapper from a closed-loop orchestrator never leaks. Non-vacuity: with
  the old code, 3 approval requests for 1 call (2 failed).
⚬ Full suite 228 passed, 3 runs in a row (was 17 failed).
Deviations: none.

---

#49 — Drafting prompt printed wrong import paths for built-ins [x]
Wave: 1 (runner, found by #47 and #46) · Added: 2026-11-09 · Source: assistant
Ask: `_format_available_tools_context` built each built-in's import as
`from tools.<tool name> import <tool name>`. Wrong for tools in a shared
module (`tools.windows` holds `list_windows` and `open_app`;
`tools.notes`, `tools.shortcuts` likewise), so a generated tool copying
the line would fail to load.
Fix (`tools/meta_tool_generator.py`): `_builtin_import_path` finds the
`tools.*` module that really defines the tool object; the drafting prompt
now says the module name can differ and to copy the listed line.
Results (wave 1, 2026-11-09):
⚬ `tests/test_tools_context.py`: every listed import line imports that exact
  tool object; shared-module paths are right. Non-vacuity: with the old
  code both tests fail.
Deviations: none.

## Wave 2 — closed 2026-11-09

Wave-close run: full suite 287 passed in 11 of 12 runs (1 unidentified
failure, tracked as #50). No Swift changes. All eight items (#14, #16,
#17, #20, #21, #24, #25, #36) are built and wait in todo.md as `[~] live`.
Runner wiring: `GET /health` reports the app-listener status; the WS
`clear_memory` command now deletes memories; tests use a temp memory DB
(`AGENT_MEMORY_DB`); README gains the uv / sqlite-vec setup note.

## Wave 3 — closed 2026-11-09

Wave-close run: full suite 361 passed, 4 of 5 runs clean; the 5th exposed
#50's flaky restart test (deadline raised to 10 s). Swift type-check clean.
All eight items (#23, #28, #38, #41, #42, #33, #35, #34) are built and wait
in todo.md as `[~] live`. Runner wiring: `read_email`, `list_events`
(read-only) and `create_event` (gated) registered; mail, calendar and
clipboard watchers started; friction detector and setup scanner wired with
one shared `IdeaFeedback`; `mail_received`, `event_starting_soon` and
`clipboard_changed` feed idea sources but don't start the tool-calling
agent; event descriptions and source intros added.

## Wave 4 — closed 2026-11-09

Wave-close run: full suite 457 passed, 3 runs in a row; Swift type-check
clean. All five items (#32, #45, #15, #18, #26) are built and wait in
todo.md as `[~] live`. Runner wiring: Reminders tools registered
(`list_reminders` read-only); one shared `IdeaState` and `IdeaFeedback`;
the pattern prompt calls built-in follow-up tools directly and a declined
built-in is remembered as a rejection; conftest blanks `AGENT_PROVIDER_*`.

## Wave 5 — closed 2026-11-09

Wave-close run: full suite 476 passed, 3 runs in a row; Swift type-check
clean. #19, #29, #31 built and wait in todo.md as `[~] live`. New gap #51
(local idea text quoting personal text into a cloud prompt) moved to wave 7.

## Wave 6 — closed 2026-11-09

Wave-close run: full suite 505 passed, 3 runs in a row; Swift type-check
clean. #27 (Origin check + token) built and waits as `[~] live`.

#50 — One unidentified full-suite failure in 12 runs [x]
Wave: — (watch) · Agent: _ · Added: 2026-11-09 · Source: assistant (wave 2 close)
Ask: At wave 2 close one full run reported `1 failed, 286 passed`; the next
11 full runs were all green, so the failing test wasn't captured. Suspects:
the timing-based tests added in wave 2 (`test_mac_listeners.py` uses real
subprocesses and sub-second timings).
Approach: when it happens again, record the test id (run with
`-p no:cacheprovider -rf`) and give that test timing headroom.
FROM WAVE 3 close (2026-11-09): caught it:
`test_mac_listeners.py::test_helper_restarts_after_crash_with_backoff` (4
interpreter starts in a 2 s deadline). Deadline raised to 10 s; the gap
assertions stay exact. Also seen: one full run took 108 s instead of ~36 s;
found: `test_memory.py::test_close_while_searching_from_threads` (47 s to
minutes: 4 spinning workers starved `close()` of the lock). Workers now
yield and the close loop is time-boxed: ~3.5 s, still segfaults 3/3 with the
lock removed. Suite back to 25–32 s, 361 passed ×3.
Verify: 10 consecutive full runs green after the fix.
Results (wave 6 close, 2026-11-09): the real cause was a bug, not only
timing: `MacAppListener`'s supervisor recorded the SIGTERM from `stop()` as
the helper's exit (`last_exit_code` -15), overwriting the last real crash.
Fixed in `events/mac_listeners.py` (an intentional stop no longer updates
`last_exit_code`). The test failed 3/8 and 1/8 before, passed 12/12 after.
Also at wave 6 close: conftest pins `AGENT_CLOUD_PERSONAL_TEXT=0` so a
developer `.env` opt-in doesn't change test results.
Deviations:

## Wave 7 — closed 2026-11-09

Wave-close run: full suite 523 passed, 3 runs in a row (~74 s); Swift
type-check clean. #22 (sandboxed generated tools) and #51 (idea-text scrub)
built and wait as `[~] live`. New #53: `open`/`osascript` inside the sandbox
can reach other apps; needs a decision.

## Live checks — 2026-11-09

#37 — Clear test entries from idea_feedback.json [x]
Wave: — (live checks) · Agent: user · Added: 2026-11-09 · Source: assistant (live run)
Ask: The 2026-11-09 test runs saved approvals for test ideas
(`create_invoice_template`, `invoice_number_generator`, `file_watcher`,
`app_switcher`, `recent_apps`, `recent_apps_list` ×4) in
`~/.bot-inator/idea_feedback.json`. The judge sees them as "the user's
past verdicts" and will favour similar ideas.
Approach: remove those entries (or the file) while the daemon is stopped.
Verify: the file holds no entry with an `at` timestamp from the test runs.
Results (live, 2026-11-09): backed up to
`~/.bot-inator/idea_feedback.json.bak-2026-11-09` (0600), then removed only
the 16 entries from the two runner test runs (10:30–11:00). The 30 later
entries are the user's real verdicts and were kept (several are 4–5
identical approvals in one minute: the #48 stacked-approval bug).
Deviations: kept post-11:00 entries the item listed (app_switcher,
recent_apps_list): they came from the user's own daemon, not the test runs.

---

#54 — Judge replies came back empty on Groq reasoning models [x]
Wave: live checks (runner) · Added: 2026-11-09 · Source: assistant (#36 live check)
Ask: Every judge evaluator (open-help, friction, setup scan) used
`max_tokens=120`. GPT-OSS on Groq spends tokens on hidden reasoning first, so
the reply was `''` and every judged idea was dropped as unparseable: under
Groq, those sources never surfaced anything.
Fix: `JUDGE_MAX_TOKENS = 600` in `ai/open_help.py`, used by all three judges
in `main.py` and the `python -m ai.open_help` CLI. The same call that
returned `''` at 120 returned `{"score": 10, …}` at 600.
Results: `tests/test_judge_budget.py` (budget ≥400; no judge literal under
400 in main.py / open_help.py); non-vacuity: old code → the test fails.
Judge eval after the fix: 8/9 match (was 7/9, by accident). Full suite green.
Deviations: none.

---

#55 — Chat and event turns failed on Groq (tool-call rejections) [x]
Wave: live checks (runner) · Added: 2026-11-09 · Source: user ("it has not answered")
Ask: "list my shortcuts" ran `list_shortcuts` and then the chat stayed
silent. Groq validates tool calls server-side and failed the whole turn
with a 400 in three ways seen in the log: (1) "Tool choice is required"
(smolagents sends `tool_choice="required"`; the model answered in text after
the tool result); (2) "Failed to parse tool call arguments as JSON"; (3)
`create_reminder` "`/due`: expected string, but got null" (the model sends
null for optional params; also "called tool 'answer' which was not in
request.tools").
Fix (`ai/orchestrator.py`): `_make_groq_model_class` wraps
`OpenAIServerModel`: `tool_choice="auto"` and a plain-text reply becomes the
`final_answer`; optional parameters are nullable in the schemas sent to Groq
(`_allow_null_optionals`); one retry on a tool-call rejection 400
(`_is_tool_call_rejection`), other errors raise. `gated_forward` drops
null arguments so tool defaults apply. Local MLX is unchanged.
Results: `tests/test_groq_model.py` (5) and a null-argument test in
`tests/test_tool_wrapping.py`. Live: "list my shortcuts" → `list_shortcuts`
→ "Your shortcuts are: Open App, What's a shortcut?, Take a Break, Text Last
Image, Shazam shortcut"; no 400s in the log.
Deviations: none.

---

#57 — Repeated approved calls; declined tool ideas asked again [x]
Wave: live checks (runner) · Added: 2026-11-09 · Source: user ("it opened goole.com 3 times"; "i keep denying send imessage … it should just remember i denied it and stop asking me"; "we should not let a reworded one be asked")
Cause 1: after `open_url` ran, GPT-OSS replied with no visible text;
smolagents treated that as a parse error and the model called `open_url`
again, three times in one turn.
Cause 2: `send_imessage` was never registered (since April), so for every
incoming iMessage the responder asked to *generate* a send tool: one build
card per message, and denials were not consulted.
Fix (`ai/orchestrator.py`, new `ai/idea_match.py`):
- An empty reply after a tool result ends the turn ("Done.").
- A side-effecting call with the same tool and arguments runs at most once
  per turn (`_turn_done`); a repeat returns the first result, no approval.
- Before any `generate_custom_tool` approval, `_previously_declined` asks
  `DeclinedIdeaMatcher`: same name / word overlap → declined; model2vec
  (`minishlab/potion-base-8M`, local) cosine ≥0.80 → declined; 0.55–0.80 →
  one judge call ("is this the same tool?"). Declined → error
  `user_declined_before`, no card. Without model2vec only names/word overlap
  count. `model2vec` added to requirements.
- `send_imessage` stays unregistered (the user doesn't want it; others
  might, and their own approve/deny now sticks). iMessages still reach the
  agent.
Results: tests `test_tool_wrapping.py` (+3), `test_groq_model.py` (+2),
`test_idea_match.py` (5). Real check (user's 7 send_imessage denials, Groq
judge): 8/8: "send a text message", "reply to the iMessage sender", "text
someone", a reworded Chrome↔Claude switcher → declined; "read my
iMessages", "send an email", "add a calendar event", "file invoices" →
allowed. Removing denials is #56.
Deviations: none.

## Wave 8 — closed 2026-11-09

Wave-close run: full suite 661 passed (3 runs at 659 before the wiring
test); Swift type-check clean. #52, #53, #56, #58, #59, #61 built and wait in
todo.md as `[~] live`. Runner wiring: `read_active_tab_text` registered
read-only; `routes/ideas.py`, `routes/permissions.py`, `routes/tool_detail.py`
included in `main.py` (FastAPI 0.162 keeps them as one `_IncludedRouter`
entry each, so `tests/test_wave8_wiring.py` calls them through the app);
`app.state.idea_feedback`; `set_current_idea` in the pattern turn;
`PermissionsView` in Settings, `DeclinedIdeasView` in Tool Manager.

## Wave 9 — closed 2026-11-09

Wave-close run: full suite 679 passed (×3 by the agent, ×1 by the runner);
Swift type-check clean; Info.plist valid. #60 (Calendar through EventKit in
the menu bar app) built and waits as `[~] live`. Every todo item is now
built; what remains is the combined live-check list.

## Wave 10 — closed 2026-11-10

Wave-close run: full suite 791 passed; Swift type-check clean. Built and
waiting as `[~] live`: #63 (email drafts in Mail, never sends), #65
(morning brief: scheduler, `brief_me`, Settings section), #66 (open-help,
friction and pattern ideas no longer propose app switchers). #67 found no
bug: only 2 memories were ever saved because the model must choose
`remember_preference`; the chat prompt now names it. Write-ups stay in
todo.md until the live checks run.

## Wave 11 — closed 2026-11-10

Wave-close run: full suite 813 passed (agent), Swift type-check clean.
#64 (plain-language tool summary on the idea card, the code-review card
and Tool Manager) built and waits as `[~] live`.

#68 — Idea scrub leaked the last word of a single-quoted quote [x]
Added and fixed 2026-11-10 by the runner (found by the #64 agent).
`privacy.scrub_personal_text` turned `'dinner at 7 with Sam'` into
`'[placeholder] Sam'`: `_WORD_RE` kept the closing apostrophe in the token
(`sam'`), so the last n-gram never matched the personal text. This reached
the #51 pattern prompt and the #64 summary prompt under a cloud engine.
Fix: `_words` in `ai/privacy.py` drops trailing apostrophes from each
token. Test: `tests/test_idea_privacy.py::
test_single_quoted_quote_does_not_leak_its_last_word` (fails without the
fix). Not changed: tool names (e.g. `dinner_reminder`) are never scrubbed.

## Wave 12 — closed 2026-11-10

Wave-close run: full suite 814 passed (runner); Swift type-check and an
optimized build clean. #62 (larger fonts, Text size setting) built and
waits as `[~] live`. No `[ ]` item is left in todo.md; what remains is
the combined live-check list.

## Live checks — marked done 2026-11-10

The user marked every `[~] live` item done on 2026-11-10 ("mark all the
live checks as done"). No per-check results were recorded in this pass:
the checks below are kept as the reference for what each item should do,
not as evidence that each was run. Results recorded earlier are under
"Live checks — 2026-11-09".

Filled in as items reach `[~] live`. Each line: item, what to do, what
to see.

- #43 Shortcuts: ask "list my shortcuts" → names, no approval card. "Run my <name> shortcut" → approval card → it runs. Run one Shortcut that returns text and confirm the text comes back.
- #44 Spotlight: ask "find my latest invoice PDF" → a real path, no approval card.
- #46 Notes: "save a note called Test with hello" → approval card → Automation prompt for Notes → a "Test" note. "Append 'world' to my note Test" → approval → new line. Appending to a missing title → error, no new note.
- #47 Windows: "what windows do I have open?" → real windows (titles if Screen Recording is granted, otherwise the note). "Open Numbers" → approval card → Numbers opens.
- #14 File watcher: download a .dmg in Chrome into ~/Downloads → one `file`/`renamed` event for `X.dmg` in the Debug tab, none for `.crdownload` or `.com.google.Chrome.*`.
- #16/#17 App listener: `kill <mac_app_helper pid>` → log "restarting in 1.0s", a new pid, `/health` shows `restart_count: 1`, `alive: true`. Fast Cmd-Tab cycling emits only the app you land on; holding >500 ms emits exactly one `app_activated`.
- #20/#21 Memory: restart the daemon → log "Backfilled vectors for N memories", no "Vector memory search disabled". Send the `clear_memory` command → `memories` table empty.
- #24 Names: ask the agent to create a tool named like an installed generated tool → error naming it, no code-review card. Delete it in Tool Manager, ask again → review card.
- #25 Cadence: repetitive activity that matches nothing → INFO "pattern detection interval" lines at 120/240/480/600 s.
- #36 Judge: `cd agent-daemon && .venv/bin/python -m ai.open_help` (add `--generate` to have the model write ideas). Record "pass rate X/9" and "matches expected Y/9". Target 3/9 and 9/9 (old always-pass judge: 8/9, 4/9).
- #23 Disable: in Tool Manager, switch a generated tool off → "Disabled", file in `generated/disabled/`, agent no longer offers it; on → usable again.
- #28 Quarantine: restart the daemon → any broken module in `generated/` (maybe `recent_apps*.py`) moves to `generated/quarantine/` with `.error.txt`; its row shows "Failed to load: …".
- #38 One call per turn: start a generation that fails → exactly one approval prompt in that turn.
- #41 Mail: grant Full Disk Access, run `cd agent-daemon && .venv/bin/python -m events.mail_watcher --schema`, paste the output into #41 (adjust `SCHEMA` if it reports gaps). Send yourself an email → `mail_received` in Debug within ~10 s, no Sent copy. Ask "what did my last email say?" → `read_email`, no approval card.
- #42 Calendar: `cd agent-daemon && .venv/bin/python tools/calendar_access.py --check` → record whether the prompt appears and for which app, and whether today's events list; rerun after a restart → `full_access`, no prompt (if silently denied, add `--applescript` and pin `AGENT_CALENDAR_BACKEND`). "What's on my calendar today?" → events, no card. "Add lunch with Sam tomorrow at noon" → card → event in Calendar. An event 10 min out → `event_starting_soon` in Debug.
- #33/#35 Setup scan: delete `~/.bot-inator/setup_scan.json`, restart (local provider) → log "setup scan done: N idea(s) queued", first idea card ~2 min later; second restart doesn't rescan; with Groq, "shell history skipped".
- #34 Friction: copy in Mail → Cmd-Tab to Numbers → back, ×5 → `clipboard_changed {app: Mail, kind: text}` (no content), then a friction idea card if the judge passes it. Copy a password from a password manager → no `clipboard_changed`.
- #32 Idea state: decline a next-step idea, restart, trigger it again → no card; `~/.bot-inator/idea_state.json` holds the cooldown. Email "Invitation: X @ <time>" → read_email then create_event card; "Your order has shipped" → open_url card; "Please submit Y by Friday" → create_reminder card; a meeting with a Zoom link → open_url card before it starts.
- #45 Reminders: `cd agent-daemon && .venv/bin/python tools/calendar_access.py --check --reminders` → prompt, `full_access`, lists (else add `--applescript`). "Remind me to call the vet tomorrow at 9" → card → reminder at 09:00 with an alert. "What's on my reminders?" → list, no card.
- #15 Red X: close the approval window → one deny in the daemon log, tool skipped; Approve then close → one response; close the code review window → one `code_approval_response` denial.
- #18 Cards: for each card (open_url, show_notification, move_file, create_event, create_reminder, create_note, append_note, run_shortcut, open_app) change a field → Approve → the tool ran with the edit; Approve unchanged → no `edited_args`; clear a required field → Approve disabled.
- #26 Providers: Settings → Drafting = Groq, default Local → save, restart → "Running now" lists each engine; a tool draft goes to Groq while per-event analysis stays on MLX.
- #19 Notices: start the daemon without Full Disk Access → two banners (iMessages, Mail); "Open Settings" opens Privacy → Full Disk Access; dismiss hides them; a new client sees the replay. With access granted + restart → no banner. Debug "Listener health": nsworkspace alive, restarts 0; `kill <helper pid>` → restarts 1 within ~5 s.
- #29 tools_changed: with Manage Tools open, `curl -X POST http://127.0.0.1:8000/api/tools/<t>/disable` → the row updates without refresh (after #27, add the token header).
- #31 Privacy: `AGENT_PROVIDER_MAIN=groq` → Debug shows "main: groq (cloud)" and "Personal text to cloud: kept on this Mac"; an inbound iMessage's reasoning prompt shows `[personal text kept on this Mac]`.
- #27 Auth: build and launch the app → it connects, approves, lists tools, saves settings; `ls -l ~/.bot-inator/token` → `-rw-------`. Move the token away + restart the app → Offline with the hint; put it back → reconnects. Settings → Restart Daemon → comes back and reconnects. From a browser devtools console, `new WebSocket("ws://127.0.0.1:8000/ws/stream")` → refused; log "Refused websocket /ws/stream: Origin not allowed". Every curl in these checks now needs `-H "Authorization: Bearer $(cat ~/.bot-inator/token)"`.
- #52 Toggle: Settings (an engine on Groq) → flip "Let cloud models see email and message text" off → the next iMessage/email prompt in the daemon log shows `[personal text kept on this Mac]`; on → real text. Reconnect → Debug matches.
- #53 Sandbox: a generated tool that calls show_notification still notifies; one that creates a Notes note still works. Dismiss/deny any stray Finder or Terminal Automation prompts left from the agent's tests.
- #56 Declined ideas: Tool Manager → Declined ideas lists `send_imessage` with its count → Remove → the next iMessage brings one build card back. Clear all → confirm → approved ideas stay.
- #58 Page text: turn on Chrome → View → Developer → Allow JavaScript from Apple Events (Safari → Develop → Allow JavaScript from Apple Events). Open an article → "summarize the page I'm on" → summary, no card. Setting off → the agent names the menu item. Bank page → sensitive-site error.
- #59 Permissions: Settings → Permissions lists 11 rows; "Request" on Automation → Calendar shows the macOS prompt; "Open Settings" on FDA opens it; refresh after granting turns the row green.
- #61 Tool detail: Tool Manager → click find_files → "Runs without asking"; open_url → "Needs your approval"; "Open in editor" opens the file as text; a newly generated tool shows its idea and trigger.
- #60 Calendar (app): rebuild and restart the menu bar app (README), then "what's on my calendar today?" → the macOS Calendar prompt comes from the app → Allow → events in seconds, no card; Settings → Permissions shows Calendars granted. "Add lunch with Sam tomorrow at noon" → card → event in the default calendar. An event 10 min out → `event_starting_soon` in Debug. Quit the app → calendar tools answer "open the bot-inator menu bar app". (A re-signed rebuild may ask again.)
- #22 Sandbox: re-enable csv_diff in Manage Tools, run it from chat → one approval → correct result. A generated tool that calls show_notification still shows the notification. Check the daemon log has no "refusing to load generated tools (no sandbox)".
- #51 Idea scrub: with Groq main and local detection (and `AGENT_CLOUD_PERSONAL_TEXT` not set to 1), an iMessage-derived idea's prompt shows `[personal text kept on this Mac]` instead of the message words.
- #63 Email drafts: "draft an email to <my address> saying hi" → card → approve (first use: Automation → Mail prompt) → an unsent draft opens in Mail. "Reply to my last email saying thanks" → a threaded reply draft with the original quoted below.
- #65 Morning brief: restart the daemon, rebuild the app; Settings → Morning brief → set the time 2 minutes ahead → exactly one brief in Thoughts and one notification, no approval card; a source it can't read shows "unavailable: <reason>". "Brief me now" → a second one at once. "Brief me" in Chat → the same content.
- #66 Idea grounding: `cd agent-daemon && .venv/bin/python -m ai.open_help` → "matches expected 14/14", the three `good_*` windows still pass. After a day of normal use: no app-switch idea card; `grep "open_help dropped\|friction dropped\|pattern detection dropped"` in the daemon log shows the drops.
- #67 Memory: in Chat, "remember that I prefer short replies" → `sqlite3 -readonly ~/.bot-inator/memory.db "select count(*) from memories"` → 1; a new chat message shows a "Recalled: …" thought in Debug.
- #64 Tool summary: rebuild the app, restart the daemon, ask in Chat for something no tool does ("make a tool that renames new screenshots on my Desktop to their date") → the idea card leads with "What this tool would do" in plain sentences ending "Each use asks for your approval first."; the code-review card leads with the same plus "Its code …" naming internet, apps and folders; after install, Tool Manager's detail shows it under "What it does".
- #62 Fonts: rebuild the app (README), then Settings → Appearance → Text size → Small / Medium / Large: text in the panel, "Open in window", Chat, Settings, Manage Tools and the next approval or code-review card resizes at once, nothing clipped, Approve/Deny visible.

## Items closed 2026-11-10 (moved from todo.md)

#14 — File-watcher noise from in-progress downloads [x] [Q1]
Wave: 2 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q1)
Ask: Chrome download churn (`.crdownload`) floods the stream. Add
debounce + extension filter? Phase 5.2 added `on_moved`, so the finished
download now arrives as a `renamed` event; the in-progress churn is
still unfiltered.
Verify:
Results (wave 2, 2026-11-09):
⚬ `events/file_watcher.py`: created/modified events for dotfiles and
  `.crdownload/.download/.part/.tmp` are dropped; a rename to a temp name or
  dotfile is dropped; a rename from a temp name to a real file is emitted
  (destination path). Modified events debounced to 1 per path per 2 s
  (injectable clock); created/renamed always pass. 9 tests in
  `tests/test_file_watcher.py`; non-vacuity: no filter → 6 fail, no debounce
  → 3 fail.
Deviations: a created or renamed event also opens the path's 2 s window, so the burst
of modified events right after a file appears is absorbed.

---

#15 — Closing the approval window with the red X [x] [Q2]
Wave: 4 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q2)
Ask: Closing the approval window via the red X currently does nothing.
Should it count as an implicit deny?
Verify:
Results (wave 4, 2026-11-09):
⚬ `ApprovalPresenter.swift`: a `WindowCloseHandler` (window delegate) sends
  Deny once when the user closes the approval window (`approval_response`)
  or the code review window (`code_approval_response`); buttons share the
  same once-only guard, so click-then-close sends one response. Esc was
  already Deny. swiftc clean.
Deviations: when a new request replaces an open window, the old one isn't denied; it
times out as before.
Live (2026-11-09): red X on an open_url card → `user_denied`, nothing
opened. ✔ Still to do: the code review window's red X.

---

#16 — MacAppListener watchdog [x] [Q3]
Wave: 2 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q3)
Ask: `MacAppListener` has no watchdog: if `_mac_app_helper` exits or
crashes, the daemon stops receiving app activations silently. Add a
supervisor that restarts the helper on exit and exposes liveness in the
Debug tab?
Verify:
Results (wave 2, 2026-11-09):
⚬ `events/mac_listeners.py`: supervisor thread restarts the helper with
  backoff 1→60 s, reset after 60 s up; `stop()` never restarts. Liveness:
  `is_alive`, `restart_count`, `last_error`, `last_exit_code`, `status()`.
  Runner wired `status()` into `GET /health` as `listeners.nsworkspace`.
⚬ `events/_mac_app_helper.py` exits on `BrokenPipeError` and when its parent
  daemon is gone (no orphans after restarts).
⚬ `tests/test_mac_listeners.py` (9 tests with real fake-helper subprocesses);
  non-vacuity: no doubling → 2 fail, no restart → 3 fail, no reset → 1 fails.
Deviations: the helper's exit-when-orphaned check was added (not in the brief). Showing
liveness in the Debug tab needs a WS field: moved to #19 (wave 5).

---

#17 — Debounce rapid app switches [x] [Q4]
Wave: 2 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q4)
Ask: Since Phase 4.7, `app_activated` no longer starts the tool-calling
agent. It still runs `evaluate_event` on the analysis engine per switch;
a debounce (sustained focus ≥ 500 ms) is still open.
Verify:
Results (wave 2, 2026-11-09):
⚬ Debounce in the parent (`mac_listeners.py`): `app_activated` emitted only
  after 500 ms of focus; a newer switch cancels the pending one; the event
  keeps the activation time. Tested with #16's suite; non-vacuity: no cancel
  → 2 fail.
Deviations: A→B→A within 500 ms emits nothing new (focus never really left A). Parent,
not helper: the helper is AppKit-only and loses state on every restart.

---

#18 — Editable approval for more tools [x] [Q5]
Wave: 4 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q5)
Ask: Extend the editable-approval pattern to other tools: `open_url`
(edit the URL), `show_notification` (edit title/body), `move_file` (edit
destination). The current UI falls back to a read-only JSON view.
FROM WAVE 3 #42 (2026-11-09): the `create_event` card shows Title, Date,
Start–End (local time), Calendar ("Default calendar" if empty), Location,
Notes (truncated).
Verify:
Results (wave 4, 2026-11-09):
⚬ `ApprovalAlert.swift`: cards from `ApprovalEditSpec` (pure functions:
  `mergedKwargs`, `initialValues`, `editedArgs`, `isComplete`); positional
  args mapped by signature order; `edited_args` sent only when a field
  changed; Approve disabled while a required field is empty. Cards:
  send_imessage, open_url, show_notification, move_file (`src`, `dst_dir`),
  create_event, create_reminder, create_note, append_note, run_shortcut,
  open_app; everything else keeps the JSON view. A scratch harness against
  the real file passed (mutation of the changed-only guard failed 2 checks).
Deviations: send_imessage now sends `edited_args` only when something changed (same
daemon result).
Live (2026-11-09): open_url card edited example.com → example.org →
example.org opened once. ✔ Other cards still to check.

---

#19 — First-run Full Disk Access prompt for the iMessage watcher [x] [Q6]
Wave: 5 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q6)
Ask: `IMessageWatcher` relies on Full Disk Access being pre-granted.
Should the daemon detect the `OperationalError` and surface a first-run
permission prompt through the MenuBar UI instead of a log line?
DECISIONS (assistant, 2026-11-09): seen live: the 2026-11-09 runs logged
"Could not open … chat.db read-only … needs macOS Full Disk Access", and
nothing told the user.
FROM WAVE 2 #16 (2026-11-09): add `listener_health: dict[str, dict] |
None` to `StatusPayload` (filled from `app.state.mac_listener.status()`)
and show `alive` / `restart_count` in the Debug tab. `/health` already has it.
Verify:
Results (wave 5, 2026-11-09):
⚬ New `notice` frame (`NoticePayload{id, level, title, body, action}`,
  `full_disk_access_notice()`). iMessage and mail watchers detect missing Full
  Disk Access and push one `system_notice` each; the orchestrator publishes
  each id once per run and `ws_stream` replays active notices after hello.
  `NoticeBanner.swift` (top of the popover) with "Open Settings" (only
  `x-apple.systempreferences:` URLs), dismissible.
⚬ `StatusPayload` gains `listener_health`, `engine_providers`,
  `cloud_personal_text`; a fresh status is sent when listener health changes
  (checked every 5 s); the Debug tab shows alive / restarts.
Deviations: edited outside the slot list: `main.py` DELETE route (`tools_changed`),
`MenuBarUIApp.swift` (banner wiring).

---

#20 — sqlite-vec needs a SQLite with load_extension [x] [Q7]
Wave: 2 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q7)
Ask: Vector memory requires `sqlite-vec` *and* a Python/SQLite build
compiled with `load_extension` support. The Homebrew default on macOS
typically lacks it, so most installs run the recency fallback. Document
(or auto-install) a `pysqlite3-binary` path, or ship a prebuilt SQLite
with the extension enabled?
DECISIONS (assistant, 2026-11-09): seen live: every recall in the
2026-11-09 runs logged "vec0 search unavailable (no such module: vec0)".
FROM WAVE 1 #40 (2026-11-09): an intermittent segfault in `ai/memory.py`
`search` (sqlite) during `test_integration.py`'s client fixture teardown, 2 of
4 paired runs, never alone. Not seen in 3 full runs after #48. Check whether
a connection is used from another thread after close.
Verify:
Results (wave 2, 2026-11-09):
⚬ Root cause: this Mac's stdlib sqlite3 (3.53.4) supports
  `enable_load_extension`; `sqlite-vec` was in requirements but not installed
  in `.venv` (uv-built, no pip). Installed `sqlite-vec` 0.1.9; vec0 loads.
⚬ `ai/memory.py`: tries stdlib then `pysqlite3`; warns once per process;
  rows without vectors are backfilled on connect.
⚬ The FROM WAVE 1 segfault: `close()` raced `asyncio.to_thread` searches.
  One `RLock` now guards every connection access; reproduced 1/4 SIGSEGV
  before, test fails with -11 without the lock.
⚬ README: uv install line and the `pysqlite3-binary` note (runner).
Deviations: installed `sqlite-vec` into the shared venv (declared but missing); added
the backfill.

---

#21 — Memory retention policy [x] [Q8]
Wave: 2 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q8)
Ask: Memory has no eviction, namespacing, or TTL; `memory.db` will
accumulate every preference saved. Add a retention policy (LRU, decayed
importance, or manual `clear_memory` wired to the existing WS command).
Verify:
Results (wave 2, 2026-11-09):
⚬ `ai/memory.py`: cap `max_rows=5000`; oldest non-preference rows evicted
  first (with their vectors); `clear_memory()` (keeps the chat transcript) and
  `count()`, plus module-level wrappers. The WS `clear_memory` command used to
  only close the DB; runner changed it to call `clear_memory()`.
⚬ `tests/test_memory.py` 11 tests; non-vacuity: wrong eviction order → 2
  fail. Runner tests: `tests/test_wave2_wiring.py`.
⚬ Runner: tests now use a temp DB (`AGENT_MEMORY_DB`, set in conftest); the
  integration fixture had been opening the real `~/.bot-inator/memory.db`.
Deviations: `clear_memory` keeps `chat_log`.

---

#22 — Real sandbox for installed generated tools [x] [Q9]
Wave: 7 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q9)
Ask: Generated tools run unsandboxed in the daemon process once
approved. The static sanity checks (`exec`, `eval`, `__import__`,
`shell=True`, …) are easy to bypass. If we ever want to relax the HITL
gate we need a real sandbox: subprocess isolation with
`sandbox-exec`/seatbelt, a capability-restricted `RestrictedPython`
interpreter, or running generated code inside a subordinate helper like
`_mac_app_helper.py`.
DECISIONS (assistant, 2026-11-09): the 5.1 dry run (DONE.md #11) already
runs unapproved drafts under `sandbox-exec`; that profile is the
starting point.
Verify:
Results (wave 7, 2026-11-09):
⚬ `tools/tool_sandbox.py`: the daemon never imports a generated module. At
  load a `sandbox-exec` child imports it and reports name/description/
  inputs/output_type; the daemon registers a `SandboxedTool` proxy. Each
  call runs a fresh child (JSON args on stdin, result on a private fd),
  60 s timeout (`AGENT_TOOL_SANDBOX_TIMEOUT`), process group killed on
  timeout. Profile per the decision; network only when the module's AST
  imports a network module; minimal env (no API keys), dotenv disabled.
  Built-in imports work via stub packages in the child.
⚬ Sandbox unavailable → no generated tool loads (one `tool_result` thought
  each, files stay); `AGENT_TOOL_SANDBOX=0` restores in-process import.
⚬ Approval-gate choice: a built-in called from a generated tool runs in the
  child with no second approval (the user approved the source and approves
  every call of the generated tool; a round-trip channel would let untrusted
  code drive daemon approvals). Tested: exactly one approval.
⚬ `tests/test_tool_sandbox.py` 13 tests; non-vacuity for network/read deny,
  minimal env, refusal, proxy; an automated control removes the Library
  deny and sees the write succeed. A copy of csv_diff ran under the sandbox
  with the real HOME and worked.
Deviations: write-deny also covers the whole repo (daemon source, venv, `.env`) and
shell start-up files; read-deny covers `agent-daemon/.env` and the token;
the child's cwd is $HOME. Residual risk tracked as #52. The suite now takes
~74 s (was ~26 s): the sandbox tests spawn real children.

---

#23 — Disable a generated tool without deleting it [x] [Q10]
Wave: 3 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q10)
Ask: Uninstall exists (ToolManagerView + `DELETE /api/tools/<name>`). Add
a *disable without deleting* toggle so the user can suspend a suspect
tool without losing the source, in case they want to tweak and
re-install it.
Verify:
Results (wave 3, 2026-11-09):
⚬ `main.py`: `POST /api/tools/{name}/disable` (unload + move to
  `generated/disabled/`) and `/enable` (move back + load); same validation as
  DELETE (400 bad/built-in, 404 missing, 409 target exists); best-effort
  nested-repo commits. `GET /api/tools` lists disabled (`disabled: true`) and
  quarantined (`quarantined: true`, `error`) rows. `ToolManagerView.swift`:
  per-row enable switch, "Disabled" caption, orange "Failed to load" line.
⚬ `tests/test_tool_lifecycle.py` (6 tests, shared with #28/#38); swiftc clean.
Deviations: DELETE also removes a disabled/quarantined copy and its `.error.txt`; enable
also restores from `quarantine/`.

---

#24 — Name collisions between generated tools [x] [Q11]
Wave: 2 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q11)
Ask: A generated tool whose name collides with a built-in is rejected.
What about two generated tools across daemon restarts (e.g. the agent
picks `summarize_pdf` twice with different logic)? Current plan: reject
the second with a surfaced error; alternative is to version the file
(`summarize_pdf.v2.py`) and let both coexist.
FROM WAVE 1 #30 (2026-11-09), fix in the same file: `_git_commit`
(`tools/meta_tool_generator.py`, `git init`/`git add` with `check=True`) falls
into the catch-all and drops git's stderr; `.splitlines()[0]` raises
`IndexError` on empty stdout; the identity args don't override
`commit.gpgsign` / `core.hooksPath`, so a signing setup can hang to the 10 s
timeout. `tests/test_git_commit.py` covers the happy paths.
Verify:
Results (wave 2, 2026-11-09):
⚬ `tools/meta_tool_generator.py`: `generate_custom_tool` rejects a name that
  is a generated tool on disk or loaded, right after the identifier check and
  before the similarity guard, drafting or approval: `error:
  name_collision`, naming the tool and its description. Re-install after
  DELETE works. `tests/test_tool_names.py` 5 tests.
⚬ `_git_commit`: init/add failures return git's stderr; empty stdout →
  "committed"; every call adds `commit.gpgsign=false`, `tag.gpgsign=false`,
  `core.hooksPath=/dev/null`, stdin from /dev/null. 4 more tests in
  `tests/test_git_commit.py`. Non-vacuity: each fix reverted fails its test.
Deviations: the collision check runs before the similarity guard: for a loaded tool the
guard used to swallow an exact name as `similar_exists`. A rejected name no
longer marks a rerun. `orchestrator.py` docstring (~line 162) still describes
the old short-circuit: handed to #38.

---

#25 — Pattern detection cost: adaptive cadence [x] [Q12]
Wave: 2 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q12)
Ask: Every 10 events we hit the detection engine with a 50-event prompt.
Phase 5.0's repeat gate skips the model call unless an event signature
repeats 3×, which removes most calls. Detection now defaults to the 8B
main engine, so each call that does run costs more. Adaptive cadence is
still open.
Verify:
Results (wave 2, 2026-11-09):
⚬ `ai/pattern_recognizer.py`: after each model call with no usable
  suggestion, `current_interval` doubles 60→120→240→480→600 s (cap); a match
  resets it; repeat-gate skips don't count. INFO log per change; new kwargs
  `max_trigger_interval`, `clock`. `tests/test_pattern_cadence.py` 5 tests;
  non-vacuity: no doubling → 4 fail, no count-trigger hold → 1 fails.
Deviations: while backed off, the 10-event trigger is held too (otherwise a busy user
never backs off: next call at +10 s instead of +120 s). Unparseable replies
back off like NO_PATTERN.

---

#26 — Per-engine provider override [x] [Q19]
Wave: 4 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q19)
Ask: The provider applies to all three engines. A per-engine override
(local analysis, cloud drafting) would keep per-event traffic on-device
and send only tool drafts to Groq.
FROM WAVE 3 #33 (2026-11-09): `SetupScanner(cloud=…)` is set from
`settings.provider`; make it follow the detection engine's provider.
Verify:
Results (wave 4, 2026-11-09):
⚬ `config.py`: `AGENT_PROVIDER_{MAIN,ANALYSIS,DRAFTING}` overrides (blank =
  default), helpers `engine_provider(role)`, `engine_providers()`,
  `is_cloud(role)`, `uses_cloud()`, `detection_is_cloud()`,
  `groq_key_required()`. `main.py`: engines built per role;
  `SetupScanner(cloud=settings.detection_is_cloud())`; settings API gains
  the three fields (invalid → 400), `engine_providers`,
  `running_engine_providers`, `groq_api_key_required`; the key is required
  only if an engine uses groq. `SettingsView.swift`: per-engine pickers.
  `user_settings.py`: the three keys added to `ALLOWED_KEYS`.
⚬ `tests/test_providers.py` 14, `test_settings_api.py` +5; non-vacuity 3.
⚬ Runner: eager-load log prints per-engine providers; conftest blanks
  `AGENT_PROVIDER_*`; the open-help CLI prints per-engine providers.
Deviations: edited `user_settings.py` (outside the slot; nobody else owned it; without
it PUT returned 500). An invalid override resolves to local with a warning
(never falls through to a cloud default, 7c).

---

#27 — HTTP API has no authentication [x] [Q14]
Wave: 6 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q14)
Ask: The daemon binds `127.0.0.1`, so any local process can hit
`DELETE /api/tools` and wipe the agent's learned behaviour. Same threat
model as the WS channel; deferred unless we add a process-local token
shared via keychain.
DECISIONS (user, 2026-11-09): Origin check AND token, Origin check first.
Why (assistant, from main.py at wave 4): no route or the WS checks who is
calling. Browsers apply no CORS to WebSockets, so any web page can open
`ws://127.0.0.1:8000/ws/stream`, read the thought stream (iMessage text,
paths, subjects) and send `approval_response` / `code_approval_response`
(approving a pending send or generated code with its own `edited_code`).
Simple cross-site POSTs also reach `/api/restart` and `/api/tools/*/disable`.
Not tested as an attack; nothing in the code blocks it.
1. ORIGIN CHECK: reject any HTTP request or WS handshake that carries an
   `Origin` header (browsers always send one on cross-site requests and on
   WebSockets; pages can't remove it). The menu bar app sends none. If a
   legitimate client needs one, allow-list it explicitly, never `*`.
2. TOKEN: random per-install secret in `~/.bot-inator/token` (0600, created
   on first start), required as `Authorization: Bearer <token>` on every
   HTTP route except `GET /health`, and on the WS handshake (header, or a
   first-frame/query fallback only if URLSessionWebSocketTask can't set
   headers — check). Swift clients read the file. Wrong/missing → 401 /
   WS close 1008.
3. Tests and the integration `client` fixture get the token automatically.
Verify:
⚬ A WS handshake with `Origin: https://evil.example` is refused; one with
  no Origin and the token is accepted; one without the token is refused.
⚬ `POST /api/restart` with an Origin header → 403; without the token → 401.
⚬ Non-vacuity: drop each check in turn, its test fails.
⚬ Live: the menu bar app still connects, approves, lists tools and saves
  settings after the change.
Results (wave 6, 2026-11-09):
⚬ `agent-daemon/auth.py` ASGI middleware for HTTP and WS, registered in
  `main.py`: (1) any request with an `Origin` header → 403 (WS close 1008
  before accept), `/health` included; exact-match allow-list
  `AGENT_ALLOWED_ORIGINS`, empty by default. (2) `~/.bot-inator/token`
  (`secrets.token_urlsafe(32)`, file 0600, dir 0700, created at startup,
  `AGENT_TOKEN_PATH` override, never logged), constant-time compare;
  missing/wrong → 401 + `WWW-Authenticate: Bearer` or WS close 1008; only
  `GET /health` exempt. `/api/restart` keeps the token file.
⚬ Swift `Services/DaemonAuth.swift` reads the token on every connect/request;
  the WS task is built from a `URLRequest` with the header. No token →
  Offline + hint, retries with backoff; 401/403 → "token rejected" hint.
⚬ Verified with a Swift probe: neither `URLSession` nor
  `URLSessionWebSocketTask` sends `Origin`; `Authorization` arrives on both.
⚬ `tests/test_auth.py` 22; conftest sets a temp `AGENT_TOKEN_PATH`; test
  clients send `auth_header()`. Non-vacuity: no Origin check → 5 fail, no
  token check → 12, no `/health` exemption → 1, no middleware → 16.
Deviations: `scripts/chat_smoke.py` now sends the token (outside the slot).
Live (2026-11-09, partial): the rebuilt app connected with the token;
`~/.bot-inator/token` is `-rw-------`; no token → 401 (HTTP) / 403 (WS);
Origin `https://evil.example` → 403 on `/api/tools`, `/api/restart` and the
WS; `/health` open (200); token without Origin accepted. After a daemon
restart the app reconnected by itself. Still to do: move the token away →
Offline hint; a real browser console attempt.

---

#28 — Quarantine a generated tool that fails to load [x] [Q15]
Wave: 3 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q15)
Ask: The stdlib allow-list is advisory. A generated tool that imports a
non-stdlib module fails at `load_dynamic_tools` time with an
`ImportError`; the static scan catches the obvious cases up front, but a
user-edited `edited_code` could still slip past. Should the loader
quarantine the file (move to `generated/disabled/`) when `exec_module`
raises?
DECISIONS (assistant, 2026-11-09): seen live: `recent_apps.py` was
installed but failed to load (duplicate tool name; loader fixed in
c89e0f5). Any load failure should quarantine, not only ImportError.
Verify:
Results (wave 3, 2026-11-09):
⚬ `ai/orchestrator.py`: `exec_module` raising, no Tool in the module, or the
  agent rebuild failing (bisected by re-adding new tools one at a time) moves
  the file to `quarantine/<name>.py` + `<name>.error.txt`; others stay loaded;
  a `tool_result` thought reports it. An "ok" install that got quarantined is
  reported as an error (no auto-rerun). Non-vacuity: no move → 2 fail; no
  bisect → 1 fails.
Deviations: quarantine moves are not git-committed (keeps git out of the loader).

---

#29 — Tool manager: refresh on a tools_changed broadcast [x] [Q16]
Wave: 5 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q16)
Ask: The view fetches once on appear and on manual refresh. Should it
also listen for a new `tools_changed` WS broadcast emitted by the
orchestrator after `load_dynamic_tools` / `unload_dynamic_tool`, so
multiple open windows stay consistent without polling?
Verify:
Results (wave 5, 2026-11-09):
⚬ One `tools_changed{reason, tool_names}` frame per change from
  `load_dynamic_tools` (loaded/quarantined) and `unload_dynamic_tool`, sent
  after the route's file moves; ToolManagerView refreshes 300 ms after it.
Deviations: DELETE also notifies (deleting a disabled/quarantined copy sent nothing).

---

#31 — Groq privacy surface [x] [Q18]
Wave: 5 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q18)
Ask: With `AGENT_PROVIDER=groq`, window titles, file paths and iMessage
text go to Groq inside prompts. Should the UI show which provider is
active (e.g. Debug tab, or in the status frame), and should iMessage
content be redacted or kept local-only when the provider is cloud?
FROM WAVE 4 (2026-11-09): use `settings.is_cloud(role)` /
`uses_cloud()` (#26) to decide redaction per engine; the placeholder must
also cover `event_details` in built-in follow-up prompts (#32), which hold
mail subjects and iMessage text.
Verify:
Results (wave 5, 2026-11-09):
⚬ New `ai/privacy.py`. "Personal text" = iMessage text, mail subjects,
  `read_email` subject and body, and the same inside built-in follow-up
  `event_details`. Replaced by `[personal text kept on this Mac]` in a cloud
  engine's prompts unless `AGENT_CLOUD_PERSONAL_TEXT=1`, per role: main
  (iMessage/generic prompts, read_email results, event_details), analysis
  (`evaluate_event`), detection (pattern recognizer, open-help and
  next-step windows). Local classification still reads the real text.
  Unchanged: sender handles/addresses, window titles, file paths, app names,
  calendar titles, the user's own chat text.
⚬ Debug tab shows each engine's provider and whether personal text goes to
  the cloud.
⚬ With a cloud main engine and redaction on, the iMessage responder sees
  only sender + placeholder (can't write a useful reply); `plan_with_time` and
  mail built-ins keep only ids and senders.
⚬ Tests: `tests/test_notices_privacy.py` 16 + 3 round trips; non-vacuity:
  `should_redact` always False fails 6, each of 7 call sites fails 1–2.
Deviations: one redaction line each in `idea_sources.py`, `open_help.py`,
`pattern_recognizer.py` (outside the slot list). Gap tracked as #51.

---

#32 — Persist idea-source state across restarts [x] [Q20]
Wave: 4 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q20, Phase 5.2 "Open")
Ask: Cooldowns and declined suggestions live in memory and are lost on
restart. Persist both in `memory.db`.
DECISIONS (assistant, 2026-11-09):
1. Partly done by #13: open-help approve/reject verdicts persist in
   `~/.bot-inator/idea_feedback.json`. Still in memory: the
   `NextStepSuggester` / `ChatGapDetector` cooldowns, and open-help's
   per-idea 7-day cooldown.
FROM WAVE 3 #42 (2026-11-09): apply in `FOLLOW_UPS`:
`calendar_invite_downloaded` = "Read the .ics file and add the event with the
built-in create_event tool (asks for approval), then move the file to the
Trash."; `plan_with_time` = "Add the plan with the built-in create_event tool
(title, start and end parsed from the text); it asks the user for
approval."; new kind `meeting_with_video_link` (classify: `event_starting_soon`
with `metadata.video_url`) = "Open the meeting's video link (open_url) a
minute before it starts." Also add mail kinds from #41 (`.ics` attachment /
invite subject → create_event; shipping email → tracking follow-up) and the
reminder kind from #45.
Verify: restart the daemon; a declined next-step idea is not offered again.
Results (wave 4, 2026-11-09):
⚬ `ai/idea_sources.py` `IdeaState`: cooldowns per source (`next_step`,
  `chat_gap`, `open_help`) in `~/.bot-inator/idea_state.json`, atomic writes,
  expired entries pruned, chat-gap keys hashed (no chat text on disk).
  NextStep/ChatGap check the shared `IdeaFeedback` rejections.
⚬ FOLLOW_UPS: wave 3 texts applied; new kinds `meeting_with_video_link`,
  `mail_invite`, `mail_shipping`, `mail_deadline` (conservative, with vetoes
  for replies, workspace invites, promos; Junk/Spam/Trash/Sent/Drafts
  skipped). `BUILTIN_FOLLOW_UPS` publish `builtin_tool` + `event_details`
  with no model call, cooling down per event.
⚬ Runner: one `IdeaState` + one `IdeaFeedback` shared in `main.py`;
  `_build_pattern_prompt` has a built-in path (call the built-in, never
  `generate_custom_tool`); declining the built-in's approval records a
  rejection (`_current_builtin_idea`). `tests/test_builtin_followups.py`.
⚬ Tests: `test_idea_state.py` 11, `test_idea_sources.py` +47 cases;
  non-vacuity 6 / 4 / 3 / 2 failing mutations.
Deviations: built-in kinds cool down per event, not per kind (a 24 h per-kind cooldown
would drop the second invite of the day).

---

#33 — Idea source: setup scan [x]
Wave: 3 · Agent: _ · Added: 2026-11-09 · Source: spec (Phase 5.2 table, row 3)
Ask: One-time scan: installed apps, `~/.zsh_history`, Downloads clutter,
recurring calendar events. Example: the same long `docker` command run
40× → one-click tool.
Verify:
Results (wave 3, 2026-11-09):
⚬ `ai/setup_scan.py` `SetupScanner`: once per install (marker + publish
  queue in `~/.bot-inator/setup_scan.json`): app names, top repeated long zsh
  commands (secret-looking ones dropped), Downloads counts/age, recurring
  calendar titles. History and calendar skipped under a cloud provider.
  Filter: `passes_checks`, `grounded_in_events`, rejected-idea memory, judge
  ≥7; at most 3, first after 120 s, then 4 h apart; re-checked before
  publishing. `tests/test_setup_scan.py` 18 tests; 4 mutations each fail.
⚬ Runner: started in `main.py` only when listeners are enabled and
  `AGENT_SETUP_SCAN` isn't `0`; calendar read only if access is already
  granted (`_calendar_reader_if_granted`), so it never prompts; shares the
  one `IdeaFeedback`.
Deviations: own `SETUP_JUDGE_SYSTEM_PROMPT` (the open-help judge would sink every catalog
idea, unverified on a real model); calendar also skipped under cloud.

---

#34 — Idea source: friction [x]
Wave: 3 · Agent: _ · Added: 2026-11-09 · Source: spec (Phase 5.2 table, row 4)
Ask: Copy in app A → paste in app B; rapid ping-pong between two apps.
Example: Mail → Numbers copy/paste ×5 → "pull invoice totals into my
sheet". Needs a clipboard listener.
Verify:
Results (wave 3, 2026-11-09):
⚬ `events/clipboard_watcher.py`: polls `changeCount` (works off the main
  thread); reads only pasteboard `types()` → kind; concealed/transient/
  auto-generated copies skipped; app via `lsappinfo front` (NSWorkspace was
  stale in-process). Emits `clipboard_changed` {app, kind}, never content.
⚬ `ai/friction.py` `FrictionDetector`: copy in A → switch to B within 60 s,
  5 times in 10 min → idea; ping-pong ≥10 alternations in 120 s with judge
  bar 8; 24 h cooldown per pair; filter: checks, names one of the apps,
  rejected-idea memory, judge. `tests/test_friction.py` 20 tests; 4
  mutations each fail (incl. 2 privacy tests).
⚬ Runner: watcher + detector wired; shares `IdeaFeedback` with
  `records_feedback` off while open-help records; `clipboard_changed`
  excluded from starting the agent; `_PATTERN_SOURCE_INTROS["friction"]`.
Deviations: own `FRICTION_JUDGE_SYSTEM_PROMPT` (the open-help judge says app switching is
not evidence).

---

#35 — Idea source: starter catalog [x]
Wave: 3 · Agent: _ · Added: 2026-11-09 · Source: spec (Phase 5.2 table, row 5)
Ask: Curated, known-good tool ideas matched to the user's apps. Example:
uses Mail + Preview → "auto-file PDF attachments". Adding a kind =
adding a `FOLLOW_UPS` entry and a classifier rule in `ai/idea_sources.py`
(see DONE.md #12).
Verify:
Results (wave 3, 2026-11-09):
⚬ `ai/starter_catalog.json`: 11 curated ideas with required apps/signals,
  matched by the setup scan (#33), published with source `starter_catalog`.
Deviations: merged into #33's scanner rather than `FOLLOW_UPS` (the catalog needs the
scan's app/file signals).

---

#36 — Open-help judge is too lenient; log its score [x]
Wave: 2 · Agent: _ · Added: 2026-11-09 · Source: assistant (live run)
Ask: In the live run every idea built from throwaway test files
(`create_invoice_template`, `invoice_number_generator`, `csv_merge`,
`csv_diff`, `recent_apps`) scored ≥7 and reached the user. The score is
only logged when an idea is dropped, so a passing score can't be seen.
Approach: log `judge_score` on pass; tighten IDEA_JUDGE_SYSTEM_PROMPT
(e.g. the events must show the user doing the step); measure the pass
rate on a fixed set of event windows before and after.
Files: agent-daemon/ai/open_help.py, agent-daemon/tests/test_open_help.py
Verify: a recorded set of ≥5 event windows; pass rate and examples written here.
Results (wave 2, 2026-11-09):
⚬ `ai/open_help.py`: score and reason logged at INFO on pass and drop;
  `parse_judge()` returns (score, reason). Stricter `IDEA_JUDGE_SYSTEM_PROMPT`
  (name the manual step; files appearing or app switches aren't evidence;
  test/sample/temp files cap at 4; when in doubt score lower). New free
  pre-judge check `grounded_in_events()`. Renames show `from -> to`.
⚬ `tests/fixtures/open_help_windows.json`: 9 windows (6 drop incl. the live
  run's, 3 pass) and `python -m ai.open_help` to score them on a real engine.
  `tests/test_open_help.py` 25 tests; non-vacuity: 4 fail when reverted.
Deviations: none.
Live (2026-11-09, Groq gpt-oss-120b): first run 1/9 pass, 7/9 match, but
almost every score was `None`: the 120-token judge budget came back empty on
a reasoning model (#54). After #54: pass rate 4/9, matches expected 8/9.
The one miss: `live_invoice_template` passes at 9 (three sequential
`test_invoice_N.txt` files read as intentional). Target was 3/9 and 9/9.

---

#38 — Repeated approval prompts after generate_custom_tool fails [x]
Wave: 3 · Agent: _ · Added: 2026-11-09 · Source: assistant (live run)
Ask: After `recent_apps` installed but failed to load, the agent retried
`generate_custom_tool` as `recent_apps_list`, and the user was asked to
approve it 4 times in one turn.
Approach: cap `generate_custom_tool` at one call per pattern_detected
turn (the pattern prompt already says "exactly once"); on an error
status, end the turn with the error instead of retrying.
Files: agent-daemon/ai/orchestrator.py (+ test)
FROM WAVE 2 #24 (2026-11-09): update the `ai/orchestrator.py` docstring
(~line 162) that still describes the old "identically-named tool already
exists on disk" short-circuit; collisions are now a `name_collision` error.
Verify: a test where the meta-tool returns an error; exactly one approval_request.
Results (wave 3, 2026-11-09):
⚬ A second `generate_custom_tool` in one turn returns
  `error: generate_custom_tool_already_called` with no approval request.
  `_turn_tool_calls` is now reset at the top of `_handle_event` (it was only
  reset for chat turns, so pattern turns had no cap). Stale docstrings fixed.
  Non-vacuity: no cap → 1 fails; no per-turn reset → 1 fails. #48 (wave 1)
  fixed the stacked-approval cause of the 4× prompts.
Deviations: none.

---

#41 — Read email from Mail.app's local store [x]
Wave: 3 · Agent: _ · Added: 2026-11-09 · Source: user
Ask: "I want the app to be able to read email." New mail becomes an
event the idea sources can act on, and the agent can read a message's
body when it needs to.
DECISIONS (user + assistant, 2026-11-09):
1. LOCAL, NOT CLOUD. Read Mail.app's on-disk store, read-only. No
   OAuth, no Gmail API, no IMAP password: nothing leaves the Mac
   (rule 7c). The user's Gmail is read by adding it to Mail.app once.
2. COPY THE iMESSAGE WATCHER. Same shape as `events/imessage_watcher.py`:
   SQLite opened with `file:…?mode=ro`, baseline the max row id at
   startup, poll every 10 s, push events via `bus.push_threadsafe`.
   Same Full Disk Access requirement and the same "log and idle" behaviour
   when access is missing (#19 covers surfacing that in the UI).
3. HEADERS IN EVENTS, BODIES ON DEMAND. The event carries sender,
   subject, date, mailbox and message id only. The body is read by a
   tool when the agent asks, never sent in every event (keeps prompts
   small, and keeps bodies out of Groq prompts unless a tool call asks).
4. READING IS READ-ONLY, so `read_email` goes in `READ_ONLY_TOOL_NAMES`
   (no approval prompt), like `read_clipboard`. Sending or moving mail is
   out of scope for this item.
Files:
⚬ OWNED: agent-daemon/events/mail_watcher.py, agent-daemon/tools/read_email.py,
  agent-daemon/tests/test_mail_watcher.py, agent-daemon/tests/fixtures/mail/**
⚬ EDITED (additive): agent-daemon/main.py (start the watcher, add to
  `listener_names`), agent-daemon/tools/__init__.py (register the tool +
  `READ_ONLY_TOOL_NAMES`), agent-daemon/ai/orchestrator.py (`_describe_event`
  for `mail_received`), agent-daemon/ai/idea_sources.py (`FOLLOW_UPS` +
  classifier rules), LIVING_SPEC.md §4 module table.
Approach:
1. Check the real schema first. With Full Disk Access granted, copy
   `~/Library/Mail/V*/MailData/Envelope Index` to a temp dir and inspect
   it (`messages`, `subjects`, `addresses`, `mailboxes` tables on recent
   macOS; the layout is undocumented and changes between versions).
   Record the tables and the query here before writing code.
2. `events/mail_watcher.py`: `MailWatcher(bus, poll_interval=10.0)`.
   Find the newest `V*` directory, open the index read-only, emit
   `ContextEvent(event_type="mail_received", metadata={"sender",
   "subject", "date", "mailbox", "message_id"})` for each new row.
   Skip the user's own sent mail.
3. `tools/read_email.py`: `read_email(message_id: int) -> dict`. Find the
   `<id>.emlx` / `<id>.partial.emlx` file under the account's mailbox
   folder, strip the leading byte count and trailing plist, parse with
   the stdlib `email` package, return `{status, sender, subject, date,
   body}` with the plain-text part (fall back to HTML stripped of tags),
   truncated to ~4,000 characters.
4. Idea sources: open-help sees `mail_received` in its event window with
   no change. Add next-step kinds: invite (`.ics` attachment or "invite"
   in the subject) → add to Calendar; shipping/tracking email → track it
   (reuse the `tracking_number` follow-up).
Fallback: if the user doesn't use Mail.app, or the index can't be read,
use IMAP with an app password stored in Keychain. That is a bigger job
and stores a credential, so it is a new item, not a silent switch.
Verify:
⚬ `test_mail_watcher.py`: a temp SQLite built with the recorded schema;
  the baseline ignores existing rows, a new row emits one `mail_received`
  event, sent mail is skipped, a missing file idles without raising.
  Non-vacuity: drop the baseline and confirm the old rows are emitted.
⚬ `read_email` on fixture `.emlx` files: plain-text, HTML-only, and
  `.partial.emlx`; a missing id returns an error status.
⚬ pytest gate (rule 8).
⚬ Live: with Full Disk Access, send yourself an email; the Debug tab
  shows a `mail_received` event within ~10 s, and asking in chat "what
  did my last email say?" calls `read_email` without an approval prompt.
Time: about 2–3 hours including tests, after Full Disk Access is granted.
Results (wave 3, 2026-11-09):
⚬ `events/mail_watcher.py`: newest `V*/MailData/Envelope Index`, `mode=ro`,
  schema check then baseline `MAX(ROWID)`, 10 s poll, `mail_received`
  {sender, subject, date, mailbox, message_id}; Sent/Drafts skipped; no
  access or schema mismatch logs once and idles. Schema isolated in one
  `SCHEMA` block; `python -m events.mail_watcher --schema` prints the real
  layout (no content).
⚬ `tools/read_email.py` (read-only): finds `<id>.emlx`/`.partial.emlx`,
  strips byte count + plist, plain text preferred, 4,000-char cap;
  `AGENT_MAIL_ROOT` override.
⚬ `tests/test_mail_watcher.py` 12 tests; 5 mutations each fail a test.
⚬ Runner: watcher started in `main.py`, tool registered read-only,
  `_describe_event` added; `mail_received` does not start the tool-calling
  agent (added to `_AGENT_TRIGGER_EXCLUDED_EVENT_TYPES`, so idea sources
  get it without an approval card per email).
Deviations: schema inspection became the live check (no Full Disk Access here); Drafts
skipped with Sent.
Live (2026-11-09): `--schema` on the real index (`V10`): "Schema check: OK"
(messages, subjects, addresses, mailboxes all as assumed; no SCHEMA change).
The mailbox list showed Outbox and SendLater (own outgoing mail) not marked
own: added to the watcher's own-mail pattern and the idea-source skip list,
with a test. Daemon has Full Disk Access now (no notice at start). Still to
do: send yourself an email → `mail_received`; "what did my last email say?".

---

#42 — Use the Mac's Calendar: read events, create events, upcoming-meeting events [x]
Wave: 3 · Agent: _ · Added: 2026-11-09 · Source: user
Ask: "I want the app to be able to use the Mac's Calendar app too." The
agent can answer "what's on my calendar?", add events (with approval),
and idea sources can react to an upcoming meeting.
DECISIONS (user + assistant, 2026-11-09):
1. EVENTKIT, LOCAL. Read and write through Apple's EventKit (via
   `pyobjc-framework-EventKit`), which sees every calendar the Calendar
   app syncs (iCloud, Google, Exchange). No calendar API keys, nothing
   leaves the Mac (rule 7c).
2. BUILT-IN TOOLS, NOT GENERATED. Calendar access is core enough to
   ship as reviewed built-ins, so the agent stops drafting a new tool
   per request (today `calendar_invite_downloaded` and `plan_with_time`
   in `FOLLOW_UPS` do exactly that).
3. READ IS FREE, WRITE NEEDS APPROVAL (rule 7a). `list_events` goes in
   `READ_ONLY_TOOL_NAMES`; `create_event` does not, so every new event
   raises an `approval_request`. No update or delete in this item.
4. PERMISSION SPIKE FIRST. macOS grants Calendar access per process. It
   is not known whether a bare Python daemon gets the TCC prompt or is
   silently denied. Step 1 settles this before any other code.
Files:
⚬ OWNED: agent-daemon/tools/calendar_access.py (shared EventKit or
  AppleScript backend), agent-daemon/tools/list_events.py,
  agent-daemon/tools/create_event.py, agent-daemon/events/calendar_watcher.py,
  agent-daemon/tests/test_calendar.py
⚬ EDITED (additive): agent-daemon/requirements.txt
  (`pyobjc-framework-EventKit`), agent-daemon/tools/__init__.py (register
  tools + `READ_ONLY_TOOL_NAMES`), agent-daemon/main.py (start the
  watcher, `listener_names`), agent-daemon/ai/orchestrator.py
  (`_describe_event` for `event_starting_soon`), agent-daemon/ai/idea_sources.py
  (`FOLLOW_UPS` for invites and plans call `create_event`),
  MenuBarUI approval alert (readable event fields instead of raw JSON;
  see #18), LIVING_SPEC.md §4 and §6.
Approach:
1. Spike (~15 min): `pip install pyobjc-framework-EventKit`, then from the
   daemon's own Python call `EKEventStore.requestFullAccessToEventsWithCompletion_`
   (macOS 14+) and list today's events. Record here whether the prompt
   appeared, which app it was attributed to, and whether access stuck
   after a daemon restart.
   - Works → EventKit backend.
   - Silently denied → AppleScript backend: `osascript` with `tell
     application "Calendar"` (needs the Automation permission, which does
     prompt from a CLI). Slower (seconds for a week of events), so cap
     ranges to 14 days.
2. `tools/calendar_access.py`: one backend interface, `list(start, end,
   calendars=None) -> list[dict]` and `create(title, start, end,
   calendar=None, location=None, notes=None) -> dict`. Dates are ISO
   8601 in local time. Events come back as `{title, start, end,
   calendar, location, notes (first 500 chars), url, all_day}`.
3. `list_events(start: str, end: str) -> dict`: read-only tool. Defaults
   to today when the agent passes empty strings; caps at 31 days.
4. `create_event(title: str, start: str, end: str, calendar: str = "",
   location: str = "", notes: str = "") -> dict`: approval-gated. An
   empty `calendar` uses the default calendar. Returns `{status,
   event_id}`.
5. `events/calendar_watcher.py`: poll every 60 s and emit one
   `ContextEvent(event_type="event_starting_soon", metadata={title,
   start, location, url})` per event starting within 10 minutes (each
   event once). open-help sees it with no change. Add a next-step kind:
   a meeting with a video link (Zoom / Meet / Teams URL in location,
   notes or url) → open the link.
6. Point the existing follow-ups at the new tool: `.ics` downloaded and
   "plan with a time" iMessages call `create_event` instead of drafting
   a tool.
Fallback: if neither EventKit nor AppleScript can get access from the
daemon, move calendar access into the MenuBarUI app (it has an
Info.plist for the usage string) and expose it to the daemon over the
WebSocket. Bigger change: becomes its own item.
Verify:
⚬ `test_calendar.py` with a fake backend: `list_events` date defaults
  and the 31-day cap; `create_event` is NOT in `READ_ONLY_TOOL_NAMES`
  and raises an `approval_request` (reuse
  `test_read_only_tool_bypasses_approval`'s pattern); the watcher emits
  once per event inside 10 minutes and never for an event already
  announced. Non-vacuity: add `create_event` to `READ_ONLY_TOOL_NAMES`
  and confirm the approval test fails.
⚬ pytest gate (rule 8).
⚬ Live: ask in chat "what's on my calendar today?" → no approval prompt,
  correct events. "Add lunch with Sam tomorrow at noon" → approval card
  → event appears in Calendar.app. An event 10 minutes out shows
  `event_starting_soon` in the Debug tab.
Time: about 3 hours including tests; about 4 if the AppleScript
fallback is needed.
Results (wave 3, 2026-11-09):
⚬ `tools/calendar_access.py`: one interface over `EventKitBackend`
  (recurrences expanded; per-entity `_ensure_access` ready for #45) and
  `AppleScriptBackend` (fixed scripts, argv, numeric dates, 14-day cap);
  `AGENT_CALENDAR_BACKEND=eventkit|applescript|auto`. Diagnostic:
  `python tools/calendar_access.py --check`.
⚬ `list_events` (read-only; empty = today; 31-day cap flagged `truncated`),
  `create_event` (gated; end defaults to +1 h).
⚬ `events/calendar_watcher.py`: 60 s poll only when the backend is already
  ready (never prompts); one `event_starting_soon` per occurrence within 10
  min; skips all-day; detects Zoom/Meet/Teams links.
⚬ `requirements.txt`: `pyobjc-framework-EventKit>=11.0` (installed).
⚬ `tests/test_calendar.py` 18 tests; non-vacuity via the gate flip, dedupe
  and cap. Runner: registered, watcher started, `_describe_event` added,
  `event_starting_soon` excluded from starting the agent.
Deviations: AppleScript backend does not expand recurrences. The diagnostic runs as a
script path (`-m` hits the tools/ai circular import).
Live (2026-11-09): `--check` → EventKit silently denied for the Python
process (no prompt; status stays `not_determined`), as the spike feared.
Same for Reminders (#45). Next: `--applescript`, then pin
`AGENT_CALENDAR_BACKEND=applescript` / `AGENT_REMINDERS_BACKEND=applescript`.
Live (2026-11-09, continued): `--applescript` also fails: listing one
day timed out after 60 s, even after the Automation prompt was allowed.
Calendar is blocked on #60 (EventKit inside the menu bar app).

---

#43 — Shortcuts runner: list and run the user's Shortcuts [x]
Wave: 1 · Agent: _ · Added: 2026-11-09 · Source: user (ranked #1)
Ask: Let the agent use every Shortcut the user already has, and the app
actions Shortcuts can reach, without writing code per action.
DECISIONS (assistant, 2026-11-09):
1. `list_shortcuts() -> dict` runs `shortcuts list` (read-only).
2. `run_shortcut(name: str, input_text: str = "") -> dict` runs
   `shortcuts run <name>` with input on stdin and returns stdout.
   Approval-gated: a Shortcut can do anything.
3. The name must exactly match a `shortcuts list` entry (no fuzzy match);
   arguments go as a list to `subprocess.run`, never `shell=True`.
   Timeout 60 s.
Files: agent-daemon/tools/shortcuts.py, agent-daemon/tests/test_shortcuts.py;
EDITED: agent-daemon/tools/__init__.py.
Verify:
⚬ Tests with `subprocess.run` mocked: unknown name returns an error status
  without running anything; output and non-zero exit are reported; the
  approval-gate check (shared decision 5).
⚬ Live: "run my <shortcut name> shortcut" → approval card → it runs.
Time: about 1.5 hours.
Results (wave 1, 2026-11-09):
⚬ `tools/shortcuts.py`: `list_shortcuts()` (read-only, 15 s timeout) and
  `run_shortcut(name, input_text="")` (gated, exact name from `shortcuts list`,
  arg list, 60 s timeout; non-zero exit → `status: error` + stderr).
⚬ `tests/test_shortcuts.py` 12 passed (gate test runs since registration).
  Non-vacuity: loosening the exact-name match failed 2 cases; moving
  `run_shortcut` to read-only failed the gate test.
Deviations: `shortcuts run` has no stdin input, so `input_text` goes to a temp file
passed as `--input-path` (deleted after). Output is read from stdout; the live
check confirms that.
Live (2026-11-09): "list my shortcuts" → the 5 Shortcuts, no card (after
#55). Still to do: run one, and one that returns text.
Live (2026-11-10): "run my Take a Break shortcut" → card → `run_shortcut`
timed out after 60 s (that Shortcut waits for user interaction); the
duplicate guard stopped a second run. Run a Shortcut that finishes on its
own to close this.

---

#44 — Spotlight file search [x]
Wave: 1 · Agent: _ · Added: 2026-11-09 · Source: user (ranked #2)
Ask: Let the agent answer "where's that invoice PDF?" and pass the path
on to `move_file` or `summarize_file`.
DECISIONS (assistant, 2026-11-09):
1. `find_files(query: str, folder: str = "", kind: str = "", limit: int = 20)
   -> dict` runs `mdfind` (`-onlyin` for `folder`, `kMDItemContentType`
   filters for `kind`: pdf, image, document, folder). Read-only.
2. Results are `{path, name, modified, size}`, newest first, capped at 50.
   Paths under `~/Library` are dropped unless `folder` asks for them.
Files: agent-daemon/tools/find_files.py, agent-daemon/tests/test_find_files.py;
EDITED: agent-daemon/tools/__init__.py.
Verify:
⚬ Tests with `mdfind` mocked: query and filter flags, the cap, `~/Library`
  filtering, an empty result; the read-only check (shared decision 5).
⚬ Live: "find my latest invoice PDF" returns a real path with no approval card.
Time: about 1 hour.
Results (wave 1, 2026-11-09):
⚬ `tools/find_files.py`: `find_files(query, folder, kind, limit)` via `mdfind`
  (name or content match, quotes escaped, 10 s timeout), newest first,
  limit clamped 1–50, `~/Library` dropped unless asked, vanished files skipped.
⚬ `tests/test_find_files.py` 12 passed. Non-vacuity: forcing the Library
  filter off and removing the cap each failed a test. A direct run on this Mac
  returned real invoice PDFs.
Deviations: Kind filters use `kMDItemContentTypeTree` (matches subtypes). Only the first
1,000 matches are stat'ed, so "newest first" is within those. The runner
changed the test to import the module via `importlib` (the package attribute
`tools.find_files` is the tool once registered).
Live (2026-11-09): "find my latest invoice PDF" → `find_files` ran and
answered with no card. ✔

---

#45 — Reminders: list and create [x]
Wave: 4 · Agent: _ · Added: 2026-11-09 · Source: user (ranked #3)
Ask: Catch to-dos from email and messages ("send me the deck by Friday"
→ a reminder) and let the agent see what's due.
DECISIONS (assistant, 2026-11-09):
1. Reuse #42's backend: EventKit reminders (`EKEntityTypeReminder`)
   through `tools/calendar_access.py`, or the Reminders app via
   AppleScript if #42's spike chose that fallback. Reminders access is
   a separate macOS permission from Calendar.
2. `list_reminders(list_name: str = "", include_completed: bool = False)
   -> dict` is read-only.
3. `create_reminder(title: str, due: str = "", list_name: str = "",
   notes: str = "") -> dict` is approval-gated.
4. Next-step source: an iMessage or email (#41) with a request and a
   deadline → suggest `create_reminder` (a `FOLLOW_UPS` kind).
Files: agent-daemon/tools/reminders.py, agent-daemon/tests/test_reminders.py;
EDITED: agent-daemon/tools/calendar_access.py (#42), agent-daemon/tools/__init__.py,
agent-daemon/ai/idea_sources.py.
FROM WAVE 3 #42 (2026-11-09): `EventKitBackend._ensure_access(entity)` in
`tools/calendar_access.py` is per entity type; add reminders beside it.
Verify:
⚬ Fake-backend tests for both tools, due-date parsing, and the gate check.
⚬ Live: "remind me to call the vet tomorrow at 9" → approval card →
  reminder appears in Reminders.app with the right due time.
Time: about 1 hour once #42 is done.
Results (wave 4, 2026-11-09):
⚬ `tools/reminders.py`: `list_reminders` (read-only; soonest due first,
  cap 200) and `create_reminder` (gated; ISO local or date-only = all-day;
  bad dates → error). Additive `calendar_access.py`: EventKit reminders
  (bounded 30 s fetch, alarm on timed reminders) and AppleScript (argv-only,
  compiled with `osacompile`); `get_reminder_backend()`;
  `--check --reminders` diagnostic. 17 tests; non-vacuity via the gate flip
  and due parsing. Runner: registered (`list_reminders` read-only).
Deviations: `AGENT_REMINDERS_BACKEND` (falls back to `AGENT_CALENDAR_BACKEND`); timed
reminders get an alert; an unknown list name is an error.
Live (2026-11-09): EventKit silently denied (as for Calendar);
`--reminders --applescript` works: 2 lists (Reminders, To Do), 1 open
reminder. Pinned `AGENT_REMINDERS_BACKEND=applescript` in
`agent-daemon/.env`. Still to do: create and list from chat.
Live (2026-11-09): "remind me to call the vet tomorrow at 9" → card →
`create_reminder` {due: 2026-11-10T09:00} → ok (AppleScript backend). ✔
Live (2026-11-10): "what's on my reminders?" → "Call the vet – due Nov 10
09:00", no card. ✔

---

#46 — Notes: create and append [x]
Wave: 1 · Agent: _ · Added: 2026-11-09 · Source: user (ranked #4)
Ask: Let the agent save things for later: meeting notes, a summary of a
long email (#41), a list it put together.
DECISIONS (assistant, 2026-11-09):
1. AppleScript to Notes.app (`tell application "Notes"`); needs the
   Automation permission, which prompts on first use.
2. `create_note(title: str, body: str, folder: str = "") -> dict` and
   `append_note(title: str, text: str) -> dict`, both approval-gated.
   `append_note` fails with an error status if no note has that exact
   title (never creates one by surprise).
3. Text reaches AppleScript as an `osascript` argument (`on run argv`),
   never by splicing it into the script source, so quotes in the text
   can't break or inject the script.
Files: agent-daemon/tools/notes.py, agent-daemon/tests/test_notes.py;
EDITED: agent-daemon/tools/__init__.py.
Verify:
⚬ Tests with `osascript` mocked: arguments passed via argv, a body
  containing `"` and `end tell` stays inert, missing-note error, gate check.
⚬ Live: "save a note called Test with hello" → approval card → note in Notes.app.
Time: about 1.5 hours.
Results (wave 1, 2026-11-09):
⚬ `tools/notes.py`: `create_note(title, body, folder)` and `append_note(title,
  text)`, both gated. Fixed AppleScripts, text passed via `on run argv`, HTML
  escaped per line; both scripts compile with `osacompile`. Nothing sent to
  Notes.app.
⚬ `tests/test_notes.py` 10 passed. Non-vacuity: splicing argv into the script
  failed 3 tests; marking `append_note` read-only failed the gate test.
Deviations: `append_note` also errors when more than one note has the exact title,
rather than guessing.
Live (2026-11-09): "save a note called Test with hello" → approval card →
Notes Automation prompt → `create_note` {title: Test, body: hello} → ok;
the model sent `folder: null`, dropped by #55. ✔ Still to do: append.
Live (2026-11-10): "append world to my note Test" → card → `append_note`
ok. ✔

---

#47 — App and window control [x]
Wave: 1 · Agent: _ · Added: 2026-11-09 · Source: user (ranked #5)
Ask: Let the agent open the right app for a task, and let idea sources
see which windows are open, not just which app is in front.
DECISIONS (assistant, 2026-11-09):
1. `list_windows() -> dict` via Quartz `CGWindowListCopyWindowInfo`
   (pyobjc-framework-Quartz is already a dependency): app name and
   window title for on-screen windows. Read-only. Window titles need the
   Screen Recording permission on recent macOS; without it, return app
   names only and say so in the result.
2. `open_app(name: str) -> dict` runs `open -a <name>`; approval-gated.
   The name must match an app in /Applications, ~/Applications or
   /System/Applications.
3. Window titles can hold private text (document names, email
   subjects): with `AGENT_PROVIDER=groq` they go to the cloud like other
   prompts, which #31 already tracks.
Files: agent-daemon/tools/windows.py, agent-daemon/tests/test_windows.py;
EDITED: agent-daemon/tools/__init__.py.
Verify:
⚬ Tests with Quartz and `subprocess.run` mocked: unknown app returns an
  error without running `open`, the no-permission fallback, gate checks.
⚬ Live: "what windows do I have open?" lists real windows; "open
  Numbers" → approval card → Numbers opens.
Time: about 1 hour.
Results (wave 1, 2026-11-09):
⚬ `tools/windows.py`: `list_windows()` (read-only; on-screen layer-0 windows,
  max 100; `CGPreflightScreenCaptureAccess` decides whether titles are
  available, with a note when not) and `open_app(name)` (gated; must match an
  `.app` in /Applications, ~/Applications, /System/Applications(/Utilities)).
⚬ `tests/test_windows.py` 10 passed. Non-vacuity: marking `open_app`
  read-only and dropping the unknown-name check each failed a test.
Deviations: `open_app` passes the resolved bundle path to `open -a`, not the raw name.
Found: the drafting prompt printed `from tools.<tool name> import …`, wrong for
tools in shared modules; fixed by the runner at wave close (see #48).
Live (2026-11-09): "what windows do I have open?" → windows with titles
(Screen Recording already granted), no card. Still to do: "open Numbers".
Live (2026-11-10): "open Numbers" → approval card → Numbers opened. ✔
(The first try waited behind failing email turns; fixed by #62's alias.)

---

#51 — Idea text from a local engine can quote personal text into a cloud prompt [x]
Wave: 7 (with #22) · Agent: _ · Added: 2026-11-09 · Source: assistant (wave 5 report)
Ask: #31 redacts personal text at each source, but when the detection engine
is local and the main engine is cloud, the idea the local engine writes
(`description`, `expected_logic`) can quote an iMessage or email subject,
and `_build_pattern_prompt` sends it to the cloud main engine.
Approach: when `is_cloud("main")` and redaction is on, scan idea text for
substrings of the personal text in the triggering window (or simply strip
quoted strings longer than N chars) before building the pattern prompt;
test with a fake idea that quotes an iMessage.
Verify: a test where a local idea quotes "dinner at 7 with Sam" and the cloud
main prompt contains the placeholder instead.
Results (wave 7, 2026-11-09):
⚬ `ai/privacy.py`: `note_personal_event` records iMessage text and mail
  subjects from the last hour; `scrub_idea_text` (applied when
  `should_redact("main")`) replaces 3+-word runs found in that text and any
  quoted string of 3+ words in the idea's description/expected_logic.
  `tests/test_idea_privacy.py` 5 tests incl. "dinner at 7 with Sam";
  non-vacuity: no scrub → fails.
Deviations: none.

---

#52 — Settings toggle for sending personal text to cloud engines [x]
Wave: 8 · Agent: _ · Added: 2026-11-09 · Source: user
Ask: "Is that a setting we should have in the app?" Today the only way to
let Groq see email and iMessage text is `AGENT_CLOUD_PERSONAL_TEXT=1` in
`agent-daemon/.env` (#31). The Debug tab shows it read-only
(`cloud_personal_text` in the status frame); Settings has no control.
DECISIONS (user, 2026-11-09):
1. The user turned the opt-in on (via `.env`) so the agent can read email
   while every engine runs on Groq.
Files:
⚬ OWNED: MenuBarUI/MenuBarUI/Views/SettingsView.swift, agent-daemon/ai/privacy.py
⚬ EDITED (additive): agent-daemon/user_settings.py (`ALLOWED_KEYS`),
  agent-daemon/main.py (settings API), agent-daemon/.env.example (document it)
Approach: add a persisted `cloud_personal_text` user setting, read by
`privacy.personal_text_allowed()` (env var still wins when set). Settings
gets a toggle next to the engine pickers, labeled e.g. "Let cloud models
see email and message text", shown only when an engine uses Groq, off by
default (invariant 7c). It applies without a restart (privacy reads it per
call); the status frame updates so the Debug tab stays in sync.
Verify:
⚬ Toggle on/off in Settings → the next iMessage/email prompt to Groq
  carries / omits the text (check the agent daemon log).
⚬ pytest gate; Xcode/swiftc build for MenuBarUI.
⚬ New test in `tests/test_settings_api.py`: PUT `cloud_personal_text`
  flips `should_redact("main")` for a cloud main engine; non-vacuity:
  ignore the setting in `personal_text_allowed()` → test fails.
Results (wave 8, 2026-11-09):
⚬ `ai/privacy.py` `personal_text_allowed()` per call: shell export >
  saved setting > `.env` > off. `user_settings.py` allows
  `AGENT_CLOUD_PERSONAL_TEXT`; `PUT /api/settings` takes `cloud_personal_text`
  (saved "1"/"0", locked when shell-exported, no restart). Settings toggle
  "Let cloud models see email and message text" (only when an engine uses
  Groq, off by default). 5 tests; non-vacuity 3 + 1.
Deviations: `.env` ranks below the saved toggle (otherwise the toggle could never turn
the user's `.env` opt-in off). The Debug tab updates on next connect.
Live (2026-11-10): Settings shows the toggle on (from `.env`). ✔ (Flip
off → placeholder in the log not exercised yet.)

---

#53 — Sandboxed tools can still reach outside via `open` / `osascript` [x]
Wave: 8 · Agent: _ · Added: 2026-11-09 · Source: assistant (wave 7 report)
Ask: #22 allows `/usr/bin/osascript` and `/usr/bin/open` in the sandbox so
generated tools can notify and open things. Both can ask another app (e.g.
Terminal, via Apple Events) to run code outside the sandbox.
Approach: options to weigh: deny Apple Events to Terminal/iTerm/Script
Editor in the profile; route osascript/open through a daemon-side allow-list
of verbs (notify, open URL/file); or accept it as covered by human review of
the source (rule 7b). Decide with the user.
Verify: a generated tool that tries `osascript -e 'tell application "Terminal" to do script "touch ~/x"'` is blocked (or the decision to accept is recorded).
Results (wave 8, 2026-11-09):
⚬ `tools/tool_sandbox.py`: profile denies `appleevent-send` to Terminal,
  iTerm, Script Editor, System Events and Finder (verified on macOS 26.6.2:
  -600, no prompt), and `(deny lsopen)`. `/usr/bin/open` is off the exec
  allow-list; the tool's `open` asks the daemon over a short-lived unix
  socket, which refuses shell apps as -a/-b, apps in writable folders,
  script/app/bundle/executable files, files whose default app is refused,
  `ssh:`/`x-man-page:` URLs, `--args`. An `osascript` stand-in asks the
  daemon to launch allowed apps a script `tell`s. Notifications, URL/file
  opens, Notes/Reminders/Calendar still work. 9 tests; non-vacuity for the
  Terminal deny, lsopen and the open check.
⚬ Residual: `/usr/bin/shortcuts` (a user Shortcut could run a shell script);
  a short check-then-open window; network-enabled tools can reach unix
  sockets. Human review of the source stays the gate for these.
Deviations: Finder Apple Events also denied (Finder open of a `.command` runs it in
Terminal); `~/Applications` apps can't be opened with -a. The user's
generated `recent_apps_list` (System Events) now fails, as chosen.

---

#56 — Review and remove remembered denials [x]
Wave: 8 · Agent: _ · Added: 2026-11-09 · Source: user
Ask: "I need a way to remove the denials too." Declined tool ideas are now
remembered and block reworded repeats (`ai/idea_match.py`, the shared
`IdeaFeedback` in `~/.bot-inator/idea_feedback.json`). The user needs to see
what's remembered and un-decline an idea (e.g. after changing their mind
about an iMessage send tool) without editing the JSON by hand.
Approach: `GET /api/ideas/declined` (token-protected, #27) listing each
declined idea (name, description, when, how many times); `DELETE
/api/ideas/declined/{name}` removing every entry with that name (and
`POST .../clear` for all), saved atomically through `IdeaFeedback`. A
"Declined ideas" section in the Tool Manager (or Settings) with a Remove
button per row and "Clear all" with a confirmation. Also clear the
matcher's embedding cache so a removed idea stops matching at once.
Verify:
⚬ Remove `send_imessage` in the app → the next send-iMessage build card is
  shown again (one card), and `idea_feedback.json` no longer has it.
⚬ pytest: list/remove/clear routes (401 without token), matcher no longer
  matches after removal; non-vacuity: skip the cache clear → test fails.
⚬ swiftc typecheck.
Results (wave 8, 2026-11-09):
⚬ `IdeaFeedback.declined_groups/remove_declined/clear_declined`,
  `DeclinedIdeaMatcher.forget/clear_cache`; `routes/ideas.py`: GET
  `/api/ideas/declined`, DELETE `/declined/{name}` (404 if none), POST
  `/declined/clear` (approvals kept). `DeclinedIdeasView.swift` (Remove,
  Clear all with confirm), embedded in Tool Manager by the runner. 9 tests;
  non-vacuity 2 + 2.
Deviations: removing from IdeaFeedback already stops matching (match() re-reads
entries); the cache clear just drops vectors for removed ideas.
Live (2026-11-10): Tool Manager → Declined ideas lists send_imessage with
its count. ✔ (Remove / Clear all not exercised yet.)

---

#58 — Read the active browser tab's page text [x]
Wave: 8 · Agent: _ · Added: 2026-11-09 · Source: user
Ask: "does the app see whats in the browser?" Today `read_active_tab`
returns only the URL and title. The user wants the agent to read what is
on the page (option 1 of three offered: page text via AppleScript, not a
server-side fetch and not tab-change events).
DECISIONS (user + assistant, 2026-11-09):
1. NEW READ-ONLY TOOL `read_active_tab_text(max_chars: int = 8000) -> dict`
   in `tools/read_active_tab.py`, beside `read_active_tab`. Goes in
   `READ_ONLY_TOOL_NAMES` (it only reads; rule 7a).
2. HOW: AppleScript `execute javascript "document.body.innerText"` on the
   active tab (Chrome: `execute front window's active tab javascript …`;
   Safari: `do JavaScript … in current tab of front window`). Logged-in
   pages work because it reads the user's own tab. No server-side fetch.
3. SETUP THE USER DOES ONCE: Chrome → View → Developer → Allow JavaScript
   from Apple Events; Safari → Develop → Allow JavaScript from Apple
   Events. When it is off, return an `error` that names the exact menu
   item, so the agent can tell the user.
4. FRONTMOST BROWSER FIRST: both tools ask the browser that is frontmost
   (or most recently active) first, then fall back to Chrome → Safari.
   Today Chrome always wins even when the user is looking at Safari.
5. TRUNCATE to `max_chars` (cap 20,000), collapse whitespace, and return
   `{"url", "title", "browser", "text", "truncated": bool}`.
6. PRIVACY (invariant 7c, same rule as #31): with a cloud provider on the
   main engine, `text` is replaced by the #31 placeholder unless
   `AGENT_CLOUD_PERSONAL_TEXT=1`. URL and title are unchanged (they
   already go to the cloud today).
7. SENSITIVE SITES ARE NEVER READ: a fixed blocklist of host patterns
   (banks, password managers such as `*.1password.com`, `vault.bitwarden.com`,
   `accounts.google.com`, `*.okta.com`, any `chrome://` / `about:` page)
   returns `{"error": "page is on the sensitive-site list"}` without
   running JavaScript.
8. Only the agent calls it, on demand. No event, no polling (that was
   option 3, not chosen).
Files:
⚬ OWNED: agent-daemon/tools/read_active_tab.py,
  agent-daemon/tests/test_read_active_tab_text.py
⚬ EDITED (additive): agent-daemon/ai/privacy.py (reuse the placeholder
  and `personal_text_allowed()`; add nothing else).
⚬ Runner registers at wave close (rule 9): `read_active_tab_text` in
  `AVAILABLE_TOOLS` and `READ_ONLY_TOOL_NAMES`; LIVING_SPEC §6 row
  "`read_active_tab_text` ✅ AppleScript JS → innerText of the active tab,
  truncated; cloud placeholder per #31".
Approach: Run the script through the existing `_run_osascript` with the JS
passed as an argv item, not interpolated. Detect the "JavaScript from Apple
Events is turned off" error text from each browser and map it to decision 3.
Fallback if `execute javascript` proves unreliable in Chrome: read the
selection-free `innerText` via `osascript -l JavaScript` (JXA), same output.
Verify:
⚬ Tests with `subprocess.run` mocked: Chrome text returned and truncated;
  Safari when it is frontmost; JS-disabled error names the menu item;
  blocklisted host never calls osascript; cloud provider without opt-in →
  placeholder, with `AGENT_CLOUD_PERSONAL_TEXT=1` → text; gate check (in
  `READ_ONLY_TOOL_NAMES`) with the rule-5 non-vacuity flip.
⚬ pytest gate (rule 8).
⚬ Live: turn on the Chrome setting, open a news article, ask "summarize
  the page I'm on" → summary of that page, no approval card. With the
  setting off → the agent says which menu item to turn on. Safari in
  front with Chrome also open → Safari's page.
Time: about 1 hour.
Results (wave 8, 2026-11-09):
⚬ `read_active_tab_text(max_chars=8000)` → {url, title, browser, text,
  truncated}; http/https only; sensitive-site lists checked before any JS;
  fixed `on run argv` script, JS as argv, runs only if the tab still shows
  the checked URL; whitespace collapsed; cap 20,000; cloud placeholder.
  Both tab tools ask the frontmost browser first (`lsappinfo
  visibleProcessList`), then Chrome, Safari. Setting-off errors name the
  menu path. 47 tests; non-vacuity 5 / 16 / 3. Registered read-only by the
  runner.
Deviations: `file://` blocked with all non-web schemes; URL re-check between steps.
Live (2026-11-10): Chrome's "Allow JavaScript from Apple Events" turned on
(Permissions row went green) → "summarize the page I'm on" →
`read_active_tab_text` read the Chrome tab → summary, no card. ✔ Still to
do: setting-off error, Safari order, a bank page.

---

#59 — Permissions checklist: check and request every macOS permission [x]
Wave: 8 · Agent: _ · Added: 2026-11-09 · Source: user
Ask: "i need a list of check / request all the permissions needed." One
place that shows every permission the app uses, whether each is granted,
and a button to request or open the right Settings pane. Today only Full
Disk Access gets a banner (#19); the rest fail silently or show a macOS
prompt mid-task.
Permissions found in the code (assistant, 2026-11-09):
| Permission | Used by | Check | Request |
| :--- | :--- | :--- | :--- |
| Full Disk Access | `events/imessage_watcher.py` (chat.db), `events/mail_watcher.py`, `tools/read_email.py` (~/Library/Mail) | open chat.db / Mail dir read-only | can't prompt; open `Privacy_AllFiles` |
| Screen Recording | `tools/windows.py` (window titles) | `CGPreflightScreenCaptureAccess` | `CGRequestScreenCaptureAccess`, else open `Privacy_ScreenCapture` |
| Calendars | `tools/calendar_access.py`, `list_events`, `create_event`, `events/calendar_watcher.py` | `EKEventStore.authorizationStatus` | `requestFullAccessToEvents` |
| Reminders | `tools/reminders.py` via `calendar_access.py` | `EKEventStore.authorizationStatus` (reminder) | `requestFullAccessToReminders` |
| Automation → Messages | `tools/send_imessage.py` | `AEDeterminePermissionToAutomateTarget` | same call with ask = true |
| Automation → Notes | `tools/notes.py` | same | same |
| Automation → Google Chrome, Safari | `tools/read_active_tab.py` (and #58) | same | same |
| Automation → Calendar, Reminders | AppleScript fallback in `calendar_access.py` | same | same |
| Automation → System Events | generated `recent_apps_list.py` | same | same |
| Browser setting (not TCC) | #58: "Allow JavaScript from Apple Events" in Chrome / Safari | try a no-op `execute javascript` | can't set; show the menu path |
DECISIONS (assistant, 2026-11-09):
1. ONE SOURCE OF TRUTH: new `agent-daemon/ai/permissions.py` with a
   `PERMISSIONS` table (id, name, why, used_by, check(), request()) and
   `check_all() -> list[dict]` returning `granted | denied | not_asked |
   unknown` per row. A new built-in tool or watcher adds its row here.
2. CHECKS NEVER PROMPT. `check_all()` must not trigger a macOS dialog;
   only an explicit "Request" click does. Automation checks use
   `AEDeterminePermissionToAutomateTarget` with ask = false, and report
   `unknown` (not denied) when the target app isn't running.
3. WHICH PROCESS: permissions attach to the process macOS sees (the
   daemon's Python binary when started by the app). The list says which
   binary it is, so the user grants the right entry in Settings.
4. UI: a "Permissions" section in Settings (`SettingsView.swift`), one row
   each: name, why, status dot, "Request" or "Open Settings" button.
   Fed by a new `GET /api/permissions` and `POST /api/permissions/<id>/request`
   (token-protected like every route, #27). #19's banners stay.
5. Each row only requests when the user clicks (invariant 7a in spirit:
   no permission dialogs in the background).
Files:
⚬ OWNED: agent-daemon/ai/permissions.py,
  agent-daemon/tests/test_permissions.py, permissions routes in
  agent-daemon/main.py, MenuBarUI `SettingsView.swift` (Permissions section)
⚬ Do NOT touch: the tools/watchers themselves; they keep their own
  fallbacks.
Verify:
⚬ Tests with pyobjc / osascript mocked: each row maps granted / denied /
  not_asked correctly; `check_all()` never calls a request function
  (non-vacuity: make one check call request() and confirm the test fails).
⚬ pytest gate and the swiftc typecheck (rule 8).
⚬ Live: Settings → Permissions lists every row above with the right
  status; "Request" on Calendars shows the macOS prompt; "Open Settings"
  on Full Disk Access opens that pane; after granting, the row turns
  green without a restart.
Time: about 3 hours (the Automation check is the fiddly part).
Results (wave 8, 2026-11-09):
⚬ `ai/permissions.py`: 11 rows (FDA, Screen Recording, Calendars via the
  app (#60 hook), Automation → Reminders/Calendar/Notes/Chrome/Safari/System
  Events, browser JS settings), `check_all()` never prompts
  (AEDeterminePermissionToAutomateTarget via ctypes with ask=false), and
  `process_info()` names the responsible process TCC charges. Routes in
  `routes/permissions.py`; `PermissionsView.swift` embedded in Settings by
  the runner. 35 tests; non-vacuity 6 / 3 / 3. Shell dry run: FDA, Screen
  Recording, Reminders, Notes, Chrome, Safari granted; Calendar not asked.
Deviations: Automation → Messages left out (send_imessage not registered); Reminders
checked as Automation (AppleScript backend).
Live (2026-11-10): Settings → Permissions shows the rows; after the user
allowed Calendar and Chrome JS, a restart/refresh showed 9 granted
(FDA, Screen Recording, Calendars, Automation → Reminders/Calendar/Notes/
Chrome/Safari, Chrome JS), System Events and Safari JS unknown. "Grant to"
named claude.app (this daemon was started from the Claude session). ✔
Follow-up: the System Events row offers Request although the sandbox (#53)
blocks System Events for generated tools; the user kept it as is.

---

#60 — Calendar through EventKit in the menu bar app [x]
Wave: 9 · Agent: _ · Added: 2026-11-09 · Source: user (live check of #42)
Ask: Calendar doesn't work from the daemon. Live check (2026-11-09):
EventKit is silently denied to the daemon's Python (no prompt, status stays
`not_determined`; a bare interpreter has no Info.plist usage string), and
the AppleScript fallback times out (>60 s for one day's events across the
user's calendars, even after the Automation prompt was allowed). The user
chose to move EventKit into the menu bar app, which has an Info.plist and
can get the normal "allow access to Calendar" prompt. Reminders works
through AppleScript (pinned `AGENT_REMINDERS_BACKEND=applescript`) and is
out of scope unless it also gets slow.
DECISIONS (user + assistant, 2026-11-09):
1. The app owns EventKit; the daemon asks it over the existing WebSocket
   (token-protected, #27). No second server, no new port.
2. Same daemon-side interface as #42: `calendar_access.list(...)` /
   `create(...)` gain an `app` backend; `list_events`, `create_event`
   (still approval-gated in the daemon, rule 7a) and `CalendarWatcher` are
   unchanged above it. `AGENT_CALENDAR_BACKEND=app` selects it; `auto`
   prefers `app` when the app is connected.
3. When the app isn't connected, calendar tools return a clear error
   ("open the bot-inator menu bar app to use Calendar") rather than
   falling back to the slow AppleScript path.
4. The app asks for Calendar access only when the daemon first needs it
   (not at launch), using `requestFullAccessToEvents` (macOS 14+).
Plan:
1. Protocol (`schemas/ws_messages.py` + `Models/WSMessage.swift`): a
   server→client `calendar_request {request_id, op: "list"|"create",
   args}` and a client→server `calendar_response {request_id, ok,
   events?|event_id?, error?}`; round-trip tests like the approval frames.
2. App: `Services/CalendarBridge.swift`: an `EKEventStore`, access request,
   list (predicate over all event calendars, recurrences expanded by
   EventKit; map to the #42 event dict: id, title, start, end, calendar,
   location, notes ≤500 chars, url, all_day) and create (default calendar
   when empty). `WebSocketManager` routes `calendar_request` to it and
   sends the response.
3. Info.plist: `NSCalendarsFullAccessUsageDescription` (and the legacy
   `NSCalendarsUsageDescription`) with a plain sentence about why.
4. Daemon: an `AppCalendarBackend` in `tools/calendar_access.py` that sends
   `calendar_request` to the connected app (orchestrator/WS subscriber
   path), waits on a future with a timeout (~15 s), and maps errors;
   `ready()` = app connected and access granted (reported in the app's
   hello/status so the watcher doesn't poll blind).
5. `CalendarWatcher` keeps polling every 60 s through the same interface;
   `_calendar_reader_if_granted` (setup scan) uses it when ready.
6. Docs: LIVING_SPEC §3 (new frames) and §6; README note that Calendar
   needs the menu bar app running.
Files: `schemas/ws_messages.py`, `MenuBarUI/MenuBarUI/Models/WSMessage.swift`,
`MenuBarUI/MenuBarUI/Services/WebSocketManager.swift`, new
`MenuBarUI/MenuBarUI/Services/CalendarBridge.swift`,
`MenuBarUI/MenuBarUI/Info.plist`, `agent-daemon/tools/calendar_access.py`,
`agent-daemon/main.py` (WS routing of `calendar_response`), tests.
Verify:
⚬ pytest: frame round trips; `AppCalendarBackend` with a fake app
  connection (list, create, timeout, app not connected → clear error);
  `list_events` / `create_event` unchanged above it; non-vacuity: drop the
  response routing → the list test times out.
⚬ swiftc typecheck.
⚬ Live: first "what's on my calendar today?" → the app shows the macOS
  Calendar prompt → Allow → today's events in seconds, no card. "Add lunch
  with Sam tomorrow at noon" → approval card → event in Calendar. An event
  10 minutes out → `event_starting_soon` in Debug. Quit the app → calendar
  tools answer with the "open the app" error.
Time: about half a day.
Results (wave 9, 2026-11-09):
⚬ Frames: `calendar_request {request_id, op: list|create|request_access, args,
  timeout_seconds}` (daemon→app), `calendar_response {request_id, ok,
  events?|event_id?, error?, authorization?}` and `calendar_status
  {authorization}` (app→daemon). Round trips in `tests/test_app_calendar.py`.
⚬ App `Services/CalendarBridge.swift`: one `EKEventStore` on a private serial
  queue; access requested only on the first daemon request
  (`requestFullAccessToEvents`); list over all event calendars (recurrences
  expanded) → the #42 dict; create in the default calendar when none named.
  `WebSocketManager` sends `calendar_status` after hello, every 30 s if
  changed, and after each request. Info.plist usage strings added.
⚬ Daemon `AppCalendarBridge` / `AppCalendarBackend` in
  `tools/calendar_access.py`: request/response via
  `run_coroutine_threadsafe` + futures (15 s; 120 s while access is
  undetermined); a disconnect fails pending requests. Requests go to the one
  connection that sent `calendar_status` most recently, never broadcast.
  `ready()` = app reported `full_access`. `auto`/`app` with no app → "open
  the bot-inator menu bar app to use Calendar". The Permissions `calendars`
  row reads the app's status; its Request sends `request_access`.
⚬ Non-vacuity: no response routing → the WS route test times out; no
  `set_result` → the list test fails; `auto` = AppleScript → 3 fail.
⚬ Gates: 679 passed ×3; swiftc clean; `plutil -lint` OK.
Deviations: added the `request_access` op and the `calendar_status` frame;
`auto` no longer uses in-daemon EventKit or AppleScript (explicit pins still
work); `test_calendar.py::test_backend_selection` updated.
Live (2026-11-10): the app showed the macOS Calendar prompt on the first
question → allowed → Permissions `calendars` = granted. "what's on my
calendar this week?" → 5 events through the app in ~2 s. ✔ Two answer fixes
from this check: an empty model reply after a tool result now gets one
tool-free completion to phrase the answer (it had replied just "Done."), and
each event carries a readable `when` (the model had called Monday Nov 9
"Wednesday"). Still to do: add an event; `event_starting_soon`; quit the app
→ "open the app" error.
Live (2026-11-10): "add lunch with Sam tomorrow at noon" → card →
event created in "Brooks Forsyth" for Nov 11 12:00–1:00 PM. ✔

---

#61 — Tool detail view: summary, run conditions, and source for every tool [x]
Wave: 8 · Agent: _ · Added: 2026-11-09 · Source: user
Ask: The app should show detailed data on every tool it has, both the
built-ins it shipped with and the ones it generated. For each tool:
1. A plain-language SUMMARY of what it does.
2. WHEN IT RUNS: the conditions that make the agent call it (the event
   or pattern that led to it, for generated tools; the situations it is
   meant for, for built-ins), and whether it needs approval or runs
   freely (in `READ_ONLY_TOOL_NAMES` or not, rule 7a).
3. A way to READ THE CODE: a button that opens the tool's source file.
Today `GET /api/tools` (`main.py` ~line 447) returns only name and a
description from the docstring, and `ToolManagerView.swift` shows a
list with enable/disable and delete.
DECISIONS (user, 2026-11-09):
1. GENERATED-TOOL CONDITIONS: if the idea that produced a tool (source,
   triggering event, pattern text) isn't saved at install time, start
   saving it in `meta_tool_generator.py`; tools installed before show
   "unknown".
2. BUILT-INS: generate the summary and conditions from the tool's schema
   + docstring, not hand-written per tool.
3. CODE: an "Open in editor" button that opens the tool's source file
   (e.g. `NSWorkspace.open` / `open -t`), not an in-app code viewer.
4. DISABLED AND QUARANTINED tools are shown too, with their source file
   and, for quarantined ones, the `.error.txt` contents.
Likely files: tool routes in `main.py` (new `GET /api/tools/{name}`
returning summary, conditions, gate, source path, error text),
`ToolManagerView.swift` (+ a new detail view), `tools/meta_tool_generator.py`
(save idea provenance), each built-in in `tools/`. Overlaps wave 5
(`ToolManagerView.swift`) and wave 6 (`main.py` routes, auth): schedule
after wave 6 so the new route gets the auth token.
Verify:
Results (wave 8, 2026-11-09):
⚬ `routes/tool_detail.py` `GET /api/tools/{name}`: kind, state
  (loaded/disabled/quarantined/not_loaded), summary, when_it_runs
  (schema + docstring for built-ins; saved provenance or "unknown" for
  generated), gate, inputs, sandbox info, source_path, error_text.
  `meta_tool_generator` writes `generated/.provenance/<name>.json` at
  install (not git-committed); the runner calls `set_current_idea` in the
  pattern turn. `ToolDetailView.swift` sheet from Tool Manager; "Open in
  editor" = `open -t`. 13 tests; non-vacuity 3 / 1 / 1.
Deviations: added state `not_loaded`.
Live (2026-11-10): find_files → "Runs without asking", open_url → "Needs
your approval", Open in editor opened the file as text, a generated tool
showed sandbox info and its when-it-runs. ✔

---

#62 — Larger fonts in the app [x]
Wave: 12 · Agent: A · Added: 2026-11-10 · Source: user
Ask: "We need the fonts larger." The menu bar app's text is too small to
read comfortably.
Today (2026-11-10): the Swift views use `.caption` 54 times (thought stream,
Debug tab, Permissions rows, tool details, notices), plus 8 fixed
`.font(.system(size: …))` sizes, so most secondary text is caption-size.
Approach (to decide when scheduled): bump the base styles one step
(`.caption` → `.callout`/`.body`, `.subheadline` → `.body`) and replace the
fixed point sizes with text styles, so the app also follows macOS's
text-size setting; optionally a "Text size" picker in Settings (Small /
Medium / Large) applied with `.dynamicTypeSize` or an environment scale.
Check the fixed-size panel (420×600) and approval cards still fit.
Files: MenuBarUI/MenuBarUI/Views/*.swift, MenuBarUIApp.swift (+ Settings if
a picker is added).
Verify:
⚬ swiftc typecheck; rebuild; the user confirms the text is readable in the
  panel, the "Open in window" view, Chat, Settings and approval cards.
⚬ Nothing is clipped at the panel's fixed size.
Results (wave 12, 2026-11-10):
⚬ New `Views/TextSize.swift`: every view names a role (`.appFont(.secondary)`
  …) that resolves to a system text style for the current setting; 91 call
  sites, no `.font(` left in the views, no fixed point sizes. Medium (the
  default) is one step up: `.caption2` 10 → 11 pt, `.caption` 10 → 12,
  `.subheadline` 11 → 13, `.callout` 12 → 13. Large is one more step;
  Small is roughly the old sizes.
⚬ Settings → Appearance → Text size (Small / Medium / Large),
  `@AppStorage("textSize")`, applied with `.appTextSize()` at the panel,
  all four windows and both approval windows (`ApprovalPresenter.swift`).
⚬ `.dynamicTypeSize` was measured to do nothing on macOS (same 216×13 at
  every size), so fonts are chosen explicitly per setting. Probe: old
  caption text 216×13 → 253×15 (Medium) → 271×16 (Large).
⚬ Frames: panel 420×600 → 460×640; thought stream / debug now fill their
  container; approval card 480 → 520 wide with the body in a scroll view
  and the buttons pinned; code review 620 → 680 wide; heights scale with
  the setting and cap at 740 pt.
⚬ Verify: type-check and an optimized build (to a scratch path) exit 0;
  offscreen renders of the approval and code-review cards at all three
  sizes show nothing clipped. Runner gate: 814 passed.
Deviations: (1) body text does not shrink in Small. (2) Bordered button
titles, segmented pickers and tab titles are drawn by AppKit at the system
size and don't follow the setting. (3) The approval card body is now a
ScrollView (layout only). (4) Existing `.callout` text also went up one
step. (5) New default window sizes apply only to windows with no saved
frame. (6) The panel, Chat, Settings, Manage Tools and Tool Detail were
not rendered offscreen (they need the daemon): live check.

---

#63 — Write email: drafts in Mail.app [x]
Wave: 10 · Agent: D · Added: 2026-11-10 · Source: user
Ask: "Can the app write email now? If not add that to the todo.md." It
can't: #41 only reads (`read_email`, the mail watcher); no tool composes or
sends mail.
DECISIONS (user, 2026-11-10: "drafts only for now"; the rest proposed by
the assistant):
1. DRAFT FIRST. `draft_email(to, subject, body, cc="", reply_to_message_id=0)`
   creates a visible draft in Mail.app (AppleScript `make new outgoing
   message … with properties {visible:true}`), never sends. Approval-gated
   (rule 7a): the card shows recipients, subject and body, all editable
   (#18 pattern). The user presses Send in Mail.
2. DRAFTS ONLY (user, 2026-11-10). No `send_email` tool; the app never
   sends mail. Revisit only if the user asks.
3. REPLIES. With `reply_to_message_id` (an id from `mail_received` /
   `read_email`), use Mail's `reply` on that message so threading and
   quoting are right.
4. SAFETY. All user text reaches AppleScript via `on run argv` (as in
   `tools/notes.py`); recipients validated as addresses; no attachments in
   v1. Under a cloud main engine the draft body is written by the cloud
   model from text the user gave or approved (the #31 placeholder rules
   still apply to the email being replied to).
5. Ideas: the #32 `mail_deadline` / invite follow-ups could offer "draft a
   reply" once this exists.
Files: new agent-daemon/tools/email_draft.py (+ test), registration in
tools/__init__.py (not read-only), an approval card in ApprovalAlert.swift,
Permissions row "Automation → Mail" in ai/permissions.py.
Verify:
⚬ Tests with osascript mocked: argv-only text (a body containing `"` and
  `end tell` stays inert), address validation, reply path, gate check +
  non-vacuity.
⚬ Live: "draft an email to <me> saying hi" → card → a draft opens in Mail,
  unsent; "reply to my last email saying thanks" → a threaded reply draft.
Results (wave 10, 2026-11-10):
⚬ `tools/email_draft.py`: `draft_email(to, subject, body, cc="",
  reply_to_message_id=0)`; two fixed AppleScripts, all text via `on run
  argv`; neither contains "send"; `delivered: False` always. Reply looks
  the message up by Envelope Index ROWID (= Mail's `id of message`), inbox
  first, then every mailbox; new body goes above Mail's quote.
⚬ `ai/permissions.py`: row "Automation → Mail". `ApprovalAlert.swift`:
  `draft_email` card (To, Cc, Subject, Body editable; "Approve & Create
  Draft"; "Draft only — nothing is sent").
⚬ Runner: registered in `tools/__init__.py`, NOT read-only; chat prompt
  says a draft is ready, never "sent".
⚬ Tests: 35 in `tests/test_email_draft.py`; mutations (interpolated body,
  added `send`, no validation, ignored reply id, …) each failed the named
  tests. Gate test fails if `draft_email` is put in `READ_ONLY_TOOL_NAMES`
  (runner checked).
Deviations: (1) empty `to` allowed on a reply (Mail fills the author); the
card still requires To. (2) Empty subject allowed. (3) `as string`, not
`as text`, inside the Mail tell block. (4) If Mail hasn't loaded the quote
in ~3 s the draft has only the new body and the result says so; reply
timeout 90 s. (5) The draft is an open compose window; Mail saves it to
Drafts itself. Scripts were compiled with `osacompile` only: nothing has
run against Mail yet.

---

#64 — Human-readable summary when a tool is suggested [x]
Wave: 11 · Agent: A · Added: 2026-11-10 · Source: user
Ask: "When a tool is suggested, create a human-readable summary of the tool."
Today the idea card for `generate_custom_tool` has no special layout, so it
shows the raw call (`tool_name`, `description`, `expected_logic` as JSON),
and the later code-review card shows Python. Neither says in plain words
what the tool will do for the user.
Approach (to decide when scheduled):
1. When an idea is published (`pattern_detected`) or `generate_custom_tool`
   is about to ask, write a short plain-language summary: what it does, when
   it would run, what it touches (files, apps, network), and whether each
   use asks first. Generated by the detection engine from the idea text
   (one call), with a deterministic fallback from `description`.
2. Show it at the top of the idea card and the code-review card (a
   dedicated card layout in `ApprovalAlert.swift`, like #18's), with the
   technical spec and code still available below / expandable.
3. Save it with the provenance (#61) so Tool Manager's detail view shows the
   same summary after install.
4. Privacy: under a cloud engine the summary prompt follows #31/#51 (no
   personal text quoted from the triggering events).
Files: ai/orchestrator.py (or the idea sources) for the summary,
MenuBarUI/MenuBarUI/Views/ApprovalAlert.swift and CodeReviewAlert.swift,
tools/meta_tool_generator.py provenance, routes/tool_detail.py.
Verify:
⚬ Tests: summary generated for an idea (fake engine), fallback when the
  engine fails, carried in the approval payload and saved in provenance.
⚬ swiftc typecheck; live: the next suggested tool's card leads with a plain
  sentence like "Renames new screenshots on your Desktop to their date.
  Asks before each run. Touches only ~/Desktop."
Results (wave 11, 2026-11-10):
⚬ New `ai/tool_summary.py`: `summarize_idea` makes one analysis-engine
  call (labelled DOES / WHEN / TOUCHES lines, 5 s bound, think blocks and
  identifiers stripped) with a deterministic fallback from `description`
  on any failure, timeout, unloaded engine or junk reply. The "asks
  first" sentence comes from `READ_ONLY_TOOL_NAMES`, never the model. For
  code review, "touches" is read from the drafted source (network via
  `tool_sandbox.network_reason`, apps, folders, file writes). The prompt
  goes through `privacy.scrub_idea_text(..., "analysis")`.
⚬ `ai/orchestrator.py`: optional `summary` on the `generate_custom_tool`
  approval and on the code-approval payload. `tools/meta_tool_generator.py`:
  summary saved in provenance, rebuilt from the installed source.
  `routes/tool_detail.py`: new `plain_summary`. WS schemas: optional
  `summary` on both payloads.
⚬ Swift: "What this tool would do" leads the idea card (raw call under
  "Technical details"), "What this tool does" leads code review, and Tool
  Manager's detail shows it. Without `summary` the old layouts render.
⚬ Tests: 22 in `tests/test_tool_summary.py`, 10 mutation-checked. Wave
  gate: 813 passed; Swift type-check clean.
Deviations: (1) the summary rides on the idea card's approval payload, not
on `pattern_detected` (covers chat-initiated ideas too). (2) It uses the
orchestrator's analysis engine, not the detection engine (the
orchestrator doesn't hold that one). (3) An engine that isn't loaded is
not asked: fallback. (4) The route key is `plain_summary`; `summary`
already meant the docstring line (#61).

---

#65 — Morning brief: one card with today's meetings, mail to answer, reminders due [x]
Wave: 10 · Agent: C · Added: 2026-11-10 · Source: assistant (usefulness review, user asked to add)
Ask: Make the app useful every day without waiting for a tool idea. The
read-only built-ins exist (`list_events`, `read_email`, `list_reminders`)
but nothing proactive uses them together.
Approach (to decide when scheduled):
1. A small scheduler in the daemon emits a `daily_brief` event once per
   day at a set time (default 08:00, or at first activity after it if the
   Mac was asleep). The last-run date is persisted in `~/.bot-inator/` so a
   restart doesn't repeat it.
2. The orchestrator handles it with read-only tools only: today's events,
   mail from the last 24 h that looks like it needs a reply (newsletters
   already skipped by the mail-noise work), reminders due today or overdue.
3. Result is one brief shown in the Thoughts tab and as a notification;
   also on demand from Chat ("brief me"). No side effects, so no approval
   card (invariant 7a holds: read-only tools only). Follow-up actions
   (reply draft #63, create reminder) go through normal approval cards.
4. Privacy: under a cloud engine, mail bodies follow #31/#52 (placeholder
   unless the user turned personal text on).
5. Settings: on/off and time of day.
Depends on: live checks for #41 (mail), #42/#60 (calendar), #45
(reminders). A source that is unavailable is named in the brief ("Calendar:
open the menu bar app"), not silently dropped.
Files: new events/daily_brief.py, `_describe_event` branch in
ai/orchestrator.py, start-up in main.py, user_settings.py + SettingsView.swift
for the toggle and time, new tests/test_daily_brief.py.
Verify:
⚬ Tests: fires once per day (fake clock), not again after restart, catches
  up after a missed time; brief built from fake tool results; an
  unavailable source is named; no approval request is made.
⚬ Live: set the time 2 minutes ahead → one notification and one Thoughts
  entry listing real meetings, mail and reminders; "brief me" in Chat gives
  the same.
Results (wave 10, 2026-11-10):
⚬ `events/daily_brief.py`: `DailyBriefScheduler` (fires when `last_run <
  today's brief time <= now`; state in `~/.bot-inator/daily_brief.json`;
  45 s start-up delay), `gather_brief` (calendar, last-24 h mail headers,
  due reminders; each source `ok` or `unavailable` with a reason),
  `recent_mail` (read-only Envelope Index query, noise skipped, cap 15).
  Mail items carry sender, subject, date, id: never a body; subjects go
  through `ai.privacy.redact_text`.
⚬ `routes/daily_brief.py`: `GET`/`PUT /api/daily-brief`, `POST
  /api/daily-brief/run`. `user_settings.py`: `AGENT_DAILY_BRIEF`,
  `AGENT_DAILY_BRIEF_TIME`. `DailyBriefSettingsView.swift` embedded in
  Settings above Permissions.
⚬ Runner: scheduler started in `main.py`; `Orchestrator._handle_daily_brief`
  (model words the gathered data, max 2 steps, falls back to the
  deterministic text, then posts one notification); `_describe_event`
  branch; `tools/brief.py` `brief_me` registered read-only.
⚬ Tests: 24 in `tests/test_daily_brief.py` (15 mutation-checked), 7 in
  `tests/test_wave10_wiring.py`.
Deviations: (1) new `routes/daily_brief.py` (rule 12) because `PUT
/api/settings` takes fixed fields only. (2) State is a timestamp, not a
date, so moving the time later the same day arms it again (the live check
needs that). (3) Sources never prompt: Calendar only when the app bridge
is ready, Reminders only with access already granted; otherwise
"unavailable: <reason>". (4) "Mail to answer" is the model's call from up
to 15 headers, not code. (5) The notification is posted by the daemon
directly, not through the model: `show_notification` is approval-gated
for the model, and the brief is a setting the user turned on. (6)
`brief_me` lives in `tools/brief.py` with a lazy import because
`events.daily_brief` imports lead back into `tools`.

---

#66 — Open-help ideas must be grounded in content, not app focus alone [x]
Wave: 10 · Agent: A · Added: 2026-11-10 · Source: assistant (usefulness review, user asked to add)
Ask: Stop the stream of app-switching tool ideas. In
`~/.bot-inator/idea_feedback.json` on 2026-11-10, 13 of the 16 distinct
ideas are about switching or counting apps (`app_switcher`,
`toggle_chrome_claude`, `chrome_teams_switcher`, `count_app_switches`,
`summarize_app_switches`, …), and four of the five installed generated tools
are the same kind. The user approves them, then deletes or disables them.
Today: `grounded_in_events` (`ai/open_help.py`) passes an idea that shares
any one content word with the event window, and an app name counts. Most
events are `app_activated`, so "switch between Chrome and Claude" is always
grounded.
Approach (to decide when scheduled):
1. Grounding needs a content event: the idea must share a word with a
   mail, calendar, file, iMessage, clipboard-friction or browser-tab event
   in the window. A window holding only `app_activated` events makes no
   open-help call at all (also saves model calls).
2. Deterministic drop in `passes_checks` for ideas whose whole job is
   switching, toggling, listing or counting apps (macOS already does this:
   Cmd-Tab, Dock, Screen Time).
3. Add the same rule to `IDEA_JUDGE_SYSTEM_PROMPT` (score 0-3) and to
   `OPEN_HELP_SYSTEM_PROMPT` ("do not propose app switchers").
4. Add app-switch-only windows to `tests/fixtures/open_help_windows.json`
   with expected "no idea".
Files: ai/open_help.py, tests/test_open_help.py,
tests/fixtures/open_help_windows.json. Check `ai/pattern_recognizer.py`
and `ai/friction.py` for the same leak.
Verify:
⚬ Tests: app-only window → no model call; app-switcher idea dropped before
  the judge; an idea naming a file from a `file` event still passes.
  Non-vacuity per rule 5.
⚬ Live: `.venv/bin/python -m ai.open_help` pass rate still meets #36's
  target; a day of normal use produces no app-switch idea card.
Results (wave 10, 2026-11-10):
⚬ `ai/open_help.py`: `CONTENT_EVENT_TYPES` (file, mail_received,
  imessage_received, event_starting_soon); `observe` makes no model call
  until a new content event arrives (stage `no_content`);
  `grounded_in_events` ignores "switched to <app>" and `clipboard_changed`
  lines; new `is_app_switcher(idea, apps)` inside `passes_checks`; both
  prompts rule app switchers out.
⚬ `ai/friction.py` and `ai/pattern_recognizer.py` had the same leak: both
  now drop `is_app_switcher` ideas (the recogniser backs off as for
  NO_PATTERN). `ai/setup_scan.py` gets it through `passes_checks`.
⚬ Fixture: 5 new drop cases with a `stage` key. Tests: 10 in
  `tests/test_open_help.py`, 5 in new `tests/test_app_switcher_ideas.py`;
  non-vacuity: gate removed / focus lines ground again / `is_app_switcher`
  → False each turn the named tests red. Checked against the real
  `idea_feedback.json` names: every switching or counting idea is flagged.
Deviations: (1) `clipboard_changed` is NOT a content event (it carries only
app + kind, so it would let an app name ground an idea again). (2) No
browser-tab event exists; tab titles arrive as window titles and count as
focus. (3) The gate needs a NEW content event since the last ask, not just
one in the window. (4) `CONTENT_EVENT_TYPES` is an allow-list. (5) The
switcher rule over-drops on purpose ("switch between Mail and Numbers" in
a description is lost). (6) The pattern recogniser's repeat gate still
counts `app_activated`, so an app-only window still costs one detection
call before the idea is dropped (changing it breaks
`tests/test_integration.py`); left as is.

---

#67 — memory.db has no memories: find out whether the agent is saving any [x]
Wave: 10 · Agent: B · Added: 2026-11-10 · Source: assistant (usefulness review, user asked to add)
Ask: On 2026-11-10 `sqlite3 ~/.bot-inator/memory.db "select count(*) from
memories"` returned 0. Either the #20/#21 live check (`clear_memory`)
emptied it, or nothing is being saved in normal use. If nothing is saved,
the agent never learns preferences and recall is always empty.
Approach:
1. Investigate first (about 10 minutes): list every call site of
   `save_memory` and `remember_preference`; check the daemon log for save
   or embedding errors; use the app for a few turns (state a preference in
   Chat) and count again.
2. If rows appear: close as "cleared by the live check", no code change.
3. If not: fix the save path and add a test that a chat turn stating a
   preference leaves a row and that the next turn recalls it.
Files: ai/memory.py, tools/remember_preference.py, ai/orchestrator.py
(only if a fix is needed), tests/test_memory.py.
Verify:
⚬ Live: "remember that I prefer short replies" → count goes up by one; a
  new chat turn shows the memory in the recalled context (Debug tab).
⚬ If code changed: pytest gate and the new test's non-vacuity check.
Results (wave 10, 2026-11-10): NO BUG, no code change in the memory files.
⚬ `sqlite_sequence` shows `memories|2`: only 2 rows were ever inserted, so
  the live check removed at most 2. `chat_log` has 218 rows, so the DB is
  live.
⚬ The only writer is `tools/remember_preference.py:38`, called only when
  the model picks the `remember_preference` tool. Nothing extracts
  preferences from chat or events. No user message in `chat_log` ever
  asked the agent to remember anything.
⚬ Save and recall work: on a temp DB with the real embedder the tool
  saved a row and `recall_memory` returned it after reopen.
  `_recall_memories` feeds chat, iMessage, mail and generic prompts.
⚬ Runner change: `_build_chat_prompt` now tells the model to call
  `remember_preference` when the user states a lasting preference or asks
  it to remember something (`tests/test_wave10_wiring.py`).
⚬ Open design point (not scheduled): memory only grows when the user says
  something explicit and the model picks the tool; there is no automatic
  learning.
Deviations: none.
