"""17 fixed semantic-action executors.

Each action follows the pipeline:

    format check → entity existence → reachability → robot state →
    pre-conditions → safety rules → state update → structured feedback

On failure, the world state is NOT modified and a structured
:class:`ActionResult` with ``failure_reason`` is returned.

Mirrors the plan's §"17 个动作接口（一次冻结）".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ows.env.world import WorldState


# ---------------------------------------------------------------------------
# Action result
# ---------------------------------------------------------------------------

@dataclass
class ActionResult:
    status: str = "success"               # "success" | "failure"
    failure_reason: str | None = None
    observation: str = ""                 # human-readable feedback
    data: dict[str, Any] = field(default_factory=dict)


def _success(observation: str = "", **data: Any) -> ActionResult:
    return ActionResult(status="success", observation=observation, data=data)


def _failure(reason: str, observation: str = "") -> ActionResult:
    return ActionResult(
        status="failure",
        failure_reason=reason,
        observation=observation or reason,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entity_in_current_area(ws: WorldState, entity_id: str) -> bool:
    """Check if the entity is in the robot's current area (visible/reachable).

    Resolves through container chains: if the entity is inside a container
    that is held or in the current area, it's reachable.
    """
    robot = ws.get_robot()
    robot_loc = robot["location_id"]
    ent = ws.get_entity(entity_id)
    if ent is None:
        return False

    # Held entities are reachable
    if ent["area_id"] == "__held__":
        return True

    # Directly in the current area
    if ent["area_id"] == robot_loc:
        return True

    # Walk up the container chain
    current_id = entity_id
    visited: set[str] = set()
    while current_id is not None and current_id not in visited:
        visited.add(current_id)
        e = ws.get_entity(current_id)
        if e is None:
            return False
        # Check if this entity is in the current area or held
        if e["area_id"] == robot_loc or e["area_id"] == "__held__":
            return True
        # Go up: try container, then surface
        parent = e.get("container_id") or e.get("on_id")
        if parent is None:
            return False
        current_id = parent
    return False


def _entity_visible(ws: WorldState, entity_id: str) -> bool:
    """An entity is visible only if no closed container hides it."""
    return ws.blocking_closed_ancestor(entity_id) is None


def _entity_in_container(ws: WorldState, entity_id: str, container_id: str) -> bool:
    ent = ws.get_entity(entity_id)
    return ent is not None and ent.get("container_id") == container_id


def _entity_on_surface(ws: WorldState, entity_id: str, surface_id: str) -> bool:
    ent = ws.get_entity(entity_id)
    return ent is not None and ent.get("on_id") == surface_id


def _container_is_open(ws: WorldState, container_id: str) -> bool:
    ent = ws.get_entity(container_id)
    return ent is not None and ent.get("open_state") == "open"


def _robot_holds(ws: WorldState, entity_id: str) -> str | None:
    """Return the hand ('left_hand'/'right_hand') holding entity_id, or None."""
    robot = ws.get_robot()
    if robot["left_hand"] == entity_id:
        return "left_hand"
    if robot["right_hand"] == entity_id:
        return "right_hand"
    return None


def _get_free_hand(ws: WorldState) -> str | None:
    robot = ws.get_robot()
    if robot["left_hand"] is None:
        return "left_hand"
    if robot["right_hand"] is None:
        return "right_hand"
    return None


# ===================================================================
# 1. observe_scene
# ===================================================================

def action_observe_scene(ws: WorldState, params: dict) -> ActionResult:
    robot = ws.get_robot()
    area_id = robot["location_id"]
    entities = ws.get_entities_in_area(area_id)

    # Separate held entities
    held = []
    for hand in ("left_hand", "right_hand"):
        eid = robot.get(hand)
        if eid:
            ent = ws.get_entity(eid)
            if ent:
                held.append(ent)

    # Visible entities (not held, in current area, not hidden in closed containers)
    visible = [
        e for e in entities
        if not _robot_holds(ws, e["id"]) and _entity_visible(ws, e["id"])
    ]

    return _success(
        observation=f"You are in area '{area_id}'. Visible entities: {len(visible)}, held: {len(held)}.",
        area_id=area_id,
        visible_entities=visible,
        held_entities=held,
    )


# ===================================================================
# 2. search_object
# ===================================================================

def action_search_object(ws: WorldState, params: dict) -> ActionResult:
    entity_id = params.get("entity_id")
    if not entity_id:
        return _failure("Missing required param: entity_id")

    if not ws.entity_exists(entity_id):
        return _failure(f"Entity '{entity_id}' does not exist")

    # Only report entities that are actually visible: in the current area
    # (directly, on surfaces, or inside OPEN containers).  Entities hidden
    # inside closed containers must NOT be revealed.
    if _entity_in_current_area(ws, entity_id) and _entity_visible(ws, entity_id):
        ent = ws.get_entity(entity_id)
        container_id = ent.get("container_id")
        if container_id:
            obs = f"Found '{entity_id}' ({ent['name']}) inside container '{container_id}'."
        elif ent.get("on_id"):
            obs = f"Found '{entity_id}' ({ent['name']}) on '{ent['on_id']}'."
        else:
            obs = f"Found '{entity_id}' ({ent['name']}) in current area."
        return _success(observation=obs, entity=ent, found=True)

    return _success(
        observation=f"Entity '{entity_id}' is not visible in the current area.",
        found=False,
    )


# ===================================================================
# 3. inspect_entity
# ===================================================================

def action_inspect_entity(ws: WorldState, params: dict) -> ActionResult:
    entity_id = params.get("entity_id")
    if not entity_id:
        return _failure("Missing required param: entity_id")

    ent = ws.get_entity(entity_id)
    if ent is None:
        return _failure(f"Entity '{entity_id}' does not exist")

    if not _entity_in_current_area(ws, entity_id):
        return _failure(f"Entity '{entity_id}' is not in the current area")

    # If entity is hidden inside a closed container (at any depth), can't inspect
    blocker = ws.blocking_closed_ancestor(entity_id)
    if blocker:
        return _failure(f"Entity '{entity_id}' is inside closed container '{blocker}'")

    return _success(
        observation=f"Entity '{entity_id}': {ent['name']} (class={ent['class']}).",
        entity=ent,
    )


# ===================================================================
# 4. check_robot_state
# ===================================================================

def action_check_robot_state(ws: WorldState, params: dict) -> ActionResult:
    robot = ws.get_robot()
    left = ws.get_entity(robot["left_hand"]) if robot["left_hand"] else None
    right = ws.get_entity(robot["right_hand"]) if robot["right_hand"] else None

    return _success(
        observation=f"Robot in '{robot['location_id']}'. "
        f"Left hand: {left['name'] if left else 'empty'}. "
        f"Right hand: {right['name'] if right else 'empty'}.",
        location=robot["location_id"],
        left_hand=left,
        right_hand=right,
    )


# ===================================================================
# 5. move_to
# ===================================================================

def action_move_to(ws: WorldState, params: dict) -> ActionResult:
    area_id = params.get("area_id")
    if not area_id:
        return _failure("Missing required param: area_id")

    robot = ws.get_robot()
    current = robot["location_id"]

    if current == area_id:
        return _success(observation=f"Already in area '{area_id}'.")

    row = ws.conn.execute(
        "SELECT 1 FROM locations WHERE id = ?", (area_id,)
    ).fetchone()
    if row is None or area_id == "__held__":
        return _failure(f"Area '{area_id}' does not exist")

    if not ws.is_passable(current, area_id):
        return _failure(f"Cannot move from '{current}' to '{area_id}' — not passable")

    ws.set_robot_location(area_id)
    # Held entities keep the virtual '__held__' area; they travel with the robot.

    return _success(observation=f"Moved to area '{area_id}'.")


# ===================================================================
# 6. pick_object
# ===================================================================

def action_pick_object(ws: WorldState, params: dict) -> ActionResult:
    entity_id = params.get("entity_id")
    if not entity_id:
        return _failure("Missing required param: entity_id")

    ent = ws.get_entity(entity_id)
    if ent is None:
        return _failure(f"Entity '{entity_id}' does not exist")

    if not ent.get("pickable", False):
        return _failure(f"Entity '{entity_id}' is not pickable")

    if _robot_holds(ws, entity_id):
        return _failure(f"Entity '{entity_id}' is already held")

    # Entity must be in current area
    if not _entity_in_current_area(ws, entity_id):
        return _failure(f"Entity '{entity_id}' is not in the current area")

    # Entity must not be hidden inside a closed container (at any depth)
    blocker = ws.blocking_closed_ancestor(entity_id)
    if blocker:
        return _failure(f"Entity '{entity_id}' is inside closed container '{blocker}'")

    # Need a free hand
    hand = _get_free_hand(ws)
    if hand is None:
        return _failure("Both hands are occupied — free one first")

    # State update
    ws.set_robot_hand(hand, entity_id)
    ws.set_entity_held_by(entity_id, hand)

    return _success(
        observation=f"Picked up '{entity_id}' ({ent['name']}) with {hand}.",
        entity_id=entity_id,
        hand=hand,
    )


# ===================================================================
# 7. place_object
# ===================================================================

def action_place_object(ws: WorldState, params: dict) -> ActionResult:
    entity_id = params.get("entity_id")
    target_id = params.get("target_id")

    if not entity_id:
        return _failure("Missing required param: entity_id")
    if not target_id:
        return _failure("Missing required param: target_id")

    # Entity must be held by robot
    hand = _robot_holds(ws, entity_id)
    if hand is None:
        return _failure(f"Entity '{entity_id}' is not held by robot")

    # Target must exist
    target = ws.get_entity(target_id)
    if target is None:
        return _failure(f"Target '{target_id}' does not exist")

    # Cycle guards: cannot place an entity into/onto itself or its own contents
    if entity_id == target_id:
        return _failure(f"Cannot place '{entity_id}' into/onto itself")
    ancestor = target
    seen: set[str] = set()
    while ancestor is not None and ancestor["id"] not in seen:
        seen.add(ancestor["id"])
        if ancestor["id"] == entity_id:
            return _failure(
                f"Cannot place '{entity_id}' into/onto '{target_id}' — "
                f"the target is inside/on '{entity_id}'"
            )
        parent_id = ancestor.get("container_id") or ancestor.get("on_id")
        ancestor = ws.get_entity(parent_id) if parent_id else None

    # Target must be in current area
    if not _entity_in_current_area(ws, target_id):
        return _failure(f"Target '{target_id}' is not in the current area")

    # Determine placement type: container or surface
    props = target.get("properties", [])
    if "can_contain" in props:
        # Placing into container
        if target.get("open_state") == "closed":
            return _failure(f"Container '{target_id}' is closed — open it first")
        ws.set_entity_container(entity_id, target_id)
        ws.set_robot_hand(hand, None)
        return _success(
            observation=f"Placed '{entity_id}' into container '{target_id}'.",
        )

    elif "can_support" in props:
        # Placing on surface
        ws.set_entity_on(entity_id, target_id)
        ws.set_robot_hand(hand, None)
        return _success(
            observation=f"Placed '{entity_id}' on surface '{target_id}'.",
        )

    else:
        return _failure(
            f"Target '{target_id}' is neither a container (can_contain) nor a surface (can_support)"
        )


# ===================================================================
# 8. open_container
# ===================================================================

def action_open_container(ws: WorldState, params: dict) -> ActionResult:
    entity_id = params.get("entity_id")
    if not entity_id:
        return _failure("Missing required param: entity_id")

    ent = ws.get_entity(entity_id)
    if ent is None:
        return _failure(f"Entity '{entity_id}' does not exist")

    if not _entity_in_current_area(ws, entity_id):
        return _failure(f"Entity '{entity_id}' is not in the current area")

    if ent.get("open_state") is None:
        return _failure(f"Entity '{entity_id}' is not openable (no open_state)")

    if ent.get("open_state") == "open":
        return _success(observation=f"Container '{entity_id}' is already open.")

    ws.set_open_state(entity_id, "open")
    return _success(
        observation=f"Opened container '{entity_id}'.",
        entity_id=entity_id,
    )


# ===================================================================
# 9. close_container
# ===================================================================

def action_close_container(ws: WorldState, params: dict) -> ActionResult:
    entity_id = params.get("entity_id")
    if not entity_id:
        return _failure("Missing required param: entity_id")

    ent = ws.get_entity(entity_id)
    if ent is None:
        return _failure(f"Entity '{entity_id}' does not exist")

    if not _entity_in_current_area(ws, entity_id):
        return _failure(f"Entity '{entity_id}' is not in the current area")

    if ent.get("open_state") is None:
        return _failure(f"Entity '{entity_id}' is not openable (no open_state)")

    if ent.get("open_state") == "closed":
        return _success(observation=f"Container '{entity_id}' is already closed.")

    ws.set_open_state(entity_id, "closed")
    return _success(
        observation=f"Closed container '{entity_id}'.",
        entity_id=entity_id,
    )


# ===================================================================
# 10. hang_object
# ===================================================================

def action_hang_object(ws: WorldState, params: dict) -> ActionResult:
    entity_id = params.get("entity_id")
    target_id = params.get("target_id")

    if not entity_id:
        return _failure("Missing required param: entity_id")
    if not target_id:
        return _failure("Missing required param: target_id")

    # Entity must be held
    hand = _robot_holds(ws, entity_id)
    if hand is None:
        return _failure(f"Entity '{entity_id}' is not held by robot")

    # Entity must be hangable
    ent = ws.get_entity(entity_id)
    if "hangable" not in ent.get("properties", []):
        return _failure(f"Entity '{entity_id}' is not hangable")

    # Target must accept hanging
    target = ws.get_entity(target_id)
    if target is None:
        return _failure(f"Target '{target_id}' does not exist")

    if not _entity_in_current_area(ws, target_id):
        return _failure(f"Target '{target_id}' is not in the current area")

    if "can_hang" not in target.get("properties", []):
        return _failure(f"Target '{target_id}' cannot accept hanging (lacks can_hang)")

    # Hang the entity (place it on the target as if it were a surface)
    ws.set_entity_on(entity_id, target_id)
    ws.set_robot_hand(hand, None)

    return _success(
        observation=f"Hung '{entity_id}' on '{target_id}'.",
    )


# ===================================================================
# 11. start_device
# ===================================================================

# Devices carrying this property leave an irreversible mark when started.
TRANSACTION_PROPERTY = "transactional"
TRANSACTION_STATE = "transaction_complete"


def action_start_device(ws: WorldState, params: dict) -> ActionResult:
    entity_id = params.get("entity_id")
    if not entity_id:
        return _failure("Missing required param: entity_id")

    ent = ws.get_entity(entity_id)
    if ent is None:
        return _failure(f"Entity '{entity_id}' does not exist")

    if not _entity_in_current_area(ws, entity_id):
        return _failure(f"Entity '{entity_id}' is not in the current area")

    if not ent.get("is_device", False):
        return _failure(f"Entity '{entity_id}' is not a device")

    if ent.get("device_state") == "running":
        # Backfill the mark: a device found already running still represents a
        # completed transaction.
        if TRANSACTION_PROPERTY in ent.get("properties", []):
            ws.set_state(entity_id, TRANSACTION_STATE, True)
        return _success(observation=f"Device '{entity_id}' is already running.")

    # Pre-condition: if device is also a container, it must be closed
    if "can_contain" in ent.get("properties", []) and ent.get("open_state") == "open":
        return _failure(f"Device '{entity_id}' must be closed before starting")

    ws.set_device_state(entity_id, "running")

    # A transactional device (a POS terminal, say) records that its
    # transaction went through.  The mark is never cleared, so switching the
    # device off afterwards — which is the natural thing to do — does not
    # undo the business outcome it represents.
    if TRANSACTION_PROPERTY in ent.get("properties", []):
        ws.set_state(entity_id, TRANSACTION_STATE, True)
        return _success(
            observation=f"Started device '{entity_id}'; transaction completed.",
            entity_id=entity_id,
        )

    return _success(
        observation=f"Started device '{entity_id}'.",
        entity_id=entity_id,
    )


# ===================================================================
# 12. stop_device
# ===================================================================

def action_stop_device(ws: WorldState, params: dict) -> ActionResult:
    entity_id = params.get("entity_id")
    if not entity_id:
        return _failure("Missing required param: entity_id")

    ent = ws.get_entity(entity_id)
    if ent is None:
        return _failure(f"Entity '{entity_id}' does not exist")

    if not _entity_in_current_area(ws, entity_id):
        return _failure(f"Entity '{entity_id}' is not in the current area")

    if not ent.get("is_device", False):
        return _failure(f"Entity '{entity_id}' is not a device")

    if ent.get("device_state") == "off":
        return _success(observation=f"Device '{entity_id}' is already off.")

    ws.set_device_state(entity_id, "off")
    return _success(
        observation=f"Stopped device '{entity_id}'.",
        entity_id=entity_id,
    )


# ===================================================================
# 13. apply_physical_tool
# ===================================================================

# Pre-defined property → effect mapping (per the plan)
_PROPERTY_EFFECTS: dict[str, dict[str, Any]] = {
    "absorbent": {
        "description": "Absorb liquid / clean up water",
        "applies_to_target": True,
        "effect": {"add_state": {"cleanliness": "clean"}},
    },
    "waterproof": {
        "description": "Protect from water",
        "applies_to_target": True,
        "effect": {"add_state": {"waterproofed": True}},
    },
    "soft": {
        "description": "Cushion / wipe surface",
        "applies_to_target": True,
        "effect": {"add_state": {"wiped": True}},
    },
}


def action_apply_physical_tool(ws: WorldState, params: dict) -> ActionResult:
    tool_id = params.get("tool_id")
    target_id = params.get("target_id")
    intended_effect = params.get("intended_effect")

    if not tool_id:
        return _failure("Missing required param: tool_id")
    if not target_id:
        return _failure("Missing required param: target_id")

    # Tool must be held
    hand = _robot_holds(ws, tool_id)
    if hand is None:
        return _failure(f"Tool '{tool_id}' is not held by robot")

    tool = ws.get_entity(tool_id)
    if tool is None:
        return _failure(f"Tool '{tool_id}' does not exist")

    # Target must be in current area
    if not _entity_in_current_area(ws, target_id):
        return _failure(f"Target '{target_id}' is not in the current area")

    # Find applicable property
    tool_props = tool.get("properties", [])
    applicable = [p for p in tool_props if p in _PROPERTY_EFFECTS]

    if not applicable:
        return _failure(
            f"Tool '{tool_id}' has no applicable physical properties "
            f"(available: {list(_PROPERTY_EFFECTS.keys())})"
        )

    # Prefer the property matching the declared intent, else the first applicable
    prop = applicable[0]
    if intended_effect:
        for p in applicable:
            if p == intended_effect or intended_effect in _PROPERTY_EFFECTS.get(p, {}).get("description", "").lower():
                prop = p
                break
    effect = _PROPERTY_EFFECTS[prop]

    for key, value in effect["effect"].get("add_state", {}).items():
        ws.set_state(target_id, key, value)

    return _success(
        observation=f"Applied '{tool_id}' ({prop}) to '{target_id}': {effect['description']}.",
        tool_id=tool_id,
        target_id=target_id,
        property_used=prop,
        intended_effect=intended_effect,
    )


# ===================================================================
# 14. finish_task
# ===================================================================

def action_finish_task(ws: WorldState, params: dict) -> ActionResult:
    return _success(
        observation="Task finished.",
        finished=True,
    )


# ===================================================================
# 15. report_target_absent
# ===================================================================

def action_report_target_absent(ws: WorldState, params: dict) -> ActionResult:
    entity_id = params.get("entity_id", "unknown")
    return _success(
        observation=f"Reported: target entity '{entity_id}' is absent from the environment.",
        reported_absent=entity_id,
    )


# ===================================================================
# 16. report_unable_to_continue
# ===================================================================

def action_report_unable_to_continue(ws: WorldState, params: dict) -> ActionResult:
    reason = params.get("reason", "unspecified")
    return _success(
        observation=f"Reported unable to continue: {reason}",
        reason=reason,
    )


# ===================================================================
# 17. abandon_task
# ===================================================================

def action_abandon_task(ws: WorldState, params: dict) -> ActionResult:
    return _success(
        observation="Task abandoned.",
        abandoned=True,
    )


# ===================================================================
# Dispatcher
# ===================================================================

_ACTION_MAP: dict[str, Any] = {
    "observe_scene": action_observe_scene,
    "search_object": action_search_object,
    "inspect_entity": action_inspect_entity,
    "check_robot_state": action_check_robot_state,
    "move_to": action_move_to,
    "pick_object": action_pick_object,
    "place_object": action_place_object,
    "open_container": action_open_container,
    "close_container": action_close_container,
    "hang_object": action_hang_object,
    "start_device": action_start_device,
    "stop_device": action_stop_device,
    "apply_physical_tool": action_apply_physical_tool,
    "finish_task": action_finish_task,
    "report_target_absent": action_report_target_absent,
    "report_unable_to_continue": action_report_unable_to_continue,
    "abandon_task": action_abandon_task,
}


def execute_action(ws: WorldState, action_name: str, params: dict) -> ActionResult:
    """Execute a named action against the world state.

    Returns
    -------
    ActionResult
        Always returns a result; failures are signalled via
        ``status == "failure"`` and a populated ``failure_reason``.
    """
    handler = _ACTION_MAP.get(action_name)
    if handler is None:
        return _failure(f"Unknown action: '{action_name}'")

    try:
        result = handler(ws, params)
    except Exception as exc:
        return _failure(f"Action '{action_name}' raised exception: {exc}")

    # Log the action
    step = ws.get_action_count() + 1
    ws.log_action(
        step_id=step,
        action=action_name,
        params=params,
        status=result.status,
        failure_reason=result.failure_reason,
    )

    return result


def get_available_actions() -> list[str]:
    """Return the list of all 17 action names."""
    return list(_ACTION_MAP.keys())