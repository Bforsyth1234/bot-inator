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
anything else red blocks the item.
============================================================ -->

Next ID: #41
Legend: `[ ]` todo · `[~]` in progress · `[x]` done (move to DONE.md)

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
2026-11-09 live run of the open-help source. None is scheduled into a
wave yet.

### Idea quality (Phase 5)

#32 — Persist idea-source state across restarts [ ] [Q20]
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q20, Phase 5.2 "Open")
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
Wave: — · Agent: _ · Added: 2026-11-09 · Source: assistant (live run)
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
Wave: — · Agent: user · Added: 2026-11-09 · Source: assistant (live run)
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
Wave: — · Agent: _ · Added: 2026-11-09 · Source: assistant (live run)
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
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (Phase 5.2 table, row 3)
Ask: One-time scan: installed apps, `~/.zsh_history`, Downloads clutter,
recurring calendar events. Example: the same long `docker` command run
40× → one-click tool.
Verify:
Deviations:

---

#34 — Idea source: friction [ ]
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (Phase 5.2 table, row 4)
Ask: Copy in app A → paste in app B; rapid ping-pong between two apps.
Example: Mail → Numbers copy/paste ×5 → "pull invoice totals into my
sheet". Needs a clipboard listener.
Verify:
Deviations:

---

#35 — Idea source: starter catalog [ ]
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (Phase 5.2 table, row 5)
Ask: Curated, known-good tool ideas matched to the user's apps. Example:
uses Mail + Preview → "auto-file PDF attachments". Adding a kind =
adding a `FOLLOW_UPS` entry and a classifier rule in `ai/idea_sources.py`
(see DONE.md #12).
Verify:
Deviations:

---

#25 — Pattern detection cost: adaptive cadence [ ] [Q12]
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q12)
Ask: Every 10 events we hit the detection engine with a 50-event prompt.
Phase 5.0's repeat gate skips the model call unless an event signature
repeats 3×, which removes most calls. Detection now defaults to the 8B
main engine, so each call that does run costs more. Adaptive cadence is
still open.
Verify:
Deviations:

### Generated tools

#28 — Quarantine a generated tool that fails to load [ ] [Q15]
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q15)
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
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q9)
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
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q10)
Ask: Uninstall exists (ToolManagerView + `DELETE /api/tools/<name>`). Add
a *disable without deleting* toggle so the user can suspend a suspect
tool without losing the source, in case they want to tweak and
re-install it.
Verify:
Deviations:

---

#24 — Name collisions between generated tools [ ] [Q11]
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q11)
Ask: A generated tool whose name collides with a built-in is rejected.
What about two generated tools across daemon restarts (e.g. the agent
picks `summarize_pdf` twice with different logic)? Current plan: reject
the second with a surfaced error; alternative is to version the file
(`summarize_pdf.v2.py`) and let both coexist.
Verify:
Deviations:

---

#29 — Tool manager: refresh on a tools_changed broadcast [ ] [Q16]
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q16)
Ask: The view fetches once on appear and on manual refresh. Should it
also listen for a new `tools_changed` WS broadcast emitted by the
orchestrator after `load_dynamic_tools` / `unload_dynamic_tool`, so
multiple open windows stay consistent without polling?
Verify:
Deviations:

---

#30 — Nested-repo first-use regression test [ ] [Q17]
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q17)
Ask: Add a slower integration case that drives `_git_commit` against a
real on-disk `tmp_path`, covering the `git init` transition and identity
derivation (host global → `bot-inator` fallback). Mark it
`@pytest.mark.slow` or skip when `git` is not on PATH, so the fast suite
stays under a second.
Verify:
Deviations:

### Events and listeners

#14 — File-watcher noise from in-progress downloads [ ] [Q1]
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q1)
Ask: Chrome download churn (`.crdownload`) floods the stream. Add
debounce + extension filter? Phase 5.2 added `on_moved`, so the finished
download now arrives as a `renamed` event; the in-progress churn is
still unfiltered.
Verify:
Deviations:

---

#16 — MacAppListener watchdog [ ] [Q3]
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q3)
Ask: `MacAppListener` has no watchdog: if `_mac_app_helper` exits or
crashes, the daemon stops receiving app activations silently. Add a
supervisor that restarts the helper on exit and exposes liveness in the
Debug tab?
Verify:
Deviations:

---

#17 — Debounce rapid app switches [ ] [Q4]
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q4)
Ask: Since Phase 4.7, `app_activated` no longer starts the tool-calling
agent. It still runs `evaluate_event` on the analysis engine per switch;
a debounce (sustained focus ≥ 500 ms) is still open.
Verify:
Deviations:

---

#19 — First-run Full Disk Access prompt for the iMessage watcher [ ] [Q6]
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q6)
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
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q2)
Ask: Closing the approval window via the red X currently does nothing.
Should it count as an implicit deny?
Verify:
Deviations:

---

#18 — Editable approval for more tools [ ] [Q5]
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q5)
Ask: Extend the editable-approval pattern to other tools: `open_url`
(edit the URL), `show_notification` (edit title/body), `move_file` (edit
destination). The current UI falls back to a read-only JSON view.
Verify:
Deviations:

### Memory

#20 — sqlite-vec needs a SQLite with load_extension [ ] [Q7]
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q7)
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
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q8)
Ask: Memory has no eviction, namespacing, or TTL; `memory.db` will
accumulate every preference saved. Add a retention policy (LRU, decayed
importance, or manual `clear_memory` wired to the existing WS command).
Verify:
Deviations:

### Security and providers

#27 — HTTP API has no authentication [ ] [Q14]
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q14)
Ask: The daemon binds `127.0.0.1`, so any local process can hit
`DELETE /api/tools` and wipe the agent's learned behaviour. Same threat
model as the WS channel; deferred unless we add a process-local token
shared via keychain.
Verify:
Deviations:

---

#31 — Groq privacy surface [ ] [Q18]
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q18)
Ask: With `AGENT_PROVIDER=groq`, window titles, file paths and iMessage
text go to Groq inside prompts. Should the UI show which provider is
active (e.g. Debug tab, or in the status frame), and should iMessage
content be redacted or kept local-only when the provider is cloud?
Verify:
Deviations:

---

#26 — Per-engine provider override [ ] [Q19]
Wave: — · Agent: _ · Added: 2026-11-09 · Source: spec (§9 Q19)
Ask: The provider applies to all three engines. A per-engine override
(local analysis, cloud drafting) would keep per-event traffic on-device
and send only tool drafts to Groq.
Verify:
Deviations:

### Test hygiene

#39 — Groq engine tests fail on Python 3.14 [ ]
Wave: — · Agent: _ · Added: 2026-11-09 · Source: assistant (test run)
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
Wave: — · Agent: _ · Added: 2026-11-09 · Source: assistant (test run)
Ask: `tests/test_dry_run.py::test_parse_plan_keeps_exists_checks_only_for_fixtures`
failed in one full-suite run and passed alone and in later full runs.
Approach: find the shared state (module globals or env) another test
leaves behind; run the suite with `-p randomly` or in reverse order.
Verify: 5 full runs in random order, no failure.
Deviations:
