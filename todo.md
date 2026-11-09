# bot-inator — Build Plan

<!-- ============================================================
HOW THIS FILE WORKS (rules agents must follow)
	1.	THIS FILE HOLDS OPEN + IN-PROGRESS WORK ONLY.
When an item is verified done, move its full write-up to DONE.md and
leave a one-line stub here (ID, title, date, link). Agents load this
file every session; done-item detail is context they pay for and
don't need.
	2.	IDs ARE UNIQUE AND NEVER REUSED, across todo.md and DONE.md. The
next ID lives in the counter below. Bump it when you add an item.
	3.	WAVES = DISJOINT FILE SETS. When items run in parallel, every item
in a wave lists the files it OWNS, and no file appears in two items of
one wave. Cross-cutting work goes in the LAST wave.
	4.	AN AGENT MAY DEVIATE from its instructions only if it verifies the
original instruction would cause a regression, and it documents the
deviation and the evidence in the item.
	5.	EVERY FIX GETS A VERIFY BLOCK, and every new test must be proven
NON-VACUOUS (revert the fix, confirm the test fails, restore).
	6.	RECURRING FEEDBACK IS A SPEC GAP. Add it to LIVING_SPEC.md.
	7.	THE INVARIANTS (LIVING_SPEC §1). Not up for per-item deviation:
a. NO BACKGROUND ACTION WITHOUT CONSENT. Every side-effecting tool call
   goes through an approval_request; only READ_ONLY_TOOL_NAMES bypass it.
b. GENERATED CODE IS REVIEWED BEFORE IT RUNS UNSANDBOXED. Idea gate,
   then code gate. The dry run is the only place unapproved code runs,
   and only inside sandbox-exec.
c. LOCAL BY DEFAULT. Cloud inference (Groq) is opt-in; nothing new may
   send user data off the machine without the same opt-in.
d. ASK ALWAYS, SURFACE RARELY. Ideas reach the user only through a
   filter (judge / checks / critic / dry run). When in doubt, drop it.
	8.	TESTS: `cd agent-daemon && .venv/bin/python -m pytest -q`. The
