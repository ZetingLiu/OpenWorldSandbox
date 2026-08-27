"""LLM-backed commonsense gate for physical world-state transitions."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from json_repair import repair_json
from loguru import logger
from openai import AsyncOpenAI

from ows.env.world import WorldState


JUDGED_ACTIONS = {
    "pick_object",
    "place_object",
    "open_container",
    "close_container",
    "hang_object",
    "start_device",
    "stop_device",
    "apply_physical_tool",
}

SYSTEM_PROMPT = """You are the independent physical-commonsense judge for an
embodied-agent benchmark. Decide whether ONE proposed action is immediately
physically reasonable in the supplied current state.

Judge only feasibility of this single atomic action, not whether it is optimal
or completes the user's task. State changes not named in the proposed action
cannot be assumed. In particular, an agent with both hands occupied cannot
open/close a container or operate a device unless the state explicitly provides
another suitable manipulator. Reject actions requiring an unavailable hand,
using inaccessible/hidden objects, physically incompatible placement, unsafe
device operation, or other clear commonsense contradictions.

Return one JSON object only:
{
  "allowed": true or false,
  "reason": "concise reason grounded in the current state",
  "violated_constraints": ["short_constraint_name"],
  "confidence": number from 0 to 1
}
When there is no clear physical contradiction, allow the action."""


@dataclass
class JudgeDecision:
    allowed: bool
    reason: str
    violated_constraints: list[str]
    confidence: float
    action: str
    params: dict[str, Any]
    model: str
    timestamp: str
    error: str | None = None
    raw_response: str | None = None


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def judge_enabled() -> bool:
    return _env_bool("OWB_COMMONSENSE_JUDGE_ENABLED", False)


def _entity_summary(entity: dict[str, Any] | None) -> dict[str, Any] | None:
    if entity is None:
        return None
    return {
        key: entity.get(key)
        for key in (
            "id", "name", "class", "area_id", "container_id", "on_id",
            "held_by", "open_state", "device_state", "pickable", "is_device",
            "properties", "states",
        )
    }


def _state_context(ws: WorldState, params: dict[str, Any]) -> dict[str, Any]:
    snapshot = ws.snapshot()
    robot = snapshot.get("robot", {})
    entities = snapshot.get("entities", {})
    current_area = robot.get("location_id") or robot.get("location")

    relevant_ids = {
        value
        for key, value in params.items()
        if key.endswith("_id") and isinstance(value, str)
    }
    for hand in ("left_hand", "right_hand"):
        if robot.get(hand):
            relevant_ids.add(robot[hand])

    nearby = [
        _entity_summary(entity)
        for entity in entities.values()
        if entity.get("area_id") in {current_area, "__held__"}
    ]
    relevant = {
        entity_id: _entity_summary(entities.get(entity_id))
        for entity_id in sorted(relevant_ids)
    }
    return {
        "robot": robot,
        "relevant_entities": relevant,
        "entities_in_current_area_or_held": nearby,
    }


class CommonsenseJudge:
    def __init__(self, db_path: str):
        self.enabled = judge_enabled()
        self.fail_closed = _env_bool("OWB_COMMONSENSE_JUDGE_FAIL_CLOSED", True)
        self.model = (
            os.getenv("OWB_COMMONSENSE_JUDGE_MODEL")
            or os.getenv("AWM_SYN_OVERRIDE_MODEL")
            or ""
        )
        self.base_url = (
            os.getenv("OWB_COMMONSENSE_JUDGE_API_BASE_URL")
            or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        )
        self.api_key = (
            os.getenv("OWB_COMMONSENSE_JUDGE_API_KEY")
            or os.getenv("OPENAI_API_KEY", "")
        )
        self.log_path = Path(db_path).resolve().parent / "judge_decisions.jsonl"
        self.client = (
            AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
            if self.enabled and self.model and self.api_key
            else None
        )

    async def evaluate(
        self, ws: WorldState, action: str, params: dict[str, Any]
    ) -> JudgeDecision | None:
        if not self.enabled or action not in JUDGED_ACTIONS:
            return None

        timestamp = datetime.now(timezone.utc).isoformat()
        if self.client is None:
            decision = JudgeDecision(
                allowed=not self.fail_closed,
                reason="Commonsense judge is enabled but its API configuration is incomplete.",
                violated_constraints=["judge_unavailable"],
                confidence=1.0,
                action=action,
                params=params,
                model=self.model,
                timestamp=timestamp,
                error="Missing judge model or API key",
            )
            self._log(decision)
            return decision

        payload = {
            "current_state": _state_context(ws, params),
            "proposed_action": {"action": action, "params": params},
        }
        raw: str | None = None
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                ],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=2048,
            )
            raw = response.choices[0].message.content or "{}"
            parsed = json.loads(repair_json(raw))
            allowed = parsed.get("allowed")
            if isinstance(allowed, str):
                normalized = allowed.strip().lower()
                if normalized in {"true", "yes", "allow", "allowed"}:
                    allowed = True
                elif normalized in {"false", "no", "reject", "rejected", "deny", "denied"}:
                    allowed = False
            if allowed is None:
                normalized = str(parsed.get("decision", "")).strip().lower()
                if normalized in {"allow", "allowed", "approve", "approved"}:
                    allowed = True
                elif normalized in {"reject", "rejected", "deny", "denied"}:
                    allowed = False
            if not isinstance(allowed, bool):
                raise ValueError("Judge response has no boolean 'allowed'")
            decision = JudgeDecision(
                allowed=allowed,
                reason=str(parsed.get("reason") or "No reason supplied"),
                violated_constraints=[
                    str(item) for item in parsed.get("violated_constraints", [])
                ],
                confidence=max(0.0, min(1.0, float(parsed.get("confidence", 0.0)))),
                action=action,
                params=params,
                model=self.model,
                timestamp=timestamp,
                raw_response=raw,
            )
        except Exception as exc:
            logger.exception(f"Commonsense judge failed for {action}: {exc}")
            decision = JudgeDecision(
                allowed=not self.fail_closed,
                reason=f"Commonsense judge unavailable: {type(exc).__name__}",
                violated_constraints=["judge_error"],
                confidence=1.0,
                action=action,
                params=params,
                model=self.model,
                timestamp=timestamp,
                error=str(exc),
                raw_response=raw,
            )

        self._log(decision)
        return decision

    def _log(self, decision: JudgeDecision) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(asdict(decision), ensure_ascii=False) + "\n")
