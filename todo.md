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
   AMENDED (user, 2026-11-10, for #78): consent is given per action, OR
   by a STANDING RULE the user created, can see, and can revoke. A
   standing rule is the only other bypass, and only within the limits in
   LIVING_SPEC §1 "Standing rules". Nothing else may act without a card.
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

Next ID: #80
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
| 10 | A: #66 · B: #67 · C: #65 · D: #63 | A `ai/open_help.py`, `ai/friction.py`, `ai/pattern_recognizer.py`, `tests/test_open_help.py`, `tests/fixtures/open_help_windows.json` · B `ai/memory.py`, `tools/remember_preference.py`, `tests/test_memory.py` · C new `events/daily_brief.py`, `user_settings.py`, new `DailyBriefSettingsView.swift` · D new `tools/email_draft.py`, `ai/permissions.py`, `ApprovalAlert.swift` (+ each slot's new test file). Runner at close: `tools/__init__.py`, `main.py`, `ai/orchestrator.py`, `SettingsView.swift`, LIVING_SPEC | [x] closed 2026-11-10 (#63 #65 #66 `[~] live`; #67 no bug, `[~] live`) |
| 11 | A: #64 (alone) | `ai/orchestrator.py`, `ApprovalAlert.swift`, `CodeReviewAlert.swift`, `tools/meta_tool_generator.py`, `routes/tool_detail.py`, `ToolDetailView.swift`, new `ai/tool_summary.py`, optional `summary` field in `schemas/ws_messages.py` + `WSMessage.swift`. Runner: #68 in `ai/privacy.py` | [x] closed 2026-11-10 (#64 `[~] live`) |
| 12 | A: #62 (alone, cross-cutting: every Swift view) | `MenuBarUI/MenuBarUI/Views/*.swift`, `MenuBarUIApp.swift`, `ApprovalPresenter.swift`, new `Views/TextSize.swift` | [x] closed 2026-11-10 (#62 `[~] live`) |
| 13 | A: #75 · B: #79 · C: #70 · D: #71 · E: #69 · F: #74 | A new `ai/activity_store.py`, new `ai/routines.py` · B new `ai/idea_outcomes.py` (also the approval-decision log #72/#78 read), new `routes/outcomes.py` · C `ai/idea_sources.py`, new `ai/needs_reply.py` · D new `events/meeting_prep.py`, `user_settings.py`, new `MeetingPrepSettingsView.swift` · E new `ai/mcp_client.py`, new `routes/mcp.py`, new `McpServersView.swift` · F new `Views/QuickAskPanel.swift`, new `Services/HotKey.swift`, `ai/permissions.py`, new `ai/chat_context.py`, `schemas/ws_messages.py`, `WSMessage.swift` (+ each slot's new test file). Runner at close: `ai/orchestrator.py`, `main.py`, `tools/__init__.py`, `MenuBarUIApp.swift`, `SettingsView.swift`, `ToolManagerView.swift`, `DebugView.swift`, `requirements.txt`, LIVING_SPEC | [ ] |
| 14 | A: #72 · B: #73 · C: #76 · D: #77 | A `ai/memory.py`, new `ai/preference_learning.py`, new `routes/preferences.py`, new `LearnedPreferencesView.swift` · B new `ai/waiting_on.py` (sent-mail query + offers), `events/mail_watcher.py` (additive sent query) · C new `ai/prepared_offers.py`, `ApprovalAlert.swift` · D new `events/lookahead.py` (+ each slot's new test file). Runner at close: `ai/idea_sources.py`, `events/daily_brief.py`, `ai/orchestrator.py`, `main.py`, `SettingsView.swift`, LIVING_SPEC | [ ] |
| 15 | A: #78 (alone; needs #75 + #79 data) | new `ai/standing_rules.py`, new `routes/rules.py`, new `RulesView.swift`, approval path in `ai/orchestrator.py`, `main.py`, `SettingsView.swift`, LIVING_SPEC §1 | [ ] |
| — | every `[~] live` item (waves 1–12) | Live checks list — marked done by the user 2026-11-10; list and write-ups moved to DONE.md | [x] closed 2026-11-10 |

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

## Live checks

None open. All live checks were marked done by the user on 2026-11-10; the
list is in DONE.md ("Live checks — marked done 2026-11-10"). New `[~] live`
items add their line here.

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
- #68 — Idea scrub leaked the last word of a single-quoted quote · 2026-11-10 · [DONE.md](DONE.md)

---
- #14 — File-watcher noise from in-progress downloads · 2026-11-10 · [DONE.md](DONE.md)
- #15 — Closing the approval window with the red X · 2026-11-10 · [DONE.md](DONE.md)
- #16 — MacAppListener watchdog · 2026-11-10 · [DONE.md](DONE.md)
- #17 — Debounce rapid app switches · 2026-11-10 · [DONE.md](DONE.md)
- #18 — Editable approval for more tools · 2026-11-10 · [DONE.md](DONE.md)
- #19 — First-run Full Disk Access prompt for the iMessage watcher · 2026-11-10 · [DONE.md](DONE.md)
- #20 — sqlite-vec needs a SQLite with load_extension · 2026-11-10 · [DONE.md](DONE.md)
- #21 — Memory retention policy · 2026-11-10 · [DONE.md](DONE.md)
- #22 — Real sandbox for installed generated tools · 2026-11-10 · [DONE.md](DONE.md)
- #23 — Disable a generated tool without deleting it · 2026-11-10 · [DONE.md](DONE.md)
- #24 — Name collisions between generated tools · 2026-11-10 · [DONE.md](DONE.md)
- #25 — Pattern detection cost: adaptive cadence · 2026-11-10 · [DONE.md](DONE.md)
- #26 — Per-engine provider override · 2026-11-10 · [DONE.md](DONE.md)
- #27 — HTTP API has no authentication · 2026-11-10 · [DONE.md](DONE.md)
- #28 — Quarantine a generated tool that fails to load · 2026-11-10 · [DONE.md](DONE.md)
- #29 — Tool manager: refresh on a tools_changed broadcast · 2026-11-10 · [DONE.md](DONE.md)
- #31 — Groq privacy surface · 2026-11-10 · [DONE.md](DONE.md)
- #32 — Persist idea-source state across restarts · 2026-11-10 · [DONE.md](DONE.md)
- #33 — Idea source: setup scan · 2026-11-10 · [DONE.md](DONE.md)
- #34 — Idea source: friction · 2026-11-10 · [DONE.md](DONE.md)
- #35 — Idea source: starter catalog · 2026-11-10 · [DONE.md](DONE.md)
- #36 — Open-help judge is too lenient; log its score · 2026-11-10 · [DONE.md](DONE.md)
- #38 — Repeated approval prompts after generate_custom_tool fails · 2026-11-10 · [DONE.md](DONE.md)
- #41 — Read email from Mail.app's local store · 2026-11-10 · [DONE.md](DONE.md)
- #42 — Use the Mac's Calendar: read events, create events, upcoming-meeting events · 2026-11-10 · [DONE.md](DONE.md)
- #43 — Shortcuts runner: list and run the user's Shortcuts · 2026-11-10 · [DONE.md](DONE.md)
- #44 — Spotlight file search · 2026-11-10 · [DONE.md](DONE.md)
- #45 — Reminders: list and create · 2026-11-10 · [DONE.md](DONE.md)
- #46 — Notes: create and append · 2026-11-10 · [DONE.md](DONE.md)
- #47 — App and window control · 2026-11-10 · [DONE.md](DONE.md)
- #51 — Idea text from a local engine can quote personal text into a cloud prompt · 2026-11-10 · [DONE.md](DONE.md)
- #52 — Settings toggle for sending personal text to cloud engines · 2026-11-10 · [DONE.md](DONE.md)
- #53 — Sandboxed tools can still reach outside via `open` / `osascript` · 2026-11-10 · [DONE.md](DONE.md)
- #56 — Review and remove remembered denials · 2026-11-10 · [DONE.md](DONE.md)
- #58 — Read the active browser tab's page text · 2026-11-10 · [DONE.md](DONE.md)
- #59 — Permissions checklist: check and request every macOS permission · 2026-11-10 · [DONE.md](DONE.md)
- #60 — Calendar through EventKit in the menu bar app · 2026-11-10 · [DONE.md](DONE.md)
- #61 — Tool detail view: summary, run conditions, and source for every tool · 2026-11-10 · [DONE.md](DONE.md)
- #62 — Larger fonts in the app · 2026-11-10 · [DONE.md](DONE.md)
- #63 — Write email: drafts in Mail.app · 2026-11-10 · [DONE.md](DONE.md)
- #64 — Human-readable summary when a tool is suggested · 2026-11-10 · [DONE.md](DONE.md)
- #65 — Morning brief: one card with today's meetings, mail to answer, reminders due · 2026-11-10 · [DONE.md](DONE.md)
- #66 — Open-help ideas must be grounded in content, not app focus alone · 2026-11-10 · [DONE.md](DONE.md)
- #67 — memory.db has no memories: find out whether the agent is saving any · 2026-11-10 · [DONE.md](DONE.md)

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
by help for effort.

#65–#67 came out of the 2026-11-10 usefulness review ("how can I make this
app more useful?"). As of 2026-11-10 every item up to #68 is closed and
archived in DONE.md; #69 (MCP client) is open.

#70–#74 came out of the second usefulness review on 2026-11-10 ("what can
I add to make the app more useful?"; user: "Yes, add them"). Suggested
order: #70, #71, #72, #73, #74. All five build on mail, calendar and
`draft_email`, none of which has a recorded live result: if one
misbehaves, check the watcher or tool underneath first.

#75–#79 came out of the third review on 2026-11-10 ("I want this app to
create solutions before a user has thought of them"; user: "Yes, add
them"). The finding behind them: the idea sources only see the last 20–50
events held in memory and nothing stores activity to disk, so the app
cannot notice anything that repeats across days. Order: #75 first (#77 and
#78 depend on its history), then #76, #77, #79. #78 was blocked on
invariant 7a; the user amended 7a on 2026-11-10, so it is open. It still
needs #75 and #79 first (their data decides when a rule is offered).

### Idea quality (Phase 5)

#70 — Offer a reply draft when an email needs one [ ]
Wave: — (not scheduled) · Agent: _ · Added: 2026-11-10 · Source: assistant (usefulness review, user asked to add)
Ask: Connect what exists. `mail_received` events, `read_email` and
`draft_email` (#63) are all there, but a draft only happens when the user
asks in Chat. When a person emails a question or request, a card should
offer the reply, already written.
Approach (to decide when scheduled):
1. A next-step rule in `ai/idea_sources.py` (as #32's `mail_deadline` and
   invite rules): on `mail_received` that is not bulk and not from the
   user, decide "needs a reply" (a direct question or request to the
   user). Deterministic pre-checks first (not a newsletter, the user is in
   To, a question mark or request phrase in subject or first lines), then
   one judge call. When in doubt, drop it (invariant 7d).
2. The idea is a built-in idea for `draft_email` with
   `reply_to_message_id` set; the agent reads the message, writes the
   body, and the normal `draft_email` approval card shows it, editable.
   Approve opens the unsent draft in Mail. Nothing is ever sent (#63).
3. Cooldown per thread and per sender through the #32 idea state, so one
   thread makes one offer; a decline is remembered (#56/#57).
4. Privacy: under a cloud engine the email body follows #31/#52. With
   personal text off, the cloud model cannot write a meaningful reply:
   then skip the offer and say why once in the Debug tab, don't draft
   from a placeholder.
Files: ai/idea_sources.py (+ ai/orchestrator.py prompt for the built-in
idea, runner), tests/test_idea_sources.py, tests/test_mail_agent.py.
Verify:
⚬ Tests: a direct question → one `draft_email` idea with the message id;
  a newsletter, a CC-only mail, the user's own mail → none; a second mail
  in the thread inside the cooldown → none; cloud engine with personal
  text off → no offer. Non-vacuity per rule 5.
⚬ Live: email yourself "Can you send me the report by Friday?" from
  another account → one card with a drafted reply → Approve → a threaded,
  unsent draft in Mail.
Deviations:

---

#71 — Meeting prep card before a meeting starts [ ]
Wave: — (not scheduled) · Agent: _ · Added: 2026-11-10 · Source: assistant (usefulness review, user asked to add)
Ask: Ten minutes before a meeting, show what the user needs for it without
being asked: who is in it, the last emails with them, files that match.
Approach (to decide when scheduled):
1. On `event_starting_soon` (already published by
   `events/calendar_watcher.py`), gather read-only, as `events/daily_brief.py`
   does: attendees and location or call link from the event; the last few
   mail headers from or to those attendees (extend `recent_mail` with a
   sender filter); `find_files` on the title's content words; the Notes
   note with the same title if one exists.
2. The orchestrator words it in at most 6 lines (thought + notification,
   as the brief). Read-only tools only, so no approval card (7a). The
   existing "open the call link" idea (#32) stays a separate, gated card.
3. Each source that fails is named, not dropped. Mail is headers only,
   subjects redacted for cloud engines (as #65).
4. Setting: on/off, and minutes before (default 10). Skip all-day events
   and events with no other attendee.
Files: new events/meeting_prep.py (or a function in daily_brief.py if it
stays small), tests/test_meeting_prep.py, user_settings.py; runner:
orchestrator branch, Settings toggle next to Morning brief.
Verify:
⚬ Tests with fake sources: payload built for an event with attendees;
  solo and all-day events skipped; a failing source is named; no approval
  path touched; subjects redacted under a cloud engine.
⚬ Live: create an event 12 minutes out with one attendee you have
  emailed → one prep thought and notification about 10 minutes before.
Deviations:


---

#75 — Activity history and routine detection across days [ ]
Wave: — (not scheduled) · Agent: _ · Added: 2026-11-10 · Source: assistant (anticipation review, user asked to add)
Ask: Let the app notice what the user repeats over days and weeks, and
offer the routine before they think to ask. Today (2026-11-10)
`PatternRecognizer` keeps 50 events and `OpenHelp` 20, in memory only;
`memory.db` has `chat_log` and `memories`, no event table.
Approach (to decide when scheduled):
1. Local event store: a table (own SQLite file in `~/.bot-inator/`, 0600)
   with time, event type and a small set of NON-CONTENT fields: app name,
   file extension and folder (not file name), mail "bulk or person" flag,
   weekday and hour. No message text, subjects, window titles, URLs or
   clipboard content. Retention 90 days, row cap, cleared by a "Clear
   activity history" button and by `clear_memory`.
2. Deterministic mining, run once a day or on demand (no model call):
   a. time routines: the same set of apps/files opened in the same
      weekday-hour slot on ≥ 3 separate weeks;
   b. trigger → action pairs: event A followed within N minutes by action
      B on ≥ 5 separate days (e.g. `.pdf` lands in Downloads → moved to
      the same folder);
   c. each candidate carries its evidence (count, days, last seen).
3. A candidate becomes an idea through the normal filter (checks, judge,
   declined-idea match, cooldown: 7d). The model only words the offer;
   the card shows the evidence ("seen on 6 of the last 6 Mondays"). The
   offer maps to an existing tool or Shortcut when one fits (`open_app`,
   `move_file`, `run_shortcut`), else to `generate_custom_tool`.
4. Every action the routine leads to is still an approval card (7a).
   Automatic execution is only through a standing rule (#78), not this
   item.
5. Privacy: the store and the mining are local. Under a cloud engine only
   the candidate's summary (app names, folder, counts) reaches a prompt.
   On/off switch in Settings; off means nothing is stored.
6. #66 still applies: "you switch between A and B a lot" is not a
   routine. A candidate must end in an action that saves steps.
Files: new ai/activity_store.py, new ai/routines.py, tests for both;
runner: record hook in ai/orchestrator.py `_handle_event`, start-up in
main.py, Settings switch, route for clear/list, LIVING_SPEC.
Verify:
⚬ Tests: only allow-listed fields are stored (a subject or title in the
  event never reaches the table); retention and cap; three Mondays of the
  same opens → one candidate with evidence, two → none; trigger→action on
  5 days → candidate; app-switch-only history → none; clear empties it.
  Non-vacuity per rule 5.
⚬ Live: `python -m ai.routines --report` on a week of real history lists
  candidates with counts; the first offer card shows its evidence.
Deviations:

---

#76 — Show the finished result, not a proposal [ ]
Wave: — (not scheduled) · Agent: _ · Added: 2026-11-10 · Source: assistant (anticipation review, user asked to add)
Ask: An offer should arrive already worked out. Today a card asks whether
to do or build something; the user still has to imagine the outcome.
Instead do every read-only step first and show the concrete result with
one decision left.
Approach (to decide when scheduled):
1. A "prepared offer" pipeline for next-step ideas (`ai/idea_sources.py`):
   before publishing, run a fixed, read-only preparation for that idea
   kind and attach the result to the idea, so the approval card arrives
   filled in and editable (#18 cards).
2. First two kinds:
   a. a document lands in Downloads (invoice, receipt, statement, ticket):
      `summarize_file` extracts vendor, amount, due date; propose a file
      name and the destination folder (from #75's history when present,
      else the folder where similar files already are, via `find_files`);
      card: "File as <name> in <folder> and remind you on <date>?" → one
      approve runs `move_file` + `create_reminder`;
   b. an email with a date or invitation: the `create_event` card already
      has title, time, place and attendees filled in.
3. Only READ_ONLY tools run during preparation; a preparation step that
   would need a gated tool is skipped. Budget: one model call and 10 s;
   on failure fall back to today's plain offer, never block the card.
4. A card that bundles two actions lists both, and each can be unticked.
5. Privacy: preparation reads file or mail content, so under a cloud
   engine it follows #31/#52 (analysis stays local or is skipped).
Files: ai/idea_sources.py (+ new ai/prepared_offers.py), ApprovalAlert.swift
for the two-action card, tests; runner: orchestrator payload.
Verify:
⚬ Tests with fake tools: an invoice file → a card payload with name,
  folder, due date and both actions; unticking one runs only the other;
  no gated tool is called before approval; preparation failure → plain
  offer.
⚬ Live: download a PDF invoice → one card with the proposed name, folder
  and reminder date → Approve → file moved, reminder created.
Deviations:

---

#79 — Track what happens to each suggestion and use it [ ]
Wave: — (not scheduled) · Agent: _ · Added: 2026-11-10 · Source: assistant (anticipation review, user asked to add)
Ask: The app should stop offering what the user ignores and lean toward
what they keep using. `idea_feedback.json` records approve/decline only.
Approach (to decide when scheduled):
1. Record per offer: source (open-help, friction, pattern, next-step,
   routine), shown / approved / declined / timed out, and afterwards:
   was the installed tool ever run again (count, last run), was it
   disabled or deleted, was the action undone soon after (file moved
   back, event or reminder deleted within an hour, where detectable).
2. Derived numbers per source and per idea kind: approval rate, "kept"
   rate. Shown in the Debug tab.
3. Use: a source or idea kind whose kept rate stays low gets a higher
   judge threshold or a longer cooldown; an "approved, never run again"
   tool is offered for removal after 30 days. Deterministic rules; no
   model call.
4. Feeds #72 (learned preferences) and the evals (`evals/`): real
   outcomes become eval cases, after the #51 scrub.
Files: ai/open_help.py `IdeaFeedback` (or new ai/idea_outcomes.py),
tool-run counter in ai/orchestrator.py (runner), routes + DebugView, tests.
Verify:
⚬ Tests: outcomes recorded for each path; a tool run twice shows count 2;
  a low kept rate raises that source's threshold; an unused tool is
  listed for removal after the fake clock passes 30 days.
⚬ Live: Debug tab shows per-source approval and kept rates.
Deviations:

### Generated tools

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

### Privacy

### Test hygiene

### Events and listeners

#73 — "Waiting on" tracker: unanswered sent mail and promises made [ ]
Wave: — (not scheduled) · Agent: _ · Added: 2026-11-10 · Source: assistant (usefulness review, user asked to add)
Ask: Catch what falls through: emails the user sent that got no reply, and
things the user promised ("I'll send it Friday").
Approach (to decide when scheduled):
1. The mail watcher skips the Sent mailbox today (#41, mail-noise work).
   Add a read-only query of sent messages (Envelope Index), not a new
   event per sent mail.
2. Once a day (run with the morning brief, #65): for each sent message
   from the last 14 days that asked something and has no later message in
   the same thread from the recipient after N days (default 3), add a
   "waiting on <name>: <subject>" line to the brief. Deterministic thread
   match on the conversation id; the "asked something" test as in #70.
3. Promises: from the user's own sent text, a date phrase next to a
   first-person commitment → offer `create_reminder` (gated card). Start
   with the unanswered-mail half; promises are the riskier half (needs the
   sent body, so the #31/#52 rules decide whether a cloud engine may see
   it) and can be a follow-up item.
4. Follow-ups are offers, never automatic: "Draft a nudge?" goes through
   `draft_email`; nothing is sent.
Files: events/mail_watcher.py or events/daily_brief.py (sent-mail query),
ai/idea_sources.py for the offers, tests.
Verify:
⚬ Tests on the mail fixture: a sent question with no reply after 3 days is
  listed; one with a reply is not; one sent yesterday is not; bulk and
  no-question mail is not.
⚬ Live: the brief lists a real unanswered thread; "Draft a nudge" opens an
  unsent reply draft.
Deviations:


---

#77 — Look-ahead gap check: what is missing for the next few days [ ]
Wave: — (not scheduled) · Agent: _ · Added: 2026-11-10 · Source: assistant (anticipation review, user asked to add)
Ask: Catch a problem before the day it matters: the meeting tomorrow whose
document was never opened, the trip with no hotel confirmation, the bill
due with no reminder.
Approach (to decide when scheduled):
1. Once a day (with the morning brief, #65) read the next 3 days of
   calendar events and the dated commitments found in recent mail headers.
2. Deterministic gap detectors, each a small function with its own test:
   a. a meeting with an attachment or a linked file the user has not
      opened since it arrived (needs #75's history; skipped without it);
   b. travel (flight / train / hotel words in a title or sender) with no
      matching confirmation mail for the other legs in the last 60 days;
   c. a mail deadline (the #32 `mail_deadline` rule) with no reminder or
      event on or before that date;
   d. two events that overlap, or back-to-back events in different places.
3. Each gap is one line in the brief with the evidence and one offer
   (open the file, create the reminder, draft a mail): all through normal
   approval cards. No gap, no line. When unsure, say nothing (7d).
4. Read-only and local; mail is headers only, as #65.
Files: new events/lookahead.py, tests/test_lookahead.py; runner: call from
the daily brief, brief text section.
Verify:
⚬ Tests per detector with fake calendar and mail: gap found, gap absent,
  source unavailable → detector skipped and named.
⚬ Live: create a deadline mail with no reminder → tomorrow's brief has one
  "no reminder for …" line with a create-reminder offer.
Deviations:

### Approvals and UI

#74 — Quick-ask hotkey with the current context [ ]
Wave: — (not scheduled) · Agent: _ · Added: 2026-11-10 · Source: assistant (usefulness review, user asked to add)
Ask: Asking the agent should take one keystroke from any app, and it should
already know what the user is looking at.
Approach (to decide when scheduled):
1. A global hotkey (default ⌥Space, changeable in Settings) opens a small
   floating prompt panel over the current app. Carbon `RegisterEventHotKey`
   needs no Accessibility permission; prefer it.
2. The panel sends a normal chat message plus context the daemon already
   has: front app and window title (`list_windows`), and the selected text
   if the user chose to include it. Getting the selection needs either
   Accessibility (AXSelectedText) or a copy keystroke; use Accessibility,
   add the row to the Permissions checklist (#59), and without it fall
   back to "use what's on the clipboard?" as an explicit choice.
3. The reply shows in the same panel; tool calls raise the normal approval
   cards. No new daemon capability: this is a faster door to Chat.
4. Privacy: the selection is user-provided text for that one message; it
   is not stored in memory unless the user asks.
Files: new MenuBarUI/MenuBarUI/Views/QuickAskPanel.swift, new
Services/HotKey.swift, MenuBarUIApp.swift, SettingsView.swift (shortcut),
ai/permissions.py (Accessibility row); daemon: optional `context` field on
the chat message in schemas/ws_messages.py + orchestrator chat prompt.
Verify:
⚬ Tests: chat prompt includes the context block when sent and is unchanged
  when absent. swiftc typecheck.
⚬ Live: in Safari, select a paragraph → ⌥Space → "summarize this" → a
  summary in the panel; without Accessibility the panel offers the
  clipboard instead.
Deviations:


---

#78 — Standing rules: "always do this" [ ]
Wave: — (not scheduled) · Agent: _ · Added: 2026-11-10 · Source: assistant (anticipation review, user asked to add)
Ask: The only way the app truly acts before the user thinks is to act
without asking. After the user approves the same action for the same
trigger several times, offer to make it automatic.
DECISIONS:
0. (user, 2026-11-10: "amend invariant 7a to allow the todo") Invariant 7a
   now allows a standing rule as consent. The amended text and its limits
   are in rule 7a above and LIVING_SPEC §1 "Standing rules". The limits
   below are part of the invariant: an agent may not loosen them.
Shape (assistant, 2026-11-10; now the limits of the amended invariant):
1. Offer only after ≥ 5 approvals of the same tool for the same trigger
   kind with no edits and no denials (from #79 / #75 data).
2. A rule is narrow: one trigger kind + one tool + fixed argument pattern
   (e.g. "`.pdf` from <sender domain> in Downloads → move to <folder>").
   Never for: sending or drafting messages, `generate_custom_tool`,
   `run_shortcut`, generated tools, deletes, or anything with network.
3. Every automatic run posts a notification with Undo, and is logged.
   Rules are listed in Settings with on/off and delete; a rule that is
   undone twice switches itself off.
4. A rule never passes through a model at run time: matching and
   arguments are deterministic, so a prompt injection in a file or mail
   cannot trigger or steer it.
Files: new ai/standing_rules.py, routes + RulesView.swift,
hook in orchestrator approval path, LIVING_SPEC §1.
Verify:
⚬ Tests: no offer before 5 clean approvals; excluded tools can never get
  a rule; a matching trigger runs without a card and logs; a non-matching
  argument falls back to a card; two undos disable the rule; model output
  cannot create or change a rule.
Deviations:

### Memory

#72 — Learn preferences from approvals, denials and edits [ ]
Wave: — (not scheduled) · Agent: _ · Added: 2026-11-10 · Source: assistant (usefulness review, user asked to add)
Ask: Memory has only ever held 2 rows (#67): it grows only when the user
says "remember" and the model picks the tool. The app already sees every
approve, deny and edit on a card; it should learn from them.
Approach (to decide when scheduled):
1. Record each approval decision locally (tool, the kind of event that led
   to it, approved / denied / edited, which fields were edited, the note
   the user typed). No message text or email bodies: field names and short
   deterministic facts only.
2. Derive preferences deterministically first: "denied <tool> for <event
   kind> 3 times" → stop offering it (extends #56/#57); "always edits
   `body` shorter" → "keeps drafts short"; a typed note is saved as a
   preference as written. A model may word the sentence, never invent it.
3. Save through `save_memory(..., kind="preference", source="approval")`
   so recall (`_recall_memories`) feeds it into later prompts. Cap and
   eviction as #21; preferences are evicted last.
4. Visible and removable: list learned preferences in Settings or Tool
   Manager with a delete button (as Declined ideas, #56). A learned
   preference never widens what runs without asking: it can stop an offer
   or shape a draft, not skip an approval card (7a).
5. Local only: derivation runs on the Mac; under a cloud engine the
   recalled sentences are the only thing that reaches a prompt.
Files: ai/memory.py, new ai/preference_learning.py, hook in
ai/orchestrator.py approval handling (runner), new routes/preferences.py +
LearnedPreferencesView.swift, tests/test_preference_learning.py.
Verify:
⚬ Tests: three denials → one preference and no fourth offer; an edit that
  shortens the body twice → "short drafts" preference recalled in the next
  draft prompt; a note is saved verbatim; deleting a preference stops it
  being recalled; no approval is ever skipped because of one.
⚬ Live: deny the same offer three times → it stops; the preference shows
  in the list → delete it → the offer can return.
Deviations:


### Security and providers

#69 — MCP client: use other apps' MCP servers as tools [ ]
Wave: — (not scheduled) · Agent: _ · Added: 2026-11-10 · Source: user
Ask: "Should I add an MCP server to this app so it can communicate with
other apps easier?" Answer given: the app should be an MCP CLIENT (it
connects to other apps' servers: Slack, GitHub, Notion, Linear, Drive) so
their tools sit next to the built-ins. That covers cloud services
AppleScript can't reach and generated tools can't either (the sandbox
withholds API keys). User: "Yes, add #69".
DECISIONS (assistant, 2026-11-10; the user can overturn any):
1. CLIENT ONLY. Not an MCP server: exposing bot-inator's tools to other AI
   apps would be a second way past the approval cards. Revisit only with a
   concrete use.
2. STDIO FIRST. v1 connects only to local stdio servers (a child process,
   no network listener). Remote (HTTP) servers come later, each behind its
   own opt-in, because they send data off the Mac (invariant 7c).
3. EVERY MCP TOOL IS APPROVAL-GATED BY DEFAULT (invariant 7a). A server's
   read-only hint is the server's claim, not ours: the user marks a tool
   "runs without asking" per tool in Tool Manager. Nothing from a server
   may add itself to `READ_ONLY_TOOL_NAMES`.
4. OFF UNTIL ENABLED, PER TOOL. A new server's tools are listed but not
   loaded into the agent until the user switches them on, with a cap on
   loaded MCP tools (start at 15): the local 8B main model picks tools
   badly from a long list.
5. TOOL RESULTS ARE UNTRUSTED TEXT. They can hold instructions; they never
   cause an action without a card (the gate already enforces this for
   non-read-only tools). Under a cloud engine, personal text passed to or
   returned from a server follows #31/#52.
6. Tool names are prefixed with the server name (`slack__post_message`) so
   they can't shadow a built-in or a generated tool (#24).
Approach (to decide when scheduled):
1. Config: `~/.bot-inator/mcp_servers.json` (0600): name, command, args,
   env. Secrets for a server live there, never in prompts or logs.
2. New `ai/mcp_client.py`: start/stop each server, list its tools, wrap
   each as a smolagents tool. smolagents 1.26.0 (installed) has
   `MCPClient` and `ToolCollection.from_mcp`, and the `mcp` package is
   installed; `mcpadapt` is NOT and may be required by them: check first.
   A server that fails to start or dies is reported as a notice (#19), and
   its tools are dropped with a `tools_changed` broadcast (#29).
3. Orchestrator: MCP tools go through `_wrap_tool_for_approval` like any
   built-in; the approval card shows the server name and raw arguments.
4. Routes in a new `routes/mcp.py` (rule 12): list servers and tools,
   enable/disable a tool, mark "runs without asking", add/remove a server.
   New `McpServersView.swift` in Tool Manager or Settings.
5. Start with one server the user would use daily; ask which.
Files: new ai/mcp_client.py, new routes/mcp.py, new
MenuBarUI/MenuBarUI/Views/McpServersView.swift, tests/test_mcp_client.py;
runner: ai/orchestrator.py tool list, main.py start-up + router,
requirements.txt, LIVING_SPEC.
Verify:
⚬ Tests with a fake stdio server (a tiny Python script in the test): tools
  listed with the prefix; none loaded until enabled; an enabled tool call
  raises an approval request; a tool hinted read-only is still gated; the
  cap holds; a crashed server drops its tools and posts a notice; a name
  that collides with a built-in is refused. Non-vacuity per rule 5.
⚬ pytest gate; swiftc typecheck.
⚬ Live: add one real server → its tools appear switched off → enable one →
  ask for it in Chat → approval card names the server → it runs.
Deviations:

---
