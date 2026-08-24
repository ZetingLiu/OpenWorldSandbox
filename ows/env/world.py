"""SQLite world-state read/write layer.

This is the single source of truth for the environment state.  All
action executors read/write through this module.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS locations (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS location_edges (
    from_id  TEXT NOT NULL REFERENCES locations(id),
    to_id    TEXT NOT NULL REFERENCES locations(id),
    passable INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (from_id, to_id)
);

CREATE TABLE IF NOT EXISTS entities (
    id              TEXT PRIMARY KEY,
    class           TEXT NOT NULL,
    name            TEXT NOT NULL,
    area_id         TEXT NOT NULL REFERENCES locations(id),
    container_id    TEXT,
    on_id           TEXT,
    properties_json TEXT NOT NULL DEFAULT '[]',
    states_json     TEXT NOT NULL DEFAULT '{}',
    pickable        INTEGER NOT NULL DEFAULT 0,
    is_container    INTEGER NOT NULL DEFAULT 0,
    is_device       INTEGER NOT NULL DEFAULT 0,
    open_state      TEXT,
    device_state    TEXT
);

CREATE TABLE IF NOT EXISTS robot (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    location_id TEXT NOT NULL REFERENCES locations(id),
    left_hand   TEXT,
    right_hand  TEXT
);

CREATE TABLE IF NOT EXISTS action_history (
    step_id         INTEGER NOT NULL,
    action          TEXT NOT NULL,
    params_json     TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'success',
    failure_reason  TEXT,
    state_diff_json TEXT,
    PRIMARY KEY (step_id)
);

CREATE TABLE IF NOT EXISTS subgoal_status (
    subgoal_id  TEXT PRIMARY KEY,
    achieved    INTEGER NOT NULL DEFAULT 0
);
"""


# ---------------------------------------------------------------------------
# Row decoding
# ---------------------------------------------------------------------------

def _row_to_entity(row: sqlite3.Row) -> dict:
    """Decode an ``entities`` row into a plain dict.

    Every caller expects ``properties`` and ``states`` to be real Python
    values.  Handing back the raw ``*_json`` columns instead silently
    disables each check that reads them, so all entity queries go through
    here.
    """
    e = dict(row)
    e["properties"] = json.loads(e.pop("properties_json", None) or "[]")
    e["states"] = json.loads(e.pop("states_json", None) or "{}")
    return e


# ---------------------------------------------------------------------------
# World state class
# ---------------------------------------------------------------------------

