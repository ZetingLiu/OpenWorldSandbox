"""Structural fingerprints for scenario / task deduplication (no embedding API)."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any

from ows.schema.scenario import Scenario
from ows.schema.task import Task


def _stable(obj: Any) -> Any:
    """Recursively sort dict keys and lists for canonical serialization."""
    if isinstance(obj, dict):
        return {k: _stable(obj[k]) for k in sorted(obj)}
    if isinstance(obj, (list, tuple)):
        return [_stable(x) for x in obj]
    return obj


def _hash(payload: Any) -> str:
    blob = json.dumps(_stable(payload), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def scenario_fingerprint(scenario: Scenario) -> str:
    """Structure-only fingerprint: two scenarios with identical topology,
    entity-class/property histograms and placement graphs are duplicates
    even if names differ."""
    class_hist = Counter(e.class_.value for e in scenario.all_entities.values())
    props_by_class: dict[str, list[tuple[str, ...]]] = {}
    for e in scenario.all_entities.values():
        props_by_class.setdefault(e.class_.value, []).append(tuple(sorted(e.properties)))
    for k in props_by_class:
        props_by_class[k].sort()
    placement_edges = sorted(
        (
            eid,
            (e.on or e.in_),
            "on" if e.on is not None else "in",
        )
        for eid, e in scenario.all_entities.items()
        if e.on is not None or e.in_ is not None
    )
    return _hash(
        {
            "areas": sorted(a.id for a in scenario.areas),
            "n_entities": len(scenario.all_entities),
            "class_hist": dict(sorted(class_hist.items())),
            "props_by_class": props_by_class,
            "placement_edges": placement_edges,
            "robot_location": scenario.robot.location,
        }
    )


_INSTRUCTION_NORM = re.compile(r"[\s　，。！？、；：""''（）《》【】,.!?;:'\"()]")


def task_fingerprint(task: Task) -> str:
    """Fingerprint over normalized goal + normalized instruction + tags."""
    instruction = _INSTRUCTION_NORM.sub("", task.instruction).lower()
    return _hash(
        {
            "scenario_id": task.scenario_id,
            "task_type": task.task_type.value,
            "tags": sorted(t.value for t in task.capability_tags),
            "goal": task.goal,
            "instruction": instruction,
        }
    )


class DedupSet:
    """Seen-fingerprint registry; returns whether a fingerprint is new."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def add_if_new(self, fingerprint: str) -> bool:
        """True if not seen before (and now registered), False if duplicate."""
        if fingerprint in self._seen:
            return False
        self._seen.add(fingerprint)
        return True
