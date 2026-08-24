"""Trajectory diagnostics.

Analyses agent trajectories for common failure patterns:
- invalid calls (wrong params, entity not found)
- repeated exploration (same action + same params consecutively)
- state conflicts (operating on closed containers, etc.)
- excessive steps
"""

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class DiagnoseConfig:
    input_dir: str                     # agent run directory with trajectory.json

    def pre_process(self) -> None:
        p = Path(self.input_dir) / "trajectory.json"
        if not p.exists():
            raise FileNotFoundError(f"trajectory.json not found in {self.input_dir}")


# Response fragments that mark a call as unsatisfiable rather than merely failed.
_REFERENCE_ERRORS = ("does not exist", "not in the current area", "Unknown tool")


def _tool_call_key(tc: dict) -> str:
    """Stable key for deduplication: action name + sorted param pairs."""
    name = tc.get("name", "unknown")
    args = tc.get("arguments", {})
    if isinstance(args, dict):
        param_str = json.dumps(args, sort_keys=True, ensure_ascii=False)
    else:
        param_str = str(args)
    return f"{name}:{param_str}"


def _action_status(entry: dict) -> str:
    """Return the outcome of an entry's tool call.

    The agent records a structured ``status`` (``success`` / ``failure`` /
    ``invalid_call`` / ``transport_error``).  Trajectories captured before
    that field existed only carry the response text, so fall back to the
    ``Error:`` prefix the executor prepends to every failure.
    """
    resp = entry.get("tool_response", {})
    status = resp.get("status")
    if status:
        return status
    return "failure" if resp.get("content", "").startswith("Error:") else "success"


def diagnose_trajectory(config: DiagnoseConfig) -> dict[str, Any]:
    """Analyse a trajectory and return diagnostic metrics."""
    with open(Path(config.input_dir) / "trajectory.json", "r") as f:
        traj = json.load(f)

    trajectory = traj.get("trajectory", [])
    max_iterations = traj.get("max_iterations", 30)

    stats: dict[str, Any] = {
        "total_iterations": traj.get("total_iterations", 0),
        "max_iterations": max_iterations,
        "tool_calls": 0,
        "failed_actions": 0,
        "invalid_calls": 0,
        "consecutive_repeats": 0,        # same action + same params, back-to-back
        "transport_errors": 0,
        "action_counts": Counter(),
        "area_visits": Counter(),
        "termination_type": "unknown",
    }

    prev_key: str | None = None

    for entry in trajectory:
        for tc in entry.get("tool_calls", []):
            stats["tool_calls"] += 1
            name = tc.get("name", "unknown")
            stats["action_counts"][name] += 1

            # Structured failure detection
            status = _action_status(entry)
            if status != "success":
                stats["failed_actions"] += 1
                content = entry.get("tool_response", {}).get("content", "")
                # invalid = the call could never have worked: unknown tool, or
                # a reference to an entity the robot cannot act on.
                if status == "invalid_call" or any(m in content for m in _REFERENCE_ERRORS):
                    stats["invalid_calls"] += 1
                if status == "transport_error":
                    stats["transport_errors"] += 1

            # Consecutive identical calls (stuck-in-place detection)
            key = _tool_call_key(tc)
            if key == prev_key:
                stats["consecutive_repeats"] += 1
            prev_key = key

            # Track area visits
            if name == "move_to":
                area = tc.get("arguments", {}).get("area_id", "unknown")
                stats["area_visits"][area] += 1

            # Track termination
            if name in ("finish_task", "abandon_task", "report_unable_to_continue", "report_target_absent"):
                stats["termination_type"] = name

    # Failure rate
    if stats["tool_calls"] > 0:
        stats["failure_rate"] = stats["failed_actions"] / stats["tool_calls"]
    else:
        stats["failure_rate"] = 0.0

    # Serialise Counters for JSON output
    stats["action_counts"] = dict(stats["action_counts"].most_common())
    stats["area_visits"] = dict(stats["area_visits"].most_common())

    return stats


def run(config: DiagnoseConfig) -> None:
    config.pre_process()
    stats = diagnose_trajectory(config)
    print(json.dumps(stats, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    from simpleArgParser import parse_args
    cfg: DiagnoseConfig = parse_args(DiagnoseConfig)
    run(cfg)