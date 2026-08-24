"""Interactive sandbox REPL for OpenWorldSandbox.

Usage:  ows sandbox --db_path outputs/compiled/home_01_umbrella_move.db

At the prompt, type action names followed by key=value parameters::

    > observe_scene
    > move_to area_id=bedroom
    > pick_object entity_id=umbrella_01

Meta-commands (prefixed with ``.``)::

    .state              print the full world snapshot
    .goal               show goal satisfaction against the compiled task
    .reset              restore the initial database snapshot
    .history            show action history
    .help               list available actions
    .quit / .exit       leave the REPL

The compiled database is never modified: all actions run against a
throw-away copy that is discarded when the REPL exits.
"""

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ows.env.world import WorldState
from ows.env.actions import execute_action, get_available_actions
from ows.env.observe import generate_observation, generate_entity_detail


@dataclass
class SandboxConfig:
    db_path: str                   # path to compiled SQLite snapshot
    task_meta_path: str | None = None

    def pre_process(self) -> None:
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(f"Database not found: {self.db_path}")
        if self.task_meta_path is None:
            candidate = Path(self.db_path).with_suffix(".meta.json")
            if candidate.exists():
                self.task_meta_path = str(candidate)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_params(tokens: list[str]) -> dict:
    """Parse key=value tokens into a dict.  Values that look like
    integers or floats are converted; bare ``true``/``false`` become bools;
    everything else stays a string."""
    params: dict = {}
    for tok in tokens:
        if "=" not in tok:
            continue
        k, v = tok.split("=", 1)
        if v.lower() == "true":
            params[k] = True
        elif v.lower() == "false":
            params[k] = False
        elif v.lstrip("-").isdigit():
            params[k] = int(v)
        else:
            try:
                params[k] = float(v)
            except ValueError:
                params[k] = v
    return params


def _print_state(ws: WorldState) -> None:
    """Pretty-print the current world snapshot."""
    snap = ws.snapshot()
    robot = snap["robot"]
    print(f"\n  Robot @ {robot['location_id']}")
    print(f"    left_hand:  {robot.get('left_hand') or 'empty'}")
    print(f"    right_hand: {robot.get('right_hand') or 'empty'}")

    print("\n  Entities:")
    for eid, ent in sorted(snap["entities"].items()):
        loc = ent.get("area_id", "?")
        in_cont = f" in {ent['container_id']}" if ent.get("container_id") else ""
        on_surf = f" on {ent['on_id']}" if ent.get("on_id") else ""
        held = f" held={ent['held_by']}" if ent.get("held_by") else ""
        states = ent.get("states", {})
        state_str = f" {json.dumps(states, ensure_ascii=False)}" if states else ""
        open_s = f" [{ent['open_state']}]" if ent.get("open_state") else ""
        device_s = f" [{ent['device_state']}]" if ent.get("device_state") else ""
        print(f"    {eid} ({ent['class']}): {ent['name']}"
              f" @{loc}{in_cont}{on_surf}{held}{open_s}{device_s}{state_str}")

    print(f"\n  Action count: {ws.get_action_count()}")


def _print_goal(ws: WorldState, meta: dict | None) -> None:
    """Evaluate and print the current goal / subgoal status."""
    if meta is None:
        print("  (no task meta loaded — run with a compiled task for goal info)")
        return
    from ows.schema.goal_dsl import evaluate_goal, evaluate_subgoal

    snap = ws.snapshot()
    goal_ok = evaluate_goal(meta["goal"], snap)
    print(f"\n  Goal: {'SATISFIED' if goal_ok else 'not satisfied'}")
    for sg in meta.get("subgoals", []):
        ok = evaluate_subgoal(sg["cond"], snap)
        print(f"    [{'x' if ok else ' '}] {sg['id']}: {sg['description']}")


