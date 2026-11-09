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