known-failing tests (#39, #40) are not a reason to skip the gate:
anything else red blocks the item. Swift changes also pass
`cd MenuBarUI/MenuBarUI && xcrun swiftc -typecheck -parse-as-library
-target arm64-apple-macos14 $(find . -name '*.swift')`.
	9.	SHARED REGISTRATION FILES ARE THE WAVE RUNNER'S. Items create their
modules; at wave close the runner registers tools in `tools/__init__.py`
(`AVAILABLE_TOOLS`, `READ_ONLY_TOOL_NAMES`), starts watchers in
`main.py`, adds `_describe_event` branches in `ai/orchestrator.py`, and
adds LIVING_SPEC §4/§6 rows. Items say exactly what to register.
	10.	AGENTS DON'T COMMIT, don't start a daemon on port 8000 (the user's
runs there), and don't edit todo.md: they report Results and
Deviations, and the runner writes them into the item.
	11.	LIVE CHECKS ARE BATCHED. Anything needing the user (a macOS
permission, an approval click, a real model run) goes to the Live
checks list under the wave plan. An item whose code and tests are done but
whose live check hasn't run is `[~] live`, not `[x]`.
============================================================ -->

Next ID: #48
Legend: `[ ]` todo · `[~]` in progress · `[~] live` built, waiting on its live check · `[x]` done (move to DONE.md)

## Wave plan

**Goal:** close every open item (#14–#47). Decided 2026-11-09 by the user:
the runner settles open design questions and records the choice in the
item; live checks are batched at the end; one branch + PR to main per
wave (`wave-N`, each branched from the previous wave); one subagent per
agent slot, all slots of a wave in parallel.

The dependency arrow runs: test hygiene + standalone tools → listener and
module fixes → generated-tool lifecycle + new sources (mail, calendar,
setup scan, friction) → idea-state persistence, reminders, approval UI,
per-engine provider → WS protocol (notices, tools_changed, provider) →
API auth → sandbox for installed tools → live checks.

| Wave | Slot: items | Owned files (disjoint within the wave) | Status |
| :--- | :--- | :--- | :--- |
| 1 | A: #39 #40 · B: #43 · C: #44 · D: #46 · E: #47 · F: #30 | A `tests/test_groq_engine.py`, `tests/test_dry_run.py`, `tools/draft_dry_run.py` · B `tools/shortcuts.py` · C `tools/find_files.py` · D `tools/notes.py` · E `tools/windows.py` · F `tests/test_git_commit.py` (+ each slot's own new test file) | [ ] |
| 2 | A: #20 #21 · B: #24 · C: #25 · D: #14 · E: #16 #17 · F: #36 | A `ai/memory.py` · B `tools/meta_tool_generator.py` · C `ai/pattern_recognizer.py` · D `events/file_watcher.py` · E `events/mac_listeners.py`, `events/_mac_app_helper.py` · F `ai/open_help.py` | [ ] |
| 3 | A: #23 #28 #38 · B: #41 · C: #42 · D: #33 #35 · E: #34 | A `ai/orchestrator.py`, tool routes in `main.py`, `ToolManagerView.swift` · B `events/mail_watcher.py`, `tools/read_email.py` · C `tools/calendar_access.py`, `tools/list_events.py`, `tools/create_event.py`, `events/calendar_watcher.py`, `requirements.txt` · D `ai/setup_scan.py`, `ai/starter_catalog.json` · E `events/clipboard_watcher.py`, `ai/friction.py` | [ ] |
| 4 | A: #32 · B: #45 · C: #15 #18 · D: #26 | A `ai/idea_sources.py`, `ai/open_help.py` · B `tools/reminders.py` (+ additive `tools/calendar_access.py`) · C `ApprovalAlert.swift`, `ApprovalPresenter.swift`, `CodeReviewAlert.swift` · D `config.py`, engine setup + settings routes in `main.py`, `SettingsView.swift` | [ ] |
| 5 | A: #19 #29 #31 (alone) | `schemas/ws_messages.py`, `WSMessage.swift`, `WebSocketManager.swift`, `DebugView.swift`, `ToolManagerView.swift`, `ai/orchestrator.py`, `events/imessage_watcher.py` | [ ] |
| 6 | A: #27 (alone) | `main.py` (all routes + WS), every Swift HTTP/WS client | [ ] |
| 7 | A: #22 (alone) | `ai/orchestrator.py` tool execution, new `tools/tool_sandbox.py` | [ ] |
| — | #37 + every `[~] live` item | Live checks list (below) — needs the user at the Mac | [ ] |

### Runner decisions on open questions (runner, 2026-11-09)

Settled per the user's "use your picks"; the user can overturn any:
#14 drop temp-suffix files at the source and debounce 2 s per path ·
#15 the red X counts as Deny, for code review too · #16 restart the
helper with backoff 1→60 s and report liveness · #17 emit `app_activated`
only after 500 ms of sustained focus · #20 try `pysqlite3` when present,
document the setup, warn once · #21 cap 5,000 rows, evict oldest
non-preference first, wire `clear_memory` · #22 run installed generated
tools in a sandboxed subprocess (no network unless the module imports a
network module; no writes to `~/Library`, `~/.ssh`, `~/.bot-inator`),
`AGENT_TOOL_SANDBOX=0` opts out · #23 disable = move to
`generated/disabled/`, enable moves back · #24 reject the second tool
with an error naming the existing one · #25 double the interval after
each NO_PATTERN up to 10 min, reset on a match · #26 per-engine
`AGENT_PROVIDER_{MAIN,ANALYSIS,DRAFTING}` override, also in Settings ·
#27 random per-install token in `~/.bot-inator/token` (0600), required by
every HTTP route and the WS handshake · #28 on any load failure move the
file to `generated/quarantine/` with a `.error.txt` and report it · #29
broadcast `tools_changed` after load/unload · #31 show the active
provider in the Debug tab; with a cloud provider, iMessage text and email
bodies are replaced by a placeholder in prompts unless
`AGENT_CLOUD_PERSONAL_TEXT=1` · #32 persist to
`~/.bot-inator/idea_state.json` (not `memory.db`: matches #13's feedback
file) · #33 run once per install, zsh history skipped under a cloud
provider · #34 the clipboard watcher records which app, never the
copied content · #36 log the score on pass and tighten the prompt; the
pass-rate measurement is a live check · #38 a second
`generate_custom_tool` call in one turn returns an error without asking.

## Live checks (batched; run with the user at the end)

Filled in as items reach `[~] live`. Each line: item, what to do, what
to see.

---

## Completed items (archived to DONE.md)

- #1 — Plumbing · 2026-04-19 · [DONE.md](DONE.md)
- #2 — MLX AI engine: analysis stage and two-engine design · 2026-04-19 · [DONE.md](DONE.md)
- #3 — Event pipeline hardening and in-app diagnostics · 2026-04-19 · [DONE.md](DONE.md)
- #4 — iMessage watcher and responder · 2026-04-19 · [DONE.md](DONE.md)
- #5 — Local vector memory · 2026-04-19 · [DONE.md](DONE.md)
- #6 — Autonomous meta-tool generation · 2026-04-19 · [DONE.md](DONE.md)
- #7 — Tool manager UI and auto-versioning · 2026-04-19 · [DONE.md](DONE.md)
- #8 — Source-control isolation for generated tools · 2026-04-19 · [DONE.md](DONE.md)
- #9 — Groq cloud provider · 2026-05-07 · [DONE.md](DONE.md)
- #10 — Detection fixes · 2026-11-09 · [DONE.md](DONE.md)
- #11 — Draft → check → repair loop, critic pass, sandboxed dry run · 2026-11-09 · [DONE.md](DONE.md)
- #12 — Idea sources: next step and chat capability gaps · 2026-11-09 · [DONE.md](DONE.md)
- #13 — Open-help idea source: ask, filter, learn · 2026-11-09 · [DONE.md](DONE.md)

---

## Open + in-progress items

### Item template (copy from here)

```markdown
#NN — <one-line title> [ ]
Wave: _ · Agent: _ · Added: YYYY-MM-DD · Source: user | spec | assistant
Ask: What the user actually wants, in their words where possible.
DECISIONS (who, YYYY-MM-DD): Numbered list of every judgment call already
resolved, so an agent never re-litigates them.
Files:
⚬ OWNED: path/one.py, path/two.swift
⚬ EDITED (additive, earlier wave): files owned by a finished item.
⚬ Do NOT touch: files owned by another item in this wave.
Approach: The intended build, plus the fallback if it doesn't survive contact.
Verify:
⚬ Concrete, observable check.
⚬ pytest gate (rule 8); Xcode build for MenuBarUI changes.
⚬ New test named, with its non-vacuity check.
Deviations: (fill in during/after) What changed from the plan.
```

Items #14–#32 are the open questions moved from LIVING_SPEC §9 on
2026-11-09 (old number in brackets). #33–#35 are the idea sources still
marked "Idea" in the Phase 5.2 table. #36–#40 came out of the
2026-11-09 live run of the open-help source. #41 (read email) and #42
(use Calendar) are new features. #43–#47 are new built-in tools, ranked
by help for effort. None is scheduled into a wave yet.

### Idea quality (Phase 5)

#32 — Persist idea-source state across restarts [ ] [Q20]
Wave: 4 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q20, Phase 5.2 "Open")
Ask: Cooldowns and declined suggestions live in memory and are lost on
restart. Persist both in `memory.db`.
DECISIONS (assistant, 2026-11-09):
1. Partly done by #13: open-help approve/reject verdicts persist in
   `~/.bot-inator/idea_feedback.json`. Still in memory: the
   `NextStepSuggester` / `ChatGapDetector` cooldowns, and open-help's
   per-idea 7-day cooldown.
Verify: restart the daemon; a declined next-step idea is not offered again.
Deviations:

---

#36 — Open-help judge is too lenient; log its score [ ]
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
Deviations:

---

#37 — Clear test entries from idea_feedback.json [ ]
Wave: — (live checks) · Agent: user · Added: 2026-11-09 · Source: assistant (live run)
Ask: The 2026-11-09 test runs saved approvals for test ideas
(`create_invoice_template`, `invoice_number_generator`, `file_watcher`,
`app_switcher`, `recent_apps`, `recent_apps_list` ×4) in
`~/.bot-inator/idea_feedback.json`. The judge sees them as "the user's
past verdicts" and will favour similar ideas.
Approach: remove those entries (or the file) while the daemon is stopped.
Verify: the file holds no entry with an `at` timestamp from the test runs.
Deviations:

---

#38 — Repeated approval prompts after generate_custom_tool fails [ ]
Wave: 3 · Agent: _ · Added: 2026-11-09 · Source: assistant (live run)
Ask: After `recent_apps` installed but failed to load, the agent retried
`generate_custom_tool` as `recent_apps_list`, and the user was asked to
approve it 4 times in one turn.
Approach: cap `generate_custom_tool` at one call per pattern_detected
turn (the pattern prompt already says "exactly once"); on an error
status, end the turn with the error instead of retrying.
Files: agent-daemon/ai/orchestrator.py (+ test)
Verify: a test where the meta-tool returns an error; exactly one approval_request.
Deviations:

---

#33 — Idea source: setup scan [ ]
Wave: 3 · Agent: _ · Added: 2026-11-09 · Source: spec (Phase 5.2 table, row 3)
Ask: One-time scan: installed apps, `~/.zsh_history`, Downloads clutter,
recurring calendar events. Example: the same long `docker` command run
40× → one-click tool.
Verify:
Deviations:

---

#34 — Idea source: friction [ ]
Wave: 3 · Agent: _ · Added: 2026-11-09 · Source: spec (Phase 5.2 table, row 4)
Ask: Copy in app A → paste in app B; rapid ping-pong between two apps.
Example: Mail → Numbers copy/paste ×5 → "pull invoice totals into my
sheet". Needs a clipboard listener.
Verify:
Deviations:

---

#35 — Idea source: starter catalog [ ]
Wave: 3 · Agent: _ · Added: 2026-11-09 · Source: spec (Phase 5.2 table, row 5)
Ask: Curated, known-good tool ideas matched to the user's apps. Example:
uses Mail + Preview → "auto-file PDF attachments". Adding a kind =
adding a `FOLLOW_UPS` entry and a classifier rule in `ai/idea_sources.py`
(see DONE.md #12).
Verify:
Deviations:

---

#25 — Pattern detection cost: adaptive cadence [ ] [Q12]
Wave: 2 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q12)
Ask: Every 10 events we hit the detection engine with a 50-event prompt.
Phase 5.0's repeat gate skips the model call unless an event signature
repeats 3×, which removes most calls. Detection now defaults to the 8B
main engine, so each call that does run costs more. Adaptive cadence is
still open.
Verify:
Deviations:

### Generated tools

#28 — Quarantine a generated tool that fails to load [ ] [Q15]
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
Deviations:

---

#22 — Real sandbox for installed generated tools [ ] [Q9]
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
Deviations:

---

#23 — Disable a generated tool without deleting it [ ] [Q10]
Wave: 3 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q10)
Ask: Uninstall exists (ToolManagerView + `DELETE /api/tools/<name>`). Add
a *disable without deleting* toggle so the user can suspend a suspect
tool without losing the source, in case they want to tweak and
re-install it.
Verify:
Deviations:

