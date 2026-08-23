#!/usr/bin/env python3
"""Fold one model's benchmark run into the leaderboard.

Re-running a model replaces its existing row; a new model appends one.  The
standings live in ``leaderboard.json`` next to the report, so the table
survives the Markdown report being regenerated from scratch.

Usage::

    python scripts/update_leaderboard.py \
        --input_dir outputs/runs/baseline/glm-5.1 \
        --model glm-5.1 \
        --report outputs/runs/baseline/baseline_report.md
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from owb.eval.report import ReportConfig, generate_report  # noqa: E402


METRIC_GLOSSARY = """\
| Metric | Meaning |
|--------|---------|
| **Success** | Fraction of runs whose goal condition holds in the final state. The headline number. |
| **pass@k** | Fraction of *tasks* solved at least once across their k runs. Compare with Success to separate capability ceiling from consistency: a large gap means the model can do it but not reliably. |
| **SPL** | Success weighted by Path Length: `S · L / max(P, L)`, where `L` is the shortest reference walkthrough and `P` the steps actually taken. Failed runs score 0, so SPL rewards solving tasks *and* solving them directly. Range 0–1. |
| **Step Ratio** | Steps taken divided by the reference walkthrough length, averaged over all runs including failures. 1.00 means optimal; higher means wasted actions. |

Task count and run count are **protocol settings**, not scores — they appear under the table so rows stay comparable. Per-run diagnostics (failed actions, invalid calls) stay in the detail report / `diagnose.json`, not on the leaderboard.
"""


def collect_metrics(input_dir: Path, tasks_dir: Path) -> dict[str, Any]:
    """Compute one leaderboard row from a directory of runs."""
    report = generate_report(
        ReportConfig(input_dir=str(input_dir), tasks_dir=str(tasks_dir), format="json")
    )
    assert isinstance(report, dict)

    rows = report["per_run"]
    by_task = report["by_task"]
    overall = report["overall"]

    solved = sum(1 for s in by_task.values() if s["pass_at_k"])
    return {
        "runs": overall["total"],
        "tasks": len(by_task),
        "success_rate": overall["success_rate"],
        "pass_at_k": round(solved / len(by_task), 3) if by_task else 0.0,
        "mean_spl": overall["mean_spl"],
        "mean_step_ratio": overall["mean_step_ratio"],
        "invalid_calls": sum(r["invalid_calls"] for r in rows),
        "failed_actions": sum(r["failed_actions"] for r in rows),
    }


def upsert(board_path: Path, model: str, metrics: dict, note: str = "") -> list[dict]:
    """Replace this model's entry (or add it) and return the sorted standings."""
    entries: list[dict] = []
    if board_path.exists():
        try:
            entries = json.loads(board_path.read_text(encoding="utf-8")).get("entries", [])
        except json.JSONDecodeError:
            entries = []

    entries = [e for e in entries if e.get("model") != model]
    entries.append({"model": model, "date": date.today().isoformat(), "note": note, **metrics})
    entries.sort(key=lambda e: (-(e.get("success_rate") or 0.0),
                                -(e.get("mean_spl") or 0.0),
                                e.get("model", "")))

    board_path.write_text(
        json.dumps({"entries": entries}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return entries


def _fmt(value: Any, spec: str) -> str:
    return "—" if value is None else format(value, spec)


def _format_protocol(tasks: Any, runs: Any) -> str:
    """Human-readable protocol: total runs, plus per-task when evenly divisible."""
    if not tasks or not runs:
        return f"{tasks} tasks, {runs} runs total"
    if runs % tasks == 0:
        return f"{tasks} tasks × {runs // tasks} runs/task ({runs} total)"
    return f"{tasks} tasks, {runs} runs total"


def _protocol_line(entries: list[dict]) -> list[str]:
    """Render task/run counts as protocol metadata under the score table."""
    if not entries:
        return []

    protocols = {(e.get("tasks"), e.get("runs")) for e in entries}
    if len(protocols) == 1:
        tasks, runs = next(iter(protocols))
        if tasks is None and runs is None:
            return []
        return [
            "",
            f"Protocol: **{_format_protocol(tasks, runs)}** "
            f"(settings shared by all rows above; not a score).",
        ]

    # Mixed protocols — spell out per model so comparisons stay honest.
    lines = ["", "Protocol (per model; not a score):", ""]
    for e in entries:
        lines.append(
            f"- **{e['model']}** — {_format_protocol(e.get('tasks'), e.get('runs'))}"
        )
    return lines


def render_leaderboard(entries: list[dict]) -> str:
    lines = [
        "## Leaderboard",
        "",
        "One row per model, best first. Re-running a model overwrites its row.",
        "",
        "| # | Model | Success | pass@k | SPL | Step Ratio |",
        "|---|-------|---------|--------|-----|------------|",
    ]
    for i, e in enumerate(entries, 1):
        lines.append(
            f"| {i} | **{e['model']}** "
            f"| {_fmt(e.get('success_rate'), '.1%')} | {_fmt(e.get('pass_at_k'), '.1%')} "
            f"| {_fmt(e.get('mean_spl'), '.3f')} | {_fmt(e.get('mean_step_ratio'), '.2f')} |"
        )

    lines += _protocol_line(entries)

    notes = [e for e in entries if e.get("note")]
    if notes:
        lines += ["", "Notes:", ""]
        lines += [f"- **{e['model']}** — {e['note']}" for e in notes]

    lines += ["", "### Metric definitions", "", METRIC_GLOSSARY]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input_dir", required=True, help="directory of runs for this model")
    ap.add_argument("--model", required=True, help="model name as it should appear")
    ap.add_argument("--report", required=True, help="Markdown report to write")
    # Resolved against the repo, not the caller's cwd: task metadata must be
    # findable when the script is invoked from anywhere.
    ap.add_argument("--tasks_dir", default=str(PROJECT / "data" / "tasks"))
    ap.add_argument("--board", default=None,
                    help="leaderboard.json (default: alongside the report)")
    ap.add_argument("--note", default="", help="short caveat shown under the table")
    args = ap.parse_args()

    input_dir = Path(args.input_dir)
    report_path = Path(args.report)
    board_path = Path(args.board) if args.board else report_path.parent / "leaderboard.json"

    if not input_dir.is_dir():
        raise SystemExit(f"No such run directory: {input_dir}")

    metrics = collect_metrics(input_dir, Path(args.tasks_dir))
    if metrics["runs"] == 0:
        raise SystemExit(
            f"No verified runs under {input_dir} — refusing to write an empty row. "
            f"Run `owb verify` on each run directory first."
        )

    entries = upsert(board_path, args.model, metrics, args.note)

    detail = generate_report(
        ReportConfig(input_dir=str(input_dir), tasks_dir=args.tasks_dir, format="markdown")
    )
    assert isinstance(detail, str)
    detail = detail.replace(
        "# OpenWorldSandbox Results",
        f"# OpenWorldSandbox Results\n\nLatest run: **{args.model}**, "
        f"{date.today().isoformat()}. Cross-model standings are in the "
        f"[Leaderboard](#leaderboard) at the end of this file.",
        1,
    )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        f"{detail}\n\n---\n\n{render_leaderboard(entries)}\n", encoding="utf-8"
    )

    print(f"Leaderboard updated: {board_path}")
    print(f"Report written:      {report_path}")
    print(f"  {args.model}: success={_fmt(metrics['success_rate'], '.1%')} "
          f"spl={_fmt(metrics['mean_spl'], '.3f')} runs={metrics['runs']}")


if __name__ == "__main__":
    main()
