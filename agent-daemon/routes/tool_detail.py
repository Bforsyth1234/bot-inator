"""``GET /api/tools/{name}``: one tool's detail for the Tool Manager (#61).

Returns, for a built-in or a generated tool (loaded, disabled or
quarantined)::

    {name, kind: "builtin"|"generated",
     state: "loaded"|"not_loaded"|"disabled"|"quarantined",
     summary,                 # first docstring paragraph
     when_it_runs: {text, hints, provenance},
     gate: "needs approval"|"runs without asking",   # READ_ONLY_TOOL_NAMES
     inputs: [{name, type, description, optional}],
     sandbox: {enabled, network_allowed, note} | null,   # generated only
     source_path, error_text}

Built-in summaries and run conditions come from the tool's schema and
docstring (no hand-written text per tool). Generated tools' conditions
come from ``<generated>/.provenance/<name>.json``, written at install time
by ``tools.meta_tool_generator``; tools installed earlier say "unknown".

The token check is the app-wide :class:`auth.AuthMiddleware`; this router
adds none of its own. Reads ``app.state.orchestrator`` (optional) and
``app.state.generated_tools_dir`` (optional; defaults to
``config.settings.generated_tools_dir``).
"""
from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_ERROR_TEXT_MAX = 20_000
GATE_FREE = "runs without asking"
GATE_APPROVAL = "needs approval"
# Sentences in a built-in's docstring that say when to use it.
_HINT_RE = re.compile(
    r"\b(use (this|it|the)|call (this|it)|for use with|when\b|do not|don't|"
    r"read-only|approves?|example)",
    re.IGNORECASE,
)
_PY_TO_SCHEMA = {
    "str": "string", "int": "integer", "float": "number", "bool": "boolean",
    "dict": "object", "list": "array",
}


# ---- helpers ---------------------------------------------------------------


def _generated_dir(request: Request) -> Path:
    override = getattr(request.app.state, "generated_tools_dir", None)
    if override is not None:
        return Path(override)
    from config import settings

    return Path(settings.generated_tools_dir)


def _read_only_names() -> frozenset[str]:
    from tools import READ_ONLY_TOOL_NAMES

    return READ_ONLY_TOOL_NAMES


def _paragraphs(doc: str) -> list[str]:
    text = inspect.cleandoc(doc or "")
    return [" ".join(p.split()) for p in re.split(r"\n\s*\n", text) if p.strip()]


def _strip_sections(doc: str) -> str:
    """Drop Google-style ``Args:`` / ``Returns:`` / ``Raises:`` sections."""
    out: list[str] = []
    for line in inspect.cleandoc(doc or "").splitlines():
        if re.match(r"^(Args|Arguments|Returns|Raises|Yields):\s*$", line.strip()):
            break
        out.append(line)
    return "\n".join(out)


def _summary(doc: str) -> str:
    paras = _paragraphs(_strip_sections(doc))
    return paras[0].replace("``", "") if paras else ""


def _hints(doc: str) -> list[str]:
    paras = _paragraphs(_strip_sections(doc))[1:]
    sentences = re.split(r"(?<=[.!?])\s+", " ".join(paras))
    return [s.replace("``", "") for s in sentences if s and _HINT_RE.search(s)][:6]


def _inputs_from_schema(inputs: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "type": str(spec.get("type", "any")),
            "description": str(spec.get("description", "")),
            "optional": bool(spec.get("nullable", False)),
        }
        for name, spec in (inputs or {}).items()
    ]


def _tool_function(tree: ast.Module, name: str) -> Optional[ast.FunctionDef]:
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for node in funcs:
        if node.name == name:
            return node  # type: ignore[return-value]
    return funcs[0] if funcs else None  # type: ignore[return-value]


def _arg_docs(doc: str) -> dict[str, str]:
    docs: dict[str, str] = {}
    in_args, current = False, None
    for line in inspect.cleandoc(doc or "").splitlines():
        stripped = line.strip()
        if re.match(r"^(Args|Arguments):\s*$", stripped):
            in_args = True
            continue
        if in_args and re.match(r"^[A-Z][a-z]+:\s*$", stripped):
            break
        if not in_args or not stripped:
            continue
        match = re.match(r"^(\w+)\s*(\([^)]*\))?:\s*(.*)$", stripped)
        if match:
            current = match.group(1)
            docs[current] = match.group(3)
        elif current:
            docs[current] += " " + stripped
    return docs