---

#24 — Name collisions between generated tools [ ] [Q11]
Wave: 2 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q11)
Ask: A generated tool whose name collides with a built-in is rejected.
What about two generated tools across daemon restarts (e.g. the agent
picks `summarize_pdf` twice with different logic)? Current plan: reject
the second with a surfaced error; alternative is to version the file
(`summarize_pdf.v2.py`) and let both coexist.
Verify:
Deviations:

---

#29 — Tool manager: refresh on a tools_changed broadcast [ ] [Q16]
Wave: 5 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q16)
Ask: The view fetches once on appear and on manual refresh. Should it
also listen for a new `tools_changed` WS broadcast emitted by the
orchestrator after `load_dynamic_tools` / `unload_dynamic_tool`, so
multiple open windows stay consistent without polling?
Verify:
Deviations:

---

#30 — Nested-repo first-use regression test [ ] [Q17]
Wave: 1 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q17)
Ask: Add a slower integration case that drives `_git_commit` against a
real on-disk `tmp_path`, covering the `git init` transition and identity
derivation (host global → `bot-inator` fallback). Mark it
`@pytest.mark.slow` or skip when `git` is not on PATH, so the fast suite
stays under a second.
Verify:
Deviations:

### New built-in tools

Shared DECISIONS for #43–#47 (user + assistant, 2026-11-09):
1. BUILT-INS, REVIEWED BY A HUMAN, not generated: each tool is a plain
   `@tool` in `agent-daemon/tools/`, registered in `AVAILABLE_TOOLS`.
