"""Environment event triggers.

首期仅接口与数据结构，不实现触发逻辑（phase ③ 实现）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    time = "time"               # time-based trigger
    state_change = "state_change"   # entity state change
    area_entry = "area_entry"       # robot enters area
    interaction = "interaction"     # entity interaction


@dataclass
class EnvironmentEvent:
    """An environment event that may trigger when conditions are met.

    Phase ③: implement trigger logic in ``ows/env/events.py``.
    """
    event_id: str
    event_type: EventType
    description: str
    trigger_condition: dict[str, Any] = field(default_factory=dict)
    effects: list[dict[str, Any]] = field(default_factory=list)
    one_shot: bool = True          # fire only once
    fired: bool = False


# Stub — no events register or fire in phase ①/②

def check_events(_ws: Any, _step: int) -> list[EnvironmentEvent]:
    """Placeholder: return empty list.  Phase ③ will implement."""
    return []