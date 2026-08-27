"""Programmatic gates applied BEFORE the compile gate (all local, no API cost).

Funnel: LLM text → extract JSON (fence strip + json-repair) → Pydantic model
→ spec cross-reference checks → (compile gate, done in pipeline.py via
``ows.env.compile``).
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

import json_repair
from pydantic import ValidationError

from ows.env.actions import get_available_actions
from ows.env.compile import _validate_scenario  # repo-internal reuse (S1–S8)
from ows.schema.goal_dsl import _normalize_condition
from ows.schema.scenario import Scenario
from ows.schema.task import Task

ACTION_NAMES = set(get_available_actions())
GOAL_OPS = {"eq", "in", "all_of", "any_of", "count"}
CMP_OPS = {"eq", "neq", "gt", "gte", "lt", "lte"}
GOAL_FIELDS = {
    "container_id",
    "on",
    "area_id",
    "held_by",
    "open_state",
    "device_state",
    "location",
}
_ID_RE = re.compile(r"^[a-z0-9_]+$")


def extract_json(text: str) -> dict:
    """Extract the first JSON object from an LLM response (fences, prose, repair)."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty response")
    s = text.strip()
    # strip markdown code fences if present
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
        raise ValueError("top-level JSON is not an object")
    except ValueError:
        raise
    except Exception:
        pass
    # fallback: slice first { to last } then repair
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in response")
    try:
        obj = json_repair.loads(s[start : end + 1])
    except Exception as e:
        raise ValueError(f"json-repair failed: {e}")
    if not isinstance(obj, dict):
        raise ValueError("top-level JSON is not an object")
    return obj


# ---------------------------------------------------------------------------
# Scenario gate
# ---------------------------------------------------------------------------

def validate_scenario(data: dict) -> tuple[bool, Optional[Scenario], str]:
    """Pydantic + S1–S8. Returns (ok, scenario, first_error)."""
    try:
        scenario = Scenario.model_validate(data)
    except ValidationError as e:
        return False, None, _first_pydantic_error(e)
    if not _ID_RE.match(scenario.scenario_id):
        return False, None, f"scenario_id '{scenario.scenario_id}' is not snake_case"
    try:
        _validate_scenario(scenario)
    except ValueError as e:
        return False, None, f"S-rules: {e}"
    return True, scenario, ""


def _first_pydantic_error(e: ValidationError) -> str:
    errs = e.errors()
    if not errs:
        return str(e)
    first = errs[0]
    loc = ".".join(str(x) for x in first.get("loc", []))
    return f"{loc}: {first.get('msg')}"


# ---------------------------------------------------------------------------
# Task gate
# ---------------------------------------------------------------------------

def validate_task(
    data: dict, scenario: Scenario
) -> tuple[bool, Optional[Task], str]:
    """Pydantic + cross-reference checks against the scenario. Returns (ok, task, first_error)."""
    try:
        task = Task.model_validate(data)
    except ValidationError as e:
        return False, None, _first_pydantic_error(e)
    if not _ID_RE.match(task.task_id):
        return False, None, f"task_id '{task.task_id}' is not snake_case"
    if task.scenario_id != scenario.scenario_id:
        return False, None, (
            f"task scenario_id '{task.scenario_id}' != scenario '{scenario.scenario_id}'"
        )

    entity_ids = set(scenario.all_entities.keys())

    # --- goal / subgoal structure + entity references ---
    err = _check_condition(task.goal, entity_ids)
    if err:
        return False, None, f"goal: {err}"
    for sg in task.subgoals:
        err = _check_condition(sg.cond, entity_ids)
        if err:
            return False, None, f"subgoal '{sg.id}': {err}"

    # --- initial_state_patch entity refs ---
    for eid in task.initial_state_patch.entities:
        if eid not in entity_ids:
            return False, None, f"initial_state_patch references unknown entity '{eid}'"

    # --- walkthroughs: action names, param refs, step budget ---
    for wi, w in enumerate(task.walkthroughs):
        if len(w.actions) >= task.max_steps:
            return False, None, (
                f"walkthrough[{wi}] has {len(w.actions)} steps >= max_steps {task.max_steps}"
            )
        for si, a in enumerate(w.actions):
            if a.action not in ACTION_NAMES:
                return False, None, (
                    f"walkthrough[{wi}] step {si}: unknown action '{a.action}'"
                )
            for key in ("entity_id", "target_id", "tool_id"):
                value = a.params.get(key)
                if value and value not in entity_ids:
                    return False, None, (
                        f"walkthrough[{wi}] step {si}: {key} '{value}' not in scenario"
                    )
    return True, task, ""