2. RULE 7a DECIDES THE GATE: tools that only read go in
   `READ_ONLY_TOOL_NAMES`; anything that changes state, runs something
   or opens something raises an `approval_request`.
3. LOCAL ONLY: macOS commands (`shortcuts`, `mdfind`, `osascript`,
   `open`) or Apple frameworks via pyobjc. No cloud APIs.
4. NO GENERAL SHELL TOOL. A "run any command" tool would bypass rule 7b;
   #43 covers most of what it would be used for.
5. EVERY TOOL'S TEST includes a check that it is (or is not) in
   `READ_ONLY_TOOL_NAMES`, with the non-vacuity flip (move it to the
   other side and confirm the approval test fails).
6. Each tool's built-in examples go into the drafting prompt's
   AVAILABLE TOOLS list automatically (`_format_available_tools_context`),
   so generated tools can call them; check the listed signature is exact
   (DONE.md #11: the wrong `show_notification` argument broke every
   notifying tool).

---

#43 — Shortcuts runner: list and run the user's Shortcuts [ ]
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
Deviations:

---

#44 — Spotlight file search [ ]
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
Deviations:

---

#45 — Reminders: list and create [ ]
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
Verify:
⚬ Fake-backend tests for both tools, due-date parsing, and the gate check.
⚬ Live: "remind me to call the vet tomorrow at 9" → approval card →
  reminder appears in Reminders.app with the right due time.
