"""Capability-tag aggregation report with SPL and step-ratio metrics.

Scans run directories, reads verify.json + diagnose output (or generates it
on the fly), and produces three views:

* per_task       — one row per task×run with SPL, step ratio, subgoals, failure mode
* by_capability  — success rate per tag
* overall        — aggregate across all runs

Task metadata (capability tags, optimal step count) is read from the
``task.meta.json`` the runner copies into each run directory.  ``tasks_dir``
is only consulted as a fallback for runs produced before that sidecar
existed.

Output formats: JSON (default) and Markdown table (``--format markdown``).
"""

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from loguru import logger


@dataclass
class ReportConfig:
    input_dir: str                     # directory containing multiple run subdirectories
    tasks_dir: str = "data/tasks"      # fallback source for task metadata
    format: str = "json"               # "json" | "markdown"

    def pre_process(self) -> None:
        if not Path(self.input_dir).is_dir():
            raise FileNotFoundError(f"Input directory not found: {self.input_dir}")
        if self.format not in ("json", "markdown"):
            raise ValueError(f"Unknown format: {self.format!r} (use json|markdown)")


def _read_json(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _load_task_fallback(
    tasks_dir: Path,
) -> tuple[dict[str, list[str]], dict[str, int]]:
    """Return (task_tags, walkthrough_min_steps) scanned from task JSONs.

    Only used for run directories that lack a ``task.meta.json`` sidecar.
    """
    task_tags: dict[str, list[str]] = {}
    walkthrough_min: dict[str, int] = {}

    if not tasks_dir.is_dir():
        return task_tags, walkthrough_min

    for task_file in tasks_dir.rglob("*.json"):
        data = _read_json(task_file)
        if not data:
            continue
        tid = data.get("task_id")
        if not tid:
            continue
        task_tags[tid] = data.get("capability_tags", [])
        steps = [len(w["actions"]) for w in data.get("walkthroughs", []) if w.get("actions")]
        if steps:
            walkthrough_min[tid] = min(steps)

    return task_tags, walkthrough_min


def _load_diagnose(run_dir: Path) -> dict:
    """Read diagnose.json, or compute diagnostics on the fly from the trajectory."""
    cached = _read_json(run_dir / "diagnose.json")
    if cached is not None:
        return cached

    from ows.eval.diagnose import DiagnoseConfig, diagnose_trajectory
    try:
        return diagnose_trajectory(DiagnoseConfig(input_dir=str(run_dir)))
    except Exception:
        return {}


def _make_per_task_rows(
    input_dir: Path,
    fallback_tags: dict[str, list[str]],
    fallback_steps: dict[str, int],
) -> list[dict[str, Any]]:
    """Iterate run directories, load verify + diagnose, compute per-run metrics."""
    rows: list[dict[str, Any]] = []

    for run_dir in sorted(input_dir.iterdir()):
        if not run_dir.is_dir():
            continue

        verify = _read_json(run_dir / "verify.json")
        if verify is None:
            continue

        details = verify.get("details", {})
        meta = _read_json(run_dir / "task.meta.json") or {}
        task_id = meta.get("task_id") or details.get("task_id", "unknown")

        subgoals = verify.get("subgoals", {})
        goal_ok = verify.get("goal_satisfied", False)

        traj = _read_json(run_dir / "trajectory.json") or {}
        actual_steps = traj.get("total_iterations", 0)

        diagnose = _load_diagnose(run_dir)

        # Optimal path length: the shortest reference walkthrough.  When it is
        # unknown, SPL is left undefined rather than silently scored as 1.0.
        optimal = meta.get("walkthrough_min_steps") or fallback_steps.get(task_id)
        if optimal and actual_steps > 0:
            spl = round((1.0 if goal_ok else 0.0) * optimal / max(actual_steps, optimal), 3)
            step_ratio = round(actual_steps / optimal, 2)
        else:
            spl = None
            step_ratio = None

        rows.append({
            "task_id": task_id,
            "scenario_id": meta.get("scenario_id") or details.get("scenario_id", "unknown"),
            "capability_tags": meta.get("capability_tags") or fallback_tags.get(task_id, []),
            "result": verify.get("result", "incomplete"),
            "goal_satisfied": goal_ok,
            "actual_steps": actual_steps,
            "max_steps": meta.get("max_steps") or details.get("max_steps", 0),
            "walkthrough_min_steps": optimal,
            "step_ratio": step_ratio,
            "spl": spl,
            "subgoal_hit": sum(1 for v in subgoals.values() if v),
            "subgoal_total": len(subgoals),
            "failed_actions": diagnose.get("failed_actions", 0),
            "consecutive_repeats": diagnose.get("consecutive_repeats", 0),
            "invalid_calls": diagnose.get("invalid_calls", 0),
            "termination_type": diagnose.get("termination_type", "unknown"),
            "run_dir": str(run_dir),
        })

    return rows


def _summarise(rows: list[dict]) -> dict[str, Any]:
    """Aggregate a group of runs.  SPL averages skip rows where it is undefined."""
    n = len(rows)
    if n == 0:
        return {"total": 0, "complete": 0, "success_rate": 0.0,
                "mean_spl": None, "mean_step_ratio": None, "spl_coverage": 0}

    complete = sum(1 for r in rows if r["goal_satisfied"])
    spls = [r["spl"] for r in rows if r["spl"] is not None]
    ratios = [r["step_ratio"] for r in rows if r["step_ratio"] is not None]

    return {
        "total": n,
        "complete": complete,
        "success_rate": round(complete / n, 3),
        "mean_spl": round(mean(spls), 3) if spls else None,
        "mean_step_ratio": round(mean(ratios), 2) if ratios else None,
        "spl_coverage": len(spls),
    }


def _aggregate_by_capability(rows: list[dict]) -> dict[str, dict[str, Any]]:
    tag_rows: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        for tag in row["capability_tags"] or ["untagged"]:
            tag_rows[tag].append(row)
    return {tag: _summarise(rlist) for tag, rlist in sorted(tag_rows.items())}


def _aggregate_by_task(rows: list[dict]) -> dict[str, dict[str, Any]]:
    """Group repeated runs of the same task: mean/std SPL and pass@k."""
    task_rows: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        task_rows[row["task_id"]].append(row)

    out: dict[str, dict[str, Any]] = {}
    for tid, rlist in sorted(task_rows.items()):
        summary = _summarise(rlist)
        spls = [r["spl"] for r in rlist if r["spl"] is not None]
        summary["runs"] = len(rlist)
        if not spls:
            summary["std_spl"] = None
        else:
            summary["std_spl"] = round(pstdev(spls), 3) if len(spls) > 1 else 0.0
        summary["pass_at_k"] = any(r["goal_satisfied"] for r in rlist)
        out[tid] = summary
    return out


def _fmt(value: Any, spec: str = "") -> str:
    """Format a metric, rendering undefined values as an em dash."""
    if value is None:
        return "—"
    return format(value, spec) if spec else str(value)


def _format_markdown(
    rows: list[dict],
    by_task: dict[str, dict],
    by_cap: dict[str, dict],
    overall_stats: dict,
) -> str:
    lines: list[str] = ["# OpenWorldSandbox Results", ""]

    lines += ["## Overall", "",
              "| runs | success_rate | mean_spl | mean_step_ratio |",
              "| --- | --- | --- | --- |",
              f"| {overall_stats['total']} | {_fmt(overall_stats['success_rate'], '.1%')} "
              f"| {_fmt(overall_stats['mean_spl'], '.3f')} "
              f"| {_fmt(overall_stats['mean_step_ratio'], '.2f')} |", ""]

    lines += ["## By Task", "",
              "| task_id | runs | success_rate | pass@k | mean_spl | std_spl |",
              "|" + "---|" * 6]
    for tid, s in by_task.items():
        lines.append(
            f"| {tid} | {s['runs']} | {_fmt(s['success_rate'], '.1%')} "
            f"| {'yes' if s['pass_at_k'] else 'no'} "
            f"| {_fmt(s['mean_spl'], '.3f')} | {_fmt(s['std_spl'], '.3f')} |"
        )
    lines.append("")

    lines += ["## By Capability", "",
              "| capability | runs | complete | success_rate | mean_spl | mean_ratio |",
              "|" + "---|" * 6]
    for tag, s in by_cap.items():
        lines.append(
            f"| {tag} | {s['total']} | {s['complete']} "
            f"| {_fmt(s['success_rate'], '.1%')} | {_fmt(s['mean_spl'], '.3f')} "
            f"| {_fmt(s['mean_step_ratio'], '.2f')} |"
        )
    lines.append("")

    lines += ["## Per Run", "",
              "| task_id | result | steps | SPL | ratio | subgoals | fails | repeats | invalid |",
              "|" + "---|" * 9]
    for row in rows:
        lines.append(
            f"| {row['task_id']} | {row['result']} "
            f"| {row['actual_steps']}/{row['max_steps']} "
            f"| {_fmt(row['spl'], '.3f')} | {_fmt(row['step_ratio'], '.2f')} "
            f"| {row['subgoal_hit']}/{row['subgoal_total']} | {row['failed_actions']} "
            f"| {row['consecutive_repeats']} | {row['invalid_calls']} |"
        )

    return "\n".join(lines)


def generate_report(config: ReportConfig) -> dict[str, Any] | str:
    """Generate the full evaluation report."""
    tasks_dir = Path(config.tasks_dir)
    fallback_tags, fallback_steps = _load_task_fallback(tasks_dir)
    rows = _make_per_task_rows(Path(config.input_dir), fallback_tags, fallback_steps)

    # A run's sidecar is a snapshot: runs recorded before the compiler emitted
    # walkthrough_min_steps can only be resolved through tasks_dir, which is
    # relative by default and so comes up empty outside the repository root.
    # Say why the metric is missing instead of just printing dashes.
    unresolved = sum(1 for r in rows if r["walkthrough_min_steps"] is None)
    if unresolved and not fallback_steps:
        logger.warning(
            f"{unresolved} of {len(rows)} run(s) have no reference walkthrough "
            f"length in their task.meta.json, and no task definitions were found "
            f"under '{tasks_dir.resolve()}' — SPL and step ratio are omitted for "
            f"them. Point --tasks_dir at the task JSONs (absolute path) to fill "
            f"them in."
        )

    by_task = _aggregate_by_task(rows)
    by_cap = _aggregate_by_capability(rows)
    overall_stats = _summarise(rows)

    if config.format == "markdown":
        return _format_markdown(rows, by_task, by_cap, overall_stats)

    return {
        "overall": overall_stats,
        "by_task": by_task,
        "by_capability": by_cap,
        "per_run": rows,
    }


def run(config: ReportConfig) -> None:
    config.pre_process()
    report = generate_report(config)
    if isinstance(report, str):
        print(report)
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    from simpleArgParser import parse_args
    cfg: ReportConfig = parse_args(ReportConfig)
    run(cfg)
