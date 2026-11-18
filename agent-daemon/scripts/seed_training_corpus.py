#!/usr/bin/env python3
"""DEV ONLY: fill a #118 training corpus with realistic fake decisions (#119).

So the live check of the training offer doesn't need 300 real decisions:

    cd agent-daemon
    .venv/bin/python scripts/seed_training_corpus.py --path /tmp/seed.db --rows 340
    AGENT_TRAINING_DB=/tmp/seed.db ...   # point a dev daemon at it

Writes approved, edited, denied and timed-out rows, marks some approvals
undone, and flags a few as tainted (origin notes), so ``usable_counts()``
passes the threshold (300 untainted usable, 50+ negatives) by default.

Refuses to touch the real ``~/.bot-inator/training.db`` unless ``--real``
is given. No network, no model.
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import training_corpus as tc  # noqa: E402

SCENARIOS = [
    ("file_created", "pdf_download", "summarize_file",
     "A PDF was saved to ~/Downloads/{name}.pdf. Should the assistant do anything?",
     lambda r, n: {"path": f"~/Downloads/{n}.pdf"}, "Summarize the PDF you just downloaded"),
    ("user_message", "user_message", "create_reminder",
     "User: remind me to {task} tomorrow morning",
     lambda r, n: {"title": n.replace("-", " "), "due": "tomorrow 9:00"}, "Add a reminder"),
    ("calendar_event_soon", "calendar_event_soon", "create_note",
     "The meeting '{name}' starts in 15 minutes. Prepare something?",
     lambda r, n: {"title": f"Notes: {n}", "body": "Agenda\n- \n- "}, "Start meeting notes"),
    ("app_activated", "focus_switch", "show_notification",
     "The user switched to Xcode for the 5th time in 10 minutes.",
     lambda r, n: {"title": "Focus", "message": "Want me to hide other apps?"}, "Suggest focusing"),
    ("pattern_detected", "move_screenshots", "move_file",
     "Pattern: screenshots on the Desktop get moved to ~/Pictures/Screens by hand.",
     lambda r, n: {"src": f"~/Desktop/Screenshot {n}.png", "dest": "~/Pictures/Screens/"},
     "Move the new screenshot"),
    ("user_message", "user_message", "open_url",
     "User: open the {name} dashboard",
     lambda r, n: {"url": f"https://example.com/{n}"}, "Open the dashboard"),
    ("user_message", "user_message", "draft_email",
     "User: draft a short thank-you to Sam about the {name} review",
     lambda r, n: {"to": "sam@example.com", "subject": f"Thanks for the {n} review",
                   "body": "Hi Sam,\n\nThanks for taking the time on the review.\n\nBest"},
     "Draft the thank-you"),
]
WORDS = ["invoice", "roadmap", "q3-plan", "standup", "budget", "design-sync", "taxes",
         "release", "launch", "offsite", "hiring", "retro", "contract", "kickoff"]


def seed(path: Path, rows: int, *, negative_rate: float = 0.22, undo_rate: float = 0.05,
         edit_rate: float = 0.12, timeout_rate: float = 0.03, taint_rate: float = 0.03,
         seed_value: int = 119) -> dict[str, int]:
    rng = random.Random(seed_value)
    now = [1_800_000_000.0]
    corpus = tc.TrainingCorpus(path, enabled=lambda: True, keep_mail_text=lambda: True,
                               clock=lambda: now[0])
    try:
        for i in range(rows):
            now[0] += 600.0
            event_type, trigger, tool, prompt, args_of, summary = rng.choice(SCENARIOS)
            name = f"{rng.choice(WORDS)}-{i}"
            args = args_of(rng, name)
            roll = rng.random()
            if roll < negative_rate:
                verdict, edited = "denied", None
            elif roll < negative_rate + timeout_rate:
                verdict, edited = "timed_out", None
            elif roll < negative_rate + timeout_rate + edit_rate:
                verdict = "approved"
                key = next(iter(args))
                edited = {"kwargs": {key: f"{args[key]} (edited)"}}
            else:
                verdict, edited = "approved", None
            offer_id = f"seed_offer_{i}"
            corpus.record(
                tool_name=tool, tool_args={"kwargs": args}, verdict=verdict,
                prompt=prompt.format(name=name, task=name.replace("-", " ")),
                edited_args=edited, offer_id=offer_id, request_id=f"seed_req_{i}",
                event_id=f"seed_evt_{i}", event_type=event_type, trigger=trigger,
                reasoning="seeded example", summary=summary,
                tainted=rng.random() < taint_rate,
            )
            if verdict == "approved" and rng.random() < undo_rate:
                corpus.mark_undone(offer_id)
        return corpus.usable_counts()
    finally:
        corpus.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--path", type=Path, help="corpus DB to write (created if missing)")
    ap.add_argument("--rows", type=int, default=340, help="rows to add (default 340)")
    ap.add_argument("--seed", type=int, default=119)
    ap.add_argument("--real", action="store_true",
                    help="allow writing the real ~/.bot-inator/training.db")
    args = ap.parse_args(argv)
    real = (tc.DATA_DIR / "training.db").expanduser().resolve()
    configured = tc.default_path().expanduser().resolve()
    path = args.path or (configured if args.real else None)
    if path is None:
        ap.error("pass --path (or --real to write the real corpus)")
    path = Path(path).expanduser().resolve()
    if path == real and not args.real:
        ap.error(f"{real} is the real corpus; pass --real to write it")
    counts = seed(path, args.rows, seed_value=args.seed)
    print(f"seeded {args.rows} rows into {path}: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
