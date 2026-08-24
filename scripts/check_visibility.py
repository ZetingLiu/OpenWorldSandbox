"""Regression check for area-local and closed-container visibility."""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from ows.env.actions import execute_action
from ows.env.observe import generate_observation
from ows.env.world import WorldState


def main() -> None:
    source = Path(sys.argv[1])
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "visibility.db"
        shutil.copy2(source, db_path)
        world = WorldState(str(db_path))
        try:
            living_room = generate_observation(world)
            assert "kitchen_table_01" not in living_room
            assert "kitchen_floor_01" not in living_room
            assert "kitchen_cabinet_01" not in living_room

            moved = execute_action(world, "move_to", {"area_id": "kitchen"})
            assert moved.status == "success"
            kitchen_closed = generate_observation(world)
            assert "kitchen_table_01" in kitchen_closed
            assert "kitchen_floor_01" in kitchen_closed
            assert "kitchen_cabinet_01" in kitchen_closed
            assert "dish_soap_01" not in kitchen_closed
            assert "garbage_bag_01" not in kitchen_closed

            opened = execute_action(
                world, "open_container", {"entity_id": "kitchen_cabinet_01"}
            )
            assert opened.status == "success"
            kitchen_open = generate_observation(world)
            assert "dish_soap_01" in kitchen_open
            assert "garbage_bag_01" in kitchen_open
        finally:
            world.close()

    print("AREA_ISOLATION_OK")
    print("CLOSED_CONTAINER_HIDING_OK")
    print("OPEN_CONTAINER_REVEALS_CONTENTS_OK")
    print("\n--- kitchen while cabinet closed ---")
    print(kitchen_closed)
    print("\n--- kitchen after opening cabinet ---")
    print(kitchen_open)


if __name__ == "__main__":
    main()
