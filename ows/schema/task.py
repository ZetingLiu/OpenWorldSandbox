"""Task JSON Pydantic models.

Mirrors data/tasks/README.md spec v0.1.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TaskType(str, Enum):
    direct = "direct"
    composite = "composite"


class CapabilityTag(str, Enum):
    navigation = "navigation"
    pick_and_place = "pick_and_place"
    container_open_close = "container_open_close"
    device_operation = "device_operation"
    multi_step = "multi_step"
    tool_use = "tool_use"
    state_awareness = "state_awareness"
    hand_management = "hand_management"
    search = "search"
    error_recovery = "error_recovery"


# ---------------------------------------------------------------------------
# DSL Goal Condition  (recursive)
# ---------------------------------------------------------------------------

class _GoalConditionBase(BaseModel):
    """Base for recursive condition model — use validate_condition factory."""

    pass


# We use a forward-ref / discriminated-union approach so the models
# can be serialised/deserialised cleanly.  See `goal_dsl.py` for the
# actual evaluator — these models just describe the *shape*.

class ConditionEq(BaseModel):
    entity: str
    field: str
    op: str = "eq"
    value: Any


class ConditionIn(BaseModel):
    entity: str
    field: str
    op: str = "in"
    value: list[Any]


class ConditionAllOf(BaseModel):
    op: str = "all_of"
    conditions: list[dict[str, Any]]  # recursive — raw dicts to avoid circular Pydantic


class ConditionAnyOf(BaseModel):
    op: str = "any_of"
    conditions: list[dict[str, Any]]


class ConditionCountWhere(BaseModel):
    field: str
    op: str = "eq"
    value: Any


class ConditionCount(BaseModel):
    op: str = "count"
    entity_class: str
    where: ConditionCountWhere
    cmp: str  # eq, neq, gt, gte, lt, lte
    value: int


# ---------------------------------------------------------------------------
# Subgoal
# ---------------------------------------------------------------------------

class Subgoal(BaseModel):
    id: str
    description: str
    cond: dict[str, Any]  # raw GoalCondition dict


# ---------------------------------------------------------------------------
# Walkthrough
# ---------------------------------------------------------------------------

class WalkthroughAction(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)


class Walkthrough(BaseModel):
    description: str
    actions: list[WalkthroughAction]


# ---------------------------------------------------------------------------
# Initial state patch
# ---------------------------------------------------------------------------

class InitialStatePatch(BaseModel):
    entities: dict[str, dict[str, Any]] = Field(default_factory=dict)
    robot: dict[str, Any] = Field(default_factory=dict)
    area_adjacency: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Task (top-level)
# ---------------------------------------------------------------------------

class Task(BaseModel):
    task_id: str
    spec_version: str = "0.1"
    scenario_id: str
    name: str
    instruction: str
    task_type: TaskType
    capability_tags: list[CapabilityTag]
    max_steps: int
    initial_state_patch: InitialStatePatch = Field(default_factory=InitialStatePatch)
    goal: dict[str, Any]
    subgoals: list[Subgoal] = Field(default_factory=list)
    walkthroughs: list[Walkthrough] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_walkthroughs(self) -> "Task":
        if not self.walkthroughs:
            raise ValueError(
                f"Task {self.task_id}: at least one walkthrough is required"
            )
        for i, w in enumerate(self.walkthroughs):
            if not w.actions:
                raise ValueError(
                    f"Task {self.task_id}: walkthrough[{i}] has no actions"
                )
        return self