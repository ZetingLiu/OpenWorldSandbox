"""Template-based partial-observability text observation generator.

When the robot enters an area, only entities in that area (and entities
inside open containers in that area) are visible.  Closed containers
hide their contents.

The observation is a structured text description suitable for feeding
into an LLM as the environment observation.
"""

from __future__ import annotations

from ows.env.world import WorldState


def generate_observation(ws: WorldState) -> str:
    """Generate a human-readable text observation of the current area.

    Only entities in the robot's current area are visible.  Entities
    inside closed containers are hidden.
    """
    robot = ws.get_robot()
    area_id = robot["location_id"]

    # Get area name from locations table
    area_name = _get_area_name(ws, area_id)

    lines: list[str] = []
    lines.append(f"## Location: {area_name} ({area_id})")

    # Held entities
    held_parts = []
    for hand in ("left_hand", "right_hand"):
        eid = robot.get(hand)
        if eid:
            ent = ws.get_entity(eid)
            if ent:
                # Contents of a carried container are spelled out: they travel
                # with the robot and never appear in the area listing, so a
                # bare count would leave the robot unable to see what it holds.
                held_parts.append(
                    f"{hand}: {_describe_entity(ws, ent, expand_contents=True)}"
                )
    if held_parts:
        lines.append("### Holding")
        for p in held_parts:
            lines.append(f"- {p}")
    else:
        lines.append("### Holding: nothing")

    # Visible entities in the area (entities hidden inside closed containers
    # are excluded — partial observability)
    entities = ws.get_entities_in_area(area_id)
    visible_entities = [
        e for e in entities
        if not _is_held(ws, e["id"])
        and ws.blocking_closed_ancestor(e["id"]) is None
    ]

    if not visible_entities:
        lines.append("### Visible entities: none")
        return "\n".join(lines)

    lines.append("### Visible entities")
    for ent in visible_entities:
        desc = _describe_entity(ws, ent)
        lines.append(f"- {desc}")

    return "\n".join(lines)


def generate_entity_detail(ws: WorldState, entity_id: str) -> str | None:
    """Generate a detailed description of a single entity."""
    ent = ws.get_entity(entity_id)
    if ent is None:
        return None

    lines = [
        f"Entity: {ent['name']} ({ent['id']})",
        f"  Class: {ent['class']}",
        f"  Pickable: {ent.get('pickable', False)}",
    ]

    props = ent.get("properties", [])
    if props:
        lines.append(f"  Properties: {', '.join(props)}")

    states = ent.get("states", {})
    if states:
        lines.append(f"  States: {states}")

    if ent.get("is_device"):
        lines.append(f"  Device state: {ent.get('device_state', 'off')}")

    if ent.get("open_state") is not None:
        lines.append(f"  Open state: {ent['open_state']}")

    # Show contents if container is open
    if ent.get("open_state") == "open" or (
        "can_contain" in props and ent.get("open_state") is None
    ):
        contents = ws.get_entities_in_container(entity_id)
        if contents:
            lines.append("  Contents:")
            for c in contents:
                lines.append(f"    - {c['name']} ({c['id']}, {c['class']})")

    # Show items on surface
    surface_items = ws.get_entities_on_surface(entity_id)
    if surface_items:
        lines.append("  Items on surface:")
        for si in surface_items:
            lines.append(f"    - {si['name']} ({si['id']}, {si['class']})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_area_name(ws: WorldState, area_id: str) -> str:
    row = ws.conn.execute(
        "SELECT name FROM locations WHERE id = ?", (area_id,)
    ).fetchone()
    return row["name"] if row else area_id


def _is_held(ws: WorldState, entity_id: str) -> bool:
    robot = ws.get_robot()
    return robot.get("left_hand") == entity_id or robot.get("right_hand") == entity_id


def _describe_entity(ws: WorldState, ent: dict, expand_contents: bool = False) -> str:
    """One-line description of an entity.

    With ``expand_contents`` the contained and supported items are named
    rather than counted; area listings keep the counts because those items
    are already listed separately.
    """
    parts = [f"{ent['name']} ({ent['id']}, {ent['class']})"]

    # Check if it's a container and describe its state
    if ent.get("open_state") is not None:
        parts.append(f"[{ent['open_state']}]")

    if ent.get("is_device") and ent.get("device_state"):
        parts.append(f"[{ent['device_state']}]")

    states = ent.get("states", {})
    if states:
        state_str = ", ".join(f"{k}={v}" for k, v in states.items())
        parts.append(f"{{{state_str}}}")

    # Show contents count for open containers
    if ent.get("open_state") == "open" or (
        "can_contain" in ent.get("properties", []) and ent.get("open_state") is None
    ):
        contents = ws.get_entities_in_container(ent["id"])
        if contents:
            if expand_contents:
                names = ", ".join(f"{c['name']} ({c['id']})" for c in contents)
                parts.append(f"contains {names}")
            else:
                parts.append(f"contains {len(contents)} item(s)")

    # Show items on surface
    surface_items = ws.get_entities_on_surface(ent["id"])
    if surface_items:
        if expand_contents:
            names = ", ".join(f"{s['name']} ({s['id']})" for s in surface_items)
            parts.append(f"with {names} on top")
        else:
            parts.append(f"with {len(surface_items)} item(s) on top")

    return " — ".join(parts)