def _from_source(path: Path, name: str) -> tuple[str, list[dict[str, Any]]]:
    """``(docstring, inputs)`` of a generated module, without importing it."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return "", []
    func = _tool_function(tree, name)
    if func is None:
        return "", []
    doc = ast.get_docstring(func) or ""
    arg_docs = _arg_docs(doc)
    args = func.args.args
    n_defaults = len(func.args.defaults)
    inputs = []
    for idx, arg in enumerate(args):
        ann = ast.unparse(arg.annotation) if arg.annotation is not None else "any"
        base = ann.replace("Optional[", "").rstrip("]").split("[")[0].split("|")[0].strip()
        inputs.append({
            "name": arg.arg,
            "type": _PY_TO_SCHEMA.get(base, base),
            "description": arg_docs.get(arg.arg, ""),
            "optional": idx >= len(args) - n_defaults or "Optional" in ann or "None" in ann,
        })
    return doc, inputs


def _builtin_source(tool_obj: Any) -> Optional[str]:
    forward = getattr(tool_obj, "forward", None)
    for target in (forward and inspect.unwrap(forward), type(tool_obj)):
        try:
            path = inspect.getsourcefile(target)  # type: ignore[arg-type]
        except (TypeError, OSError):
            continue
        if path and "site-packages" not in path:
            return str(Path(path).resolve())
    return None


def _load_provenance(gen: Path, name: str) -> Optional[dict[str, Any]]:
    from tools.meta_tool_generator import provenance_path

    try:
        data = json.loads(provenance_path(gen, name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


_SOURCE_PHRASES = {
    "repetition": "the pattern recognizer saw you repeat a workflow",
    "friction": "you kept copying data from one app and pasting it into another",
    "setup_scan": "the one-time setup scan found repeated manual work",
    "starter_catalog": "a curated starter idea matched the apps on your Mac",
    "next_step": "an event had an obvious follow-up",
    "chat_gap": "you asked in chat for something no tool could do",
    "chat": "you asked for it in chat",
}


def _provenance_text(prov: dict[str, Any]) -> str:
    source = prov.get("source")
    reason = _SOURCE_PHRASES.get(str(source), f"idea source: {source}" if source else "")
    idea = prov.get("idea") or {}
    requested = prov.get("requested") or {}
    what = idea.get("description") or requested.get("description") or ""
    parts = []
    if reason:
        parts.append(f"Created because {reason}.")
    if prov.get("trigger"):
        parts.append(f"Trigger: {prov['trigger']}.")
    if what:
        parts.append(f"Idea: {what}")
    parts.append(
        "The agent calls it when an event or chat request matches that idea; "
        "every call asks for approval first."
    )
    return " ".join(parts)


def _builtin_when(doc: str, summary: str, inputs: list[dict[str, Any]], free: bool) -> dict[str, Any]:
    needed = [i["name"] for i in inputs if not i["optional"]]
    text = f"The agent calls this when an event or a chat request needs to: {summary[:1].lower() + summary[1:]}"
    if needed:
        text += f" It needs {', '.join(needed)}."
    text += " Runs without asking (read-only)." if free else " Asks for your approval first."
    return {"text": text, "hints": _hints(doc), "provenance": None}


# ---- route -----------------------------------------------------------------


def _find(request: Request, name: str, gen: Path) -> tuple[str, Any, Optional[Path], str]:
    """``(kind, live_tool_or_None, source_file, state)``; 404 when unknown."""
    orch = getattr(request.app.state, "orchestrator", None)
    live = {getattr(t, "name", ""): t for t in getattr(orch, "tools", None) or []}
    dynamic = set(getattr(orch, "_dynamic_tool_names", set()) or set())
    if orch is None:
        from tools import AVAILABLE_TOOLS

        live = {t.name: t for t in AVAILABLE_TOOLS}
    if name in live and name not in dynamic:
        return "builtin", live[name], None, "loaded"
    if name in live:
        return "generated", live[name], gen / f"{name}.py", "loaded"
    for path, state in (
        (gen / f"{name}.py", "not_loaded"),
        (gen / "disabled" / f"{name}.py", "disabled"),
        (gen / "quarantine" / f"{name}.py", "quarantined"),
    ):
        if path.is_file():
            return "generated", None, path, state
    raise HTTPException(status_code=404, detail=f"no tool named {name!r}")


def _sandbox_info(live: Any, path: Path) -> dict[str, Any]:
    try:
        from tools import tool_sandbox as sb
    except Exception:  # pragma: no cover - defensive
        sb = None  # type: ignore[assignment]
    enabled = bool(sb and sb.sandbox_enabled())
    if live is not None and hasattr(live, "network_allowed"):
        return {"enabled": True, "network_allowed": bool(live.network_allowed),
                "note": str(getattr(live, "sandbox_note", ""))}
    reason = None
    if sb is not None:
        try:
            reason = sb.network_reason(path.read_text(encoding="utf-8"))
        except OSError:
            reason = None
    if not enabled:
        note = "runs in the daemon process (AGENT_TOOL_SANDBOX=0)"
    elif reason:
        note = f"runs under sandbox-exec; network allowed ({reason})"
    else:
        note = "runs under sandbox-exec; no network"
    return {"enabled": enabled, "network_allowed": reason is not None, "note": note}


@router.get("/api/tools/{name}")
async def tool_detail(name: str, request: Request) -> dict[str, Any]:
    """Summary, run conditions, gate, inputs and source for one tool (#61)."""
    if not _IDENTIFIER_RE.match(name):
        raise HTTPException(status_code=404, detail=f"no tool named {name!r}")
    gen = _generated_dir(request)
    kind, live, path, state = _find(request, name, gen)
    free = name in _read_only_names()
    gate = GATE_FREE if free else GATE_APPROVAL

    if live is not None and kind == "builtin":
        doc = str(getattr(live, "description", "") or "")
        inputs = _inputs_from_schema(getattr(live, "inputs", {}))
    elif live is not None:
        doc = str(getattr(live, "description", "") or "")
        inputs = _inputs_from_schema(getattr(live, "inputs", {}))
    else:
        doc, inputs = _from_source(path, name)  # type: ignore[arg-type]
    summary = _summary(doc)

    body: dict[str, Any] = {
        "name": name, "kind": kind, "state": state, "summary": summary,
        "gate": gate, "inputs": inputs, "sandbox": None, "error_text": None,
    }
    if kind == "builtin":
        body["when_it_runs"] = _builtin_when(doc, summary, inputs, free)
        body["source_path"] = _builtin_source(live)
        return body

    assert path is not None
    prov = _load_provenance(gen, name)
    body["when_it_runs"] = {
        "text": _provenance_text(prov) if prov else "unknown",
        "hints": [],
        "provenance": prov,
    }
    body["sandbox"] = _sandbox_info(live, path)
    body["source_path"] = str(path.resolve())
    if state == "quarantined":
        try:
            body["error_text"] = (gen / "quarantine" / f"{name}.error.txt").read_text(
                encoding="utf-8")[:_ERROR_TEXT_MAX]
        except OSError:
            body["error_text"] = ""
    return body