Time: about 1 hour once #42 is done.
Deviations:

---

#46 — Notes: create and append [ ]
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
Deviations:

---

#47 — App and window control [ ]
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
Deviations:

---

### Events and listeners

#41 — Read email from Mail.app's local store [ ]
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
Deviations:

---

#42 — Use the Mac's Calendar: read events, create events, upcoming-meeting events [ ]
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
Deviations:

---

#14 — File-watcher noise from in-progress downloads [ ] [Q1]
Wave: 2 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q1)
Ask: Chrome download churn (`.crdownload`) floods the stream. Add
debounce + extension filter? Phase 5.2 added `on_moved`, so the finished
download now arrives as a `renamed` event; the in-progress churn is
still unfiltered.
Verify:
Deviations:

---

#16 — MacAppListener watchdog [ ] [Q3]
Wave: 2 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q3)
Ask: `MacAppListener` has no watchdog: if `_mac_app_helper` exits or
crashes, the daemon stops receiving app activations silently. Add a
supervisor that restarts the helper on exit and exposes liveness in the
Debug tab?
Verify:
Deviations:

---

#17 — Debounce rapid app switches [ ] [Q4]
Wave: 2 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q4)
Ask: Since Phase 4.7, `app_activated` no longer starts the tool-calling
agent. It still runs `evaluate_event` on the analysis engine per switch;
a debounce (sustained focus ≥ 500 ms) is still open.
Verify:
Deviations:

---

