"""Run an eval suite against a real engine and save the scores.

    python -m evals.run --suite idea_judge
    python -m evals.run --suite idea_judge --provider groq --repeats 5
    python -m evals.run --suite idea_judge --model mlx-community/SomeModel
    python -m evals.run --suite safety --adapter ~/.bot-inator/models/1
    python -m evals.run --suite email_draft --judge-provider groq
    python -m evals.run --suite safety --min-accuracy 0.9

Judged suites (``NEEDS_JUDGE``, todo #133) grade free text with a judge
model (:mod:`evals.judge`) from ``--judge-provider`` / ``--judge-model``;
the run refuses a judge that is the model under test. The judge's
per-line verdicts and reasons are saved with each run. ``--min-accuracy``
makes the run exit 1 when accuracy falls below it (any suite).

``--adapter`` loads a LoRA adapter folder (``mlx_lm.lora`` output) on top
of the local MLX model; results then carry ``adapter`` and their file name
ends in ``--adapter-<folder>``. The daemon runs a suite in-process with
:func:`evaluate` (todo #121), which returns the same result dict.

Every case runs ``--repeats`` times, because one model run proves little.
Scores are written to ``evals/results/`` (git-ignored) and compared with
the previous run of the same suite, so a model or prompt change shows up
as "these cases got better, these got worse".
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from evals import suites
from evals.judge import check_distinct
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


def _label(result: dict[str, Any]) -> str:
    """The model id, plus the adapter when one was loaded."""
    adapter = result.get("adapter")
    return f"{result.get('model', '?')}" + (f" + adapter {adapter}" if adapter else "")


def compare(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Lines describing what changed from result ``old`` to result ``new``."""
    lines = [
        f"vs {_label(old)} ({old.get('started_at', '?')}): accuracy "
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
        f"{result['suite']} on {_label(result)} ({result['provider']}): "
        f"accuracy {s['accuracy']:.0%} ({s['ok_runs']}/{s['runs']} runs), "
        f"{s['solid']}/{s['cases']} cases right every time, "
        f"{len(s['flaky'])} flaky, {s['errors']} errors"
        + (f"; judged by {result.get('judge_provider')} {result['judge_model']}"
           if result.get("judge_model") else "")
    )
    return "\n".join(lines)


# --- running ---------------------------------------------------------------

async def run_suite(
    suite: Any, cases: list[dict[str, Any]], engine: Any, settings: Any,
    *, repeats: int = 3, progress: Any = None, judge: Any = None,
) -> list[dict[str, Any]]:
    """Run every case ``repeats`` times. A run that raises is recorded as
    a failed run with ``error`` set, so one bad call can't lose the rest.
    ``judge`` (an engine) is passed to judged suites' ``run_case``; each
    run asks it once, so repeats repeat the judge too (#133 decision 5)."""
    extra = {"judge": judge} if judge is not None else {}
    out = []
    for case in cases:
        runs = []
        for _ in range(repeats):
            try:
                run = dict(await suite.run_case(case, engine, settings, **extra))
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


def build_engine(role: str, settings: Any, model: Optional[str] = None,
                 *, adapter: Optional[str] = None, provider: Optional[str] = None) -> Any:
    """The engine for ``role`` on its configured provider (as ``main.py``
    builds it) or on ``provider``, optionally on another ``model``, with an
    optional LoRA ``adapter`` folder (local MLX only)."""
    provider = provider or settings.engine_provider(role)
    if adapter and provider != "local":
        raise ValueError(f"an adapter needs the local MLX engine, not {provider!r}")
    if provider != "local":
        from ai.groq_engine import build_engine

        engine = build_engine(settings, provider, role)
        if model:
            engine.model_name = model
        return engine
    from ai.mlx_engine import MLXEngine

    local = {"main": settings.model_path, "analysis": settings.analysis_model_path,
             "drafting": settings.drafting_model_path}
    return MLXEngine(model or local[role], adapter_path=str(adapter) if adapter else None)


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
    if result.get("adapter"):
        slug += "--adapter-" + _adapter_slug(result["adapter"])
    stamp = datetime.fromisoformat(result["started_at"]).strftime("%Y%m%d-%H%M%S")
    path = results_dir / f"{stamp}-{result['suite']}-{slug}.json"
    path.write_text(json.dumps(result, indent=1))
    return path


