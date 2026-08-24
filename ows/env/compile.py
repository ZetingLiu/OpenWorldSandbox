"""Scenario + task JSON → SQLite compiler with walkthrough replay validation.

This module:

1. Loads scenario JSON and task JSON
2. Validates against the Pydantic schema models
3. Creates an initial SQLite world snapshot
4. Replays each walkthrough through the action executors
5. Verifies that every step succeeds and the goal is satisfied
6. Writes the compiled snapshot

Mirrors the spec in data/tasks/README.md §"任务可解性校验".
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from ows.schema.scenario import Scenario
from ows.schema.task import Task
from ows.schema.goal_dsl import evaluate_goal, evaluate_subgoal, SubgoalTracker
from ows.env.world import WorldState, save_snapshot


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------

def load_scenario(path: str | Path) -> Scenario:
    """Load and validate a scenario JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    scenario = Scenario.model_validate(data)
    _validate_scenario(scenario)
    return scenario


def load_task(path: str | Path) -> Task:
    """Load and validate a task JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return Task.model_validate(data)


# ---------------------------------------------------------------------------
# Scenario validation
# ---------------------------------------------------------------------------

def _validate_scenario(scenario: Scenario) -> None:
    """Run the 8 validation rules (S1-S8) from data/scenarios/README.md."""
    all_entities = scenario.all_entities
    entity_ids = set(all_entities.keys())

    # S1: entity IDs globally unique — enforced by dict key uniqueness
    # S2: entity ID references are valid
    for eid, e in all_entities.items():
        if e.on is not None and e.on not in entity_ids:
            raise ValueError(f"S2: entity {eid} references unknown surface {e.on}")
        if e.in_ is not None and e.in_ not in entity_ids:
            raise ValueError(f"S2: entity {eid} references unknown container {e.in_}")

    # S3: placement relations are closed
    for eid, e in all_entities.items():
        if e.on is not None:
            target = all_entities[e.on]
            if "can_support" not in target.properties:
                raise ValueError(
                    f"S3: entity {eid} placed on {e.on} which lacks can_support"
                )
        if e.in_ is not None:
            target = all_entities[e.in_]
            if "can_contain" not in target.properties:
                raise ValueError(
                    f"S3: entity {eid} placed in {e.in_} which lacks can_contain"
                )
    # cycle check
    for eid in entity_ids:
        visited: set[str] = set()
        cur = eid
        while cur is not None:
            if cur in visited:
                raise ValueError(f"S3: reference cycle detected involving {eid}")
            visited.add(cur)
            e = all_entities.get(cur)
            if e is None:
                break
            cur = e.in_ or e.on

    # S4: adjacency graph is connected
    if scenario.areas:
        graph = scenario.passable_graph
        start = scenario.areas[0].id
        visited_areas: set[str] = set()
        stack = [start]
        while stack:
            a = stack.pop()
            if a in visited_areas:
                continue
            visited_areas.add(a)
            for nb in graph.get(a, set()):
                if nb not in visited_areas:
                    stack.append(nb)
        area_ids = {a.id for a in scenario.areas}
        if visited_areas != area_ids:
            raise ValueError(
                f"S4: adjacency graph not fully connected; unreachable: {area_ids - visited_areas}"
            )

    # S5: robot initial location is valid
    if scenario.robot.location not in {a.id for a in scenario.areas}:
        raise ValueError(
            f"S5: robot location {scenario.robot.location} not in areas"
        )

    # S6: area adjacency references are valid
    area_ids = {a.id for a in scenario.areas}
    for adj in scenario.area_adjacency:
        if adj.from_ not in area_ids:
            raise ValueError(f"S6: adjacency from {adj.from_} not in areas")
        if adj.to not in area_ids:
            raise ValueError(f"S6: adjacency to {adj.to} not in areas")

    # S7: states values are JSON basic types
    for eid, e in all_entities.items():
        for key, val in e.states.items():
            if not isinstance(val, (str, int, float, bool, type(None))):
                raise ValueError(
                    f"S7: entity {eid} states.{key} has non-basic type {type(val).__name__}"
                )

    # S8: open_state semantics — no check needed at compile time; observed at runtime


# ---------------------------------------------------------------------------
# Compile
# ---------------------------------------------------------------------------

def compile_scenario_task(
    scenario_path: str | Path,
    task_path: str | Path,
    output_dir: str | Path,
    *,
    run_walkthrough: bool = True,
) -> dict[str, Any]:
    """Compile a scenario + task pair into an initial SQLite snapshot.

    Parameters
    ----------
    scenario_path : Path
        Path to the scenario JSON file.
    task_path : Path
        Path to the task JSON file.
    output_dir : Path
        Directory to write the compiled ``initial.db``.
    run_walkthrough : bool
        If True, replay walkthroughs to verify solvability.

    Returns
    -------
    dict
        Compilation report with keys:
        - ``solvable``: bool
        - ``walkthrough_results``: list of per-walkthrough dicts
        - ``initial_db``: path to the saved snapshot
        - ``errors``: list of error strings
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load
    scenario = load_scenario(scenario_path)
    task = load_task(task_path)

    if task.scenario_id != scenario.scenario_id:
        raise ValueError(
            f"Task scenario_id {task.scenario_id} != scenario {scenario.scenario_id}"
        )

    report: dict[str, Any] = {
        "scenario_id": scenario.scenario_id,
        "task_id": task.task_id,
        "solvable": False,
        "walkthrough_results": [],
        "initial_db": "",
        "errors": [],
    }

    # Create initial world state
    scenario_dict = json.loads(scenario.model_dump_json(by_alias=True))
    task_dict = json.loads(task.model_dump_json(by_alias=True))

    ws = WorldState()
    ws.initialise_schema()
    ws.populate_from_scenario(scenario_dict, task_dict)

    # Check: initial snapshot must NOT satisfy the goal
    initial_snapshot = ws.snapshot()
    if evaluate_goal(task.goal, initial_snapshot):
        report["errors"].append(
            "Goal is already satisfied in the initial state — task is trivially complete"
        )
        ws.close()
        return report

    if not run_walkthrough:
        db_path = str(output_dir / f"{task.task_id}.db")
        save_snapshot(ws, db_path)
        report["initial_db"] = db_path
        report["solvable"] = True  # assumed (user opted out of replay)
        _write_task_meta(task, output_dir)
        ws.close()
        return report

    # Walkthrough replay
    from ows.env.actions import execute_action  # deferred import to avoid circular

    all_walkthroughs_ok = True

    for wi, walkthrough in enumerate(task.walkthroughs):
        # Step budget: EACH walkthrough must fit within max_steps
        if len(walkthrough.actions) >= task.max_steps:
            all_walkthroughs_ok = False
            report["errors"].append(
                f"Walkthrough[{wi}] has {len(walkthrough.actions)} steps "
                f">= max_steps {task.max_steps}"
            )
            continue

        # Reset to initial state for each walkthrough
        ws2 = WorldState()
        ws2.initialise_schema()
        ws2.populate_from_scenario(scenario_dict, task_dict)

        tracker = SubgoalTracker(
            [sg.model_dump() for sg in task.subgoals]
        )

        w_result = {"index": wi, "description": walkthrough.description, "steps": [], "ok": True}

        for si, wa in enumerate(walkthrough.actions):
            params = wa.params
            result = execute_action(ws2, wa.action, params)

            w_result["steps"].append({
                "step": si,
                "action": wa.action,
                "params": params,
                "status": result.status,
                "failure_reason": result.failure_reason,
            })

            if result.status != "success":
                w_result["ok"] = False
                all_walkthroughs_ok = False
                report["errors"].append(
                    f"Walkthrough[{wi}] step {si} '{wa.action}' failed: {result.failure_reason}"
                )
                break

            # Update subgoal tracker
            tracker.update(ws2.snapshot())

        if w_result["ok"]:
            # Check goal satisfaction
            final_snapshot = ws2.snapshot()
            if not evaluate_goal(task.goal, final_snapshot):
                w_result["ok"] = False
                all_walkthroughs_ok = False
                report["errors"].append(
                    f"Walkthrough[{wi}] finished but goal is not satisfied"
                )

        report["walkthrough_results"].append(w_result)
        ws2.close()

    ws.close()

    # Final verdict
    if all_walkthroughs_ok and not report["errors"]:
        report["solvable"] = True
        # Re-create clean snapshot for the output
        db_path = output_dir / f"{task.task_id}.db"
        if db_path.exists():
            db_path.unlink()
        ws_final = WorldState(str(db_path))
        ws_final.initialise_schema()
        ws_final.populate_from_scenario(scenario_dict, task_dict)
        report["initial_db"] = str(db_path)
        ws_final.close()
        _write_task_meta(task, output_dir)
    else:
        report["solvable"] = False

    return report


