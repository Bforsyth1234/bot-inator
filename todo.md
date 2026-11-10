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
	12.	NEW HTTP ROUTES IN WAVE 8+ GO IN THEIR OWN MODULE (`agent-daemon/routes/
<name>.py`, a FastAPI `APIRouter`); NEW SWIFT SCREENS GO IN THEIR OWN FILE.
The runner adds `app.include_router(...)` in `main.py` and embeds new
views (e.g. in `SettingsView.swift` / `ToolManagerView.swift`) at wave
close, so parallel slots never share `main.py` or a view file.
============================================================ -->

Next ID: #62
Legend: `[ ]` todo · `[~]` in progress · `[~] live` built, waiting on its live check · `[x]` done (move to DONE.md)

## Wave plan

**Goal:** close every open item (#14–#47; waves 8–9 add #52, #53, #56, #58–#61, decided 2026-11-09). Decided 2026-11-09 by the user:
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
| 1 | A: #39 #40 · B: #43 · C: #44 · D: #46 · E: #47 · F: #30 | A `tests/test_groq_engine.py`, `tests/test_dry_run.py`, `tools/draft_dry_run.py` · B `tools/shortcuts.py` · C `tools/find_files.py` · D `tools/notes.py` · E `tools/windows.py` · F `tests/test_git_commit.py` (+ each slot's own new test file) | [x] closed 2026-11-09 (#43 #44 #46 #47 `[~] live`) |
| 2 | A: #20 #21 · B: #24 · C: #25 · D: #14 · E: #16 #17 · F: #36 | A `ai/memory.py` · B `tools/meta_tool_generator.py` · C `ai/pattern_recognizer.py` · D `events/file_watcher.py` · E `events/mac_listeners.py`, `events/_mac_app_helper.py` · F `ai/open_help.py` | [x] closed 2026-11-09 (all `[~] live`) |
| 3 | A: #23 #28 #38 · B: #41 · C: #42 · D: #33 #35 · E: #34 | A `ai/orchestrator.py`, tool routes in `main.py`, `ToolManagerView.swift` · B `events/mail_watcher.py`, `tools/read_email.py` · C `tools/calendar_access.py`, `tools/list_events.py`, `tools/create_event.py`, `events/calendar_watcher.py`, `requirements.txt` · D `ai/setup_scan.py`, `ai/starter_catalog.json` · E `events/clipboard_watcher.py`, `ai/friction.py` | [x] closed 2026-11-09 (all `[~] live`) |
| 4 | A: #32 · B: #45 · C: #15 #18 · D: #26 | A `ai/idea_sources.py`, `ai/open_help.py` · B `tools/reminders.py` (+ additive `tools/calendar_access.py`) · C `ApprovalAlert.swift`, `ApprovalPresenter.swift`, `CodeReviewAlert.swift` · D `config.py`, engine setup + settings routes in `main.py`, `SettingsView.swift` | [x] closed 2026-11-09 (all `[~] live`) |
| 5 | A: #19 #29 #31 (alone) | `schemas/ws_messages.py`, `WSMessage.swift`, `WebSocketManager.swift`, `DebugView.swift`, `ToolManagerView.swift`, `ai/orchestrator.py`, `events/imessage_watcher.py` | [x] closed 2026-11-09 (all `[~] live`) |
| 6 | A: #27 (alone) | `main.py` (all routes + WS), every Swift HTTP/WS client | [x] closed 2026-11-09 (`[~] live`) |
| 7 | A: #22 #51 (alone) | `ai/orchestrator.py` tool execution, new `tools/tool_sandbox.py` | [x] closed 2026-11-09 (`[~] live`) |
| 8 | A: #52 · B: #53 · C: #56 · D: #58 · E: #59 · F: #61 | A `ai/privacy.py`, `user_settings.py`, settings routes in `main.py`, `SettingsView.swift` · B `tools/tool_sandbox.py` · C new `routes/ideas.py`, `IdeaFeedback` in `ai/open_help.py`, `ai/idea_match.py`, new `DeclinedIdeasView.swift` · D `tools/read_active_tab.py` · E new `ai/permissions.py`, new `routes/permissions.py`, new `PermissionsView.swift` · F `ToolManagerView.swift`, new `ToolDetailView.swift`, new `routes/tool_detail.py`, provenance in `tools/meta_tool_generator.py` (+ each slot's new test file) | [x] closed 2026-11-09 (all `[~] live`) |
| 9 | A: #60 (alone) | `schemas/ws_messages.py`, `WSMessage.swift`, `WebSocketManager.swift`, new `CalendarBridge.swift`, `Info.plist`, `tools/calendar_access.py`, WS routing in `main.py` | [x] closed 2026-11-09 (`[~] live`) |
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
#27 (user: Origin check + token) reject browser-originated requests, plus a
random per-install token in `~/.bot-inator/token` (0600), required by
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
- #30 — Nested-repo first-use regression test · 2026-11-09 · [DONE.md](DONE.md)
- #39 — Groq engine tests fail on Python 3.14 · 2026-11-09 · [DONE.md](DONE.md)
- #40 — test_dry_run order flake (not reproducible, closed) · 2026-11-09 · [DONE.md](DONE.md)
- #48 — Approval wrapping stacked on every agent rebuild · 2026-11-09 · [DONE.md](DONE.md)
- #49 — Drafting prompt printed wrong import paths for built-ins · 2026-11-09 · [DONE.md](DONE.md)
- #50 — Flaky restart test: stop() overwrote the last exit code · 2026-11-09 · [DONE.md](DONE.md)
- #37 — Clear test entries from idea_feedback.json · 2026-11-09 · [DONE.md](DONE.md)
- #54 — Judge replies came back empty on Groq reasoning models · 2026-11-09 · [DONE.md](DONE.md)
- #55 — Chat and event turns failed on Groq (tool-call rejections) · 2026-11-09 · [DONE.md](DONE.md)
- #57 — Repeated approved calls; declined tool ideas asked again · 2026-11-09 · [DONE.md](DONE.md)

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

#32 — Persist idea-source state across restarts [~] live [Q20]
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

#36 — Open-help judge is too lenient; log its score [~] live
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


#38 — Repeated approval prompts after generate_custom_tool fails [~] live
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

#33 — Idea source: setup scan [~] live
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

#34 — Idea source: friction [~] live
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

#35 — Idea source: starter catalog [~] live
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

#25 — Pattern detection cost: adaptive cadence [~] live [Q12]
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

### Generated tools

#28 — Quarantine a generated tool that fails to load [~] live [Q15]
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

#22 — Real sandbox for installed generated tools [~] live [Q9]
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

#23 — Disable a generated tool without deleting it [~] live [Q10]
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

#24 — Name collisions between generated tools [~] live [Q11]
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

#29 — Tool manager: refresh on a tools_changed broadcast [~] live [Q16]
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

#61 — Tool detail view: summary, run conditions, and source for every tool [~] live
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

#43 — Shortcuts runner: list and run the user's Shortcuts [~] live
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

---

#44 — Spotlight file search [~] live
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

#45 — Reminders: list and create [~] live
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

---

#46 — Notes: create and append [~] live
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

---

#47 — App and window control [~] live
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

---

#58 — Read the active browser tab's page text [~] live
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

### Privacy

#51 — Idea text from a local engine can quote personal text into a cloud prompt [~] live
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

#52 — Settings toggle for sending personal text to cloud engines [~] live
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

#53 — Sandboxed tools can still reach outside via `open` / `osascript` [~] live
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

#56 — Review and remove remembered denials [~] live
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

### Test hygiene


### Events and listeners

#60 — Calendar through EventKit in the menu bar app [~] live
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

---

#41 — Read email from Mail.app's local store [~] live
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

#42 — Use the Mac's Calendar: read events, create events, upcoming-meeting events [~] live
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

#14 — File-watcher noise from in-progress downloads [~] live [Q1]
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

#16 — MacAppListener watchdog [~] live [Q3]
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

#17 — Debounce rapid app switches [~] live [Q4]
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

#19 — First-run Full Disk Access prompt for the iMessage watcher [~] live [Q6]
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

#59 — Permissions checklist: check and request every macOS permission [~] live
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

### Approvals and UI

#15 — Closing the approval window with the red X [~] live [Q2]
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

#18 — Editable approval for more tools [~] live [Q5]
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

### Memory

#20 — sqlite-vec needs a SQLite with load_extension [~] live [Q7]
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

#21 — Memory retention policy [~] live [Q8]
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

### Security and providers

#27 — HTTP API has no authentication [~] live [Q14]
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

#31 — Groq privacy surface [~] live [Q18]
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

#26 — Per-engine provider override [~] live [Q19]
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