def _check_condition(cond: dict, entity_ids: set[str], depth: int = 0) -> Optional[str]:
    """Structural check of one GoalCondition node; returns error string or None."""
    if depth > 12:
        return "condition nesting too deep"
    if not isinstance(cond, dict):
        return "condition is not an object"
    # Accept both JSON formats ({"all_of": [...]} and {"op": "all_of", ...})
    # — same normalization the goal_dsl evaluator performs.
    cond = _normalize_condition(cond)
    op = cond.get("op", "eq")
    if op == "all_of" or op == "any_of":
        children = cond.get("conditions")
        if not isinstance(children, list) or not children:
            return f"{op} requires a non-empty conditions list"
        for c in children:
            err = _check_condition(c, entity_ids, depth + 1)
            if err:
                return err
        return None
    if op in ("eq", "in"):
        if "entity" not in cond or "field" not in cond:
            return f"{op} condition missing entity/field"
        eid, field = cond["entity"], cond["field"]
        if eid != "robot" and eid not in entity_ids:
            return f"condition references unknown entity '{eid}'"
        if not _field_valid(field, eid):
            return f"unsupported field '{field}'"
        return None
    if op == "count":
        if cond.get("entity_class") not in {
            "furniture", "container", "device", "clothing", "item",
            "consumable", "tool", "fixture",
        }:
            return f"count: invalid entity_class '{cond.get('entity_class')}'"
        if cond.get("cmp") not in CMP_OPS:
            return f"count: invalid cmp '{cond.get('cmp')}'"
        where = cond.get("where") or {}
        if not _field_valid(where.get("field", ""), None):
            return f"count.where: unsupported field '{where.get('field')}'"
        if not isinstance(cond.get("value"), int):
            return "count: value must be int"
        return None
    return f"unknown op '{op}'"


def _field_valid(field: str, entity_id: Optional[str]) -> bool:
    if field.startswith("states."):
        return len(field) > len("states.")
    if entity_id == "robot":
        return field in ("location", "left_hand", "right_hand", "area_id")
    return field in GOAL_FIELDS


# ---------------------------------------------------------------------------
# Exploration-task deterministic gates (plan §5.1)
# ---------------------------------------------------------------------------

INFO_ACTIONS = {
    "observe_scene",
    "inspect_entity",
    "search_object",
    "open_container",
    "check_robot_state",
}
KEY_OPERATIONS = {
    "pick_object",
    "place_object",
    "start_device",
    "stop_device",
    "apply_physical_tool",
    "hang_object",
}
_STEP_VERBS = [
    "走到", "拿起", "打开", "放进", "放到", "挂到", "关闭", "启动",
    "取出", "装袋", "结账", "然后", "接着", "最后",
]


def check_instruction_leak(task: Task, scenario: Scenario) -> Optional[str]:
    """指令不得包含实体 ID（英文 snake_case）或步骤清单式写法。

    Semantic leaks (exact hiding place, full route) are judged by the
    GPT-5 structured review; this gate catches the deterministic cases.
    """
    inst = task.instruction
    leaks = [
        eid
        for eid in scenario.all_entities
        if re.search(rf"(?<![a-z0-9_]){re.escape(eid)}(?![a-z0-9_])", inst)
    ]
    if leaks:
        return f"instruction 泄露实体 ID: {leaks[:3]}"
    hits = [v for v in _STEP_VERBS if v in inst]
    if len(hits) >= 4:
        return f"instruction 疑似步骤清单（含动作/顺序词 {hits}）"
    return None


def check_info_action_before_key_op(task: Task) -> Optional[str]:
    """每个 walkthrough 在首个关键操作前必须出现信息获取动作。"""
    for wi, w in enumerate(task.walkthroughs):
        seen_info = False
        for a in w.actions:
            if a.action in INFO_ACTIONS:
                seen_info = True
            if a.action in KEY_OPERATIONS and not seen_info:
                return (
                    f"walkthrough[{wi}] 关键操作 {a.action} 之前没有信息获取动作"
                )
    if task.walkthroughs and not any(
        a.action in INFO_ACTIONS
        for w in task.walkthroughs
        for a in w.actions
    ):
        return "walkthrough 全程无信息获取动作，不构成探索任务"
    return None


def check_action_budget(task: Task, min_steps: int) -> Optional[str]:
    """max_steps ≈ 1.8× 最短 walkthrough 基线 + 4 步探索余量（plan §5.1）。"""
    required = int(min_steps * 1.8) + 4
    if task.max_steps < required:
        return (
            f"max_steps={task.max_steps} 低于建议下限 {required}"
            f"（基线 {min_steps} × 1.8 + 4 步探索余量）"
        )
    return None


def check_variant_difference(v1: dict, v2: dict) -> Optional[str]:
    """两个环境变体必须在 initial_state_patch 或 goal 上存在差异。"""

    def _patch_sig(t: dict) -> str:
        p = t.get("initial_state_patch") or {}
        ent = {
            k: sorted(v.items()) if isinstance(v, dict) else v
            for k, v in (p.get("entities") or {}).items()
        }
        return json.dumps(
            {
                "entities": ent,
                "robot": p.get("robot"),
                "area_adjacency": p.get("area_adjacency"),
            },
            sort_keys=True,
            ensure_ascii=False,
        )

    same_patch = _patch_sig(v1) == _patch_sig(v2)
    same_goal = json.dumps(
        v1.get("goal"), sort_keys=True, ensure_ascii=False
    ) == json.dumps(v2.get("goal"), sort_keys=True, ensure_ascii=False)
    if same_patch and same_goal:
        return "v1/v2 的 initial_state_patch 与 goal 完全相同，无环境变体差异"
    return None