#19 — First-run Full Disk Access prompt for the iMessage watcher [ ] [Q6]
Wave: 5 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q6)
Ask: `IMessageWatcher` relies on Full Disk Access being pre-granted.
Should the daemon detect the `OperationalError` and surface a first-run
permission prompt through the MenuBar UI instead of a log line?
DECISIONS (assistant, 2026-11-09): seen live: the 2026-11-09 runs logged
"Could not open … chat.db read-only … needs macOS Full Disk Access", and
nothing told the user.
Verify:
Deviations:

### Approvals and UI

#15 — Closing the approval window with the red X [ ] [Q2]
Wave: 4 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q2)
Ask: Closing the approval window via the red X currently does nothing.
Should it count as an implicit deny?
Verify:
Deviations:

---

#18 — Editable approval for more tools [ ] [Q5]
Wave: 4 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q5)
Ask: Extend the editable-approval pattern to other tools: `open_url`
(edit the URL), `show_notification` (edit title/body), `move_file` (edit
destination). The current UI falls back to a read-only JSON view.
Verify:
Deviations:

### Memory

#20 — sqlite-vec needs a SQLite with load_extension [ ] [Q7]
Wave: 2 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q7)
Ask: Vector memory requires `sqlite-vec` *and* a Python/SQLite build
compiled with `load_extension` support. The Homebrew default on macOS
typically lacks it, so most installs run the recency fallback. Document
(or auto-install) a `pysqlite3-binary` path, or ship a prebuilt SQLite
with the extension enabled?
DECISIONS (assistant, 2026-11-09): seen live: every recall in the
2026-11-09 runs logged "vec0 search unavailable (no such module: vec0)".
Verify:
Deviations:

---

#21 — Memory retention policy [ ] [Q8]
Wave: 2 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q8)
Ask: Memory has no eviction, namespacing, or TTL; `memory.db` will
accumulate every preference saved. Add a retention policy (LRU, decayed
importance, or manual `clear_memory` wired to the existing WS command).
Verify:
Deviations:

### Security and providers

#27 — HTTP API has no authentication [ ] [Q14]
Wave: 6 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q14)
Ask: The daemon binds `127.0.0.1`, so any local process can hit
`DELETE /api/tools` and wipe the agent's learned behaviour. Same threat
model as the WS channel; deferred unless we add a process-local token
shared via keychain.
Verify:
Deviations:

---

#31 — Groq privacy surface [ ] [Q18]
Wave: 5 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q18)
Ask: With `AGENT_PROVIDER=groq`, window titles, file paths and iMessage
text go to Groq inside prompts. Should the UI show which provider is
active (e.g. Debug tab, or in the status frame), and should iMessage
content be redacted or kept local-only when the provider is cloud?
Verify:
Deviations:

---

#26 — Per-engine provider override [ ] [Q19]
Wave: 4 · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q19)
Ask: The provider applies to all three engines. A per-engine override
(local analysis, cloud drafting) would keep per-event traffic on-device
and send only tool drafts to Groq.
Verify:
Deviations:

### Test hygiene

#39 — Groq engine tests fail on Python 3.14 [ ]
Wave: 1 · Agent: _ · Added: 2026-11-09 · Source: assistant (test run)
Ask: 6 tests in `tests/test_groq_engine.py` fail with "There is no
current event loop in thread 'MainThread'": they call
`asyncio.get_event_loop().run_until_complete(...)`, which Python 3.14 no
longer supports without a running loop. Fails on the untouched baseline
too.
Approach: use `asyncio.run(...)` in the tests.
Verify: `pytest tests/test_groq_engine.py` 8/8.
Deviations:

---

#40 — test_dry_run fails depending on test order [ ]
Wave: 1 · Agent: _ · Added: 2026-11-09 · Source: assistant (test run)
Ask: `tests/test_dry_run.py::test_parse_plan_keeps_exists_checks_only_for_fixtures`
failed in one full-suite run and passed alone and in later full runs.
Approach: find the shared state (module globals or env) another test
leaves behind; run the suite with `-p randomly` or in reverse order.
Verify: 5 full runs in random order, no failure.
Deviations:
