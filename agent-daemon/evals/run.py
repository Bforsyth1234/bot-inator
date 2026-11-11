"""Run an eval suite against a real engine and save the scores.

    python -m evals.run --suite idea_judge
    python -m evals.run --suite idea_judge --provider groq --repeats 5
    python -m evals.run --suite idea_judge --model mlx-community/SomeModel

Every case runs ``--repeats`` times, because one model run proves little.
Scores are written to ``evals/results/`` (git-ignored) and compared with
the previous run of the same suite, so a model or prompt change shows up
as "these cases got better, these got worse".
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from evals import suites
from providers import PROVIDERS

ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / "results"
ROLES = ("main", "analysis", "drafting")


# --- scoring (pure; covered by tests/test_evals.py) ------------------------

def aggregate(case_runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals over ``[{"id", "expected", "runs": [run, ...]}, ...]``.

    ``accuracy`` counts every run; ``solid`` counts cases right on every
    run; ``flaky`` lists cases whose verdict changed between runs.
    """
    runs = [r for c in case_runs for r in c["runs"]]
    return {
        "cases": len(case_runs),
        "runs": len(runs),
        "ok_runs": sum(bool(r.get("ok")) for r in runs),
        "accuracy": round(sum(bool(r.get("ok")) for r in runs) / len(runs), 3) if runs else 0.0,
        "solid": sum(all(r.get("ok") for r in c["runs"]) for c in case_runs if c["runs"]),
        "flaky": [c["id"] for c in case_runs if len({r.get("got") for r in c["runs"]}) > 1],
        "errors": sum("error" in r for r in runs),
    }


def _ok_count(case: dict[str, Any]) -> tuple[int, int]:
    return sum(bool(r.get("ok")) for r in case["runs"]), len(case["runs"])