class WorldState:
    """Manages an SQLite world state database.

    Usage::

        ws = WorldState(":memory:")       # or file path
        ws.initialise_schema()
        ws.populate_from_scenario(scenario, task_patch)
        # ...
        ws.close()
    """

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    # -- connection management ------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextmanager
    def transaction(self):
        """Context manager that commits on success, rolls back on error."""
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # -- initialisation -------------------------------------------------------

    def initialise_schema(self) -> None:
        self.conn.executescript(SCHEMA_DDL)
        self.conn.commit()

    def populate_from_scenario(self, scenario: dict, task: dict | None = None) -> None:
        """Populate the database from a scenario dict and optional task patch.

        Parameters
        ----------
        scenario : dict
            The raw scenario JSON (keys: areas, area_adjacency, area_tables,
            robot).
        task : dict or None
            If given, ``initial_state_patch`` from the task is applied on top
            of the scenario data.
        """
        with self.transaction() as conn:
            # --- locations ---
            for area in scenario["areas"]:
                conn.execute(
                    "INSERT INTO locations (id, name) VALUES (?, ?)",
                    (area["id"], area["name"]),
                )
            # Add virtual location for held entities
            conn.execute(
                "INSERT OR IGNORE INTO locations (id, name) VALUES (?, ?)",
                ("__held__", "(held by robot)"),
            )

            # --- location_edges ---
            for adj in scenario.get("area_adjacency", []):
                conn.execute(
                    "INSERT INTO location_edges (from_id, to_id, passable) VALUES (?, ?, ?)",
                    (adj["from"], adj["to"], int(adj.get("passable", True))),
                )

            # --- entities ---
            robot_loc = scenario["robot"]["location"]
            for area_id, entities in scenario.get("area_tables", {}).items():
                for ent in entities:
                    self._insert_entity(conn, ent, area_id)

            # --- robot (must be inserted BEFORE the patch is applied) ---
            conn.execute(
                "INSERT INTO robot (id, location_id, left_hand, right_hand) VALUES (1, ?, ?, ?)",
                (
                    robot_loc,
                    scenario["robot"].get("left_hand"),
                    scenario["robot"].get("right_hand"),
                ),
            )

            # --- apply initial_state_patch ---
            if task and task.get("initial_state_patch"):
                patch = task["initial_state_patch"]
                # patch entities (shallow merge: only explicitly given fields)
                for eid, overrides in patch.get("entities", {}).items():
                    self._patch_entity(conn, eid, overrides)
                # patch robot
                rp = patch.get("robot", {})
                if rp:
                    conn.execute(
                        "UPDATE robot SET location_id = ?, left_hand = ?, right_hand = ? WHERE id = 1",
                        (
                            rp.get("location", robot_loc),
                            rp.get("left_hand", scenario["robot"].get("left_hand")),
                            rp.get("right_hand", scenario["robot"].get("right_hand")),
                        ),
                    )
                # patch area_adjacency
                for adj_patch in patch.get("area_adjacency", []):
                    conn.execute(
                        "UPDATE location_edges SET passable = ? WHERE from_id = ? AND to_id = ?",
                        (int(adj_patch["passable"]), adj_patch["from"], adj_patch["to"]),
                    )

    def _patch_entity(self, conn: sqlite3.Connection, eid: str, overrides: dict) -> None:
        """Shallow-merge task patch fields onto an entity row."""
        if "states" in overrides:
            row = conn.execute(
                "SELECT states_json FROM entities WHERE id = ?", (eid,)
            ).fetchone()
            states = json.loads(row["states_json"]) if row else {}
            states.update(overrides["states"])
            conn.execute(
                "UPDATE entities SET states_json = ? WHERE id = ?",
                (json.dumps(states, ensure_ascii=False), eid),
            )
        for key, col in (("open_state", "open_state"), ("device_state", "device_state")):
            if key in overrides:
                conn.execute(
                    f"UPDATE entities SET {col} = ? WHERE id = ?",
                    (overrides[key], eid),
                )
        if "pickable" in overrides:
            conn.execute(
                "UPDATE entities SET pickable = ? WHERE id = ?",
                (int(overrides["pickable"]), eid),
            )
        # placement patches: 'in' / 'on' are mutually exclusive; parent's area wins
        if "in" in overrides:
            parent = conn.execute(
                "SELECT area_id FROM entities WHERE id = ?", (overrides["in"],)
            ).fetchone()
            if parent is None:
                raise ValueError(f"Patch for {eid}: container {overrides['in']} not found")
            conn.execute(
                "UPDATE entities SET container_id = ?, on_id = NULL, area_id = ? WHERE id = ?",
                (overrides["in"], parent["area_id"], eid),
            )
        elif "on" in overrides:
            parent = conn.execute(
                "SELECT area_id FROM entities WHERE id = ?", (overrides["on"],)
            ).fetchone()
            if parent is None:
                raise ValueError(f"Patch for {eid}: surface {overrides['on']} not found")
            conn.execute(
                "UPDATE entities SET on_id = ?, container_id = NULL, area_id = ? WHERE id = ?",
                (overrides["on"], parent["area_id"], eid),
            )
        elif "area_id" in overrides:
            conn.execute(
                "UPDATE entities SET area_id = ?, container_id = NULL, on_id = NULL WHERE id = ?",
                (overrides["area_id"], eid),
            )

    def _insert_entity(self, conn: sqlite3.Connection, ent: dict, area_id: str) -> None:
        props = ent.get("properties", [])
        conn.execute(
            """INSERT INTO entities
               (id, class, name, area_id, container_id, on_id,
                properties_json, states_json,
                pickable, is_container, is_device,
                open_state, device_state)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ent["id"],
                ent["class"],
                ent["name"],
                area_id,
                ent.get("in"),            # container_id
                ent.get("on"),            # on_id
                json.dumps(props, ensure_ascii=False),
                json.dumps(ent.get("states", {}), ensure_ascii=False),
                int(ent.get("pickable", False)),
                int("can_contain" in props),
                int(ent.get("is_device", False)),
                ent.get("open_state"),
                ent.get("device_state"),
            ),
        )

    # -- snapshot -------------------------------------------------------------

    def snapshot(self) -> dict:
        """Return a flat dict snapshot suitable for the DSL evaluator."""
        entities: dict[str, dict] = {}
        rows = self.conn.execute("SELECT * FROM entities").fetchall()
        for row in rows:
            e = _row_to_entity(row)
            e["held_by"] = None
            entities[e["id"]] = e

        # fill held_by from robot
        robot = self.conn.execute("SELECT * FROM robot WHERE id = 1").fetchone()
        if robot:
            robot_dict = dict(robot)
            for hand in ("left_hand", "right_hand"):
                eid = robot_dict.get(hand)
                if eid and eid in entities:
                    entities[eid]["held_by"] = hand

        return {
            "entities": entities,
            "robot": dict(robot) if robot else {},
        }

    # -- robot helpers --------------------------------------------------------

    def get_robot(self) -> dict:
        row = self.conn.execute("SELECT * FROM robot WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("Robot not initialised")
        return dict(row)

    def set_robot_location(self, area_id: str) -> None:
        self.conn.execute("UPDATE robot SET location_id = ? WHERE id = 1", (area_id,))

    def set_robot_hand(self, hand: str, entity_id: str | None) -> None:
        col = "left_hand" if hand == "left_hand" else "right_hand"
        self.conn.execute(f"UPDATE robot SET {col} = ? WHERE id = 1", (entity_id,))

    def robot_hand_free(self, hand: str) -> bool:
        robot = self.get_robot()
        return robot.get(hand) is None

    # -- entity helpers -------------------------------------------------------

    def get_entity(self, entity_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_entity(row)

    def entity_exists(self, entity_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return row is not None

    def _propagate_area(self, entity_id: str, area_id: str) -> None:
        """Recursively update area_id of all entities contained in / placed
        on `entity_id`, so the area_id column always reflects the true area."""
        queue = [entity_id]
        seen: set[str] = set()
        while queue:
            parent = queue.pop()
            if parent in seen:
                continue
            seen.add(parent)
            rows = self.conn.execute(
                "SELECT id FROM entities WHERE container_id = ? OR on_id = ?",
                (parent, parent),
            ).fetchall()
            for r in rows:
                self.conn.execute(
                    "UPDATE entities SET area_id = ? WHERE id = ?",
                    (area_id, r["id"]),
                )
                queue.append(r["id"])

    def set_entity_area(self, entity_id: str, area_id: str) -> None:
        self.conn.execute(
            "UPDATE entities SET area_id = ?, container_id = NULL, on_id = NULL WHERE id = ?",
            (area_id, entity_id),
        )
        self._propagate_area(entity_id, area_id)

    def set_entity_container(self, entity_id: str, container_id: str) -> None:
        container = self.get_entity(container_id)
        if container is None:
            raise ValueError(f"Container {container_id} not found")
        self.conn.execute(
            "UPDATE entities SET container_id = ?, on_id = NULL, area_id = ? WHERE id = ?",
            (container_id, container["area_id"], entity_id),
        )
        self._propagate_area(entity_id, container["area_id"])

    def set_entity_on(self, entity_id: str, surface_id: str) -> None:
        surface = self.get_entity(surface_id)
        if surface is None:
            raise ValueError(f"Surface {surface_id} not found")
        self.conn.execute(
            "UPDATE entities SET on_id = ?, container_id = NULL, area_id = ? WHERE id = ?",
            (surface_id, surface["area_id"], entity_id),
        )
        self._propagate_area(entity_id, surface["area_id"])

    def set_entity_held_by(self, entity_id: str, hand: str | None) -> None:
        """Set held_by.  If hand is None, the entity is placed in the robot's area."""
        if hand is None:
            robot = self.get_robot()
            target_area = robot["location_id"]
        else:
            target_area = "__held__"
        self.conn.execute(
            "UPDATE entities SET area_id = ?, container_id = NULL, on_id = NULL WHERE id = ?",
            (target_area, entity_id),
        )
        self._propagate_area(entity_id, target_area)

    def set_open_state(self, entity_id: str, state: str) -> None:
        self.conn.execute(
            "UPDATE entities SET open_state = ? WHERE id = ?", (state, entity_id)
        )

    def set_device_state(self, entity_id: str, state: str) -> None:
        self.conn.execute(
            "UPDATE entities SET device_state = ? WHERE id = ?", (state, entity_id)
        )

    def set_state(self, entity_id: str, key: str, value: Any) -> None:
        e = self.get_entity(entity_id)
        if e is None:
            raise ValueError(f"Entity {entity_id} not found")
        states = e.get("states", {})
        states[key] = value
        self.conn.execute(
            "UPDATE entities SET states_json = ? WHERE id = ?",
            (json.dumps(states, ensure_ascii=False), entity_id),
        )

    def blocking_closed_ancestor(self, entity_id: str) -> str | None:
        """Return the id of the first closed container on the entity's
        ancestor chain, or None if the entity is not hidden.

        An entity is hidden if any ancestor it is (transitively) *inside*
        is closed.  Items *on* a surface are never hidden by that surface,
        but the surface itself may sit inside a closed container.
        """
        visited: set[str] = set()
        current = self.get_entity(entity_id)
        while current is not None and current["id"] not in visited:
            visited.add(current["id"])
            container_id = current.get("container_id")
            if container_id is not None:
                parent = self.get_entity(container_id)
                if parent is not None and parent.get("open_state") == "closed":
                    return container_id
                current = parent
            elif current.get("on_id") is not None:
                current = self.get_entity(current["on_id"])
            else:
                return None
        return None

    # -- area helpers ---------------------------------------------------------

    def get_entities_in_area(self, area_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM entities WHERE area_id = ?", (area_id,)
        ).fetchall()
        return [_row_to_entity(r) for r in rows]

    def get_entities_in_container(self, container_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM entities WHERE container_id = ?", (container_id,)
        ).fetchall()
        return [_row_to_entity(r) for r in rows]

    def get_entities_on_surface(self, surface_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM entities WHERE on_id = ?", (surface_id,)
        ).fetchall()
        return [_row_to_entity(r) for r in rows]

    def is_passable(self, from_id: str, to_id: str) -> bool:
        row = self.conn.execute(
            "SELECT passable FROM location_edges WHERE from_id = ? AND to_id = ?",
            (from_id, to_id),
        ).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT passable FROM location_edges WHERE from_id = ? AND to_id = ?",
                (to_id, from_id),
            ).fetchone()
        return bool(row["passable"]) if row else False

    # -- action history -------------------------------------------------------

    def log_action(
        self,
        step_id: int,
        action: str,
        params: dict,
        status: str = "success",
        failure_reason: str | None = None,
        state_diff: dict | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO action_history
               (step_id, action, params_json, status, failure_reason, state_diff_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                step_id,
                action,
                json.dumps(params, ensure_ascii=False),
                status,
                failure_reason,
                json.dumps(state_diff, ensure_ascii=False) if state_diff else None,
            ),
        )
        self.conn.commit()

    def get_action_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM action_history").fetchone()
        return row["cnt"] if row else 0

    # -- subgoal tracking -----------------------------------------------------

    def set_subgoal_achieved(self, subgoal_id: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO subgoal_status (subgoal_id, achieved) VALUES (?, 1)",
            (subgoal_id,),
        )
        self.conn.commit()

    def get_achieved_subgoals(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT subgoal_id FROM subgoal_status WHERE achieved = 1"
        ).fetchall()
        return {r["subgoal_id"] for r in rows}


# ---------------------------------------------------------------------------
# Snapshot persistence helpers
# ---------------------------------------------------------------------------

def save_snapshot(ws: WorldState, path: str) -> None:
    """Save the current world state as a new SQLite database file."""
    ws.conn.commit()
    dest = sqlite3.connect(path)
    try:
        ws.conn.backup(dest)
    finally:
        dest.close()


def load_snapshot(path: str) -> WorldState:
    """Load a world state from a saved SQLite database file."""
    ws = WorldState(path)
    return ws