def _print_history(ws: WorldState) -> None:
    """Print action history from the database."""
    rows = ws.conn.execute(
        "SELECT step_id, action, params_json, status, failure_reason "
        "FROM action_history ORDER BY step_id"
    ).fetchall()
    if not rows:
        print("  (no actions yet)")
        return
    print()
    for row in rows:
        params = json.loads(row["params_json"])
        params_str = ", ".join(f"{k}={v}" for k, v in params.items())
        failure_reason = row["failure_reason"]
        reason = f" — {failure_reason}" if failure_reason else ""
        marker = "ok  " if row["status"] == "success" else "FAIL"
        print(f"  {row['step_id']:3d} {marker} {row['action']}({params_str}){reason}")


# Actions after which the full scene description is the useful output.
_SCENE_ACTIONS = frozenset({"observe_scene", "move_to"})


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

def run_sandbox(config: SandboxConfig) -> None:
    """Run the interactive sandbox REPL against a disposable copy of the DB."""
    config.pre_process()

    meta: dict | None = None
    if config.task_meta_path:
        with open(config.task_meta_path, encoding="utf-8") as f:
            meta = json.load(f)

    actions = get_available_actions()
    action_names = ", ".join(sorted(actions))

    # The compiled snapshot stays read-only; the REPL drives a working copy.
    work_root = tempfile.mkdtemp(prefix="owb_sandbox_")
    work_db = os.path.join(work_root, "world.db")
    shutil.copy2(config.db_path, work_db)
    ws = WorldState(work_db)

    print("=" * 60)
    print("OpenWorldSandbox Interactive Sandbox")
    print("=" * 60)
    if meta:
        print(f"Task:  {meta['name']} ({meta['task_id']})")
        print(f"Goal:  {meta['instruction']}")
    print(f"DB:    {config.db_path} (read-only; working copy in {work_root})")
    print("Type '.help' for commands, '.quit' to exit.")
    print()
    print(generate_observation(ws))

    try:
        while True:
            try:
                raw = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not raw:
                continue

            # --- meta-commands ---
            if raw.startswith("."):
                cmd = raw[1:].strip().lower()
                if cmd in ("quit", "exit", "q"):
                    break
                elif cmd == "help":
                    print(f"\n  Actions: {action_names}")
                    print("\n  Meta-commands:")
                    print("    .state    print full world snapshot")
                    print("    .goal     show goal / subgoal status")
                    print("    .reset    restore initial DB snapshot")
                    print("    .history  show action history")
                    print("    .help     this message")
                    print("    .quit     exit")
                    print("\n  Usage:  <action> [key=value ...]")
                    print("  Example: pick_object entity_id=umbrella_01")
                elif cmd == "state":
                    _print_state(ws)
                elif cmd == "goal":
                    _print_goal(ws, meta)
                elif cmd == "reset":
                    ws.close()
                    shutil.copy2(config.db_path, work_db)
                    ws = WorldState(work_db)
                    print("  Reset to initial snapshot.")
                    print(generate_observation(ws))
                elif cmd == "history":
                    _print_history(ws)
                else:
                    print(f"  Unknown meta-command: .{cmd}  (try .help)")
                continue

            # --- action ---
            parts = raw.split()
            action_name = parts[0]
            params = _parse_params(parts[1:])

            if action_name not in actions:
                print(f"  Unknown action: '{action_name}'. Try .help")
                continue

            result = execute_action(ws, action_name, params)
            if result.status != "success":
                print(f"  FAIL {result.failure_reason}")
                continue

            if action_name in _SCENE_ACTIONS:
                print(generate_observation(ws))
                continue

            detail = None
            if action_name == "inspect_entity" and params.get("entity_id"):
                detail = generate_entity_detail(ws, params["entity_id"])
            print(f"  ok  {detail or result.observation}")

    finally:
        ws.close()
        shutil.rmtree(work_root, ignore_errors=True)

    print("Done.")


def run(config: SandboxConfig) -> None:
    run_sandbox(config)


if __name__ == "__main__":
    from simpleArgParser import parse_args
    cfg: SandboxConfig = parse_args(SandboxConfig)
    run(cfg)
