"""Goal-condition DSL evaluator.

Evaluates declarative conditions (eq / in / all_of / any_of / count) against
an in-memory world-state dict.  Used by both the compile-time walkthrough
replay and the final-task verification.

Field resolution
----------------
The evaluator receives a *flat* world snapshot (dict) with these keys:

    entities : dict[str, dict]
        entity_id → {id, class, name, container_id, on, area_id,
                     held_by, open_state, device_state, states, ...}

    robot : dict
        {location, left_hand, right_hand}

A condition references an entity by its id and resolves the requested
``field`` against the entity dict.  Supported fields:

    container_id  — entity's ``in`` container id (None if not in any)
    on            — entity's ``on`` surface id
    area_id       — resolved area (walks up the container/surface chain)
    held_by       — "left_hand" / "right_hand" / None
    open_state    — "open" / "closed" / None
    device_state  — "off" / "running" / None
    states.<key>  — value inside entity["states"]
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Field resolution helpers
# ---------------------------------------------------------------------------

def _resolve_area_id(entity_id: str, entities: dict[str, dict], robot: dict) -> str | None:
    """Resolve the entity's true area by walking up to the topmost parent.

    The parent chain (container/surface) is authoritative: a stale
    ``area_id`` on a nested entity is ignored in favour of the topmost
    ancestor's area.  The virtual ``__held__`` area maps to the robot's
    current location (a held entity travels with the robot).
    """
    visited: set[str] = set()
    current_id = entity_id
    e = entities.get(current_id)
    while e is not None and current_id not in visited:
        visited.add(current_id)
        parent = e.get("container_id") or e.get("on_id") or e.get("on")
        if parent is None or parent not in entities:
            break
        current_id = parent
        e = entities[current_id]
    if e is None:
        return None
    area = e.get("area_id")
    if area == "__held__":
        return robot.get("location_id") or robot.get("location")
    return area


def _resolve_field(entity_id: str, field: str, entities: dict[str, dict], robot: dict) -> Any:
    """Resolve a field for an entity (or 'robot' pseudo-entity)."""
    if entity_id == "robot":
        if field in ("location", "area_id"):
            return robot.get("location_id") or robot.get("location")
        return robot.get(field)

    e = entities.get(entity_id)
    if e is None:
        return None

    if field == "container_id":
        return e.get("container_id")
    if field == "on":
        # snapshot rows use the DB column name 'on_id'
        return e.get("on_id", e.get("on"))
    if field == "area_id":
        return _resolve_area_id(entity_id, entities, robot)
    if field == "held_by":
        return e.get("held_by")
    if field == "open_state":
        return e.get("open_state")
    if field == "device_state":
        return e.get("device_state")
    if field.startswith("states."):
        key = field[len("states."):]
        return (e.get("states") or {}).get(key)

    return None


# ---------------------------------------------------------------------------
# Condition evaluators
# ---------------------------------------------------------------------------

def _eval_eq(cond: dict, entities: dict[str, dict], robot: dict) -> bool:
    entity_id: str = cond["entity"]
    field: str = cond["field"]
    expected: Any = cond["value"]
    actual = _resolve_field(entity_id, field, entities, robot)
    return actual == expected


def _eval_in(cond: dict, entities: dict[str, dict], robot: dict) -> bool:
    entity_id: str = cond["entity"]
    field: str = cond["field"]
    allowed: list = cond["value"]
    actual = _resolve_field(entity_id, field, entities, robot)
    return actual in allowed


def _eval_count(cond: dict, entities: dict[str, dict], _robot: dict) -> bool:
    entity_class: str = cond["entity_class"]
    where: dict = cond["where"]
    cmp: str = cond["cmp"]
    target: int = cond["value"]

    count = 0
    for e in entities.values():
        if e.get("class") != entity_class:
            continue
        if not _eval_eq({"entity": e["id"], "field": where["field"], "value": where["value"]}, entities, _robot):
            continue
        count += 1

    match cmp:
        case "eq":  return count == target
        case "neq": return count != target
        case "gt":  return count > target
        case "gte": return count >= target
        case "lt":  return count < target
        case "lte": return count <= target
    return False


def _normalize_condition(cond: dict) -> dict:
    """Normalize JSON-format goal conditions to the internal evaluator format.

    JSON format:  {"all_of": [c1, c2, ...], "any_of": [...]}
    Internal:     {"op": "all_of", "conditions": [c1, c2, ...]}
    """
    # Already normalized
    if "op" in cond:
        if cond["op"] in ("all_of", "any_of"):
            cond["conditions"] = [
                _normalize_condition(c) for c in cond.get("conditions", [])
            ]
        return cond

    # Key-based format: {"all_of": [...]} or {"any_of": [...]}
    for key in ("all_of", "any_of"):
        if key in cond:
            return {
                "op": key,
                "conditions": [_normalize_condition(c) for c in cond[key]],
            }

    # Leaf condition: {"entity": ..., "field": ..., "op": "eq", "value": ...}
    # These already have "op" in them, so they'll be caught above.
    # But if "op" is missing, default to "eq"
    if "op" not in cond:
        cond = dict(cond, op="eq")
    return cond


def evaluate_condition(cond: dict, entities: dict[str, dict], robot: dict) -> bool:
    """Evaluate a single condition node (recursive for all_of / any_of)."""
    # Normalize the condition format: JSON uses key-based format
    # {"all_of": [...]} → {"op": "all_of", "conditions": [...]}
    cond = _normalize_condition(cond)

    op = cond.get("op", "eq")

    if op == "eq":
        return _eval_eq(cond, entities, robot)
    if op == "in":
        return _eval_in(cond, entities, robot)
    if op == "all_of":
        return all(
            evaluate_condition(c, entities, robot)
            for c in cond["conditions"]
        )
    if op == "any_of":
        return any(
            evaluate_condition(c, entities, robot)
            for c in cond["conditions"]
        )
    if op == "count":
        return _eval_count(cond, entities, robot)

    return False


# ---------------------------------------------------------------------------
# Top-level goal / subgoal evaluation
# ---------------------------------------------------------------------------

def evaluate_goal(goal: dict, world_snapshot: dict) -> bool:
    """Evaluate the top-level goal condition against a world snapshot."""
    entities: dict[str, dict] = world_snapshot.get("entities", {})
    robot: dict = world_snapshot.get("robot", {})
    goal = _normalize_condition(goal)
    return evaluate_condition(goal, entities, robot)


def evaluate_subgoal(subgoal_cond: dict, world_snapshot: dict) -> bool:
    """Evaluate a single subgoal condition."""
    return evaluate_goal(subgoal_cond, world_snapshot)


# ---------------------------------------------------------------------------
# Latch-tracking subgoal evaluator
# ---------------------------------------------------------------------------

class SubgoalTracker:
    """Tracks subgoal completion with latching semantics.

    Each subgoal, once satisfied, remains satisfied even if the state
    later changes (latch).  Use :meth:`update` after each action step.
    """

    def __init__(self, subgoals: list[dict]):
        self._subgoals = subgoals
        self._achieved: set[str] = set()

    def update(self, world_snapshot: dict) -> None:
        for sg in self._subgoals:
            sid = sg["id"]
            if sid not in self._achieved:
                if evaluate_subgoal(sg["cond"], world_snapshot):
                    self._achieved.add(sid)

    @property
    def achieved_ids(self) -> set[str]:
        return self._achieved

    @property
    def all_achieved(self) -> bool:
        return len(self._achieved) == len(self._subgoals)

    def summary(self) -> dict[str, bool]:
        return {sg["id"]: sg["id"] in self._achieved for sg in self._subgoals}