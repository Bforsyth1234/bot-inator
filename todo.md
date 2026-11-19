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
   (#78 built in wave 16, 2026-11-11.)
   AMENDED (user, 2026-11-10, for #85): a reply "yes <code>" / "no <code>"
   from the user's own devices in the own Messages thread is consent for
   the one card carrying that code. Never for code review,
   generate_custom_tool, run_shortcut, cards with origin notes (#81) or
   cards with several actions.
   AMENDED (user, 2026-11-18, for #116/#117): for the undoable built-ins
   only, an "Always allow" click (per tool + trigger kind, revocable,
   every run announced with Undo) and a 10 s countdown while the user is
   active are consent too. LIVING_SPEC §1 "Always allow and the countdown".
   CONSTRAINT (user, 2026-11-18, for #118–#121): a personal model (LoRA
   adapter) never touches consent; the gate is the same code whichever
   model is loaded. LIVING_SPEC §1 "Personal model".
b. GENERATED CODE IS REVIEWED BEFORE IT RUNS UNSANDBOXED. Idea gate,
   then code gate. The dry run is the only place unapproved code runs,
   and only inside sandbox-exec.
c. LOCAL BY DEFAULT. Cloud inference (Groq) is opt-in; nothing new may
   send user data off the machine without the same opt-in.
   AMENDED (user, 2026-11-10, for #85): answer and card text may go through
   Apple's iMessage to the user's own handle only while the iPhone remote
   is on (AGENT_REMOTE, off by default); every send is in the privacy
   ledger (#82).
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

Next ID: #133
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
| 13 | A: #75 · B: #79 · C: #70 · D: #71 · E: #69 · F: #74 | A new `ai/activity_store.py`, new `ai/routines.py` · B new `ai/idea_outcomes.py` (also the approval-decision log #72/#78 read), new `routes/outcomes.py` · C `ai/idea_sources.py`, new `ai/needs_reply.py` · D new `events/meeting_prep.py`, `user_settings.py`, new `MeetingPrepSettingsView.swift` · E new `ai/mcp_client.py`, new `routes/mcp.py`, new `McpServersView.swift` · F new `Views/QuickAskPanel.swift`, new `Services/HotKey.swift`, `ai/permissions.py`, new `ai/chat_context.py`, `schemas/ws_messages.py`, `WSMessage.swift` (+ each slot's new test file). Runner at close: `ai/orchestrator.py`, `main.py`, `tools/__init__.py`, `MenuBarUIApp.swift`, `SettingsView.swift`, `ToolManagerView.swift`, `DebugView.swift`, `requirements.txt`, LIVING_SPEC | [x] closed 2026-11-10 (all `[~] live`) |
| 14 | A: #72 · B: #73 · C: #76 · D: #77 · E: #80 | A `ai/memory.py`, new `ai/preference_learning.py`, new `routes/preferences.py`, new `LearnedPreferencesView.swift` · B new `ai/waiting_on.py` (sent-mail query + offers), `events/mail_watcher.py` (additive sent query) · C new `ai/prepared_offers.py`, `ApprovalAlert.swift` · D new `events/lookahead.py` · E `CalendarBridge.swift`, `tools/calendar_access.py`, `events/calendar_watcher.py` (+ each slot's new test file). Runner at close: `ai/idea_sources.py`, `events/daily_brief.py`, `ai/orchestrator.py`, `main.py`, `SettingsView.swift`, LIVING_SPEC | [x] closed 2026-11-10 (all `[~] live`) |
| 15 | A: #83 · B: #81 · C: #82 · D: #87 · E: #88 · F: #90 | A new `ai/action_journal.py`, `routes/actions.py`, `ActionHistoryView.swift`, `tools/notes.py`, `tools/calendar_access.py`, `CalendarBridge.swift`, `ApprovalPresenter.swift`, `ApprovalAlert.swift`, `schemas/ws_messages.py`, `WSMessage.swift` (sole owner of the schema files this wave) · B new `ai/taint.py` · C new `ai/egress_log.py`, `routes/egress.py`, `PrivacyLedgerView.swift` · D new `ai/delivery.py`, `routes/delivery.py`, `OfferInboxView.swift`, `events/mac_listeners.py`, `events/clipboard_watcher.py`, `tools/read_active_tab.py` · E new `ai/watches.py`, `routes/watches.py`, `WatchesView.swift` · F new `ai/tool_health.py`, `routes/tool_health.py`, `tools/meta_tool_generator.py`, `CodeReviewAlert.swift`, `ToolDetailView.swift` (+ each slot's tests). B/D/F report their schema + card fields; the runner adds them at close. Runner: `ai/orchestrator.py`, `main.py`, `ai/groq_engine.py`, `ai/mlx_engine.py`, `ai/mcp_client.py`, `tools/tool_sandbox.py`, `tools/open_url.py`, `ai/idea_outcomes.py`, `user_settings.py`, Swift app/settings/debug views, LIVING_SPEC | [x] closed 2026-11-10 (all `[~] live`) |
| 16 | A: #78 · B: #89 · C: #84 · D: #86 · E: #85 (A–C accepted 2026-11-10) | A new `ai/standing_rules.py`, `routes/rules.py`, `RulesView.swift` · B new `Services/ScreenTextBridge.swift`, `tools/read_front_window.py`, `schemas/ws_messages.py`, `WSMessage.swift`, `WebSocketManager.swift` · C new `ai/demonstration.py`, `DemonstrationPanel.swift`, `Services/HotKey.swift` · D new `Services/MeetingRecorder.swift`, `events/meeting_notes.py`, `MeetingNotesSettingsView.swift`, `Info.plist`, `ai/permissions.py` · E new `events/remote_channel.py`, `RemoteSettingsView.swift`, `events/imessage_watcher.py`. Runner: orchestrator approval path, `main.py`, `tools/__init__.py`, `ai/privacy.py`, `requirements.txt`, Settings, LIVING_SPEC §1 | [x] closed 2026-11-11 (all `[~] live`) |
| 17 | runner: #98 #99 #100 #101 #91 #92 #96 (prep) · subagent: #97 #93 (#94 #95 deleted by the user 2026-11-11) | runner `ai/permissions.py`, `PanelPlacement.swift`, `DemonstrationPanel.swift`, `MeetingRecorder.swift`, `PermissionsView.swift`, `MeetingNotesSettingsView.swift`, `MenuBarUIApp.swift` footer, `LICENSE`, `README.md`, `install.sh`, `run.sh`, `scripts/release/`, `docs/releasing.md` · subagent (worktree) providers, first run, `SettingsView.swift`, `docs/providers.md` | [x] closed 2026-11-11 (all `[~] live`; #96 signing prep only; 1678 tests) |
| 18 | A: #102 #103 · B: #96 (the rest: daemon in the app, login switch, update notice) · C: #104 | A new `tools/quit_app.py`, new `tools/take_screenshot.py`, `tools/__init__.py`, `tools/meta_tool_generator.py` (prompt only), `ai/tool_health.py`, new tests · B `scripts/release/`, `docs/releasing.md`, new `Services/DaemonLauncher.swift`, `MenuBarUIApp.swift` (AppDelegate launch/stop only), `SettingsView.swift` (+ new settings view), `Info.plist`, `config.py` / `user_settings.py` (writable-path overrides only), new tests. C new `ai/tool_trust.py`, `Orchestrator._load_tool` (+ the trusted flag on approval payloads), tool routes in `routes/tool_detail.py`, `ToolDetailView.swift`, `ToolManagerView.swift`, `CodeReviewAlert.swift`, `ApprovalAlert.swift` (one label), `schemas/ws_messages.py` + `WSMessage.swift` (one optional field), new tests. Runner: disables generated `close_app` / `capture_screenshot` / `list_recent_emails` once their built-ins land, the tool_health sentence (A) pointing at trust (C), README, todo, LIVING_SPEC | [x] closed 2026-11-11 (#102 #103 #104 #96 `[~] live`; 1774 tests; #105 added, not scheduled) |
| 19 | A: #106 · B: #105 · C: #107 (user 2026-11-11: "implement the other open todos") | A `events/remote_channel.py`, `tools/send_imessage.py`, `tests/test_remote_channel.py`, new tests · B `MenuBarUI/MenuBarUI/Services/MeetingRecorder.swift`, `Views/MeetingNotesSettingsView.swift`, `Info.plist` (usage text only), `events/meeting_notes.py` only if retry needs it · C new `tools/look_at_screen.py`, new `ai/vision_engine.py`, `config.py`, `user_settings.py`, `providers.py` / `routes/providers.py`, `ai/privacy.py`, `ai/egress_log.py`, `SettingsView.swift` (+ new vision settings view), `tools/__init__.py`, `tools/meta_tool_generator.py` (prompt), `requirements.txt`, `main.py` (engine setup only), new tests. Runner: merge, `ai/taint.py` (look_at_screen = window), README, todo, LIVING_SPEC | [x] closed 2026-11-11 (#105 #106 #107 `[~] live`; 1834 tests; #108 added) |
| 20 | A: #110 · B: #108 · C: #109 (user 2026-11-11: "implement the open todos … use subagents and complete them at once") | A `ai/orchestrator.py` (reply unwrap only), `events/remote_channel.py`, `tools/send_imessage.py`, `tools/take_screenshot.py`, `tools/look_at_screen.py`, new tests · B `events/meeting_notes.py`, `Services/MeetingRecorder.swift` (daemon-side retry only), new tests · C `ai/egress_log.py`, `Orchestrator._wrap_tool_for_approval` (the forward call only), `ai/tool_trust.py` (`NETWORK_NOTE`), `PrivacyLedgerView.swift`, new tests. A and C both add egress kinds in `ai/egress_log.py` (A: image to phone; C: trusted_tool): additive, runner merges. Runner: `main.py` no extra notification on a kept failure (#108). | [x] closed 2026-11-11 (#108 #109 #110 `[~] live`; 1887 tests) |
| 21 | A: #112 · B: #111 (user 2026-11-11: "go through the open todos … use subagents to complete them") | A `tools/tool_sandbox.py` (relay + child shim), `tools/meta_tool_generator.py` (prompt), `tools/draft_dry_run.py`, `ai/mcp_client.py` (relay entry), `ai/orchestrator.py` (relayed-call cards), README, new tests · B `tools/look_at_screen.py` (visible-area check), `events/remote_channel.py` `card_text` (skip empty args), the Mac card renderer only if it shows nulls, new tests | [x] closed 2026-11-11 (#111 #112 `[~] live`; 1923 tests; #113 added, not scheduled) |
| 22 | A: #118 · B: #121 part 1 · C: #113 (user 2026-11-18: "scope every open item" → "Start wave 22") | A new `ai/training_corpus.py` (+ frozen held-out split), new `routes/training.py`, new `TrainingDataView.swift`, new `tests/test_training_corpus.py` · B `evals/run.py` (`--adapter`, callable entry), `evals/suites/__init__.py`, new `evals/suites/safety.py`, new `evals/cases/safety.json`, `ai/mlx_engine.py` (adapter load only), new `tests/test_personal_evals.py` · C `ai/orchestrator.py` (#113 reply fill-in + mail prompt only), `tools/email_draft.py`, `ai/needs_reply.py`, new `tests/test_reply_threading.py`. Runner at close: `ai/orchestrator.py` (#118 record hook, after C), `user_settings.py`, `main.py`, `SettingsView.swift`, LIVING_SPEC §1 | [x] closed 2026-11-18 (#113 #118 `[~] live`; #121 part 1 done; 2048 tests) |
| 23 | A: #119 · B: #121 part 2 (user 2026-11-18: "Start wave 23") | A new `ai/personal_training.py`, `routes/training.py` (start / cancel / status), new `TrainingProgressView.swift`, new `scripts/seed_training_corpus.py`, new `tests/test_personal_training.py` · B new `evals/suites/personal.py`, new `ai/model_compare.py`, `ai/training_corpus.py` (additive comparison table only), `evals/suites/__init__.py`, `tests/test_personal_evals.py`. Runner at close: `ai/orchestrator.py` (threshold → training card; #79 never-run → corpus), `schemas/ws_messages.py` + `WSMessage.swift` (`training_progress`), `main.py`, `SettingsView.swift` | [x] closed 2026-11-18 (#119 #121 `[~] live`; 2102 tests) |
| 24 | A: #120 (alone; user 2026-11-18: "Start wave 24") | A new `ai/model_eval.py`, `ai/mlx_engine.py` (adapter on/off), `user_settings.py` (active adapter key), new `routes/personal_model.py`, `routes/training.py` (opt-out turns the adapter off), new `PersonalModelView.swift`, new `tests/test_model_eval.py`. Runner at close: `ai/orchestrator.py` (10% base routing + comparison record), `main.py` (gate after training, daily switch-back check, router), `SettingsView.swift`, LIVING_SPEC | [x] closed 2026-11-18 (#120 `[~] live`; 2137 tests) |
| 25 | A: #122 · B: #123 (user 2026-11-18: "finish the todos i want to do B") | A `ai/orchestrator.py` (refusal in `gated_forward` + mail prompt only), `ai/taint.py`, new `tests/test_injected_shortcut.py` · B `tools/meta_tool_generator.py` (prompt + idea check), `tools/tool_sandbox.py` (profile), `ai/open_help.py` (idea judge text only, if needed), `evals/cases/safety.json` (`del_*`), new `tests/test_trash_only_tools.py`. Runner at close: LIVING_SPEC §1, card text plumbing in `ai/orchestrator.py` if B needs it | [x] closed 2026-11-18 (#122 #123 `[~] live`; 2177 tests) |
| 26 | A: #124 · B: #126 · C: #127 · D: #128 (user 2026-11-18: "divide up all the todos into waves. and then implement them") | A new `tools/web_search.py`, `ai/egress_log.py` (one kind + one record helper, additive), `ai/taint.py` (the two tool sets only), new `tests/test_web_search.py`, `evals/cases/safety.json` (`inj_search_exfil` only) · B `tools/find_files.py`, new `tools/recall_activity.py`, `tests/test_find_files.py`, new `tests/test_recall_activity.py` · C new `Services/ContactsBridge.swift`, new `tools/contacts.py`, `schemas/ws_messages.py`, `WSMessage.swift`, `WebSocketManager.swift`, `Info.plist`, new `tests/test_contacts.py` · D new `events/weekly_report.py`, new `routes/weekly_report.py`, new `Views/WeeklyReportView.swift`, new `tests/test_weekly_report.py`. Runner at close: `tools/__init__.py`, `ai/orchestrator.py` (#124 card in a marked or background turn; `_describe_event` for the weekly report), `main.py`, `user_settings.py`, `ai/permissions.py`, `ai/privacy.py`, `ai/taint.py` (#127 origin label, after A), `tools/meta_tool_generator.py` (prompt), `SettingsView.swift`, `PermissionsView.swift`, LIVING_SPEC, README | [x] closed 2026-11-18 (#124 #126 #127 #128 `[~] live`; full gate with wave 27: 2408 tests) |
| 27 | A: #125 (alone: it changes how a background turn runs, so it goes last) | `ai/watches.py`, new `tests/test_scheduled_ask.py`, `ai/orchestrator.py` (the ask turn only), `ai/delivery.py` (one event type) | [x] closed 2026-11-19 (#125 `[~] live`; 2408 tests, swiftc clean) |
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

None open (marked done by the user 2026-11-19; list in DONE.md). #96 is open below: it needs the user's signing run, not a live check.

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

- #98 — BUG: Automation → Messages "Request" does nothing · 2026-11-11 · [DONE.md](DONE.md)
- #99 — bot-inator never appears in System Settings → Microphone · 2026-11-11 · [DONE.md](DONE.md)
- #101 — BUG: Watch me and meeting-notes panels open off the right edge of the screen · 2026-11-11 · [DONE.md](DONE.md)

- #129 — Smaller stock local models for Macs under 24 GB · 2026-11-19 · [DONE.md](DONE.md)
- #70 — Offer a reply draft when an email needs one · 2026-11-19 · [DONE.md](DONE.md)
- #113 — BUG: reply drafts open as a new email instead of a reply in the thread · 2026-11-19 · [DONE.md](DONE.md)
- #71 — Meeting prep card before a meeting starts · 2026-11-19 · [DONE.md](DONE.md)
- #75 — Activity history and routine detection across days · 2026-11-19 · [DONE.md](DONE.md)
- #76 — Show the finished result, not a proposal · 2026-11-19 · [DONE.md](DONE.md)
- #79 — Track what happens to each suggestion and use it · 2026-11-19 · [DONE.md](DONE.md)
- #80 — Attendees in the calendar layer (for #71 meeting prep) · 2026-11-19 · [DONE.md](DONE.md)
- #88 — "Watch for…": triggers the user writes, with a backtest · 2026-11-19 · [DONE.md](DONE.md)
- #125 — Scheduled asks: a time watch that runs a saved question and shows the answer · 2026-11-19 · [DONE.md](DONE.md)
- #84 — "Watch me": record one demonstration, get a reviewed tool · 2026-11-19 · [DONE.md](DONE.md)
- #90 — Repair an installed tool when it starts failing · 2026-11-19 · [DONE.md](DONE.md)
- #112 — Generated tools can call enabled MCP tools (through a daemon relay) · 2026-11-19 · [DONE.md](DONE.md)
- #114 — BUG: a draft that keeps its file in ~/.bot-inator passes the dry run, then fails every call · 2026-11-19 · [DONE.md](DONE.md)
- #89 — Read the front window's text in any app, on demand · 2026-11-19 · [DONE.md](DONE.md)
- #102 — Built-in quit_app and take_screenshot (generated versions can't work in the sandbox) · 2026-11-19 · [DONE.md](DONE.md)
- #103 — Built-in list_recent_emails (Mail won't give message data to a sandboxed tool) · 2026-11-19 · [DONE.md](DONE.md)
- #104 — Per-tool trust: run a reviewed generated tool outside the sandbox · 2026-11-19 · [DONE.md](DONE.md)
- #107 — Vision: "look at my screen" with a local vision model · 2026-11-19 · [DONE.md](DONE.md)
- #124 — Built-in web_search and fetch_page (opt-in, in the privacy ledger) · 2026-11-19 · [DONE.md](DONE.md)
- #126 — Recall: "what was that file from last Tuesday?" (date filter in find_files, plus a timeline tool) · 2026-11-19 · [DONE.md](DONE.md)
- #127 — Contacts lookup: resolve a name to an address or number · 2026-11-19 · [DONE.md](DONE.md)
- #82 — Privacy ledger: a record of everything that left the Mac · 2026-11-19 · [DONE.md](DONE.md)
- #109 — Privacy ledger: show where a trusted tool connected · 2026-11-19 · [DONE.md](DONE.md)
- #73 — "Waiting on" tracker: unanswered sent mail and promises made · 2026-11-19 · [DONE.md](DONE.md)
- #111 — BUG: screenshots refused when a never-watch window is open but fully covered · 2026-11-19 · [DONE.md](DONE.md)
- #77 — Look-ahead gap check: what is missing for the next few days · 2026-11-19 · [DONE.md](DONE.md)
- #86 — Meeting follow-through: local notes, action items as one card · 2026-11-19 · [DONE.md](DONE.md)
- #105 — BUG: meeting notes fail when Dictation is off, and the recording is deleted · 2026-11-19 · [DONE.md](DONE.md)
- #108 — Meeting notes: a Whisper failure in the daemon still deletes the audio · 2026-11-19 · [DONE.md](DONE.md)
- #128 — Weekly report: what bot-inator did for you this week · 2026-11-19 · [DONE.md](DONE.md)
- #115 — One card for a burst of email suggestions · 2026-11-19 · [DONE.md](DONE.md)
- #116 — "Always allow" button on undoable cards · 2026-11-19 · [DONE.md](DONE.md)
- #117 — 10-second countdown on undoable cards while the user is active · 2026-11-19 · [DONE.md](DONE.md)
- #74 — Quick-ask hotkey with the current context · 2026-11-19 · [DONE.md](DONE.md)
- #78 — Standing rules: "always do this" · 2026-11-19 · [DONE.md](DONE.md)
- #83 — Undo for the agent's actions, and a lighter card for reversible ones · 2026-11-19 · [DONE.md](DONE.md)
- #85 — iPhone remote: ask and approve from your own Messages thread · 2026-11-19 · [DONE.md](DONE.md)
- #106 — BUG: iPhone remote never replies (AppleScript breaks on ’, → and other non-ASCII) · 2026-11-19 · [DONE.md](DONE.md)
- #110 — iPhone remote: send the screenshot itself to the phone, and stop raw JSON replies · 2026-11-19 · [DONE.md](DONE.md)
- #87 — Right-moment delivery: hold offers at bad times, cap them, real pause · 2026-11-19 · [DONE.md](DONE.md)
- #100 — BUG: popped-out window is missing the bottom button bar · 2026-11-19 · [DONE.md](DONE.md)
- #72 — Learn preferences from approvals, denials and edits · 2026-11-19 · [DONE.md](DONE.md)
- #118 — Opt-in training corpus: keep the prompt and card text behind each decision · 2026-11-19 · [DONE.md](DONE.md)
- #119 — Offer to train a personal model once there's enough data · 2026-11-19 · [DONE.md](DONE.md)
- #120 — Use the personal model only if it beats the base model; one-click rollback · 2026-11-19 · [DONE.md](DONE.md)
- #121 — Evals that show whether the personal model is better than the original · 2026-11-19 · [DONE.md](DONE.md)
- #122 — BUG: an injected email gets the model to run a Shortcut · 2026-11-19 · [DONE.md](DONE.md)
- #123 — Generated tools may delete only to the Trash · 2026-11-19 · [DONE.md](DONE.md)
- #69 — MCP client: use other apps' MCP servers as tools · 2026-11-19 · [DONE.md](DONE.md)
- #81 — Injection shield: mark turns that read outside text, label the card · 2026-11-19 · [DONE.md](DONE.md)
- #93 — First-run engine choice: recommend local or a cloud key from this Mac's memory · 2026-11-19 · [DONE.md](DONE.md)
- #97 — One OpenAI-compatible provider, with presets for OpenAI, Anthropic, Gemini, xAI and Groq · 2026-11-19 · [DONE.md](DONE.md)
- #91 — Make the GitHub repo say what this is: description, LICENSE, demo at the top of the README · 2026-11-19 · [DONE.md](DONE.md)
- #92 — One-command install and start · 2026-11-19 · [DONE.md](DONE.md)

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

#81–#90 came out of the fourth review on 2026-11-10 ("10 more things to
add that will differentiate this app from its competitors and that will
make the app more useful and impressive"; user: "Add all 10"). The
comparison used a same-day web search (OpenClaw's reported prompt-injection
and gateway problems, Siri AI's on-screen awareness in macOS 27, OpenAI
retiring Pulse, screenpipe's constant recording), not hands-on tests of
those apps. Three groups: trust (#81 injection shield, #82 privacy ledger,
#83 undo), demo value (#84 "Watch me", #85 iPhone remote, #86 meeting
follow-through), daily use (#87 right-moment delivery, #88 "Watch for…",
#89 front-window text, #90 tool repair). Suggested order: #81, #87, #83
(before #78 in wave 15, which needs Undo), then #82, #89, #90, #88, #84,
#86. #85 is blocked on three user decisions (see the item). None is
scheduled into a wave yet. Code facts found while writing them
(2026-11-10, read from the source, not tested): `remember_preference` runs
with no card in a turn that has read an email (#81); cloud inference has
two call paths, not one (#82); `resume_listeners` is a no-op, so Pause
cannot be undone without a daemon restart (#87); `_build_imessage_prompt`
tells the model to call `send_imessage`, which is not a registered tool
(#85).

#91–#96 came out of the user's question "how can i get users?" on
2026-11-10 (user: "add all of this to the todo.md"; #93 is the user's own
idea from the same turn). They are not features: they are what stands
between the app and a first stranger using it. Found that day (`gh repo
view`, the README, the source and the local model cache; nothing was
tested on a second Mac): the GitHub repo is public with no description,
no LICENSE, no release and 0 stars; install is 11 terminal commands plus
a 12.7 GB model download; there is no install script and no first-run
screen, and the app does not start the daemon. So today only a developer
on Apple Silicon can try it, and that is the right first audience.
Order: #91, #92, #93, #96 later. (#94 "watch five people install it" and
#95 "launch posts" were deleted by the user on 2026-11-11: "delete those
todos".) #91 and #92 both edit
README.md: one slot, or one after the other. #93 is under "Security and providers",
the rest under "Getting users (launch)". #97 (one OpenAI-compatible
provider with presets; user, same day: "i want to add more support to
other apis") sits with #93 and comes before it: #93's Cloud choice offers
#97's presets, and its Local choice takes a model folder, a local server
or a download on a button. None is scheduled into a wave yet.

#124–#128 came out of the usefulness review on 2026-11-18 ("what can i add
to this app to make it more useful?"; user: "Scope all five"). Found that
day by reading the source (nothing was run): no tool reaches the web
(#124); a watch has a `time` trigger but can only raise a card for one
fixed built-in call (#125); `find_files` has no date filter and no tool
reads `activity.db` or `actions.db` (#126); nothing reads Contacts (#127);
nothing sums up the action journal and outcome stores (#128). Two of the
five changed while being scoped: #125 was first described as "timed
tasks", which #88 already has in part, and #126 as "search your activity",
which the store cannot answer alone because it keeps no file names.
Suggested order: #124, #127, #126, #128, #125. #124's search provider was
decided by the user the same day (Brave with a key, plus a no-key
provider). Their OWNED files are disjoint, so all five
fit one wave with the runner taking the shared files; #127 is the only
one that touches the WS schema. None is scheduled into a wave yet.

### Idea quality (Phase 5)

---

---

---

---

---

---

---

### Generated tools

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

---

---

---

---

### Privacy

### Test hygiene

### Events and listeners

---

---

---

---

---

### Approvals and UI

#130 — Chat on the tool-idea and code-review cards instead of a one-shot note [ ]
Wave: _ · Agent: _ · Added: 2026-11-19 · Source: user
Ask: "when the app suggests a tool instead of a note input we should have a chat input that we can chat with the llm about what they plan on creating or in the code review on what they did create."
Today: both cards have a single "Note (optional)" `TextField` sent once with the decision (`user_note`): `Views/ApprovalAlert.swift:259` (idea gate, `generate_custom_tool`) and `Views/CodeReviewAlert.swift:182` (code gate). No back-and-forth before deciding.
Want: a small chat thread on the card. Idea gate: ask what the tool will do, which apps/files it touches, push back on scope. Code gate: ask what a line does, why it needs a permission, ask for a change. Approve / Deny stay the only way to decide.
DECISIONS (user, 2026-11-19):
1. Chat can change the plan (idea gate) or the code (code gate), not only answer. Limits from the invariants: changed code always goes back through the code gate and the dry run (7b); nothing said in chat counts as approval, only the Approve button (7a).
2. The chat history is passed as extra context to `generate_custom_tool` (idea gate) and to the code rewrite (code gate).
4. The thread is thrown away when the card closes. Nothing is stored.
5. The chat replaces the "Note (optional)" field on both cards.
3. (assistant recommendation, user OK'd 2026-11-19: "Yes, same engine") The chat uses the engine already chosen for the daemon (first-run choice, #93/#97): local unless the user opted into a cloud key. Reasons: no new data leaves the Mac without the existing opt-in (7c), and the model you talk to is the same one that rewrites the plan/code, so what it says matches what it builds. No separate "chat model" setting.
Files (expected): routes/card_chat.py (new, rule 12), ai/card_chat.py (new), schemas/ws_messages.py; Swift: Views/CardChatView.swift (new), ApprovalAlert.swift, CodeReviewAlert.swift.
Verify: a chat question on each card gets an answer grounded in the plan / code shown; a message like "approve it" does not approve; a chat-requested code change re-enters the code gate; pytest gate + Swift typecheck (rule 8).

#131 — BUG: Inbox "Held for later" fills with dozens of rows titled "file" that do nothing when clicked [ ]
Wave: _ · Agent: _ · Added: 2026-11-19 · Source: user (screenshot)
Ask: "what are all these files nothing happens when i click them."
Evidence (2026-11-19): Inbox showed "Held for later (35)", every row "file · held: you were typing · expires 4:58–5:04 PM". `~/.bot-inator/held_offers.json` had 33 held entries; 32 were `file` / `created` events for paths inside `~/Downloads/bot-inator.app/Contents/Resources/python/lib/python3.14/site-packages/torch/...` (`.py` / `__pycache__/*.pyc`), all stamped 19:58:47Z within a few ms. Unpacking the app download into Downloads emitted one event per file in the bundle.
Causes (three, all needed for the symptom):
1. Watcher floods: `events/file_watcher.py` schedules Downloads `recursive=True` and `_is_noise` only drops dotfiles and temp suffixes, so every file inside a `.app` bundle (and `__pycache__`, `site-packages`) becomes its own `file` event. Should be one event for the top-level item that appeared (the `.app`), not its contents.
2. Bad title: `ai/delivery.py` `_title()` looks for `card_text` / `description` / `tool_name` and falls back to the event type, so a raw file event shows as "file". It should show the file name (from `metadata.path`) and the action, e.g. "New in Downloads: bot-inator.app".
3. Dead rows: `Views/OfferInboxView.swift:253` renders Held with `showActions: false`, so held rows have no Show / Dismiss and clicking does nothing. Held rows need at least Dismiss, plus a "Clear all held" button; Show should run the offer now (bypassing the busy check), same as the Inbox.
Also worth checking: 3 s typing idle + 1 release per 10 s (`TYPING_IDLE_S`, `RELEASE_PER_TICK`) means a burst like this drains slowly even when it's valid; consider coalescing held `file` events from one folder within a few seconds into one entry.
Files (expected): events/file_watcher.py (skip paths inside bundles / `__pycache__` / `site-packages`; report the top-level item once), ai/delivery.py (`_title` for `file`; dismiss-held + clear-held; optional coalescing), routes/delivery.py (endpoints), Swift: Views/OfferInboxView.swift (actions on held rows, Clear all).
Verify:
⚬ New test: creating a `.app` folder with nested files in a watched dir emits exactly one `file` event; non-vacuous: restore old `_is_noise`, test fails.
⚬ New test: `_title("file", {"path": ".../Downloads/x.pdf", "action": "created"})` contains "x.pdf"; non-vacuous: revert, title is "file".
⚬ New test: dismissing a held offer removes it from `status()["held"]` and from `held_offers.json`.
⚬ Live: unzip a large app into Downloads → at most one held row, with a readable name, and Dismiss / Clear all work.
⚬ pytest gate + Swift typecheck (rule 8).

#132 — BUG: Thoughts list is not in time order [ ]
Wave: _ · Agent: _ · Added: 2026-11-19 · Source: user (screenshot)
Ask: "there is bug in the thoughts list. Its not listing in order."
Evidence (2026-11-19): Thoughts showed 16:31:41, 16:33:02, 16:33:03, 16:33:07, 16:34:45, then 15:41:25, 15:41:48 below them.
Likely cause (not yet proven): the daemon's `seq` is an in-memory counter (`ai/orchestrator.py:462` `self._seq = 0`, `_next_seq()` at :2892) that restarts at 0 whenever the daemon restarts. The app keeps `ws.messages` across reconnects (`Services/WebSocketManager.swift:435` only appends), and `Views/ThoughtStreamView.swift:43` renders `List(thoughts, id: \.seq)` with `.id(entry.seq)`. After a restart, new thoughts reuse seq numbers already in the list, so the List has duplicate ids: SwiftUI reuses/misplaces rows, and `scrollTo(last.seq)` can jump to an old row.
Confirm first: restart the daemon with the app open, wait for a few thoughts, check for duplicate `seq` values in `ws.messages` and rows out of order.
Fix (expected):
1. Row identity must be unique across daemon restarts: key rows by a client-side UUID (or daemon start id + seq), not raw `seq`.
2. Show thoughts sorted by `timestamp` (stable on ties) instead of arrival order, so a late or replayed message still lands in place.
3. Optional: the daemon could seed `_seq` from the clock at start so seq only grows; other views that key on `seq` (approvals, notices) have the same restart risk, check them.
Files (expected): Swift: Views/ThoughtStreamView.swift, Services/WebSocketManager.swift (if ids are added on receive); maybe agent-daemon/ai/orchestrator.py (`_seq` seed).
Verify:
⚬ Live: restart the daemon with the app open; new thoughts appear at the bottom in time order, old ones stay above them, scroll goes to the newest.
⚬ If the daemon seed changes: test that `_next_seq()` after a fresh Orchestrator is greater than a seq from an earlier instance; non-vacuous: revert to 0, test fails.
⚬ pytest gate + Swift typecheck (rule 8).


---

---

---

---

---

---

---

### Memory

### Personal model

#118–#121 came out of a 2026-11-18 question ("is there a use case for fine
tuning a model with this app?" → "what about adding the ability for users
to create their own models based on their own data? ... when you reach a
certain threshold (x data points) the app offers to start training a local
model for you" → "add it"; #121 from "we would need to have evals too").
Build in order: #118, #119, #121, #120 (#120's gate calls #121's suites). Code facts
(2026-11-18, read from the source, not tested): the default model is
`mlx-community/Qwen2.5-1.5B-Instruct-4bit` (`ai/mlx_engine.py:20`);
`mlx-lm>=0.32` is already in requirements.txt and ships `mlx_lm.lora`;
`MLXEngine` already swaps models under a lock; `DecisionLog` and
`OutcomeStore` (#79) hold verdicts and edit facts but no prompt or card
text, and `ActionJournal` (#83) stores ids and paths only, on purpose.

### Security and providers

#122–#123 came out of the first real safety baseline (user ran `python -m
evals.run --suite safety` on 2026-11-18, Groq openai/gpt-oss-120b: 56%,
12/24 cases solid; result in `evals/results/20261118-141921-safety-…json`;
user: "Add them"). Most failures were harmless because every side effect
still needs a card and `open_url` refuses non-http(s) URLs. These two are
not.

---

---

---

### Getting users (launch)

#96 — A signed, notarized app download for people who do not use a terminal [~] live (wave 18 B built; needs the user's signing run)
Wave: 17 (prep only; user, 2026-11-11: "Prep, I'll sign"; A answered) · Agent: _ · Added: 2026-11-10 · Source: assistant (get-users review, user: "add all of this to the todo.md")
Ask: Everything before this reaches developers only. A non-developer
needs one download that opens without a Gatekeeper warning and starts its
own daemon.
Today (2026-11-10): the app is built by hand with `swiftc` and signed ad
hoc (`codesign --sign -`); there is no Xcode project and no release; the
daemon is a Python venv the user starts in a terminal. An ad-hoc signed
rebuild may be asked for Calendar access again (README).
USER DECISION (2026-11-11): A. "Prep, I'll sign": the assistant writes
   the build / sign / notarize script and docs; the user enrols in the
   Apple Developer Program (99 USD a year), holds the Developer ID
   certificate and the notarization credentials, and runs the signing.
   The assistant never enters them.
DECISIONS (assistant, 2026-11-10; the user can overturn any):
1. DIRECT DOWNLOAD (a GitHub release: DMG or zip), not the Mac App Store:
   the App Store sandbox does not fit an app that reads `chat.db` and
   Mail's store and runs `sandbox-exec`.
2. THE APP STARTS AND STOPS ITS DAEMON, bundled inside the .app with its
   own Python, so macOS permissions belong to the app and not to a
   terminal. How to bundle Python is decided when scheduled, by trying
   the smallest option that loads MLX.
3. Start at login is a switch in Settings, off by default.
4. Updates: a "new version" notice that links to the release page. An
   in-app updater later, if at all.
Files: to decide when scheduled (a build script, entitlements, a daemon
launcher in MenuBarUIApp.swift).
Verify:
⚬ On a Mac that has never had the repo: download, open, no Gatekeeper
  warning (`spctl --assess` passes), the first-run screen (#93), one
  approved action.
Results (wave 17 prep, 2026-11-11, commit 931e8bc):
⚬ `scripts/release/build-release.sh`: builds `dist/bot-inator.app`
  (VERSION + build number stamped), signs with `--options runtime` and
  `scripts/release/MenuBarUI.entitlements` (audio-input, calendars; the app
  sends no Apple Events and uses no Reminders), zips, `notarytool submit
  --wait`, staples, re-zips, `spctl --assess`, prints SHA-256. Refuses a
  dirty tree. Without DEVELOPER_ID: unsigned dry run (checked: arm64
  binary, version 0.2.0-dryrun, CFBundleName bot-inator). shellcheck clean.
⚬ `docs/releasing.md`: enrol, Developer ID certificate, `notarytool
  store-credentials`, each-release command, `gh release create`, failures.
⚬ (wave 17) STILL OPEN then: decisions 2–4. Done in wave 18, below.
Results (wave 18 slot B, 2026-11-11):
⚬ PYTHON CHOICE: python-build-standalone CPython 3.14.10, release
  20261026, `aarch64-apple-darwin-install_only_stripped` (URL in
  `scripts/release/bundle-daemon.sh`; SHA-256
  95eb8c87d16af37c54e9765e31d8c56c0d8ffa9968ea0f704f73a9b83f84e9f9, matches
  GitHub's asset digest; 27 MB download, 69 MB unpacked). Why: the smallest
  thing that loads MLX. It links only system libraries, ships sqlite with
  `enable_load_extension` (sqlite-vec works), and pip installs the same
  cp314 wheels as the dev venv (also 3.14), so nothing is rebuilt or
  patched. Not chosen: copying Homebrew's Python (not relocatable, links
  /opt/homebrew), the system python3 (a stub on Macs without the command
  line tools), py2app/PyInstaller (a frozen app breaks the sandbox child
  and the dry run, which re-run `sys.executable -c ...`). Release 20261110
  skipped: a day old.
⚬ SIZE: app 1.5 GB on disk (Python + packages 1.5 GB, of which torch,
  pulled in by sentence-transformers, is 634 MB and .pyc 90 MB; daemon
  sources 4 MB; menu bar app 7.5 MB); zip 482 MB. Models are not bundled.
⚬ `scripts/release/bundle-daemon.sh` (called by `build-release.sh`): pinned
  download into `dist/cache/` + checksum, pip install of requirements.txt
  minus browser-use and playwright, tracked daemon sources only (no tests,
  evals, .env, generated tools), prunes Tk/IDLE/pip/headers, compiles
  unchecked-hash .pyc so the signed bundle is never written to (the app
  runs Python with `-I -B`), then imports mlx.core. `build-release.sh` now
  signs every Mach-O inside-out (283 files, found by `list-macho.py`), the
  bundled Python with `Python.entitlements`, the app last, runs `codesign
  --verify --deep --strict`, then `smoke-test.sh`. Without DEVELOPER_ID it
  signs ad hoc and stops before notarizing. `BUNDLE_DAEMON=0` /
  `SMOKE_TEST=0` opt out.
⚬ ENTITLEMENTS (bundled Python only): `personal-information.calendars`, for
  the daemon's in-process EventKit backend (calendar_access.py,
  reminders.py). Tested under the hardened runtime: ctypes callbacks, PyObjC
  blocks, mx.compile on GPU and CPU, torch and sqlite-vec all work without
  `allow-jit` and `allow-unsigned-executable-memory`, so neither is added.
  Library validation stays ON for Developer ID (every library is in the
  bundle with the same Team ID; `-I` keeps user site-packages out). The
  ad-hoc build adds `disable-library-validation` (`Python-adhoc.entitlements`):
  without it the ad-hoc import of mlx.core failed with "mapping process and
  mapped file (non-platform) have different Team IDs". The smoke test runs
  the SIGNED binary, so the release run proves library validation with the
  real Team ID before notarization.
⚬ WRITABLE HOME: the only writes into the daemon's own tree were `.env`
  (read) and `tools/generated` (+ nested repo, `disabled/`, `quarantine/`);
  everything else already lives in `~/.bot-inator`, the HF cache or temp
  dirs. New in `config.py`: `AGENT_DATA_DIR` moves both to `<dir>/.env` and
  `<dir>/tools/generated` (created at import); `AGENT_GENERATED_TOOLS_DIR`
  moves only the tools; unset = the dev layout, unchanged. A meta-path
  finder points the `tools.generated` package at the moved dir, so one
  generated tool still imports another in-process. `tool_sandbox.py`: the
  data dir and moved generated dir are write-denied and `<data dir>/.env`
  read-denied (added paths), and the child adds the module's own dir to
  `tools.generated.__path__` (one line, see Deviations). The app sets
  `AGENT_DATA_DIR=~/Library/Application Support/bot-inator` (also under the
  existing ~/Library deny rule).
⚬ APP: `Services/DaemonLauncher.swift` starts the bundled daemon only when
  `Contents/Resources/python` + `agent-daemon/main.py` exist AND nothing
  answers 127.0.0.1:8000/health; log `~/Library/Logs/bot-inator/daemon.log`
  (rotated at 10 MB); on quit SIGTERM, SIGKILL after 5 s; restart with
  backoff 1, 2, 4, 8, 16 s, reset after 120 s up; after 5 quick failures an
  alert with the log tail (port-in-use named), Try Again / Show Log. A pid
  file lets the next launch adopt (and later stop) its own daemon left by a
  force quit; any other daemon is "external" and never touched.
  `MenuBarUIApp.swift`: start() at launch, stop() in
  applicationWillTerminate, nothing else. Info.plist unchanged.
⚬ START AT LOGIN: Settings → App → "Start bot-inator at login"
  (new `Views/AppLifecycleSettingsView.swift`, one line in SettingsView),
  `SMAppService.mainApp`, off by default (never registered until switched
  on); shows the "needs approval in Login Items" state.
⚬ UPDATE NOTICE: `Services/UpdateChecker.swift` + Settings → App → "Check
  for new versions", DEFAULT OFF. Why: 7c is local by default; the check
  sends no user data (not even the version: the comparison is local), but
  it is a new request that tells GitHub this Mac's IP runs bot-inator, so
  it waits for the user. "Check now" works with the switch off (the click
  is consent for one request). What it sends: one `GET
  https://api.github.com/repos/Bforsyth1234/bot-inator/releases/latest`,
  headers `Accept: application/vnd.github+json`, `User-Agent: bot-inator`,
  ephemeral session (no cookies, no cache); daily while on. A newer tag →
  one alert per version ("Open Release Page") and a link in Settings. It
  runs IN THE APP, so the privacy ledger (ai/egress_log.py, the daemon's own
  sends) doesn't see it: it must work when the daemon is down or won't
  start, and the daemon has no route for app-originated rows. The Settings
  caption says so and names the URL.
⚬ Tests: new `tests/test_bundled_data_dir.py` (8): dev layout unchanged;
  data dir moves .env + generated tools; `tools.generated` import from the
  moved dir; profile rules; real sandbox-exec: a generated tool imports
  another from the data dir, its write there and its read of `.env` are
  PermissionError, and with the #96 rules removed the same tool writes and
  reads. Rule 5, each fix reverted on its own: child `__path__` line → 2
  fail; finder → 1 fails; dotenv path → 1 fails; sandbox rules → 2 fail;
  restored → 8 pass. Full suite: 1686 passed. Swift typecheck clean. A
  swiftc harness (scratch, not committed) checked version compare, backoff,
  argv/env, layout detection, and the lifecycle against the real bundled
  daemon on port 8766 with a throwaway HOME: start + quit (process gone);
  SIGKILL → restarting(1 s) → new pid healthy; force-quit orphan adopted,
  then stopped (port free); a fake /health already answering → "external",
  nothing started, still answering after stop. shellcheck clean.
⚬ Live check done here (ad hoc): `ALLOW_DIRTY=1 VERSION=0.2.0-dryrun
  scripts/release/build-release.sh` → 283 nested files signed, `codesign
  --verify --deep --strict` valid, the signed Python imported mlx 0.32.4 on
  the GPU + torch + PyObjC + sqlite-vec, daemon on a free port answered
  /health, nothing written inside the bundle; the zip round-trips (the
  unzipped copy verifies). In the bundle, a generated tool in the data dir
  loaded sandboxed, called another, and was refused the write to the data
  dir and the .env read.
⚬ UNVERIFIED: notarization and `spctl --assess` (no certificate); library
  validation with a real Team ID (the release run's smoke test checks it);
  that the notary accepts Mach-O under Contents/Resources (codesign strict
  does); the menu bar app itself starting the daemon (the launcher ran in a
  harness, not in the app, because the app would join the user's daemon on
  8000); permission prompts naming bot-inator for the daemon (Full Disk
  Access for chat.db, Automation via osascript, EventKit from the bundled
  Python); the failure alert UI; SMAppService on a signed build; a Mac that
  has never had the repo.
⚬ FOR THE RUNNER / USER: (1) on a Mac without the Xcode command line tools
  `/usr/bin/git` is a stub that offers to install developer tools when a
  generated tool is committed (`_git_commit` in tools/meta_tool_generator.py,
  slot A's file this wave); it needs an `xcode-select -p` guard or a
  bundled git. → DONE by the runner at wave-18 close: `_git_available()`
  (git on PATH; if it's `/usr/bin/git`, `xcode-select -p` must succeed,
  which never prompts) guards `_git_commit`, `_git_read` and the identity
  lookup; without it the tool installs with "git isn't installed …" and no
  history. `tests/test_git_stub_guard.py` 3; non-vacuity: drop the guard →
  2 fail. (2) torch is 634 MB of the 1.5 GB, only for memory
  embeddings (sentence-transformers); leaving it out halves the download
  but memory recall falls back to hash embeddings. Kept for parity: the
  user's call. (3) the dry-run probe (`tools/draft_dry_run.py`) runs with a
  minimal env, so a DRAFT that imports a generated tool may fail its dry
  run in the bundled build; untested.
⚬ Live (user): `DEVELOPER_ID=… NOTARY_PROFILE=… VERSION=0.2.0
  scripts/release/build-release.sh` passes the smoke test, notarizes,
  `spctl` accepts; stop any ./run.sh daemon; open the zip's app from
  Applications → Settings → App says "Assistant running"; first-run screen
  (#93), one approved action; Calendar / Full Disk Access prompts name
  bot-inator; quit → port 8000 free; Start at login on → it starts after
  logging in; Check now → "Up to date" or the release link.
Deviations:
⚬ The bundle installs requirements.txt minus browser-use and playwright:
  nothing in agent-daemon imports them outside tests (grep).
⚬ `tool_sandbox.py` got one line beyond protected paths (the child's
  `tools.generated.__path__` includes the module's dir): without it a
  generated tool that imports another fails in the bundled build
  (test_sandboxed_tool_composes_... fails when it is reverted).
⚬ `BOT_INATOR_PORT` (as in run.sh) also moves DaemonLauncher's port, for
  testing the launcher off 8000; the app's clients still use 8000.

---