def compare(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Lines describing what changed from result ``old`` to result ``new``."""
    lines = [
        f"vs {old.get('model', '?')} ({old.get('started_at', '?')}): accuracy "
        f"{old['summary']['accuracy']:.0%} -> {new['summary']['accuracy']:.0%}"
    ]
    before = {c["id"]: c for c in old["cases"]}
    for case in new["cases"]:
        prev = before.get(case["id"])
        if prev is None:
            lines.append(f"  NEW    {case['id']}")
            continue
        (a, an), (b, bn) = _ok_count(prev), _ok_count(case)
        if a * bn != b * an:  # compare a/an with b/bn without dividing
            word = "BETTER" if b * an > a * bn else "WORSE "
            lines.append(f"  {word} {case['id']}: {a}/{an} -> {b}/{bn}")
    if len(lines) == 1:
        lines.append("  no case changed")
    return lines


def report(result: dict[str, Any]) -> str:
    lines = []
    for case in result["cases"]:
        ok, n = _ok_count(case)
        mark = "OK  " if ok == n else ("BAD " if ok == 0 else "FLKY")
        gots = ",".join(str(r.get("got", "error")) for r in case["runs"])
        last = case["runs"][-1] if case["runs"] else {}
        detail = last.get("error") or last.get("reason") or ""
        want = f"expected {case['expected']}, " if case.get("expected") is not None else ""
        lines.append(f"{mark} {case['id']}: {want}got {gots} ({ok}/{n}) {detail}".rstrip())
    s = result["summary"]
    lines.append(
        f"{result['suite']} on {result['model']} ({result['provider']}): "
        f"accuracy {s['accuracy']:.0%} ({s['ok_runs']}/{s['runs']} runs), "
        f"{s['solid']}/{s['cases']} cases right every time, "
        f"{len(s['flaky'])} flaky, {s['errors']} errors"
    )
    return "\n".join(lines)


# --- running ---------------------------------------------------------------

async def run_suite(
    suite: Any, cases: list[dict[str, Any]], engine: Any, settings: Any,
    *, repeats: int = 3, progress: Any = None,
) -> list[dict[str, Any]]:
    """Run every case ``repeats`` times. A run that raises is recorded as
    a failed run with ``error`` set, so one bad call can't lose the rest."""
    out = []
    for case in cases:
        runs = []
        for _ in range(repeats):
            try:
                run = dict(await suite.run_case(case, engine, settings))
            except Exception as exc:  # noqa: BLE001 - keep going, report it
                run = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            run.pop("id", None)
            run.pop("expected", None)
            runs.append(run)
        entry = {"id": case["id"], "expected": case.get("expected"), "runs": runs}
        out.append(entry)
        if progress:
            progress(entry)
    return out


def build_engine(role: str, settings: Any, model: Optional[str] = None) -> Any:
    """The engine for ``role`` on its configured provider (as ``main.py``
    builds it), optionally on another ``model``."""
    provider = settings.engine_provider(role)
    if provider != "local":
        from ai.groq_engine import build_engine

        engine = build_engine(settings, provider, role)
        if model:
            engine.model_name = model
        return engine
    from ai.mlx_engine import MLXEngine

    local = {"main": settings.model_path, "analysis": settings.analysis_model_path,
             "drafting": settings.drafting_model_path}
    return MLXEngine(model or local[role])


def previous_result(suite: str, results_dir: Path = RESULTS_DIR) -> Optional[dict[str, Any]]:
    """The most recent saved result for ``suite`` (file names sort by time)."""
    for path in sorted(results_dir.glob(f"*-{suite}-*.json"), reverse=True):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            continue
    return None


def save(result: dict[str, Any], results_dir: Path = RESULTS_DIR) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9.]+", "-", str(result["model"])).strip("-")[:60]
    stamp = datetime.fromisoformat(result["started_at"]).strftime("%Y%m%d-%H%M%S")
    path = results_dir / f"{stamp}-{result['suite']}-{slug}.json"
    path.write_text(json.dumps(result, indent=1))
    return path


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - live check
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", required=True, choices=suites.SUITES)
    parser.add_argument("--provider", choices=PROVIDERS,
                        help="override the configured provider for this run")
    parser.add_argument("--model", help="override the model id for this run")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--cases", type=Path, help="another cases file")
    parser.add_argument("--only", help="run only cases whose id contains this text "
                        "(a partial run is not saved or compared)")
    parser.add_argument("--baseline", type=Path,
                        help="result file to compare with (default: the previous run)")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args(argv)

    if args.provider:
        # Set before ``config`` is imported; the per-role keys too, so a
        # saved per-engine override can't redirect the run.
        for key in ("AGENT_PROVIDER", *(f"AGENT_PROVIDER_{r.upper()}" for r in ROLES)):
            os.environ[key] = args.provider

    suite = suites.get(args.suite)
    from config import settings

    role = suite.role(settings)
    engine = build_engine(role, settings, args.model)
    started = datetime.now().isoformat(timespec="seconds")
    print(f"{args.suite}: {role} engine, {settings.engine_provider(role)}, "
          f"{engine.model_name}, {args.repeats} run(s) per case")

    def progress(entry: dict[str, Any]) -> None:
        ok, n = _ok_count(entry)
        print(f"  {entry['id']}: {ok}/{n}", flush=True)

    cases = suite.load_cases(args.cases)
    if args.only:
        cases = [c for c in cases if args.only in c["id"]]
        if not cases:
            parser.error(f"no case id contains {args.only!r}")
    case_runs = asyncio.run(run_suite(
        suite, cases, engine, settings,
        repeats=args.repeats, progress=progress,
    ))
    result = {
        "suite": args.suite,
        "provider": settings.engine_provider(role),
        "model": engine.model_name,
        "repeats": args.repeats,
        "started_at": started,
        "git_commit": _git("rev-parse", "--short", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "summary": aggregate(case_runs),
        "cases": case_runs,
    }
    print(report(result))
    if args.only:
        return 1 if result["summary"]["errors"] else 0
    baseline = json.loads(args.baseline.read_text()) if args.baseline else previous_result(args.suite)
    if baseline:
        print("\n".join(compare(baseline, result)))
    if not args.no_save:
        print(f"saved {save(result)}")
    return 1 if result["summary"]["errors"] else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
