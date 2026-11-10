"""Logic review for drafted @tool modules (Phase 5.1, critic pass).

:mod:`tools.draft_checks` catches structural problems (syntax, imports,
signatures) but not logic: a draft can pass every check and still do the
wrong thing, e.g. glob ``*.pdf`` when asked for invoice PDFs only.
:func:`critique` asks a model to compare the draft against the spec it
was written from and returns concrete problems for the repair loop.

The critic is advisory. If its reply cannot be parsed, or the model call
fails, it returns no problems so a flaky critic never blocks a draft that
passed the structural checks.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

MAX_CRITIC_PROBLEMS = 5
_CRITIC_MAX_TOKENS = 600

CRITIC_SYSTEM_PROMPT = (
    "You review a Python tool written from a spec. Find places where the "
    "code does something DIFFERENT from what the spec says: it selects "
    "the wrong items (too many or too few), skips a step the spec lists, "
    "ignores an input or option the spec defines, returns a different "
    "shape than the spec says, or deletes/overwrites/sends something the "
    "spec did not ask for.\n\n"
    "Check every glob pattern, filter, and condition against the spec "
    "literally: a pattern that matches more files than the spec names "
    "(e.g. *.pdf when the spec says invoice PDFs) is a problem.\n\n"
    "Do NOT report:\n"
    "- missing error handling for permissions, missing folders, full "
    "disks, locked files, or other failures the spec does not mention;\n"
    "- style, naming, comments, docstrings, type hints, logging, "
    "performance, or extra features.\n\n"
    "Every problem must quote the exact spec sentence it violates and "
    "the exact code that violates it, copied character for character. "
    f"At most {MAX_CRITIC_PROBLEMS} problems. Most correct code has none.\n\n"
    "Reply with exactly one JSON object and nothing else:\n"
    '{"problems": [{"spec_quote": "<exact words from the spec>", '
    '"code_quote": "<exact code>", "problem": "<one sentence>"}]}\n'
    'Reply {"problems": []} when the code does what the spec says.'
)

from ai.mlx_engine import THINK_BLOCK_RE as _THINK_BLOCK_RE


def critic_enabled() -> bool:
    """``AGENT_DRAFT_CRITIC=0`` turns the critic off (default on)."""
    return os.environ.get("AGENT_DRAFT_CRITIC", "1") != "0"


def critique(
    engine: Any,
    tool_name: str,
    description: str,
    expected_logic: str,
    source: str,
) -> list[str]:
    """Return logic problems in ``source`` versus its spec; ``[]`` = pass.

    ``engine`` needs ``generate_chat_sync(system, user, max_tokens)``.
    Must be called from a worker thread (it blocks on inference).
    """
    spec = f"description: {description}\nexpected_logic: {expected_logic}"
    # Review the code with comments stripped: a drafter "repairing" a
    # problem can add ``# case-insensitive search`` above unchanged code,
    # and the critic believed the comment (Phase 5.1 test).
    source = _strip_comments(source)
    user = f"Spec\n----\ntool_name: {tool_name}\n{spec}\n\nCode\n----\n{source}"
    try:
        raw = engine.generate_chat_sync(
            CRITIC_SYSTEM_PROMPT, user, max_tokens=_CRITIC_MAX_TOKENS
        )
    except Exception:
        logger.exception("critic call failed; treating draft as pass")
        return []
    return parse_critique(raw, spec=spec, source=source)


def _strip_comments(source: str) -> str:
    """Return ``source`` without comments (via ``ast.unparse``)."""
    try:
        return ast.unparse(ast.parse(source))
    except Exception:
        return source


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _grounded(item: dict[str, Any], spec: str, source: str) -> bool:
    """Both quotes must really appear in the spec and the code.

    This filter is what keeps the critic honest: in the Phase 5.1 test
    an unconstrained critic padded every review with invented "does not
    handle permission errors" problems, and the repair loop never
    converged. A problem the model cannot anchor to real text is dropped.
    """
    spec_q = _norm(str(item.get("spec_quote") or ""))
    code_q = _norm(str(item.get("code_quote") or ""))
    if len(spec_q) < 4 or len(code_q) < 3:
        return False
    return spec_q in _norm(spec) and code_q in _norm(source)


def parse_critique(raw: str, *, spec: str, source: str) -> list[str]:
    """Extract grounded problems; unparseable replies give ``[]``."""
    text = _THINK_BLOCK_RE.sub("", raw or "").strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        logger.warning("critic reply had no JSON; treating as pass: %r", text[:200])
        return []
    try:
        data = json.loads(match.group(0), strict=False)
    except json.JSONDecodeError:
        logger.warning("critic reply was not valid JSON; treating as pass")
        return []
    items = data.get("problems") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    kept: list[str] = []
    for item in items:
        if not isinstance(item, dict) or not _grounded(item, spec, source):
            logger.info("critic problem dropped (not grounded): %r", item)
            continue
        problem = str(item.get("problem") or "").strip()
        if problem:
            kept.append(
                f"{problem} (spec: \"{item['spec_quote']}\"; code: `{item['code_quote']}`)"
            )
    return kept[:MAX_CRITIC_PROBLEMS]


def with_review_notes(source: str, notes: list[str]) -> str:
    """Prefix unresolved critic notes as comments for the human reviewer."""
    if not notes:
        return source
    header = ["# REVIEW NOTES: automated review (dry run / critic) flagged these.", "# Check them before approving."]
    header += [f"# - {n}".replace("\n", " ") for n in notes]
    return "\n".join(header) + "\n\n" + source
