"""Scenario JSON Pydantic models.

Mirrors data/scenarios/README.md spec v0.1.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EntityClass(str, Enum):
    furniture = "furniture"
    container = "container"
    device = "device"
    clothing = "clothing"
    item = "item"
    consumable = "consumable"
    tool = "tool"
    fixture = "fixture"


class Property(str, Enum):
    # receiver-side
    can_support = "can_support"
    can_contain = "can_contain"
    can_hang = "can_hang"
    can_wash = "can_wash"
    hangable_inside = "hangable_inside"
    has_water = "has_water"
    # Starting such a device records an irreversible completion mark, so a
    # transaction survives the device being switched off afterwards.
    transactional = "transactional"
    # item-side
    portable = "portable"
    absorbent = "absorbent"
    soft = "soft"
    waterproof = "waterproof"
    hangable = "hangable"
    washable = "washable"


class OpenState(str, Enum):
    open = "open"
    closed = "closed"


class DeviceState(str, Enum):
    off = "off"
    running = "running"


# ---------------------------------------------------------------------------
# Area & adjacency
# ---------------------------------------------------------------------------

class Area(BaseModel):
    id: str
    name: str


class Adjacency(BaseModel):
    from_: str = Field(alias="from")
    to: str
    passable: bool = True


# ---------------------------------------------------------------------------
# Entity
# ---------------------------------------------------------------------------

class Entity(BaseModel):
    id: str
    class_: EntityClass = Field(alias="class")
    name: str
    pickable: bool = False
    on: Optional[str] = None
    in_: Optional[str] = Field(default=None, alias="in")
    is_device: bool = False
    open_state: Optional[OpenState] = None
    device_state: Optional[DeviceState] = None
    properties: list[Property] = Field(default_factory=list)
    states: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_placement_exclusive(self) -> "Entity":
        if self.on is not None and self.in_ is not None:
            raise ValueError(
                f"Entity {self.id}: 'on' and 'in' are mutually exclusive"
            )
        return self

    @property
    def container_id(self) -> Optional[str]:
        """Return the container this entity is inside (via 'in'), or None."""
        return self.in_

    @property
    def surface_id(self) -> Optional[str]:
        """Return the surface this entity is on (via 'on'), or None."""
        return self.on


# ---------------------------------------------------------------------------
# Robot
# ---------------------------------------------------------------------------

class Robot(BaseModel):
    location: str
    left_hand: Optional[str] = None
    right_hand: Optional[str] = None


# ---------------------------------------------------------------------------
# Scenario (top-level)
# ---------------------------------------------------------------------------

class Scenario(BaseModel):
    scenario_id: str
    spec_version: str = "0.1"
    name: str
    description: Optional[str] = None
    areas: list[Area]
    area_adjacency: list[Adjacency]
    area_tables: dict[str, list[Entity]]
    robot: Robot

    # --- derived helpers ---

    @property
    def all_entities(self) -> dict[str, Entity]:
        """Return flat {entity_id: Entity} across all areas."""
        result: dict[str, Entity] = {}
        for entities in self.area_tables.values():
            for e in entities:
                result[e.id] = e
        return result

    @property
    def passable_graph(self) -> dict[str, set[str]]:
        """Undirected graph of passable area connections."""
        g: dict[str, set[str]] = {}
        for a in self.areas:
            g.setdefault(a.id, set())
        for adj in self.area_adjacency:
            if adj.passable:
                g.setdefault(adj.from_, set()).add(adj.to)
                g.setdefault(adj.to, set()).add(adj.from_)
        return g