_save_result = save  # ``evaluate``'s ``save`` flag shadows the name


def _adapter_slug(adapter: str) -> str:
    """The adapter folder's last two path parts (``models/3`` -> ``models-3``)."""
    parts = Path(str(adapter)).expanduser().parts[-2:]
    return re.sub(r"[^A-Za-z0-9.]+", "-", "-".join(parts)).strip("-")[:40] or "adapter"


def _engine_provider(engine: Any) -> Optional[str]:
    provider = getattr(engine, "provider", None)
    if isinstance(provider, str):
        return provider
    return "local" if type(engine).__name__ == "MLXEngine" else None


async def evaluate(
    suite: Any,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    adapter: Optional[str] = None,
    repeats: int = 3,
    save: bool = True,
    settings: Any = None,
    engine: Any = None,
    cases: Optional[list[dict[str, Any]]] = None,
    cases_path: Optional[Path] = None,
    progress: Any = None,
    results_dir: Path = RESULTS_DIR,
    judge: Any = None,
    judge_provider: Optional[str] = None,
) -> dict[str, Any]:
    """Run ``suite`` (a name from :data:`suites.SUITES`, or a suite module)
    in-process and return the result dict the CLI saves::

        {"suite", "provider", "model", "adapter", "repeats", "started_at",
         "git_commit", "git_dirty", "summary": aggregate(...), "cases": [...],
         "passed_all": bool, "saved_to": str}   # saved_to only when saved

    ``engine`` reuses a built engine (e.g. the daemon's own); otherwise one
    is built for the suite's role (:func:`build_engine`) with ``provider``,
    ``model`` and ``adapter``, and unloaded afterwards so it doesn't stay in
    memory next to the daemon's. ``passed_all`` is every run of every case
    ok with no errors (the #121 safety gate). ``cases`` replaces the suite's
    cases file (a partial run is still saved when ``save`` is true; the
    caller decides).
    """
    mod = suites.get(suite) if isinstance(suite, str) else suite
    name = suite if isinstance(suite, str) else mod.__name__.rsplit(".", 1)[-1]
    if getattr(mod, "NEVER_SAVE", False):
        save = False  # #121 decision 8: the user's own data never lands in results/
    if settings is None:
        from config import settings as _settings

        settings = _settings
    role = mod.role(settings)
    if getattr(mod, "NEEDS_JUDGE", False) and judge is None:
        raise ValueError(f"{name} is a judged suite: pass a judge engine (--judge-provider)")
    own = engine is None
    if own:
        engine = build_engine(role, settings, model, adapter=adapter, provider=provider)
    else:
        loaded = getattr(engine, "adapter_path", None)
        if adapter and str(adapter) != str(loaded):
            raise ValueError(f"engine has adapter {loaded!r}, not {adapter!r}")
        adapter = loaded or None
    judge_provider = judge_provider or (_engine_provider(judge) if judge is not None else None)
    if judge is not None:
        check_distinct(provider or _engine_provider(engine) or settings.engine_provider(role),
                       getattr(engine, "model_name", model),
                       judge_provider, getattr(judge, "model_name", None))
    started = datetime.now().isoformat(timespec="seconds")
    try:
        case_list = cases if cases is not None else mod.load_cases(cases_path)
        case_runs = await run_suite(mod, case_list, engine, settings,
                                    repeats=repeats, progress=progress, judge=judge)
    finally:
        if own:
            unload = getattr(engine, "unload", None)
            if callable(unload):
                done = unload()
                if inspect.isawaitable(done):
                    await done
    summary = aggregate(case_runs)
    result: dict[str, Any] = {
        "suite": name,
        "provider": provider or _engine_provider(engine) or settings.engine_provider(role),
        "model": getattr(engine, "model_name", model),
        "adapter": str(adapter) if adapter else None,
        "repeats": repeats,
        "judge_provider": judge_provider if judge is not None else None,
        "judge_model": getattr(judge, "model_name", None) if judge is not None else None,
        "started_at": started,
        "git_commit": _git("rev-parse", "--short", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "summary": summary,
        "cases": case_runs,
        "passed_all": bool(summary["runs"]) and summary["ok_runs"] == summary["runs"]
                      and not summary["errors"],
    }
    if save:
        result["saved_to"] = str(_save_result(result, results_dir))
    return result


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def exit_code(result: dict[str, Any], min_accuracy: Optional[float] = None) -> int:
    """1 if any run errored or accuracy is below ``min_accuracy``, else 0."""
    s = result["summary"]
    if s["errors"]:
        return 1
    if min_accuracy is not None and s["accuracy"] < min_accuracy:
        return 1
    return 0


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - live check
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", required=True, choices=suites.SUITES)
    parser.add_argument("--provider", choices=PROVIDERS,
                        help="override the configured provider for this run")
    parser.add_argument("--model", help="override the model id for this run")
    parser.add_argument("--adapter", type=Path,
                        help="LoRA adapter folder to load on top of the local MLX model")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--cases", type=Path, help="another cases file")
    parser.add_argument("--only", help="run only cases whose id contains this text "
                        "(a partial run is not saved or compared)")
    parser.add_argument("--baseline", type=Path,
                        help="result file to compare with (default: the previous run)")
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--judge-provider", choices=PROVIDERS,
                        help="provider of the judge model, for judged suites (e.g. email_draft)")
    parser.add_argument("--judge-model", help="judge model id (default: the judge provider's main model)")
    parser.add_argument("--min-accuracy", type=float,
                        help="exit 1 when accuracy is below this (0-1)")
    args = parser.parse_args(argv)
    if args.min_accuracy is not None and not 0 <= args.min_accuracy <= 1:
        parser.error("--min-accuracy is a fraction between 0 and 1")

    if args.provider:
        # Set before ``config`` is imported; the per-role keys too, so a
        # saved per-engine override can't redirect the run.
        for key in ("AGENT_PROVIDER", *(f"AGENT_PROVIDER_{r.upper()}" for r in ROLES)):
            os.environ[key] = args.provider

    suite = suites.get(args.suite)
    from config import settings

    adapter = str(args.adapter.expanduser()) if args.adapter else None
    if adapter and not Path(adapter).is_dir():
        parser.error(f"no adapter folder at {adapter}")
    role = suite.role(settings)
    try:
        engine = build_engine(role, settings, args.model, adapter=adapter)
    except ValueError as exc:
        parser.error(str(exc))
    judge = None
    if getattr(suite, "NEEDS_JUDGE", False) or args.judge_provider or args.judge_model:
        if not args.judge_provider and not args.judge_model:
            parser.error(f"--suite {args.suite} is judged: pass --judge-provider (and optionally --judge-model)")
        judge_provider = args.judge_provider or settings.engine_provider("main")
        try:
            judge = build_engine("main", settings, args.judge_model, provider=judge_provider)
            check_distinct(settings.engine_provider(role), engine.model_name,
                           judge_provider, judge.model_name)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"judge: {judge_provider}, {judge.model_name}")
    print(f"{args.suite}: {role} engine, {settings.engine_provider(role)}, "
          f"{engine.model_name}{f' + adapter {adapter}' if adapter else ''}, "
          f"{args.repeats} run(s) per case")

    def progress(entry: dict[str, Any]) -> None:
        ok, n = _ok_count(entry)
        print(f"  {entry['id']}: {ok}/{n}", flush=True)

    if getattr(suite, "NEVER_SAVE", False):
        parser.error(f"--suite {args.suite} runs on your own data and is never saved: "
                     f"use python -m evals.suites.{args.suite}")
    cases = suite.load_cases(args.cases)
    if args.only:
        cases = [c for c in cases if args.only in c["id"]]
        if not cases:
            parser.error(f"no case id contains {args.only!r}")
    result = asyncio.run(evaluate(
        args.suite, provider=settings.engine_provider(role), adapter=adapter,
        repeats=args.repeats, save=False, settings=settings, engine=engine,
        cases=cases, progress=progress, judge=judge,
        judge_provider=(args.judge_provider or settings.engine_provider("main")) if judge else None,
    ))
    print(report(result))
    if args.only:
        return exit_code(result, args.min_accuracy)
    baseline = json.loads(args.baseline.read_text()) if args.baseline else previous_result(args.suite)
    if baseline:
        print("\n".join(compare(baseline, result)))
    if not args.no_save:
        print(f"saved {save(result)}")
    code = exit_code(result, args.min_accuracy)
    if args.min_accuracy is not None and result["summary"]["accuracy"] < args.min_accuracy:
        print(f"FAIL: accuracy {result['summary']['accuracy']:.0%} is below "
              f"--min-accuracy {args.min_accuracy:.0%}")
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
