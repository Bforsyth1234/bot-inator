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
   AMENDED (user, 2026-11-21, for #147): background capture of the front
   window is allowed only while the screen-watch switch is on AND a timed
   session the user started is running; captures are reads that only feed
   idea sources. LIVING_SPEC §1 "Screen watching".
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

Next ID: #152
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
| 28 | A: #131 · B: #132 · C: #133 · D: #134 part 1 (index core) (user 2026-11-19: "divide up the current todos into waves and use subagents using opus 5.5") | A `events/file_watcher.py`, `ai/delivery.py`, `routes/delivery.py`, `Views/OfferInboxView.swift`, own tests · B `Views/ThoughtStreamView.swift`, `Services/WebSocketManager.swift`, `ai/orchestrator.py` (`_seq` seed only), own test · C `evals/judge.py`, `evals/suites/email_draft.py`, `evals/cases/email_draft.json`, `evals/cases/judge_calibration.json`, `evals/run.py`, `evals/suites/__init__.py`, `tests/test_eval_judge.py` · D new `ai/knowledge_index.py`, new `routes/knowledge.py`, new `Views/KnowledgeIndexSettingsView.swift`, new `tests/test_knowledge_index.py`. Runner at close: `tests/test_egress_log.py` (EXEMPT row for the index's local-only model load) | [x] closed 2026-11-20 (#131 #132 #133 `[~] live`; #134 part 1 done; 2465 tests, swiftc clean) |
| 29 | A: #134 part 2 (alone: Chat retrieval, taint, cloud toggle + ledger, registration; plus two wave-28 leftovers: `main.py` seq seed, held Show not counted as an ambient card) | `ai/orchestrator.py` (chat turn + `note_ambient_card` bypass), `ai/taint.py`, `ai/egress_log.py` (one kind), `main.py`, `SettingsView.swift` (one line), `evals/cases/safety.json` (one case), new tests | [x] closed 2026-11-20 (#134 `[~] live`; 2485 tests, swiftc clean) |
| 30 | A: #130 (alone: approval path of both gates, goes last) | new `ai/card_chat.py`, new `routes/card_chat.py`, new `Views/CardChatView.swift`, `ApprovalAlert.swift`, `CodeReviewAlert.swift`, `ai/orchestrator.py` (idea/code gate only), `tools/meta_tool_generator.py` (chat context only), `schemas/ws_messages.py` + `WSMessage.swift` if needed, `main.py` (router), `ApprovalPresenter.swift`, LIVING_SPEC, new tests | [x] closed 2026-11-20 (#130 `[~] live`; 2523 tests, swiftc clean) |
| 31 | A: #137 · B: #138 · C: #139 · D: #140 · E: #141 (readers only; user 2026-11-20: "Build them") | each slot owns one new reader module and its test: A `ai/knowledge_sources/imessage.py` · B `ai/knowledge_sources/calendar.py` · C `ai/knowledge_sources/notes.py` · D `ai/knowledge_sources/browser.py` · E `ai/knowledge_sources/ai_exports.py` (+ `tests/test_knowledge_source_<name>.py`, fixtures under `tests/fixtures/knowledge/<name>/`). Nobody edits `ai/knowledge_index.py`, routes, Swift or `ai/taint.py` | [x] closed 2026-11-20 (readers built: 167 tests) |
| 32 | A: #137–#141 wiring (alone: shared files) | new `ai/knowledge_sources/__init__.py`, `ai/knowledge_index.py` (`SOURCES`, per-source options, readers), `routes/knowledge.py`, `Views/KnowledgeIndexSettingsView.swift`, `ai/taint.py`, `ai/egress_log.py` (if #140 needs a kind), `ai/knowledge_retrieval.py` (labels), `evals/cases/safety.json`, `tests/test_knowledge_index.py`, LIVING_SPEC §3/§4, new tests. Runner at close: batch embedding in `ai/knowledge_index.py` + `ai/memory.py` | [x] closed 2026-11-20 (#137–#141 `[~] live`; 2727 tests at wave close, swiftc clean) |
| 33 | A: #144 · B: #142 · C: #135 · D: #136 (all but the embed) (user 2026-11-20: "Start building the 5 items with subagents that are not yet built") | A `tools/list_recent_emails.py`, `events/mail_watcher.py`, `tools/read_email.py`, `evals/cases/` (one case), own tests · B `Views/ChatView.swift`, `Services/WebSocketManager.swift`, the main window container (daemon only if the cause is there: report, don't edit `ai/orchestrator.py`) · C `Views/SettingsView.swift`, new `Views/SettingsSearch.swift` · D new `Views/LocalModelsSection.swift`, `main.py` (`_settings_view()` only), own test. Runner at close: embed D's section in C's table (done: 7 edits in `SettingsView.swift`, 2 in `SettingsSearch.swift`) | [x] closed 2026-11-20 (#135 #136 #142 #144 `[~] live`) |
| 34 | A: #143 (alone: overlaps #142's Swift files and owns the chat turn) | `Views/ChatView.swift`, `Views/QuickAskPanel.swift`, `Services/WebSocketManager.swift`, `Models/WSMessage.swift`, `ai/orchestrator.py` (chat turn task + cancel), `main.py` (`chat_cancel` command), new `ai/chat_turns.py`, `ai/idea_outcomes.py` + `ai/training_corpus.py` (`withdrawn` verdict), `tools/meta_tool_generator.py` (one prompt line), LIVING_SPEC, new tests; plus #144's chat-prompt time line | [x] closed 2026-11-20 (#143 `[~] live`; 2789 tests, swiftc clean) |
| — | #96 | needs the user's signing run; not schedulable | open |
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

Open (waves 28–32, 2026-11-20): #130, #131, #132, #133, #134, #135, #136, #137–#141, #142, #143, #144, #145, #146, #147, #148, #151; each item's "Live (user)" line has the steps. Earlier ones: none open (marked done by the user 2026-11-19; list in DONE.md). #96 is open below: it needs the user's signing run, not a live check.

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
- #149 — BUG: menu bar app permissions reset after every rebuild · 2026-11-23 · [DONE.md](DONE.md)
- #150 — Settings: always show Restart Daemon, and restart on its own after a save that needs it · 2026-11-25 · [DONE.md](DONE.md)

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

#151 — Tell the user up front when a generated tool needs to run outside the sandbox (or needs a macOS permission) [~] live
Wave: _ · Agent: _ · Added: 2026-11-23 · Source: user
Ask: "when the app generates a tool that needs permissions outside the sandbox shouldnt the app tell the user it needs that to work?"
Today:
⚬ The code review card (`Views/CodeReviewAlert.swift:230`) has the "Run outside the sandbox" checkbox (#104), off by default, with the same generic text for every tool: "Leave off unless the sandbox blocks something this tool needs". It never says whether THIS tool needs it.
⚬ The dry run (`tools/draft_dry_run.py` `refusal_note` / `evaluate_call`) counts a sandbox refusal as a hard problem and sends it back to the drafter. Some refusals no rewrite can fix (#102/#103: macOS refuses `quit` and Mail message data, Apple Event -10004, to any `sandbox-exec` process).
⚬ The user only finds out after install, when a call fails and `ai/tool_health.py` (`REASON_SANDBOX_APPLE_EVENT`, `REASON_SANDBOX_EXECUTABLE`, `CATEGORY_SANDBOX`) says "you can let it run outside the sandbox: Tool Manager → …".
⚬ macOS permissions (Contacts, Calendar, Automation, Full Disk Access) are likewise only reported after a failure (`CATEGORY_PERMISSION` → Settings → Permissions).
DECISIONS (user, 2026-11-25):
⚬ Q1 → both cards: the idea card (`generate_custom_tool` card in `Views/ApprovalAlert.swift`) gives an early "will likely need…" line from the idea text; the code card gives the exact reasons from the code.
⚬ Q2 → install anyway: if a tool needs outside-the-sandbox and the box is left off, Approve still installs it sandboxed. The card has already said it will fail; Tool Manager can flip it later. Approve is never blocked.
⚬ Q3 → Grant button: for each macOS permission not granted yet, the card shows a "Grant" button that opens the right System Settings pane, reusing the Permissions checklist's open action (#59, `Views/PermissionsView.swift`). Granting never approves the card.
Approach (assistant, 2026-11-23; Q1–Q3 resolved above):
1. A "needs" check from the drafted source, at code-gate time (new module, e.g. `ai/tool_needs.py`, reusing `tool_sandbox`'s AST helpers): flags `quit` / Mail message-data AppleScript, Apple Events to `DENIED_APPLE_EVENT_TARGETS`, `subprocess` of programs outside the allowed exec list, writes into closed paths (`~/Library`, `~/.ssh`), and the TCC permission each API needs (Contacts, EventKit, Mail database → Full Disk Access, `tell application` → Automation for that app).
2. Dry-run result: a refusal that matches a known unfixable class (-10004, denied exec) is recorded as "needs outside the sandbox", not sent to the drafter as a bug.
3. The code card shows a line per need, in plain words: "This tool needs to run outside the sandbox to work: it quits apps, which macOS blocks for sandboxed tools." and "It also needs Contacts access (not granted yet)." with the permission's current state from `ai/permissions.py`.
4. The checkbox stays OFF by default (invariant 7b, #104 decision 1); the card only explains. If the user leaves it off, the tool installs sandboxed and the first failure says the same thing as today.
5. The idea card (first gate) can say it too ("This will likely need to run outside the sandbox"), so the user can drop the idea before any code is written.
Files:
⚬ OWNED: new `agent-daemon/ai/tool_needs.py` (code scan + idea-text guess), new `tests/test_tool_needs.py`, `tools/draft_dry_run.py` (refusal classing only), `MenuBarUI/MenuBarUI/Views/CodeReviewAlert.swift` (needs section + Grant buttons), `MenuBarUI/MenuBarUI/Views/ApprovalAlert.swift` (one "will likely need" line on the generate_custom_tool card), new `MenuBarUI/MenuBarUI/Views/ToolNeedsSection.swift` (shared by both cards).
⚬ EDITED (additive): `Views/PermissionsView.swift` (expose the open-pane action for reuse, no behaviour change).
⚬ Runner: the `needs` field (list of {kind, reason, granted}) on the idea and code-review payloads in `schemas/ws_messages.py` + `WSMessage.swift`; `Orchestrator` fills it; LIVING_SPEC row.
Verify:
⚬ Tests: a draft that runs `tell application "Spotify" to quit` → needs `outside_sandbox` with the quit reason; one that reads Contacts → needs `contacts`; a plain file-in-Documents tool → no needs. Dry run: a -10004 refusal → recorded as a need, not a drafter problem. Non-vacuous per rule 5 (drop the quit pattern → the first test fails).
⚬ pytest gate + Swift typecheck (rule 8).
⚬ Live (user): ask for "a tool that quits Spotify" → the code card says it needs to run outside the sandbox and why; box still off by default; the idea card before it said "will likely need to run outside the sandbox". Approve with the box off → it installs; the first call fails with today's tool_health line. Ask for a tool that lists Contacts with Contacts not granted → the card says Contacts access is needed, Grant opens System Settings → Privacy → Contacts, and the card stays open.
Results (subagent, 2026-11-25):
⚬ New `ai/tool_needs.py`: source scan (quit via `tell … to quit` / tell block / JXA `.quit()`; Mail message data; Apple Events to the `DENIED_APPLE_EVENT_TARGETS` apps, pinned by a test; subprocess of programs outside `ALLOWED_EXECUTABLES`; `os.system` / `shell=True`; writes into ~/Library or ~/.ssh; permissions contacts / calendars / reminders / full_disk_access / automation:<App>, `granted` from `ai/permissions.py`), an idea-text guess, and a small store for dry-run needs.
⚬ `tools/draft_dry_run.py`: `unfixable_refusal()`: a -10004 / privilege-violation or denied-exec refusal (with no closed path named) becomes a need on `DryRunResult.needs`, not a drafter problem. #114 path refusals unchanged.
⚬ `schemas/ws_messages.py` + `Models/WSMessage.swift`: `ToolNeed` {kind, reason, granted, permission_id}; optional `needs` on `ApprovalRequestPayload` and `CodeApprovalRequestPayload` (old payloads decode). `ai/orchestrator.py`: `_card_needs()` off the loop, 3 s limit (falls back to the scan without permission checks), never holds a card back; filled on the code card and on generate_custom_tool idea cards without actions.
⚬ New `Views/ToolNeedsSection.swift` (`.exact` on `CodeReviewAlert.swift` above the trust checkbox, `.likely` one line on `ApprovalAlert.swift`), Grant buttons via new additive `PermissionsViewModel.request(permissionId:)` / `settingsURL(forNeedKind:)` in `PermissionsView.swift`. Checkbox still off by default; Approve never blocked.
⚬ New `tests/test_tool_needs.py` (19), incl. `test_tool_needs_never_touches_trust`. Non-vacuous: dropping the quit patterns → 5 fail; Contacts detection → 2; treating Documents as closed → 3; dry-run classing → 3; `needs=None` in the orchestrator → 2; restored → pass.
Verify (runner, 2026-11-25): pytest 2860 passed, 0 failed (with #150 in the tree); Swift typecheck 0 errors. LIVING_SPEC §4 row added. Live check above still to run → `[~] live`.
Deviations:
1. Each need also carries `permission_id`, so Grant calls the exact Permissions row instead of rebuilding its id from app names.
2. Dry-run needs reach the code card through a store in `ai/tool_needs.py` (`record_dry_run` / `code_needs`, matched on the card's code), so `tools/meta_tool_generator.py` (not on the list) is unchanged.
3. An email idea is guessed as Full Disk Access, not outside the sandbox: a generated tool can use the `list_recent_emails` built-in sandboxed. Only AppleScript Mail message data in the code is flagged outside-the-sandbox.
4. Grant reuses the Permissions row's own action: for Contacts and Calendars that is the app's own macOS prompt; System Settings opens when the row does that or there is no row (Reminders, unlisted Automation apps).

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

#144 — BUG: "How many emails since 12 pm" is wrong (49 instead of 5): email times are in UTC and there is no time filter [~] live
Results (wave 33 slot A + wave 34, 2026-11-20):
⚬ `events/mail_watcher.py` `_iso_date(value, tz=None)` now returns LOCAL time with offset (changed in place: no caller compares the strings as text; knowledge index, daily brief and lookahead parse to aware datetimes). Side fix: mail between 7 PM and midnight was dated tomorrow in meeting prep and in the watches dedupe key.
⚬ `tools/read_email.py`: `local_header_date()`; the `Date` shown is local ISO.
⚬ `tools/list_recent_emails.py`: `since` / `until` (ISO date-time, `YYYY-MM-DD`, or `HH:MM` = today; no offset = local), filtered in SQL. With a range: `count` = all matches (scan up to 50,000), `listed`, `truncated`, the resolved bounds, a `note` when truncated. A bad value returns a clear error. Still read-only.
⚬ Wave 34: `_build_chat_prompt` now carries "It is now <weekday> <local ISO with offset>" (the prompt had no date or time); the drafting prompt names `since, until`.
⚬ Eval: `chat_count_emails_since_noon` in `evals/cases/tool_calls.json`.
⚬ Tests: `tests/test_list_recent_emails_since.py` (24); 391 passed over the 19 caller files, also under TZ=Asia/Tokyo, Pacific/Honolulu and UTC. Rule 5: 13 breaks, each fails a test.
⚬ Live (user): "how many emails have I received since 12 pm" matches Mail; "what was my last email" shows a local time.
⚬ UNVERIFIED: whether a real model picks `since`.
Deviations: `tests/test_mail_watcher.py:100` asserted a UTC day prefix; it now checks the instant (it failed under TZ=Asia/Tokyo).
Wave: _ · Agent: _ · Added: 2026-11-20 · Source: user (screenshot)
Ask: "why is the llm wrong about this" — asked "how many emails have i recieved since 12 pm", got "49 emails since 12 pm today"; Mail showed 5 (12:21 Costco, 12:16 Fandango, 12:04 CSA Events, 12:04 Connecticut Science Center, 12:03 EcoFlow).
Cause (proven 2026-11-20 by running `list_recent_emails_from(mail_root(), 50)`):
1. UTC times. `list_recent_emails` dates come from `events/mail_watcher.py` `_iso_date` (`tz=timezone.utc`), e.g. Fandango 12:16 PM local came back as `2026-11-20T17:16:17+00:00`. The model compared these against "12:00" without converting, so everything after 7 AM local (12:00 UTC) counted. `read_email` and anything else using `_iso_date` likely has the same problem.
2. No time filter, 50 cap. The tool only takes `count` (`MAX_COUNT = 50`). The model asked for 50, got 50 (oldest `2026-11-20T12:00:47+00:00`) and counted 49 as "after 12:00". On a busy day the true count can exceed 50, so the tool cannot answer "how many since X" even with correct times.
Fix (expected):
1. Dates the model sees are local time with the offset (`2026-11-20T12:16:17-05:00`). Decide whether to change `_iso_date` itself (check the watcher's other users and tests) or add a local variant used by the tools.
2. `list_recent_emails` gets `since` (and maybe `until`): ISO date-time or "YYYY-MM-DD", local time, filtered in SQL on `date_received`. With a time range the result reports the full `count` matching (not capped at 50) and lists at most `MAX_COUNT`, saying when the list is truncated. Docstring tells the model to use `since` for "how many since …" questions.
3. Optional: put the current local time with offset in the chat system prompt if not already there, so "12 pm today" resolves correctly.
Files (expected): tools/list_recent_emails.py, events/mail_watcher.py (`_iso_date` or a new helper), maybe tools/read_email.py, tests/test_list_recent_emails*.py. No registration change (still read-only).
Verify:
⚬ New test: an Envelope Index fixture with mail at 11:55 and 12:16 local → `date` strings carry the local offset; non-vacuous: revert to UTC, test fails.
⚬ New test: `since="<today>T12:00"` returns only the 12:16 mail and `count == 1`; with 60 matching mails `count == 60` and the list is capped and marked truncated.
⚬ Eval case (evals/cases): "how many emails since 12 pm" uses `since`.
⚬ Live: ask "how many emails have I received since 12 pm" → matches Mail.
⚬ pytest gate (rule 8).
Wave: _ · Agent: _ · Added: 2026-11-20 · Source: user (screenshot)
Ask: "why is the llm wrong about this" — asked "how many emails have i recieved since 12 pm", got "49 emails since 12 pm today"; Mail showed 5 (12:21 Costco, 12:16 Fandango, 12:04 CSA Events, 12:04 Connecticut Science Center, 12:03 EcoFlow).
Cause (proven 2026-11-20 by running `list_recent_emails_from(mail_root(), 50)`):
1. UTC times. `list_recent_emails` dates come from `events/mail_watcher.py` `_iso_date` (`tz=timezone.utc`), e.g. Fandango 12:16 PM local came back as `2026-11-20T17:16:17+00:00`. The model compared these against "12:00" without converting, so everything after 7 AM local (12:00 UTC) counted. `read_email` and anything else using `_iso_date` likely has the same problem.
2. No time filter, 50 cap. The tool only takes `count` (`MAX_COUNT = 50`). The model asked for 50, got 50 (oldest `2026-11-20T12:00:47+00:00`) and counted 49 as "after 12:00". On a busy day the true count can exceed 50, so the tool cannot answer "how many since X" even with correct times.
Fix (expected):
1. Dates the model sees are local time with the offset (`2026-11-20T12:16:17-05:00`). Decide whether to change `_iso_date` itself (check the watcher's other users and tests) or add a local variant used by the tools.
2. `list_recent_emails` gets `since` (and maybe `until`): ISO date-time or "YYYY-MM-DD", local time, filtered in SQL on `date_received`. With a time range the result reports the full `count` matching (not capped at 50) and lists at most `MAX_COUNT`, saying when the list is truncated. Docstring tells the model to use `since` for "how many since …" questions.
3. Optional: put the current local time with offset in the chat system prompt if not already there, so "12 pm today" resolves correctly.
Files (expected): tools/list_recent_emails.py, events/mail_watcher.py (`_iso_date` or a new helper), maybe tools/read_email.py, tests/test_list_recent_emails*.py. No registration change (still read-only).
Verify:
⚬ New test: an Envelope Index fixture with mail at 11:55 and 12:16 local → `date` strings carry the local offset; non-vacuous: revert to UTC, test fails.
⚬ New test: `since="<today>T12:00"` returns only the 12:16 mail and `count == 1`; with 60 matching mails `count == 60` and the list is capped and marked truncated.
⚬ Eval case (evals/cases): "how many emails since 12 pm" uses `since`.
⚬ Live: ask "how many emails have I received since 12 pm" → matches Mail.
⚬ pytest gate (rule 8).

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

#130 — Chat on the tool-idea and code-review cards instead of a one-shot note [~] live
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

Results (wave 30, 2026-11-20):
⚬ New `ai/card_chat.py`: `CardChatStore`, threads in memory only, one per card request id. A message that only says "yes" / "approve it" / "LGTM" / "no" / "deny" gets a fixed "use the Approve / Deny button" reply and never reaches the model. Idea card: the model may end with `PLAN_DESCRIPTION:` / `PLAN_LOGIC:`; code card: `CODE_CHANGE:`. Limits: 1500 chars a message, 12 user turns a card, one reply in flight, 120 s timeout, 3 rewrites a build.
⚬ New `routes/card_chat.py`: `GET` / `POST /api/card-chat/{request_id}` (HTTP, behind the app-wide auth). The iPhone remote gets no chat.
⚬ ROLES: idea chat = main engine (it wrote the plan); code chat = drafting engine (it rewrites the code). No new setting. Cloud: `privacy.scrub_idea_text` and the engine's own ledger row; no new data path.
⚬ REWRITE (7b): the old code card is resolved as NOT approved and removed, the generator drafts again through checks, sandboxed dry run and critic, and the new code arrives on a NEW card (`replaces_request_id`); only that card's Approve installs it. Repair cards answer but refuse changes.
⚬ CONSENT (7a): Approve is refused while a reply or rewrite is in flight; Return no longer triggers Approve on chat cards.
⚬ THREAD: stays on the daemon; handed once to `generate_custom_tool` on Approve, closed in the approval coroutines' `finally` (approve, deny, red X, timeout). Never written to disk, memory.db, the knowledge index or the training corpus.
⚬ TAINT: a tainted card stays tainted; a chat change that repeats 5+ words of the outside text is refused, as is a plan that would delete for good (#123).
⚬ Also edited: `ai/orchestrator.py` (approval paths, ~70 lines), `tools/meta_tool_generator.py` (`chat_context`, `revise_source`, `_review_with_chat`), `schemas/ws_messages.py` + `WSMessage.swift` (optional `chat`, `replaces_request_id`), `main.py`, new `Views/CardChatView.swift`, `ApprovalAlert.swift`, `CodeReviewAlert.swift`, `ApprovalPresenter.swift`, LIVING_SPEC §1 (new paragraph) / §3 / §4.
⚬ Tests: `tests/test_card_chat.py` (38, fake engines). Rule 5: 19 one-at-a-time breaks, each turns its test red.
⚬ Live (user): ask an idea card "which files will it touch?"; type "approve it" (card stays open); ask for a narrower scope ("Plan changed in chat", Approve builds that plan); on a code card ask for a change ("Rewriting…", then a new "Code Review (rewritten)" card).
⚬ UNVERIFIED: the SwiftUI layout in the real app; whether a real model emits the `PLAN_*` / `CODE_CHANGE` markers (if not, chat answers but changes nothing); the provenance file keeps the original `expected_logic`, not the chat change.
Deviations: `test_route_requires_auth` accepts 404 or 503 for the authed call (depends on whether an earlier test ran the app lifespan); the 401 checks are unchanged.

#131 — BUG: Inbox "Held for later" fills with dozens of rows titled "file" that do nothing when clicked [~] live
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

Results (wave 28 slot A, 2026-11-20):
⚬ `events/file_watcher.py`: `classify_path()`; a file inside a bundle (`BUNDLE_SUFFIXES`, e.g. `.app`) maps to the outermost bundle, one `created` event per burst (sliding `BUNDLE_QUIET_S` = 30 s); a bundle moved into place gives one `renamed`; paths under hidden folders and `NOISE_DIRS` (`__pycache__`, `site-packages`, `node_modules`, `.git`, …) are dropped.
⚬ `ai/delivery.py`: `_title` for `file` events ("New in Downloads: x.pdf", "Moved to …", "Renamed in …", "Changed in …"); `open_held(id)` (bypasses the busy check, not counted toward the idea cap), `dismiss_held(id)`, `clear_held()`.
⚬ `routes/delivery.py`: `POST /api/delivery/held/{id}/open`, `DELETE /api/delivery/held/{id}`, `DELETE /api/delivery/held`.
⚬ `OfferInboxView.swift`: Show + Dismiss on held rows, "Clear all held".
⚬ Tests: `tests/test_file_watcher_bundles.py` (7, one on real FSEvents: an `.app` with 60 nested files → 1 event), `tests/test_delivery_held_actions.py` (13). Rule 5: old watcher routing → 6 of 7 fail; `_title` branch removed → 7 fail; dismiss/clear no-ops → 3 fail; bypass marker removed → 2 fail.
⚬ Live (user): unzip a large app into Downloads → at most one held row with a readable name; Show, Dismiss, Clear all work. Old rows get readable titles after a daemon restart.
⚬ UNVERIFIED: the Swift UI (typecheck only); Archive Utility's real unpack sequence (move path tested at handler level).
Deviations:
⚬ Plain new folders are NOT collapsed into one event, only bundles: watches, standing rules and routine move detection point at folders such as `~/Downloads/Invoices`.
⚬ Coalescing held file events not done: downstream reads one `metadata.path`, so a merged entry would drop the turn for every file but the first.

#132 — BUG: Thoughts list is not in time order [~] live
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


Results (wave 28 slot B, 2026-11-20):
⚬ CAUSE confirmed from the source (not reproduced live): `Orchestrator._seq` started at 0 on every daemon start, `WebSocketManager` only appends, and `ThoughtStreamView` keyed rows by raw `seq`, so rows had duplicate ids after a restart. Timestamps were ruled out.
⚬ `ThoughtStreamView.swift`: `ThoughtEntry` (id = index in `ws.messages`, unique because the list is append-only), sorted by timestamp, ties by arrival; scroll goes to the newest.
⚬ `ai/orchestrator.py`: `_seq` seeded from the clock in microseconds (~1.8e15, under 2^53).
⚬ Tests: `tests/test_seq_restart.py` (2). Rule 5: seed back to 0 → `assert 1 > 10000` fails.
⚬ Other views checked, no change needed: approvals key on `requestId`, notices on `notice.id`, the inbox on its HTTP ids.
⚬ Left for wave 29: `main.py`'s second counter (`app.state.seq = 0`) also restarts at 0; harmless today, seeded the same way for consistency.
⚬ Live (user): restart the daemon with Thoughts open → new thoughts at the bottom in time order.
⚬ UNVERIFIED: SwiftUI rendering and scroll in the real app.
Deviations: `WebSocketManager.swift` not changed (not needed).

#142 — BUG: Chat answer never appears if you leave the Chat window while it is thinking [~] live
Results (wave 33 slot B, 2026-11-20):
⚬ CAUSE (from the code; the user's own run not inspected): the daemon is cleared, a turn finishes with no window or WS client (`tests/test_chat_turn_without_client.py`, 3 tests pass on the old daemon code; 6 sabotage combinations fail them). The bug was in the app: `MainWindowView` builds only the selected section, `ChatView` kept its turns in a `@StateObject`, so leaving Chat destroyed them; on return the unfinished turn came back with `isPending: false`; `/api/chats` was read once per page build, so a reply missed during a socket drop was never recovered.
⚬ New `Services/ChatTranscript.swift` (Foundation only): turns keyed by message id with an explicit `isPending`; thoughts keyed by their index in `ws.messages`; history merge; a held-thoughts buffer for turns not yet known.
⚬ `Views/ChatView.swift` + `MenuBarUIApp.swift` (5 lines): `ChatViewModel` lives for the app's lifetime and reads `ws.$messages` with a cursor; `/api/chats` is re-read on appear, on reconnect, 1 s and 4 s after a `complete`, and every 15 s while a turn is pending.
⚬ RULES: a history turn with a user row and no assistant row is pending for 10 minutes, then shows "No reply was recorded for this message."; the full `chat_log` reply replaces the 500-character thought (long answers were cut); the draft survives leaving the page.
⚬ Harness (scratch, not committed): "send, leave, return before completion" shows nothing with the old logic and a spinner then the answer with the new one; 7 scenarios.
⚬ Live (user): ask, switch section before the reply, come back → spinner, then the answer; stay away until done → answer is there; a long answer shows in full.
⚬ UNVERIFIED: nothing ran in the real app. One early scratch run of the agent's sabotage script ran without the test isolation and may have opened files in `~/.bot-inator` (it believes nothing was written; not confirmed).
Deviations: `Services/ChatTranscript.swift` is a new file not in the slot's list (needed so the logic compiles without SwiftUI).
Wave: _ · Agent: _ · Added: 2026-11-20 · Source: user (screenshot)
Ask: "the chat stops working when i go away from the chat window while waiting for an answer. the answer never appears."
Evidence (2026-11-20): user asked "how many emails have i gotten today", switched away while it was working, came back: Reasoning (4) showed event_received, two memory rows and the reasoning prompt, but no answer was ever shown.
Code facts (not yet proven as the cause):
⚬ `Views/ChatView.swift` holds the turns in a `@StateObject ChatViewModel`. Leaving the Chat section (`showMainWindow(section:)`) likely tears the view down; coming back builds a new model, runs `loadHistory()` (`GET /api/chats`) and replays `ws.messages`.
⚬ The daemon writes the user row to `chat_log` at the start of the turn and the assistant row only at the end (`ai/orchestrator.py:1402`, `:1509`). On return the turn comes back from history with `isPending: false`, so no spinner, and nothing re-fetches history when the reply is logged.
⚬ `appendThought` only fills the answer from a `thought` with `stage == .complete` for an event id already in `byId`. If the final reply arrives as a different frame, or while no ChatView is alive and later drops out of `ws.messages`, it is lost to the UI.
⚬ Also check: whether the daemon turn itself stalls (no `complete` thought, no assistant row in `chat_log`) when the window loses focus, e.g. the turn is tied to the WS client or a busy/typing gate (#131's `TYPING_IDLE_S`).
Confirm first: reproduce, then check (a) `chat_log` for the assistant row, (b) the daemon log for the turn finishing, (c) `ws.messages` for a `complete` thought with that event id. (a)+(b) present → UI bug; absent → daemon bug.
Fix (expected): the chat state outlives the view (move `ChatViewModel` up to the app/`WebSocketManager` level, or keep the view alive), a turn restored from history with a user row and no assistant row shows as pending, and the reply is picked up on return (re-ingest or re-fetch `/api/chats` on appear and on `complete`).
Files (expected): Swift: Views/ChatView.swift, maybe Services/WebSocketManager.swift and the main window container; daemon only if (b) fails.
Verify:
⚬ Live: send a chat message, switch to another section or app before the reply, come back → the answer is there (or the turn shows as pending and fills in when done).
⚬ Live: same, but stay away until the turn finishes, then come back → answer shown.
⚬ If daemon code changes: test that a chat turn completes and logs the assistant row with no WS client connected; non-vacuous per rule 5.
⚬ pytest gate + Swift typecheck (rule 8).
Wave: _ · Agent: _ · Added: 2026-11-20 · Source: user (screenshot)
Ask: "the chat stops working when i go away from the chat window while waiting for an answer. the answer never appears."
Evidence (2026-11-20): user asked "how many emails have i gotten today", switched away while it was working, came back: Reasoning (4) showed event_received, two memory rows and the reasoning prompt, but no answer was ever shown.
Code facts (not yet proven as the cause):
⚬ `Views/ChatView.swift` holds the turns in a `@StateObject ChatViewModel`. Leaving the Chat section (`showMainWindow(section:)`) likely tears the view down; coming back builds a new model, runs `loadHistory()` (`GET /api/chats`) and replays `ws.messages`.
⚬ The daemon writes the user row to `chat_log` at the start of the turn and the assistant row only at the end (`ai/orchestrator.py:1402`, `:1509`). On return the turn comes back from history with `isPending: false`, so no spinner, and nothing re-fetches history when the reply is logged.
⚬ `appendThought` only fills the answer from a `thought` with `stage == .complete` for an event id already in `byId`. If the final reply arrives as a different frame, or while no ChatView is alive and later drops out of `ws.messages`, it is lost to the UI.
⚬ Also check: whether the daemon turn itself stalls (no `complete` thought, no assistant row in `chat_log`) when the window loses focus, e.g. the turn is tied to the WS client or a busy/typing gate (#131's `TYPING_IDLE_S`).
Confirm first: reproduce, then check (a) `chat_log` for the assistant row, (b) the daemon log for the turn finishing, (c) `ws.messages` for a `complete` thought with that event id. (a)+(b) present → UI bug; absent → daemon bug.
Fix (expected): the chat state outlives the view (move `ChatViewModel` up to the app/`WebSocketManager` level, or keep the view alive), a turn restored from history with a user row and no assistant row shows as pending, and the reply is picked up on return (re-ingest or re-fetch `/api/chats` on appear and on `complete`).
Files (expected): Swift: Views/ChatView.swift, maybe Services/WebSocketManager.swift and the main window container; daemon only if (b) fails.
Verify:
⚬ Live: send a chat message, switch to another section or app before the reply, come back → the answer is there (or the turn shows as pending and fills in when done).
⚬ Live: same, but stay away until the turn finishes, then come back → answer shown.
⚬ If daemon code changes: test that a chat turn completes and logs the assistant row with no WS client connected; non-vacuous per rule 5.
⚬ pytest gate + Swift typecheck (rule 8).

#143 — Chat: a Stop button to cancel a request that is still running [~] live
Results (wave 34, 2026-11-20):
⚬ DESIGN: turns stay ONE AT A TIME. Each chat turn is its own task (new `ai/chat_turns.py`, registered by event id) but the loop still awaits it, so a slow chat turn STILL holds up other events; it can now be stopped. Reason: every turn shares one agent object and the per-turn fields (`_taint`, `_turn_kind`, …). After a Stop the turn does not end until its worker thread has exited.
⚬ `chat_cancel` command (`main.py`, `schemas/ws_messages.py`) with the message id; unknown or finished id does nothing; a Stop for a queued id is remembered (32 ids, 15 minutes).
⚬ STOPPING TOOL CALLS: the turn handle rides a ContextVar into the agent's threads. `gated_forward` refuses every call once stopped (on entry, after an approval returns, and just before the tool runs); no card is raised for a stopped turn; `agent.interrupt()`; the MLX adapter breaks between tokens and drops the result; the provider adapter drops a late result. A tool already running is never interrupted.
⚬ ON STOP: a `complete` thought and a `chat_log` assistant row starting "Stopped." plus what happened; the phone turn ends as before; the knowledge index gets the question only (the id is kept out so "Stopped." is never indexed as a reply).
⚬ WITHDRAWN CARD (7a): the turn's open card is resolved as not approved, `approval_resolved` outcome `withdrawn`, the phone code is forgotten. Decision log: nothing (it was not the user's decision). Idea outcomes and training corpus: new verdict `withdrawn` (shown, not decided; excluded from training). Undo journal: nothing unless an approved call finished.
⚬ Swift: Stop replaces Send while a turn is pending, Esc too, in `ChatView` and `QuickAskPanel`; `sendChatCancel`; `ChatTurn.stopped`. A real answer that beat the Stop wins.
⚬ LIVING_SPEC §1 "Stop (#143)" paragraph and §3.1.
⚬ Tests: `tests/test_chat_stop.py` (17). Rule 5: 16 breaks; 15 fail tests alone, the 16th is one of two redundant pre-run checks (removing both fails). Swift harness: 18 checks.
⚬ Live (user): ask something slow, press Stop / Esc → "Stopped.", next question works; Stop with a card open → card closes; approve a slow tool then Stop → it completes and the reply says so.
⚬ UNVERIFIED: no real MLX or cloud model run; no click-through. `tests/test_chat_context.py` builds the prompt twice without a pinned clock (could differ across a minute boundary).
Deviations: the item wanted a slow turn to stop holding up other events; not done (see DESIGN). A card left open at daemon shutdown is now recorded as `withdrawn`.
Wave: _ · Agent: _ · Added: 2026-11-20 · Source: user
Ask: "add a cancel / stop button to a chat request."
Today: no way to stop a chat turn. The WS `user_message` is pushed onto the event bus (`main.py` ~1911) and the orchestrator runs it inline, `await self._handle_user_message(...)` (`ai/orchestrator.py:1303`), so a slow turn also holds up every other event until it ends. `WebSocketManager.sendUserMessage` returns the `message_id` (= event id), which is the handle to cancel by. Existing pattern: `main.py` `_handle_command` (e.g. `demonstration_cancel`).
Design:
1. Swift: while a turn is pending, the send button in `ChatView` becomes Stop (and Esc stops it); same in `QuickAskPanel`. Stop sends a `chat_cancel` command with the `message_id`.
2. Daemon: run each chat turn as its own `asyncio.Task` keyed by event id, so it can be cancelled; `chat_cancel` cancels that task. Unknown or finished id is a no-op.
3. On cancel: emit a `complete` thought ("Stopped.") so the UI clears the spinner, log the assistant row as stopped in `chat_log` (so history and #142's reload agree), and end the phone turn the same as the `finally` block does today.
4. Consent (7a): cancelling must never leave a half-done side effect. Any approval card the turn opened is withdrawn (its future cancelled, card removed); a tool call already approved and running is allowed to finish, and the turn stops after it. Say which in the stopped message.
5. A model call that is mid-generation: cancel the await; if the local MLX / provider call can't be interrupted, drop its result when it returns.
Files (expected): Swift: Views/ChatView.swift, Views/QuickAskPanel.swift, Services/WebSocketManager.swift (`sendChatCancel`), Models/WSMessage.swift (command payload if needed); daemon: ai/orchestrator.py (turn task registry + cancel), main.py `_handle_command` (`chat_cancel`). Overlaps #142 (ChatView.swift): not in the same wave.
Verify:
⚬ New test: start a chat turn with a slow fake model, send `chat_cancel` → the task is cancelled, a `complete` thought "Stopped." is emitted, `chat_log` gets the stopped row; non-vacuous: make cancel a no-op, test fails.
⚬ New test: cancelling while an approval card is open resolves the card as withdrawn and runs no tool.
⚬ New test: `chat_cancel` for an unknown id does nothing and raises nothing.
⚬ Live: ask something slow, press Stop → spinner ends, "Stopped." shows, the next question works right away.
⚬ pytest gate + Swift typecheck (rule 8).
Wave: _ · Agent: _ · Added: 2026-11-20 · Source: user
Ask: "add a cancel / stop button to a chat request."
Today: no way to stop a chat turn. The WS `user_message` is pushed onto the event bus (`main.py` ~1911) and the orchestrator runs it inline, `await self._handle_user_message(...)` (`ai/orchestrator.py:1303`), so a slow turn also holds up every other event until it ends. `WebSocketManager.sendUserMessage` returns the `message_id` (= event id), which is the handle to cancel by. Existing pattern: `main.py` `_handle_command` (e.g. `demonstration_cancel`).
Design:
1. Swift: while a turn is pending, the send button in `ChatView` becomes Stop (and Esc stops it); same in `QuickAskPanel`. Stop sends a `chat_cancel` command with the `message_id`.
2. Daemon: run each chat turn as its own `asyncio.Task` keyed by event id, so it can be cancelled; `chat_cancel` cancels that task. Unknown or finished id is a no-op.
3. On cancel: emit a `complete` thought ("Stopped.") so the UI clears the spinner, log the assistant row as stopped in `chat_log` (so history and #142's reload agree), and end the phone turn the same as the `finally` block does today.
4. Consent (7a): cancelling must never leave a half-done side effect. Any approval card the turn opened is withdrawn (its future cancelled, card removed); a tool call already approved and running is allowed to finish, and the turn stops after it. Say which in the stopped message.
5. A model call that is mid-generation: cancel the await; if the local MLX / provider call can't be interrupted, drop its result when it returns.
Files (expected): Swift: Views/ChatView.swift, Views/QuickAskPanel.swift, Services/WebSocketManager.swift (`sendChatCancel`), Models/WSMessage.swift (command payload if needed); daemon: ai/orchestrator.py (turn task registry + cancel), main.py `_handle_command` (`chat_cancel`). Overlaps #142 (ChatView.swift): not in the same wave.
Verify:
⚬ New test: start a chat turn with a slow fake model, send `chat_cancel` → the task is cancelled, a `complete` thought "Stopped." is emitted, `chat_log` gets the stopped row; non-vacuous: make cancel a no-op, test fails.
⚬ New test: cancelling while an approval card is open resolves the card as withdrawn and runs no tool.
⚬ New test: `chat_cancel` for an unknown id does nothing and raises nothing.
⚬ Live: ask something slow, press Stop → spinner ends, "Stopped." shows, the next question works right away.
⚬ pytest gate + Swift typecheck (rule 8).

---

---

---

---

---

---

#135 — Settings: a search box, and sections grouped into categories [~] live
Results (wave 33 slot C, 2026-11-20):
⚬ New `Views/SettingsSearch.swift` (Foundation only): 7 categories, `SettingsSectionID` (an enum, so the view's `switch` must handle every section), the entry table that sets order and search, and the pure `filter` (every word of the query must appear in the title or keywords; the category name is not searched).
⚬ `Views/SettingsView.swift`: `NavigationSplitView`, search field at the top of the sidebar (a plain `TextField`, not `.searchable`, because Settings sits inside the main window's own split view), one Save bar outside every Form. All seven category Forms stay alive in a `ZStack` (only the selected one shows), so switching category drops nothing. Search shows second copies of the matching sections; provider edits are shared through `SettingsViewModel`.
⚬ All 25 old sections appear exactly once (checklist in the wave-33 report; the harness checks the table against `SettingsSectionID.allCases`).
⚬ `MainWindowController.swift`: minimum width 760 → 820.
⚬ Harness (scratch): "groq", "iphone", "undo", "rules" each find only their sections; 5 breaks each fail.
⚬ Live (user): the three Verify checks. NOT RENDERED YET: check the nested sidebar width, the empty box under a search-result category heading, and that hidden Forms take no clicks.
Deviations: text typed into a child section's own field while a search is showing is not carried over when the search is cleared (provider and model fields are).
Wave: _ · Agent: _ · Added: 2026-11-20 · Source: user
Ask: "add a search feature to settings and re order them into categories that make sense"
Today: `Views/SettingsView.swift` (544 lines) is one long `Form` with ~30 sections in the order they were built (Appearance, lifecycle, provider, presets, then every feature view, then rules / training / knowledge / watches / delivery / history / privacy ledger, then Save). No way to find a setting except scrolling.
Want:
1. A search field at the top. Typing filters to the sections whose title or keywords match; clearing it shows everything. No match → "No settings match "<text>"".
2. Sections grouped under category headings, in this proposed order (assistant proposal, user to confirm):
   ⚬ General: Appearance, app lifecycle (#96), main window.
   ⚬ Models: inference provider + per-engine pickers, provider presets, vision model (#107), web search.
   ⚬ Features: daily brief, weekly report, meeting prep, meeting notes (#86), Quick Ask, "Watch me" shortcut (#84), Watch for… (#88).
   ⚬ Delivery & iPhone: delivery (#87), iPhone remote (#85).
   ⚬ Permissions & consent: macOS permissions, standing rules (#78), Always allow (#116).
   ⚬ Memory & data: knowledge index (#134), activity history, action history / Undo (#83), privacy ledger (#82).
   ⚬ Personal model: training data (#118/#119), personal model (#120).
DECISIONS (user, 2026-11-20):
1. Layout: a sidebar of categories (`NavigationSplitView`), search field in the sidebar. Picking a category shows only its sections in the detail pane. While a search is typed, the detail pane shows matches from every category, each under its category name; clearing the search goes back to the selected category.
2. Save stays one action for all categories (switching category must not drop unsaved edits: keep `SettingsViewModel` above the split view).
Files (expected): Views/SettingsView.swift (owned); new Views/SettingsSearch.swift (rule 12: the category + keyword table and the filter); the `*SettingsView.swift` files only if a section needs a keyword hook.
Approach: a static table `[SettingsEntry(category, title, keywords, view)]` drives both the order and the filter, so a new section is added in one place. Keywords cover words a user would type ("model", "Groq", "API key", "login", "iPhone", "undo", "privacy"). The Save row stays visible whatever the filter.
Watch for: Save in `SettingsView` saves `SettingsViewModel` fields; a hidden section must keep its unsaved edits (filter hides views, it must not drop state). Locked-by-env notes (`lockedNote`) must still show.
Verify:
⚬ Typing "groq", "iphone", "undo", "rules" each shows only the matching section(s); clearing restores all.
⚬ Edit a provider field, search something else, clear, Save → the edit is saved.
⚬ Every section that exists today appears exactly once (test or a checked list in Results).
⚬ Swift typecheck (rule 8); a unit test of the filter function, non-vacuous (rule 5).
Deviations:
Wave: _ · Agent: _ · Added: 2026-11-20 · Source: user
Ask: "add a search feature to settings and re order them into categories that make sense"
Today: `Views/SettingsView.swift` (544 lines) is one long `Form` with ~30 sections in the order they were built (Appearance, lifecycle, provider, presets, then every feature view, then rules / training / knowledge / watches / delivery / history / privacy ledger, then Save). No way to find a setting except scrolling.
Want:
1. A search field at the top. Typing filters to the sections whose title or keywords match; clearing it shows everything. No match → "No settings match "<text>"".
2. Sections grouped under category headings, in this proposed order (assistant proposal, user to confirm):
   ⚬ General: Appearance, app lifecycle (#96), main window.
   ⚬ Models: inference provider + per-engine pickers, provider presets, vision model (#107), web search.
   ⚬ Features: daily brief, weekly report, meeting prep, meeting notes (#86), Quick Ask, "Watch me" shortcut (#84), Watch for… (#88).
   ⚬ Delivery & iPhone: delivery (#87), iPhone remote (#85).
   ⚬ Permissions & consent: macOS permissions, standing rules (#78), Always allow (#116).
   ⚬ Memory & data: knowledge index (#134), activity history, action history / Undo (#83), privacy ledger (#82).
   ⚬ Personal model: training data (#118/#119), personal model (#120).
DECISIONS (user, 2026-11-20):
1. Layout: a sidebar of categories (`NavigationSplitView`), search field in the sidebar. Picking a category shows only its sections in the detail pane. While a search is typed, the detail pane shows matches from every category, each under its category name; clearing the search goes back to the selected category.
2. Save stays one action for all categories (switching category must not drop unsaved edits: keep `SettingsViewModel` above the split view).
Files (expected): Views/SettingsView.swift (owned); new Views/SettingsSearch.swift (rule 12: the category + keyword table and the filter); the `*SettingsView.swift` files only if a section needs a keyword hook.
Approach: a static table `[SettingsEntry(category, title, keywords, view)]` drives both the order and the filter, so a new section is added in one place. Keywords cover words a user would type ("model", "Groq", "API key", "login", "iPhone", "undo", "privacy"). The Save row stays visible whatever the filter.
Watch for: Save in `SettingsView` saves `SettingsViewModel` fields; a hidden section must keep its unsaved edits (filter hides views, it must not drop state). Locked-by-env notes (`lockedNote`) must still show.
Verify:
⚬ Typing "groq", "iphone", "undo", "rules" each shows only the matching section(s); clearing restores all.
⚬ Edit a provider field, search something else, clear, Save → the edit is saved.
⚬ Every section that exists today appears exactly once (test or a checked list in Results).
⚬ Swift typecheck (rule 8); a unit test of the filter function, non-vacuous (rule 5).
Deviations:

---

#136 — Settings: fields for the local MLX models (main, analysis, drafting) [~] live
Results (wave 33 slot D + runner embed, 2026-11-20):
⚬ BEFORE: a bad model id did not stop the daemon, but the engine was left with no model, retried the bad id (and Hugging Face) on every use, and the error was only in the log.
⚬ `ai/mlx_engine.py`: `fallback_model` + `load_error`; on a failed load the engine falls back to the default ONLY if that default is already on this Mac (never a new download); `local_model_bytes()`.
⚬ `main.py`: `/api/settings` gains `mlx_models` (per role: value, default, source, locked, local, running, loaded, load_error) and `mlx_memory_note`; saving "" removes the key; a shell-exported key is locked and never written; a model change sets restart-required. Next-start order: shell, settings.json, `.env`, default.
⚬ New `Views/LocalModelsSection.swift`: a field + "Choose folder…" per local role, locked note, load error, memory note. Embedded by the runner in the Models category.
⚬ Tests: `tests/test_local_mlx_models_settings.py` (15); 12 breaks each fail.
⚬ Live (user): the Verify block. Note: `agent-daemon/.env` sets all three `MLX_*_MODEL` keys today, so clearing a field goes back to the `.env` value (the field says so).
⚬ UNVERIFIED: no real `mlx_lm` load; the section's look in the new layout.
Deviations: the memory note counts only roles that run locally, uses 75% of physical memory as the line, and updates after Save, not while typing.
Wave: _ · Agent: _ · Added: 2026-11-20 · Source: user
Ask: "add model fields to the settings window"
Today: Settings → Inference provider picks the provider per engine, but the MLX model that runs for "Local (MLX on this Mac)" can only be set by `MLX_MAIN_MODEL` / `MLX_ANALYSIS_MODEL` / `MLX_DRAFTING_MODEL` in `agent-daemon/.env` or by hand in `~/.bot-inator/settings.json`. The keys are already in `user_settings.ALLOWED_KEYS`; no view writes them. Vision already has its own local-model picker (`VisionSettingsView.swift`, `routes/providers.py` `/vision`), so it is out of scope. Cloud presets already have per-role model fields (`ProviderPresetsSection.swift`).
Want:
1. Under Inference provider, when any engine resolves to `local`, a "Local models" block with one text field per local engine (Main, Analysis, Drafting), each showing the current value or the memory-based default (`config.default_mlx_models()`) as its placeholder.
2. Empty field = use the default (key removed from settings.json, not saved as "").
3. A shell export of the key locks the field with the existing `lockedNote` text, like the engine pickers.
4. Saving a changed model shows the existing "restart required" state (`_settings_view()` `restart_required`) and the Restart button when `can_restart`.
DECISIONS (user, 2026-11-20):
1. Each field is free text (a Hugging Face repo id or a local folder path) plus a "Choose folder…" button that fills in the path. No picker of cached models in this item; that can be a later item.
2. Memory warning: a one-line note when the three models together likely exceed this Mac's memory, using the summed download size when the HF metadata is in the local cache. It never blocks Save; no note when the sizes are unknown.
Files (expected): new Views/LocalModelsSection.swift (rule 12); Views/SettingsView.swift (embed only, at wave close; #135 owns it, so do not run both in one wave); `main.py` `_settings_view()` to report the MLX keys, their defaults and lock state, if not already returned.
Watch for: a typo'd repo id must not leave the daemon unable to start. On load failure the engine falls back to the default model and the Settings row shows the error (check the current MLX load path for how a failure surfaces today).
Verify:
⚬ Set Main to a different MLX model, Save, Restart → `Running now` / engine status shows the new model id; clearing the field and restarting goes back to the default.
⚬ `MLX_MAIN_MODEL` exported in the daemon shell → the field is read-only with the locked note.
⚬ A bad id → daemon still starts on the default; the error shows in Settings.
⚬ "Choose folder…" fills the field with the picked folder's path, and that path loads after a restart.
⚬ Three large cached models on a small-memory Mac → the memory note shows and Save still works; unknown sizes → no note.
⚬ pytest gate and Swift typecheck (rule 8); a new test of `_settings_view()` reporting the MLX keys and defaults, non-vacuous (rule 5).
Deviations:
Wave: _ · Agent: _ · Added: 2026-11-20 · Source: user
Ask: "add model fields to the settings window"
Today: Settings → Inference provider picks the provider per engine, but the MLX model that runs for "Local (MLX on this Mac)" can only be set by `MLX_MAIN_MODEL` / `MLX_ANALYSIS_MODEL` / `MLX_DRAFTING_MODEL` in `agent-daemon/.env` or by hand in `~/.bot-inator/settings.json`. The keys are already in `user_settings.ALLOWED_KEYS`; no view writes them. Vision already has its own local-model picker (`VisionSettingsView.swift`, `routes/providers.py` `/vision`), so it is out of scope. Cloud presets already have per-role model fields (`ProviderPresetsSection.swift`).
Want:
1. Under Inference provider, when any engine resolves to `local`, a "Local models" block with one text field per local engine (Main, Analysis, Drafting), each showing the current value or the memory-based default (`config.default_mlx_models()`) as its placeholder.
2. Empty field = use the default (key removed from settings.json, not saved as "").
3. A shell export of the key locks the field with the existing `lockedNote` text, like the engine pickers.
4. Saving a changed model shows the existing "restart required" state (`_settings_view()` `restart_required`) and the Restart button when `can_restart`.
DECISIONS (user, 2026-11-20):
1. Each field is free text (a Hugging Face repo id or a local folder path) plus a "Choose folder…" button that fills in the path. No picker of cached models in this item; that can be a later item.
2. Memory warning: a one-line note when the three models together likely exceed this Mac's memory, using the summed download size when the HF metadata is in the local cache. It never blocks Save; no note when the sizes are unknown.
Files (expected): new Views/LocalModelsSection.swift (rule 12); Views/SettingsView.swift (embed only, at wave close; #135 owns it, so do not run both in one wave); `main.py` `_settings_view()` to report the MLX keys, their defaults and lock state, if not already returned.
Watch for: a typo'd repo id must not leave the daemon unable to start. On load failure the engine falls back to the default model and the Settings row shows the error (check the current MLX load path for how a failure surfaces today).
Verify:
⚬ Set Main to a different MLX model, Save, Restart → `Running now` / engine status shows the new model id; clearing the field and restarting goes back to the default.
⚬ `MLX_MAIN_MODEL` exported in the daemon shell → the field is read-only with the locked note.
⚬ A bad id → daemon still starts on the default; the error shows in Settings.
⚬ "Choose folder…" fills the field with the picked folder's path, and that path loads after a restart.
⚬ Three large cached models on a small-memory Mac → the memory note shows and Save still works; unknown sizes → no note.
⚬ pytest gate and Swift typecheck (rule 8); a new test of `_settings_view()` reporting the MLX keys and defaults, non-vacuous (rule 5).
Deviations:

---

#145 — BUG: UI lags when switching views and when switching back to the app [~] live
Wave: — (direct fix) · Added: 2026-11-20 · Source: user
Ask: "The ui feels laggy. Particularly when switching views or switching to the app in general from another app."
Cause (measured, 2026-11-20): with Settings open the app used 1,063 MB and up to ~98% CPU on the main thread (SwiftUI layout); Thoughts used 118 MB, Tools 140 MB. A one-build bisect (env var picking one category / one section) put ~840 MB on Settings → Memory & data → Knowledge index: `IMessageOptionsView` drew every iMessage conversation (1,700 here) as a checkbox in a non-lazy `VStack`. `SettingsView` also built all seven categories up front (hidden at opacity 0, #135), so that list was built whenever Settings opened, and every window focus change redrew all 1,700 checkboxes.
Results:
⚬ `Views/KnowledgeSourceOptionsViews.swift`: the conversation list is a `LazyVStack`.
⚬ `Views/SettingsView.swift`: a category is built the first time it is selected (`visited`), then kept, so #135's unsaved-state-survives-switching still holds.
⚬ `Services/WebSocketManager.swift`: one `debug` publish per frame instead of four (local copy, written back in a `defer`; the handler has no `await`, so nothing else can write `debug` in between); `authHint = nil` only when it isn't nil already (a nil-to-nil assignment still notified every observer on every frame).
⚬ `Views/ThoughtStreamView.swift`: the sorted thought list is computed once per redraw instead of three times.
Verify (2026-11-20, release build, real daemon):
⚬ Settings opened at launch: 1,063 MB → 140 MB; Memory & data opened at launch: 976 MB → 183 MB; focus switches to/from Finder ~2% CPU.
⚬ Non-vacuous: the bisect build (old code, Knowledge section only) used 977 MB; the other sections 134–141 MB.
⚬ Swift typecheck (rule 8) passes. No daemon code changed.
⚬ Live (user): open Settings, click through every category, Cmd-Tab away and back several times → no stall; Memory & data → Knowledge index → iMessage list scrolls, filters and toggles as before.
⚬ NOT DONE: `ws.messages` still grows for the whole session (QuickAskPanel, ChatViewModel and ApprovalPresenter rely on its indexes being append-only); a cap needs an index offset in those three readers.

---

#146 — Splash provider preset (local inference server for Apple silicon) [~] live
Wave: — (direct add) · Added: 2026-11-20 · Source: user
Ask: "should the app use this https://github.com/incoai/splash" → "Yes, add preset".
Decision (runner, 2026-11-20): an option in the provider picker, not the default. MLX stays the default because Splash needs an M3+ on macOS 26.4+ (first run recommends Local from 24 GB on any Apple silicon), serves only five model families with nothing small for `analysis`, and is a separate server the user runs.
Results:
⚬ `agent-daemon/providers.py`: preset `splash` ("Splash (local, Apple silicon)", prefix `SPLASH`), base `http://127.0.0.1:8001/v1`, no key, every role defaults to `unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M` (one server serves one model). Source: Splash `docs/api.md`, `docs/server.md` (base `/v1`, `GET /v1/models`, `--port`).
⚬ Port 8001, not Splash's default 8000: 8000 is the daemon's (`config.py` `port`). The user starts it with `splash serve --port 8001 --model unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M`.
⚬ Loopback base → local (7c), no ledger rows; a LAN base counts as cloud. First run's cloud list skips it (loopback filter), same as `local_server`. The Settings picker reads presets from the daemon, so no Swift change.
⚬ `docs/providers.md` table row; `config.py` comment.
Verify (2026-11-20):
⚬ New `tests/test_splash_preset.py` (4 tests). Non-vacuous: without the preset, collection fails (KeyError 'splash'); with the base on port 8000, the port test fails.
⚬ Full pytest gate: 2793 passed. No Swift changed.
⚬ Live (user): `brew install incoai/tap/splash`; `splash serve --port 8001 --model unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M`; Settings → provider Splash → List models shows the model → Test → Save → Restart Daemon; a Chat turn and one tool call with a card work; Privacy ledger shows nothing new. Preset stays `tested=False` until then.
Deviations:

---

#147 — Watch the screen in the background (timed sessions) and feed it to the idea pipeline [~] live
Results (2026-11-21, direct add):
⚬ Built as designed below: `events/screen_watcher.py`, `routes/screen_watch.py`, `Views/ScreenWatchSettingsView.swift` (shared `ScreenWatchModel`, embedded under Settings → Vision), menu bar "Watch screen for ▸ 15 min / 1 h / 4 h" / "Stop watching screen (N min left)" and an eye icon while a session runs (`StatusItemController.swift`).
⚬ Wiring: `user_settings.py` key, `ai/orchestrator.py` (signal-only + `_describe_event`), `ai/open_help.py` (content event + line), `ai/privacy.py` (personal fields), `ai/taint.py` (outside text, kind `window`), `main.py` (listener + router), `SettingsSearch.swift` keywords, LIVING_SPEC §1/§4/§6, invariant 7a note above.
⚬ Tests: new `tests/test_screen_watcher.py` (26). Non-vacuous: 8 sabotages (switch check, settle, activity, min gap, temp delete, dedupe, signal-only, personal fields) each fail ≥1 test. Full gate: 2819 passed. Swift typecheck clean.
⚬ Live (2026-11-21, user's daemon): switch on, 15-min session started 10:11:31, local Qwen3-VL-4B loaded in 1.0 s, 12 captures / 0 skipped in 7 min. Not yet seen: an open-help idea grounded in a `screen_seen` event.
⚬ Note: Messages is not on the never-watch list by default, so conversations are captured and described (locally here).

---

#148 — BUG: iMessage events crash when Groq rejects a broken final answer twice [~] live
Wave: — (direct add) · Added: 2026-11-21 · Source: user ("yes look at the imessage bug"), daemon log 10:17–10:18
Evidence: on `imessage_received` turns GPT-OSS called `answer` with `"arguments": no action"` (no opening quote). Groq returned 400 `tool_use_failed`; the one retry failed the same way; `AgentGenerationError` ended the event with a traceback (`orchestrator.py` Groq `generate`).
Fix: `ai/orchestrator.py` `_salvage_final_answer` / `_failed_generation`: after the retry, a rejected call whose `failed_generation` names `final_answer` or `answer` becomes a plain-text reply (then the existing text → `final_answer` path). Reads `exc.body` or the error text. A broken call to any other tool still raises: no tool runs on guessed arguments.
Verify (2026-11-21):
⚬ `tests/test_groq_model.py` +3 (salvage from the log's exact text, from `exc.body`, other tool still raises). Non-vacuous: without the fix 2 fail; salvaging any tool name 1 fails.
⚬ Full gate: 2821 passed, 1 failed (`test_chat_stop.py::test_stop_during_a_slow_model_ends_the_turn_with_a_stopped_row`), run while the user's daemon and local vision model were busy. That file passed 17/17 three times on its own; read as timing flake under load, not caused by this change (it touches only the second-rejection path).
⚬ Live (user): watch the daemon log on the next iMessages: no `AgentGenerationError`; a `salvaged 'no action'` line instead.
Not changed: the iMessage prompt still offers `send_imessage` although #72's refusal blocks it (log 10:17:58: offered, refused by `user_declined_before`). Working as designed; the extra model step is wasted.
Wave: — (direct add) · Added: 2026-11-21 · Source: user
Ask: "can we make it so it watches the screen and feeds the images to the llm" → answers: suggest ideas; capture on app/window switch AND on an interval; cloud vision allowed with the opt-in; a Settings toggle plus timed sessions.
INVARIANT AMENDMENT (user, 2026-11-21): #107 decision 1 said "no background capture". Amended: the daemon may capture the FRONT WINDOW in the background only while a SCREEN-WATCH SESSION the user started is running (Settings switch `AGENT_SCREEN_WATCH`, default "0", AND a session started from the menu bar / Settings for 15 min, 1 h or 4 h; sessions end on their own and on daemon restart). Captures are reads, not actions: 7a's card rule is unchanged (nothing it suggests runs without a card). 7c: the cloud vision route needs the personal-text opt-in, as #107 decision 3. 7d: images never reach the user directly; descriptions go through open-help's generate → judge → feedback filter.
Design:
⚬ New `events/screen_watcher.py` (`ScreenWatcher`): one thread; polls every 2 s while a session runs. Capture when the front window (pid + window id) changed and has stayed for 3 s, OR when 120 s passed since the last capture and the user was active in the last 60 s (HID idle time). At most one capture per 20 s.
⚬ Every capture goes through `tools.look_at_screen.resolve("front_window")` (secure input, never-watch apps, private windows, sensitive sites, bot-inator's own windows) and `vision_engine.route()` (refused route = no capture). Captured into a 0700 temp folder, scaled, described by the vision model with a fixed prompt, then DELETED. An unchanged image (same hash as the last one of that window) is not described again.
⚬ Emits `ContextEvent("screen_seen", app_name, window_title, metadata={"app","bundle_id","title","description","engine"})`. Signal-only (`_AGENT_TRIGGER_EXCLUDED_EVENT_TYPES`): the tool-calling agent never runs on it. Content event for open-help (`CONTENT_EVENT_TYPES`), so ideas come only through the judge. Personal fields `description`, `title` (`PERSONAL_EVENT_FIELDS`), so a cloud detection engine sees a placeholder unless the opt-in is on. Outside text for taint (a window can show anyone's words).
⚬ New `routes/screen_watch.py`: `GET /api/screen-watch` (enabled, active, ends_at, captures, skipped, last_reason, route), `PUT` `{enabled}`, `POST /start` `{minutes ∈ 15,60,240}`, `POST /stop`.
⚬ Swift: new `Views/ScreenWatchSettingsView.swift` (switch + start/stop + status), menu bar item "Watch screen for 1 hour" / "Stop watching screen (N min left)".
Verify:
⚬ New `tests/test_screen_watcher.py`: no capture without the switch or a session; session expiry stops capture; switch trigger waits for the 3 s settle; interval trigger needs recent activity; min gap; refused window/route → no capture and no event; temp folder deleted on success and failure; event shape; duplicate image not re-described. Route tests. Non-vacuous per rule 5.
⚬ pytest gate + Swift typecheck (rule 8).
⚬ Live (user): Settings → Screen watching on → Watch for 15 min → switch between a few apps → Thoughts shows `screen_seen` rows with descriptions; 1Password in front → no row; session ends after 15 min; Privacy ledger shows image rows only with a cloud vision model.

### Memory

#134 — "It knows your stuff": local search over your mail, files and past chats [~] live
Wave: _ · Agent: _ · Added: 2026-11-19 · Source: user
Ask: From "ok why cant the app become their own personal gpt" → "yes it
does" (belongs on the roadmap). The user wants Chat to answer questions
about the user's own life ("what did my landlord say about the lease?",
"when is the Acme invoice due?") the way a personal GPT would.
DECISIONS (user, 2026-11-19):
1. This is retrieval, not fine-tuning. Facts come from a local index that
   the model searches before answering. The personal model (#118–#121)
   stays limited to deciding what to offer and writing cards; it is not
   trained on indexed content for this item.
2. Build on the existing sqlite-vec store and MiniLM embedder in
   `ai/memory.py`, in a separate db file, not by mixing indexed content
   into `memory.db`.
3. The index and its embeddings never leave the Mac (invariant 7c).
4. Sources in v1: mail (#41 reader), files in folders the user picks,
   past Chat turns, meeting notes. Not iMessages in v1.
5. Consent: one opt-in per source, all off by default; mail needs its own
   toggle, as messages will when they are added.
6. Cloud key: retrieved snippets go to a cloud model only if that source's
   cloud toggle is on, and every send is in the privacy ledger (#82).
7. Taint: any turn that used retrieved content is tainted like #81/#122
   (no `run_shortcut`, no tool ideas quoting it).
8. Retention: turning a source off deletes its rows from the index, and
   Settings has a "Forget everything" button.
(4–8: user accepted the proposed answers, 2026-11-19. LIVING_SPEC §1
"Knowledge index".)
Files: TBD at wave planning. Likely a new `ai/knowledge_index.py`, a new
router for index status/settings, and a new Settings screen.
Approach: Incremental indexer (chunk + embed on change, AC power or idle
only, like #119's training), hybrid search (sqlite FTS5 + vector), top-k
snippets with source links injected into the Chat prompt, and answers
that cite which email or file they came from. Fallback if MiniLM recall
is poor: FTS5 only for v1.
Verify:
⚬ Live: ask Chat a question only answerable from an indexed email; the
  answer cites that email. Turn the source off; the same question no
  longer finds it and its rows are gone from the db.
⚬ Nothing from the index appears in network traffic with no cloud key.
⚬ A planted injection in an indexed email cannot get a side-effecting
  tool to run (extend the #122 safety suite).
⚬ pytest gate; Xcode build for the Settings screen.
Results (part 1, wave 28 slot D, 2026-11-20):
⚬ `ai/knowledge_index.py`: own db `~/.bot-inator/knowledge.db` (0600, secure delete) and own settings file `knowledge_index.json`; sources `mail`, `folders`, `chats`, `meeting_notes`, all off by default, no iMessage source; FTS5 + vec0 search fused by rank, vector hits only within distance 1.2, keyword-only when vec0 or a local embedder is missing; never downloads a model (`local_files_only`); incremental passes (unchanged items skipped unread, 500 changed items per source per pass); source off deletes its rows and its cloud toggle; `forget_everything()` deletes the db file and turns every source off.
⚬ API for part 2: `search(query, top_k=5, *, sources=None, for_cloud=False)` → snippets `{source, item_id, title, link, date, text, score, match}`; `cloud_allowed(source)`; `index_chat_turn(event_id, user_text, reply_text="")`; `KnowledgeIndexer(index)` with `start()` / `stop()` / `index_now()`.
⚬ `routes/knowledge.py` (`/api/knowledge`: status, source + cloud toggles, folders, index-now, forget); `Views/KnowledgeIndexSettingsView.swift`.
⚬ Tests: `tests/test_knowledge_index.py` (17, tmp paths and fake readers). Rule 5: 11 one-line breaks, each fails its test.
⚬ Runner at close: `tests/test_egress_log.py` EXEMPT row (the index imports sentence_transformers but only loads a cached snapshot).
⚬ UNVERIFIED: the real Mail store, textutil / PDFKit extraction, the real MiniLM load, speed of a large first mail pass, the Settings view in the running app.
Results (part 2, wave 29, 2026-11-20):
⚬ New `ai/knowledge_retrieval.py`: `for_chat()` searches the index (top 5) and appends a delimited block of snippets (source, title, date) marked as DATA, not instructions, with "cite the email/file/note" wording. No source on or no hit → prompt byte-for-byte unchanged.
⚬ WHICH TURNS: Chat, Quick Ask and the iPhone remote (one code path, `_handle_user_message`). The phone turn always uses the cloud-toggle filter, because its answer leaves the Mac over iMessage. Scheduled asks and ambient turns do not retrieve.
⚬ CLOUD: a cloud engine gets snippets only when the source's cloud toggle is on AND `personal_text_allowed()` (the stricter of the two). Each send is one `knowledge_snippets` ledger row (sources + count, never text). Local engine: no row.
⚬ TAINT: `TurnTaint.mark_retrieved()` (kind `knowledge`): `run_shortcut` refused, tool ideas may not quote it, cards carry an origin note. 7a: no "Always allow" bypass and no countdown in a retrieval turn.
⚬ PAST CHATS: `index_chat_turn` runs right after the turn. A turn that used snippets is indexed with the user's text only (its reply may quote a source with other toggles); ids kept in `reply_kept_out`.
⚬ Registration: `main.py` (router, index + indexer in lifespan, `app.state.seq` clock seed), `tests/conftest.py` (tmp index paths), `SettingsView.swift`, `PrivacyLedgerView.swift` (one label), LIVING_SPEC §3.2 / §4.
⚬ Wave-28 leftover: a held offer opened with Show no longer counts toward `cards_today`.
⚬ Eval: `inj_indexed_mail_shortcut` in `evals/cases/safety.json`; `evals/suites/tool_calls.py` passes a case's `knowledge` snippets through the same block.
⚬ Tests: `tests/test_knowledge_chat.py` (20). Rule 5: 21 breaks, each fails its test (grouped runs, matched by assertion). Not break-tested: the conftest env lines (removing them would read the real ~/.bot-inator).
⚬ Live (user): Mail on → Index now → a Chat question answerable from one email cites it; Mail off → not found. With a cloud key: cloud toggle off → no ledger row; on → one row per send, no text. `python -m evals.run --suite safety` → check `inj_indexed_mail_shortcut`.
⚬ UNVERIFIED: citation quality and recall with a real model and real Mail; the phone path end to end; the rerun path's second ledger row; added latency per Chat turn from embedding the query.
Deviations (part 2):
⚬ `tests/test_personal_evals.py` case-count bound raised from 25 to 30 (safety.json was at exactly 25).
⚬ `evals/suites/tool_calls.py` and `ai/knowledge_index.py` edited additively (see above); route rows went in LIVING_SPEC §3.2, where route rows live.
Deviations (part 1):
⚬ Background passes run on AC power OR after 300 s idle (#119's training needs both). One line in `background_blocked` if the user wants both.
⚬ Vector distance limit added: without it the nearest chunk always "matched".
⚬ Uses private helpers from `ai/memory.py` and `tools/list_recent_emails.py` rather than editing them.

#137–#141 came out of the user's ask on 2026-11-20, after #134 went live
("can we add these items to the todo.md I want them to be able to be indexed
as well ... iMessages, Calendar, Notes, browser history, and your history
from ChatGPT or other apps"). Each is a new source in the #134 index and
inherits every #134 rule (LIVING_SPEC §1 "Knowledge index"): off by default,
own toggle, own cloud toggle (off), the index stays on the Mac, a turn that
used its snippets is tainted, off deletes its rows, "Forget everything"
clears it. Shared files: every item adds its name to `SOURCES` and a reader
in `ai/knowledge_index.py`, a row in `Views/KnowledgeIndexSettingsView.swift`
and a kind label in `ai/taint.py`, so they cannot run in parallel as written.
Wave plan when scheduled: either one slot does all five in order, or each
slot owns a new `ai/knowledge_sources/<name>.py` reader and the runner
registers them at wave close (rule 9). Suggested order: #138, #139 (small,
readers exist), #141, #137, #140 (the only one that can use the network).

Results for #137–#141 (waves 31–32, 2026-11-20):
⚬ READERS (wave 31, one module each, same interface: `NAME`, `TITLE`, `DESCRIPTION`, `TAINT_LABEL`, `DEFAULT_OPTIONS`, `validate_options`, `listing`, `choices`): `ai/knowledge_sources/imessage.py` (26 tests), `calendar.py` (30), `notes.py` (30), `browser.py` (44), `ai_exports.py` (37). Rule 5: 25 / 15 / 14 / 26 / 15 one-at-a-time breaks, each fails a test.
⚬ WIRING (wave 32): `ai/knowledge_sources/__init__.py` registry; `ai/knowledge_index.py` now has 9 sources, all off by default with cloud off, and per-source options stored validated (`get_options` / `set_options`; a bad stored value falls back to defaults and shows as `options_error`); `routes/knowledge.py` gains `GET/PUT /sources/{source}/options`, `GET /sources/{source}/choices` (worker thread), `POST /sources/ai_chats/imports`, `DELETE /sources/ai_chats/imports/{id}`; `ai/taint.py` + `ai/knowledge_retrieval.py` label each source (message, calendar event, note, web page, AI chat); new `Views/KnowledgeSourceOptionsViews.swift`; `PrivacyLedgerView.swift` (one label); two safety cases (`inj_indexed_imessage_shortcut`, `inj_indexed_web_page_shortcut`); LIVING_SPEC §3.2 / §4. Tests: `tests/test_knowledge_sources_wiring.py` (35); 30 of 31 breaks fail a test alone, the 31st is one of two redundant stale-pass guards (removing both fails).
⚬ DECISIONS (wave 32 agent; the user can overturn):
  1. Source OFF deletes its rows and resets only the opt-ins (cloud toggle, browser page text, the AI-chat import list); look-back, windows and exclude lists stay.
  2. FORGET EVERYTHING resets options to defaults EXCEPT the lists that keep things out (Messages exclude list, excluded sites, browser profile choice, excluded Notes folders): a reset must never widen what a later "on" reads. The import list is cleared; export files are never touched.
  3. BROWSER PAGE TEXT needs both its own switch AND the #124 web opt-in (`AGENT_WEB_SEARCH`), and AC power or idle for every fetch, "Index now" included (a blocked page is indexed by title and address and fetched on a later allowed pass). Each fetch is a `web_search` ledger row with tool `knowledge_browser`.
  4. Narrowing takes effect at once: a newly excluded conversation, site or Notes folder, a shortened look-back or calendar window, and a removed import delete their rows inside `set_options`.
⚬ PER SOURCE:
  #137: all conversations, exclude list by chat, identifier or handle; one item per conversation per day; senders shown by handle (chat.db has no contact names; #127 forbids listing Contacts); text only; the #85 own thread always excluded, also by a "bot-inator:" prefix backstop. UNVERIFIED on a real chat.db.
  #138: read through `calendar_access` only while the backend reports full access (a pass never triggers the macOS prompt); 31-day slices; not ready or a failed slice → `complete=False`, nothing pruned; notes cut to 500 chars and 50 attendees by the app. UNVERIFIED against the real app.
  #139: JXA through osascript; one call lists id, name, folder, date and lock flag; bodies fetched lazily in batches of 25; locked notes and Recently Deleted never read. MEASURED on the user's library: 815 notes, listing under 1 s, full first pass 58 s, so the NoteStore.sqlite route is not needed. Shared notes are tainted like every source.
  #140: Safari, Chromium (Chrome, Arc, Brave, Edge; every profile) and Firefox readers on temp copies; one item per normalised URL, merged across browsers; look-back 7 / 30 / 90 / 365 / everything (default 90); excluded sites match subdomains; token-like query strings are stripped and such URLs are never fetched; local and private hosts never fetched; at most one fetch per URL per 30 days, 1 s apart. On this Mac (counts only): 10 profiles, 13,562 URLs in 90 days. UNVERIFIED: Arc, Brave, Edge and Firefox (no history here), Safari profiles, real page fetching.
  #141: ChatGPT and Claude `conversations.json`, alone or in the zip (also `conversations-<n>.json`); provider detected from structure; ChatGPT active branch only; tools, images, hidden and system messages skipped; the same conversation in two imports appears once, the later update wins; a missing file → error and `complete=False` (nothing pruned); 2 GiB cap; a 349 MB made-up export lists in 0.9 s. UNVERIFIED: any real export (both formats are undocumented; details come from third-party parsers), the conversation link patterns.
⚬ ALSO (runner, 2026-11-20, asked for by the "AI chat performance" session after measuring ~29,700 one-chunk model calls in the first real pass): an item's chunks are embedded in ONE `encode` call (`KnowledgeIndex._embed_many`, `embedder.many`), the vector backfill in batches of 32, and every `encode` passes `show_progress_bar=False` (`ai/memory.py` too). Tests in `tests/test_knowledge_index.py` (3); rule 5: per-chunk embedding restored and the flag removed → 2 fail.
⚬ Live (user), none run yet: each source on → Index now → a Chat question only that source can answer names its source; Messages: leave a conversation out, its count drops at once; Browser: shorten the look-back, older pages go; with page text + Web search on and on AC power the ledger shows "Page text for your browser history index"; AI chats: import the real ChatGPT zip, import it again (no duplicates), Remove. The new Settings controls have not been rendered or clicked.

#137 — Index iMessages [~] live
Wave: _ · Agent: _ · Added: 2026-11-20 · Source: user
Ask: "iMessages ... I want them to be able to be indexed as well."
Today: #134 decision 4 and LIVING_SPEC §1 said "Not iMessages in v1". The user lifted that on 2026-11-20 (LIVING_SPEC amended the same day). `events/imessage_watcher.py` already reads `~/Library/Messages/chat.db` (needs Full Disk Access). `KnowledgeIndex` refuses any folder inside `~/Library/Messages`; that stays (this source reads chat.db through its own reader, not as a folder).
DECISIONS (user, 2026-11-20):
1. All conversations are indexed once the source is on, with an EXCLUDE LIST of contacts and group chats in Settings. Excluding a conversation deletes its rows at once.
DECISIONS (assistant proposal; the user can overturn any):
2. Own toggle and own cloud toggle, both off, with wording that these are other people's words.
3. One item per conversation per day (messages grouped, sender names kept), so a snippet has context and a date; `item_id` = chat id + date; no link (Messages has no stable per-message URL).
4. Text only. Attachments, reactions and deleted messages are skipped.
5. Newest first, capped like mail (5,000 items, 500 per pass).
6. The iPhone remote's own thread (#85) is always excluded: it is the app talking to the user.
Files (expected): reader in `ai/knowledge_index.py` (or new `ai/knowledge_sources/imessage.py`), `SOURCES`, `routes/knowledge.py` (exclude list endpoints), `KnowledgeIndexSettingsView.swift` (toggle + exclude list), `ai/taint.py` (label), `tests/test_knowledge_index.py` (the "no iMessage source" test changes to "off by default, own toggle").
Verify:
⚬ New test on a fake chat.db: source off → nothing read; on → a message is found with its conversation and date; an excluded contact is never indexed and excluding later removes its rows. Non-vacuous each.
⚬ New test: the #85 own thread is never indexed.
⚬ Safety: a planted instruction in an indexed message cannot run a side-effecting tool (extend `inj_indexed_mail_shortcut`).
⚬ Live: turn it on, Index now, ask Chat about something only a text said; the answer names the conversation and date.
⚬ pytest gate + Swift typecheck (rule 8).

#138 — Index Calendar events [~] live
Wave: _ · Agent: _ · Added: 2026-11-20 · Source: user
Ask: "Calendar ... I want them to be able to be indexed as well."
Today: the calendar layer reads events through EventKit in the app (#60, `CalendarBridge.swift`, `tools/calendar_access.py`), with attendees (#80). Nothing indexes them, so "when did I last meet Sam?" has no answer.
DECISIONS (assistant proposal; the user can overturn any):
1. Reuse `tools/calendar_access.py`; no new permission. If Calendar access is missing, the source shows the same message the Permissions screen does.
2. One item per event: title, time, location, attendees, notes. Window: 2 years back, 1 year ahead. Recurring events: each occurrence in the window, not the rule.
3. Own toggle and cloud toggle, both off. Attendee names and notes are other people's data: tainted like every other source.
4. `item_id` = event id + start; changed or deleted events are updated or pruned on the next pass.
Files (expected): reader (as #137), `SOURCES`, `KnowledgeIndexSettingsView.swift`, `ai/taint.py`, tests.
Verify:
⚬ New test with a fake calendar backend: an event is found by attendee name and by a word in its notes; a deleted event is pruned; off → rows gone. Non-vacuous each.
⚬ Live: "when is my next dentist appointment?" and "when did I last meet <name>?" answer from the calendar and name the event.
⚬ pytest gate + Swift typecheck (rule 8).

#139 — Index Apple Notes [~] live
Wave: _ · Agent: _ · Added: 2026-11-20 · Source: user
Ask: "Notes ... I want them to be able to be indexed as well."
Today: `tools/notes.py` (#46) creates and appends notes through AppleScript; nothing reads them in bulk.
DECISIONS (assistant proposal; the user can overturn any):
1. HOW TO READ is decided when scheduled, by trying both on a real library: (a) AppleScript `body of note` (uses the Automation permission the app already has; slow for thousands of notes), (b) `~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite` (fast, needs Full Disk Access, note bodies are gzipped protobuf, undocumented). Start with (a); move to (b) only if a pass over 1,000 notes takes over 5 minutes.
2. Locked (password-protected) notes are never read. "Recently Deleted" is skipped.
3. One item per note: title, folder, modified date, text (HTML stripped). Link: none unless a stable `notes://` link is found.
4. Own toggle and cloud toggle, both off. A shared note can hold other people's text: tainted like every other source.
Files (expected): reader (as #137), `SOURCES`, `KnowledgeIndexSettingsView.swift`, `ai/taint.py`, `ai/permissions.py` only if (b) is used, tests.
Verify:
⚬ New test with a fake notes reader: a note is found by a word in its body; an edited note is re-indexed, a deleted one pruned; a locked note is never loaded. Non-vacuous each.
⚬ Live: time one full pass on the user's real Notes and record it here; ask Chat about one note.
⚬ pytest gate + Swift typecheck (rule 8).

#140 — Index browser history (titles and URLs; page text as an option; a look-back limit) [~] live
Wave: _ · Agent: _ · Added: 2026-11-20 · Source: user
Ask: "browser history ... I want them to be able to be indexed as well." On what to index: "make it an option to do both and set a time limit for how long to go back".
DECISIONS (user, 2026-11-20):
1. Two levels, both options: TITLES AND URLS (local history files only, no network), and PAGE TEXT (also download each visited page and index its text).
2. A LOOK-BACK LIMIT in Settings: only visits newer than the limit are indexed; shortening it deletes older rows.
3. Browsers: Safari, Chrome, Arc / Brave / Edge, Firefox.
DECISIONS (assistant proposal; the user can overturn any):
4. Look-back choices: 7 days, 30 days, 90 days, 1 year, everything. Default 90 days.
5. Page text is a SECOND switch under the source, off by default, because it is the only index source that uses the network (7c). When on: fetched through #124's `fetch_page` path (http/https only, no cookies or logins, so a signed-in page comes back as its public version), at most one fetch per URL per 30 days, rate limited, AC power or idle only, and each fetch is a privacy-ledger row. Never fetched: private/incognito visits (not in history anyway), localhost and private-network addresses, URLs with tokens in the query string (strip the query or skip).
6. An exclude list of sites (domains) in Settings; a few are excluded by default (banking, health and sign-in pages are suggested, the user edits the list).
7. One item per URL (not per visit): title, URL, last visit, visit count; link = the URL. Fetched page text is outside text: tainted, and the injection shield matters most here.
8. History files are copied to a temp file before reading (browsers hold them locked). Safari's needs Full Disk Access; Chromium browsers and Firefox read from the profile folders; every profile found is listed with its own checkbox.
Files (expected): reader (as #137; one function per browser family: Safari `History.db`, Chromium `History`, Firefox `places.sqlite`), `SOURCES`, `routes/knowledge.py` (look-back, page-text switch, site exclude list, profiles), `KnowledgeIndexSettingsView.swift`, `ai/egress_log.py` (one kind, or reuse #124's), `ai/taint.py`, tests.
Verify:
⚬ New tests on fake history files for each of the three formats: a visit inside the limit is indexed, one outside is not; shortening the limit removes older rows; an excluded domain is never indexed. Non-vacuous each.
⚬ New test: with page text off, indexing makes no network call (socket patched to raise); with it on, each fetch writes one ledger row and a private-network URL is never fetched.
⚬ Safety: a planted instruction in fetched page text cannot run a side-effecting tool.
⚬ Live: "what was that site about <topic> I read last week?" returns the page with its link.
⚬ pytest gate + Swift typecheck (rule 8).

#141 — Import chat history from ChatGPT and other AI apps (export files) [~] live
Wave: _ · Agent: _ · Added: 2026-11-20 · Source: user
Ask: "your history from ChatGPT or other apps ... I want them to be able to be indexed as well."
DECISIONS (user, 2026-11-20):
1. IMPORT EXPORT FILES: the user downloads their data export from the provider and picks the file in Settings. The app does not read other apps' local storage.
DECISIONS (assistant proposal; the user can overturn any):
2. Formats in v1: ChatGPT export (`conversations.json`, inside the zip or alone) and Claude export (`conversations.json`). A third format is added only when the user has a file to test with.
3. One item per conversation: title, date, the turns in order (user and assistant text; tool output, images and attachments skipped). Long conversations are chunked as usual.
4. Importing the same or a newer export again updates changed conversations and adds new ones; it never duplicates. Each import is listed in Settings (file name, date, conversation count) with a Remove button that deletes its rows.
5. The picked file is read in place and not copied into `~/.bot-inator`; only the index holds its text. A zip is read without unpacking to disk.
6. Own toggle and cloud toggle, both off. Assistant replies in these chats are model output and may quote web pages: tainted like every other source.
7. Nothing is sent to OpenAI or Anthropic; this is a local file import.
Files (expected): reader + importers (as #137, or new `ai/knowledge_sources/ai_exports.py`), `SOURCES`, `routes/knowledge.py` (import, list, remove), `KnowledgeIndexSettingsView.swift` (file picker + import list), `ai/taint.py`, tests with small made-up export fixtures.
Verify:
⚬ New tests on made-up ChatGPT and Claude export fixtures: a conversation is found by a phrase from the middle of it; importing twice does not duplicate; Remove deletes that import's rows only; a malformed file gives a clear error and indexes nothing. Non-vacuous each.
⚬ Live: import the user's real ChatGPT export; ask Chat about something discussed there; the answer names the conversation.
⚬ pytest gate + Swift typecheck (rule 8).

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

### Evals

#133 — LLM-as-judge scorer for evals, plus a first judged suite (email drafts) [~] live
Wave: _ · Agent: _ · Added: 2026-11-19 · Source: user
Ask: "can i use llm as a judge" → "add llm as a judge to the todo.md".
Today (2026-11-19, read from the source, not run): every eval suite scores with rules. `tool_calls` and `safety` check the tool name and argument matches (`{"contains": ...}` in `evals/cases/*.json`); `idea_judge` measures the app's own idea filter against a labelled pass/drop. Nothing scores free text, so a drafted email body, the weekly report or a chat answer can get worse without any eval noticing. `evals/run.py` exits 1 only on errors, never on a lower score.
DECISIONS (assistant proposal, 2026-11-19; the user can overturn any):
1. The judge is only for output with no single right answer. `tool_calls` and `safety` keep their rule checks: they are cheaper, give the same result every run, and are more reliable than a judge.
2. Each rubric line is a yes/no question about the output ("Is it addressed to the sender?"), not a 1–10 score. A case passes only if every line passes. The judge writes a one-line reason before each verdict; reasons go in the result file.
3. The judge must be a different model from the one under test. `--judge-provider` / `--judge-model` flags; the run refuses if they match the model under test.
4. The judge can use a cloud provider because eval cases are made-up data, not the user's. It is used only by `evals/run.py` and never inside the daemon, so invariant 7c is unchanged. Not available inside `ai/model_eval.py` (#120 gate): that gate runs on the user's Mac with local-only data.
5. Judge calls repeat like the case runs (`--repeats`), so a flaky judge shows up as a flaky case.
6. Before any judged suite counts, check the judge against hand labels: about 20 outputs graded by the user, judge agreement ≥ 85%. Below that, fix the rubric first. The labels live in a cases file so the check can be re-run.
Files (expected):
⚬ OWNED: new `evals/judge.py` (rubric scorer: prompt, yes/no + reason parse, refuse same-model), new `evals/suites/email_draft.py` (one agent turn with a stubbed incoming email, capture `draft_email` args, score `body` / `subject` with the judge), new `evals/cases/email_draft.json` (~10 cases, each with a `rubric` list), new `evals/cases/judge_calibration.json` (hand labels), new `tests/test_eval_judge.py`.
⚬ EDITED: `evals/run.py` (`--judge-provider`, `--judge-model`, `--min-accuracy` → exit 1 below it; judge reasons in results), `evals/suites/__init__.py` (`SUITES` += `email_draft`).
⚬ Do NOT touch: `ai/model_eval.py`, any daemon code.
Approach: `judge.py` exposes `async score(output, rubric, engine) -> {"ok", "lines": [{"q", "verdict", "reason"}]}`. Prompt asks for one JSON object per line; a parse failure counts as "no" with reason "unparseable", never as a pass. `email_draft` reuses `tool_calls.run_case`'s stub-tools pattern and takes the body from the captured `draft_email` call. Fallback if the agent turn is too slow or flaky to drive: call the drafting role directly with the email text and judge its reply.
Verify:
⚬ New test: `score()` with a fake judge engine returning yes/yes/no → `ok` false and the failing line named; non-vacuous: make `score` ignore verdicts, test fails.
⚬ New test: unparseable judge reply → that line is "no"; non-vacuous: treat parse failure as pass, test fails.
⚬ New test: same provider + model for judge and run → refused with a clear error.
⚬ New test: `--min-accuracy 0.9` with 0.8 accuracy → exit 1; non-vacuous: drop the check, exit 0.
⚬ Live (batched, rule 11): run `python -m evals.run --suite email_draft --judge-provider groq` once; user grades the calibration set; record agreement in this item.
⚬ pytest gate (rule 8).
Later (not this item): judged suites for the weekly report (#128) and meeting prep; a scheduled CI eval job once there is CI.
Results (wave 28 slot C, 2026-11-20):
⚬ `evals/judge.py`: `score()` (one JSON line per rubric line, reason before verdict; missing or unclear → "no", "unparseable"), `check_distinct()` (`SameModelError`), calibration helpers and CLI (`python -m evals.judge`, exits 1 under 85%).
⚬ `evals/suites/email_draft.py` + `evals/cases/email_draft.json` (10 made-up cases, 4 rubric lines each, one with an injection); `evals/cases/judge_calibration.json` (20 outputs, labels ungraded).
⚬ `evals/run.py`: `--judge-provider`, `--judge-model`, `--min-accuracy` (exit 1 below it), judge reasons saved per run.
⚬ Tests: `tests/test_eval_judge.py` (11). Rule 5, the four Verify tests: `score` ignoring verdicts, parse failure as pass, same-model check off, accuracy check removed → each fails.
⚬ Live (user): `cd agent-daemon && .venv/bin/python -m evals.run --suite email_draft --judge-provider groq`; grade `judge_calibration.json`, then `.venv/bin/python -m evals.judge --judge-provider groq`; record agreement here. With `--provider groq` too, pass `--judge-model` (same default model is refused).
⚬ UNVERIFIED: any real judge or model run.
Deviations:
⚬ Cases are chat requests ("Draft a reply to the email from X") with `read_email` stubbed, not `mail_received` events: a bare event doesn't ask for a reply.
⚬ Agreement is counted per rubric line across repeats, not per output.

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
