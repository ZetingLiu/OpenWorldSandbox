"""DSL-based task verification.

Evaluates the goal condition against the final database state to
determine task completion.  No LLM judge — purely programmatic.
"""

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from loguru import logger

from ows.env.world import WorldState
from ows.schema.goal_dsl import evaluate_goal, evaluate_subgoal, SubgoalTracker
from ows.schema.scenario import Scenario
from ows.schema.task import Task


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class TaskResult(str, Enum):
    complete = "complete"               # all goal conditions met
    partial = "partial"                 # some subgoals met, goal not met
    incomplete = "incomplete"           # no subgoals met
    error = "error"                     # agent error / invalid termination
    exceeded = "exceeded"               # max steps exceeded
    abandoned = "abandoned"             # agent abandoned task


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class VerifyConfig:
    input_dir: str                      # agent run directory with trajectory.json, initial.db, final.db
    task_path: str | None = None        # path to task JSON (optional; auto-detected from trajectory)
    tasks_dir: str = "data/tasks"       # base directory for auto-detection

    def pre_process(self) -> None:
        if not os.path.isdir(self.input_dir):
            raise FileNotFoundError(f"Run directory not found: {self.input_dir}")
        traj = os.path.join(self.input_dir, "trajectory.json")
        if not os.path.exists(traj):
            raise FileNotFoundError(f"trajectory.json not found in {self.input_dir}")


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------

def verify_run(config: VerifyConfig) -> dict[str, Any]:
    """Verify a single agent run.

    Returns
    -------
    dict
        Keys: result, subgoals, goal_satisfied, details
    """
    run_dir = Path(config.input_dir)

    # Load trajectory
    with open(run_dir / "trajectory.json", "r") as f:
        traj = json.load(f)

    # Load task
    task = _load_task(config, traj)

    # Load final DB state
    final_db = run_dir / "final.db"
    if not final_db.exists():
        return {
            "result": TaskResult.error.value,
            "subgoals": {},
            "goal_satisfied": False,
            "details": {"error": "final.db not found"},
        }

    ws = WorldState(str(final_db))
    snapshot = ws.snapshot()
    ws.close()

    # Evaluate goal
    goal_satisfied = evaluate_goal(task.goal, snapshot)

    # Evaluate subgoals (latch-based from final state — but we also
    # replay the trajectory to get proper latching)
    subgoal_results = _evaluate_subgoals_from_trajectory(task, config)

    # Determine result
    result = _classify_result(goal_satisfied, subgoal_results, traj, task)

    report = {
        "result": result,
        "subgoals": subgoal_results,
        "goal_satisfied": goal_satisfied,
        "details": {
            "task_id": task.task_id,
            "scenario_id": task.scenario_id,
            "total_iterations": traj.get("total_iterations", 0),
            "max_steps": task.max_steps,
        },
    }

    # Save result
    with open(run_dir / "verify.json", "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    logger.info(f"Verification result: {result}")
    return report


def _load_task(config: VerifyConfig, traj: dict) -> Task:
    """Load the task definition, trying multiple strategies."""
    # Try explicit path
    if config.task_path and os.path.exists(config.task_path):
        from ows.env.compile import load_task
        return load_task(config.task_path)

    # Try to find from trajectory metadata (search recursively — task files
    # are organised by scene family, e.g. data/tasks/home/<task_id>.json)
    task_id = traj.get("task_id")
    if task_id:
        from ows.env.compile import load_task
        for candidate in Path(config.tasks_dir).rglob(f"{task_id}.json"):
            return load_task(str(candidate))

    raise FileNotFoundError(
        "Cannot locate task JSON. Provide --task_path or ensure trajectory has task_id"
    )


def _evaluate_subgoals_from_trajectory(task: Task, config: VerifyConfig) -> dict[str, bool]:
    """Replay subgoal evaluation through the trajectory for proper latching."""
    run_dir = Path(config.input_dir)

    # Load initial DB
    initial_db = run_dir / "initial.db"
    if not initial_db.exists():
        # Without initial DB, evaluate from final state only
        final_db = run_dir / "final.db"
        if final_db.exists():
            ws = WorldState(str(final_db))
            snapshot = ws.snapshot()
            ws.close()
            return {
                sg.id: evaluate_subgoal(sg.cond, snapshot)
                for sg in task.subgoals
            }
        return {sg.id: False for sg in task.subgoals}

    # Replay trajectory
    try:
        with open(run_dir / "trajectory.json", "r") as f:
            traj = json.load(f)
    except Exception:
        return {sg.id: False for sg in task.subgoals}

    # Replay on a throw-away COPY so the initial.db snapshot stays pristine
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        replay_db = os.path.join(tmp, "replay.db")
        shutil.copy2(initial_db, replay_db)

        ws = WorldState(replay_db)
        tracker = SubgoalTracker([sg.model_dump() for sg in task.subgoals])

        from ows.env.actions import execute_action

        for entry in traj.get("trajectory", []):
            tool_response = entry.get("tool_response") or {}
            if tool_response.get("status") != "success":
                continue
            for tc in entry.get("tool_calls", []):
                try:
                    execute_action(ws, tc["name"], tc.get("arguments", {}))
                    tracker.update(ws.snapshot())
                except Exception:
                    pass  # skip failed actions during replay

        ws.close()
    return tracker.summary()


def _classify_result(
    goal_satisfied: bool,
    subgoals: dict[str, bool],
    traj: dict,
    task: Task,
) -> str:
    """Classify the task result.

    The DB final state is authoritative: if the goal is satisfied the
    task is complete regardless of how the agent terminated.  Otherwise
    the termination decision determines the failure mode:

    - abandon_task / report_unable_to_continue  → abandoned
    - finish_task / report_target_absent (wrong claim) → error
    - hit max steps without terminating          → exceeded
    - some subgoals latched                      → partial
    - nothing achieved                           → incomplete
    """
    if goal_satisfied:
        return TaskResult.complete.value

    total_iterations = traj.get("total_iterations", 0)

    termination = None
    for entry in traj.get("trajectory", []):
        for tc in entry.get("tool_calls", []):
            name = tc.get("name")
            if name in (
                "finish_task", "abandon_task",
                "report_unable_to_continue", "report_target_absent",
            ):
                termination = name

    if termination in ("abandon_task", "report_unable_to_continue"):
        return TaskResult.abandoned.value
    if termination in ("finish_task", "report_target_absent"):
        # claimed completion / absence, but the goal is not satisfied
        return TaskResult.error.value

    if total_iterations >= task.max_steps:
        return TaskResult.exceeded.value

    if any(subgoals.values()):
        return TaskResult.partial.value

    return TaskResult.incomplete.value


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run(config: VerifyConfig) -> None:
    config.pre_process()
    report = verify_run(config)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    from simpleArgParser import parse_args
    cfg: VerifyConfig = parse_args(VerifyConfig)
    run(cfg)