def _write_task_meta(task: Task, output_dir: Path) -> None:
    """Write a sidecar meta JSON next to the compiled DB so that the runner
    and verifier can recover the task definition (instruction, max_steps, …)."""
    walkthrough_steps = [
        len(w.actions) for w in task.walkthroughs
    ]
    meta = {
        "task_id": task.task_id,
        "scenario_id": task.scenario_id,
        "name": task.name,
        "instruction": task.instruction,
        "task_type": task.task_type.value,
        "capability_tags": [t.value for t in task.capability_tags],
        "max_steps": task.max_steps,
        "walkthrough_min_steps": min(walkthrough_steps) if walkthrough_steps else None,
        "goal": task.goal,
        "subgoals": [sg.model_dump() for sg in task.subgoals],
    }
    meta_path = output_dir / f"{task.task_id}.meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Batch compile
# ---------------------------------------------------------------------------

def compile_all(
    scenarios_dir: str | Path,
    tasks_dir: str | Path,
    output_dir: str | Path,
) -> list[dict]:
    """Compile all task files in tasks_dir, matching scenarios from scenarios_dir."""
    scenarios_dir = Path(scenarios_dir)
    tasks_dir = Path(tasks_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load all scenarios into a lookup
    scenarios: dict[str, Path] = {}
    for f in scenarios_dir.rglob("*.json"):
        try:
            sc = load_scenario(f)
            scenarios[sc.scenario_id] = f
        except Exception as e:
            logger.warning(f"Failed to load scenario {f}: {e}")

    reports = []
    for f in sorted(tasks_dir.rglob("*.json")):
        try:
            task = load_task(f)
            sc_path = scenarios.get(task.scenario_id)
            if sc_path is None:
                logger.error(f"No scenario found for {task.scenario_id} (task {f})")
                continue

            logger.info(f"Compiling {task.task_id}...")
            report = compile_scenario_task(sc_path, f, output_dir)
            reports.append(report)

            if report["solvable"]:
                logger.success(f"  {task.task_id}: SOLVABLE")
            else:
                logger.error(f"  {task.task_id}: NOT SOLVABLE")
                for err in report["errors"]:
                    logger.error(f"    {err}")

        except Exception as e:
            logger.error(f"Failed to compile {f}: {e}")

    return reports


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

@dataclass
class CompileConfig:
    # Single mode: compile one scenario+task pair
    scenario: str | None = None        # path to scenario JSON
    task: str | None = None            # path to task JSON
    # Batch mode: compile every task under tasks_dir
    batch: bool = False
    scenarios_dir: str = "data/scenarios"
    tasks_dir: str = "data/tasks"
    # Output
    output_dir: str = "outputs/compiled"

    def pre_process(self) -> None:
        if not self.batch and (not self.scenario or not self.task):
            raise ValueError(
                "Provide --scenario and --task, or use --batch with "
                "--scenarios_dir/--tasks_dir"
            )


def run(config: CompileConfig) -> None:
    config.pre_process()
    if config.batch:
        reports = compile_all(config.scenarios_dir, config.tasks_dir, config.output_dir)
    else:
        reports = [
            compile_scenario_task(config.scenario, config.task, config.output_dir)
        ]
    print(json.dumps(reports, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    from simpleArgParser import parse_args
    cfg: CompileConfig = parse_args(CompileConfig)
    run(cfg)