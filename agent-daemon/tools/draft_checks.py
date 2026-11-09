"""Pre-review checks for drafted @tool modules (Phase 5.1).

:func:`check_draft` runs a draft through increasingly expensive checks and
returns a list of human-readable problems (empty list = passed). The
problems are fed back to the drafting model so it can repair its own
mistakes before the user ever sees the code.

The checks never call the tool function: running it has side effects
(notifications, file moves, network) that only the user may approve.
The subprocess import check is safe because :func:`_check_inert_top_level`
first guarantees the module's top level only imports, defines functions,
and assigns literals, so importing it executes no drafted logic.
"""

from __future__ import annotations

import ast
import builtins
import json
import subprocess
import sys
import symtable
import tempfile
from pathlib import Path
from typing import Callable, Optional

# Agent-daemon root, so the subprocess can resolve ``tools.*`` imports.
_DAEMON_ROOT = Path(__file__).resolve().parent.parent
_IMPORT_TIMEOUT_SECONDS = 60

_BUILTIN_NAMES: frozenset[str] = frozenset(dir(builtins)) | {"__file__", "__name__", "__doc__"}

# Runs in a fresh interpreter: load the draft by path, confirm the
# requested name is a smolagents Tool, and report the outcome as JSON on
# the last stdout line.
_IMPORT_PROBE = r"""
import importlib.util, json, sys, traceback
root, path, name = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, root)
try:
    # Import ``ai`` first, the same order as the daemon: ``tools`` and
    # ``ai`` import each other, and importing ``tools`` first fails.
    import ai  # noqa: F401
    spec = importlib.util.spec_from_file_location("draft_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    obj = getattr(mod, name, None)
    if obj is None:
        raise RuntimeError(f"module defines no attribute {name!r}")
    try:
        from smolagents import Tool
    except ImportError:
        Tool = None
    if Tool is not None and not isinstance(obj, Tool):
        raise RuntimeError(
            f"{name!r} is not a smolagents Tool; decorate it with @tool"
        )
    print(json.dumps({"ok": True}))
except BaseException as exc:
    tb = traceback.format_exception_only(type(exc), exc)
    print(json.dumps({"ok": False, "error": "".join(tb).strip()}))
"""


def check_draft(
    tool_name: str,
    source: str,
    *,
    static_validator: Optional[Callable[[str, str], None]] = None,
    run_import: bool = True,
) -> list[str]:
    """Return every problem found in ``source``; empty when the draft passes.

    Stages run cheapest first and stop at the first stage that finds a
    problem, so the drafter gets a focused list instead of cascading
    errors (an undefined-name scan of unparsable code is noise).

    Args:
        tool_name: Expected @tool function name.
        source: Draft module source.
        static_validator: Optional ``(tool_name, source)`` callable that
            raises on failure (the meta-tool's existing safety checks).
        run_import: Run the subprocess import probe (disable in tests).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"SyntaxError on line {exc.lineno}: {exc.msg}"]

    if static_validator is not None:
        try:
            static_validator(tool_name, source)
        except Exception as exc:  # ToolGenerationError and friends
            return [str(exc)]

    problems = _check_inert_top_level(tree)
    if problems:
        return problems

    problems = _check_tool_signature(tree, tool_name)
    if problems:
        return problems

    problems = find_undefined_names(source)
    if problems:
        return problems

    if run_import:
        return _check_import(tool_name, source)
    return []


def _is_literal(node: Optional[ast.expr]) -> bool:
    if node is None:
        return True
    try:
        ast.literal_eval(node)
    except Exception:
        return False
    return True


def _check_inert_top_level(tree: ast.Module) -> list[str]:
    """Allow only imports, defs, literal constants and a docstring at top level."""
    problems: list[str] = []
    for i, node in enumerate(tree.body):
        line = getattr(node, "lineno", "?")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if i == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # module docstring
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            if _is_literal(node.value):
                continue
            problems.append(
                f"line {line}: top-level assignments must be literal constants; "
                "move computed values inside the function"
            )
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Name) and dec.id == "tool"):
                    problems.append(
                        f"line {line}: only the bare @tool decorator is allowed "
                        f"on {node.name}()"
                    )
            defaults = node.args.defaults + [d for d in node.args.kw_defaults if d is not None]
            if not all(_is_literal(d) for d in defaults):
                problems.append(
                    f"line {line}: default argument values of {node.name}() must be literals"
                )
            continue
        problems.append(
            f"line {line}: {type(node).__name__} statements are not allowed at "
            "module level; only imports, constants and function definitions"
        )
    return problems


def _check_tool_signature(tree: ast.Module, tool_name: str) -> list[str]:
    """smolagents needs type hints and an Args: entry for every parameter."""
    fn = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == tool_name),
        None,
    )
    if fn is None:
        return [f"no top-level function named {tool_name!r}"]
    problems: list[str] = []
    doc = ast.get_docstring(fn) or ""
    if not doc:
        problems.append(f"{tool_name}() needs a docstring with Args: and Returns:")
    params = fn.args.args + fn.args.kwonlyargs
    for arg in params:
        if arg.annotation is None:
            problems.append(f"parameter {arg.arg!r} needs a type hint")
        if doc and f"{arg.arg}:" not in doc:
            problems.append(
                f"docstring Args: section is missing an entry for {arg.arg!r}"
            )
    if fn.returns is None:
        problems.append(f"{tool_name}() needs a return type hint (e.g. -> dict)")
    return problems


def find_undefined_names(source: str) -> list[str]:
    """Names read as globals that are never imported, defined, or builtin.

    Catches the classic drafting bug: calling ``show_notification`` (or
    any helper) without importing it. Inside a background thread that
    raises ``NameError`` silently and the tool "succeeds" doing nothing.
    """
    try:
        top = symtable.symtable(source, "<draft>", "exec")
    except SyntaxError as exc:
        return [f"SyntaxError on line {exc.lineno}: {exc.msg}"]

    module_defined = {
        s.get_name()
        for s in top.get_symbols()
        if s.is_assigned() or s.is_imported() or s.is_namespace()
    }
    missing: set[str] = set()

    def visit(table: symtable.SymbolTable) -> None:
        for sym in table.get_symbols():
            name = sym.get_name()
            if not sym.is_referenced():
                continue
            is_global_ref = (
                sym.is_global() if table.get_type() != "module"
                else not (sym.is_assigned() or sym.is_imported() or sym.is_namespace())
            )
            if is_global_ref and name not in module_defined and name not in _BUILTIN_NAMES:
                missing.add(name)
        for child in table.get_children():
            visit(child)

    visit(top)
    return [
        f"name {name!r} is used but never imported or defined; add the import "
        "at the top of the module (e.g. `from tools.X import X` for a tool)"
        for name in sorted(missing)
    ]


def _check_import(tool_name: str, source: str) -> list[str]:
    """Import the draft in a fresh interpreter and confirm it builds a Tool."""
    with tempfile.TemporaryDirectory(prefix="draft-check-") as tmp:
        path = Path(tmp) / f"{tool_name}.py"
        path.write_text(source, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "-c", _IMPORT_PROBE, str(_DAEMON_ROOT), str(path), tool_name],
                capture_output=True, text=True, timeout=_IMPORT_TIMEOUT_SECONDS,
                cwd=tmp,
            )
        except subprocess.TimeoutExpired:
            return [f"importing the module took longer than {_IMPORT_TIMEOUT_SECONDS}s"]
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    try:
        result = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        result = {}
    if result.get("ok"):
        return []
    error = result.get("error") or (proc.stderr.strip().splitlines() or ["unknown error"])[-1]
    return [f"importing the module failed: {error}"]
