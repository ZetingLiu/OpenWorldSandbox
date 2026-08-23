"""Universal FastAPI + MCP environment service.

Loads a compiled SQLite snapshot and exposes the 17 semantic actions as
MCP tools.  One server instance serves one scenario+task combination.

The server is *data-driven*: it reads the world state from a SQLite
database and modifies it in-place.  No code generation per scenario.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi_mcp import FastApiMCP
from loguru import logger

from owb.env.world import WorldState
from owb.env.actions import ActionResult, execute_action, get_available_actions
from owb.env.commonsense_judge import CommonsenseJudge
from owb.env.observe import generate_observation, generate_entity_detail


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class ServerConfig:
    db_path: str                      # path to compiled SQLite snapshot
    host: str = "127.0.0.1"
    port: int = 8001

    def pre_process(self) -> None:
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(f"Database not found: {self.db_path}")


# ---------------------------------------------------------------------------
# Global world state (one per server process)
# ---------------------------------------------------------------------------

_world_state: WorldState | None = None


def get_world_state() -> WorldState:
    if _world_state is None:
        raise RuntimeError("World state not initialised")
    return _world_state


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------

def create_app(db_path: str) -> FastAPI:
    """Create a FastAPI app with the 17 semantic actions as endpoints."""
    global _world_state
    _world_state = WorldState(db_path)
    judge = CommonsenseJudge(db_path)

    app = FastAPI(
        title="OpenWorldSandbox Environment",
        description="Data-driven embodied agent sandbox environment with 17 semantic actions",
        version="0.1.0",
    )

    async def execute_with_judge(
        action_name: str, params: dict[str, Any]
    ) -> ActionResult:
        """Judge a physical action before allowing its state transition."""
        ws = get_world_state()
        decision = await judge.evaluate(ws, action_name, params)
        if decision is not None and not decision.allowed:
            reason = f"Commonsense judge rejected action: {decision.reason}"
            logger.info(f"{action_name} rejected by commonsense judge: {decision.reason}")
            ws.log_action(
                step_id=ws.get_action_count() + 1,
                action=action_name,
                params=params,
                status="failure",
                failure_reason=reason,
            )
            return ActionResult(
                status="failure",
                failure_reason=reason,
                observation=reason,
                data={
                    "judge": {
                        "allowed": decision.allowed,
                        "reason": decision.reason,
                        "violated_constraints": decision.violated_constraints,
                        "confidence": decision.confidence,
                        "model": decision.model,
                    }
                },
            )
        return execute_action(ws, action_name, params)

    # ------------------------------------------------------------------
    # Perception endpoints
    # ------------------------------------------------------------------

    @app.get("/action/observe_scene")
    async def observe_scene() -> dict[str, Any]:
        """Describe the current area: visible entities, held items."""
        result = execute_action(get_world_state(), "observe_scene", {})
        obs = generate_observation(get_world_state())
        return {"status": result.status, "observation": obs, "data": result.data}

    @app.get("/action/search_object")
    async def search_object(entity_id: str) -> dict[str, Any]:
        """Search for an entity in the current area."""
        result = execute_action(get_world_state(), "search_object", {"entity_id": entity_id})
        return {"status": result.status, "observation": result.observation, "data": result.data}

    @app.get("/action/inspect_entity")
    async def inspect_entity(entity_id: str) -> dict[str, Any]:
        """Get detailed information about an entity."""
        result = execute_action(get_world_state(), "inspect_entity", {"entity_id": entity_id})
        detail = None
        if result.status == "success":
            detail = generate_entity_detail(get_world_state(), entity_id)
        return {"status": result.status, "observation": detail or result.observation, "data": result.data}

    @app.get("/action/check_robot_state")
    async def check_robot_state() -> dict[str, Any]:
        """Check the robot's current location and hands."""
        result = execute_action(get_world_state(), "check_robot_state", {})
        return {"status": result.status, "observation": result.observation, "data": result.data}

    # ------------------------------------------------------------------
    # Movement endpoint
    # ------------------------------------------------------------------

    @app.post("/action/move_to")
    async def move_to(area_id: str) -> dict[str, Any]:
        """Move to another area."""
        result = execute_action(get_world_state(), "move_to", {"area_id": area_id})
        obs = generate_observation(get_world_state()) if result.status == "success" else result.observation
        return {"status": result.status, "observation": obs, "data": result.data}

    # ------------------------------------------------------------------
    # Operation endpoints
    # ------------------------------------------------------------------

    @app.post("/action/pick_object")
    async def pick_object(entity_id: str) -> dict[str, Any]:
        """Pick up an entity."""
        result = await execute_with_judge("pick_object", {"entity_id": entity_id})
        return {"status": result.status, "observation": result.observation, "data": result.data}

    @app.post("/action/place_object")
    async def place_object(entity_id: str, target_id: str) -> dict[str, Any]:
        """Place an entity into a container or onto a surface."""
        result = await execute_with_judge(
            "place_object",
            {"entity_id": entity_id, "target_id": target_id},
        )
        return {"status": result.status, "observation": result.observation, "data": result.data}

    @app.post("/action/open_container")
    async def open_container(entity_id: str) -> dict[str, Any]:
        """Open a container."""
        result = await execute_with_judge("open_container", {"entity_id": entity_id})
        return {"status": result.status, "observation": result.observation, "data": result.data}

    @app.post("/action/close_container")
    async def close_container(entity_id: str) -> dict[str, Any]:
        """Close a container."""
        result = await execute_with_judge("close_container", {"entity_id": entity_id})
        return {"status": result.status, "observation": result.observation, "data": result.data}

    @app.post("/action/hang_object")
    async def hang_object(entity_id: str, target_id: str) -> dict[str, Any]:
        """Hang an entity on a hangable surface."""
        result = await execute_with_judge(
            "hang_object",
            {"entity_id": entity_id, "target_id": target_id},
        )
        return {"status": result.status, "observation": result.observation, "data": result.data}

    # ------------------------------------------------------------------
    # Device endpoints
    # ------------------------------------------------------------------

    @app.post("/action/start_device")
    async def start_device(entity_id: str) -> dict[str, Any]:
        """Start a device."""
        result = await execute_with_judge("start_device", {"entity_id": entity_id})
        return {"status": result.status, "observation": result.observation, "data": result.data}

    @app.post("/action/stop_device")
    async def stop_device(entity_id: str) -> dict[str, Any]:
        """Stop a device."""
        result = await execute_with_judge("stop_device", {"entity_id": entity_id})
        return {"status": result.status, "observation": result.observation, "data": result.data}

    # ------------------------------------------------------------------
    # Tool endpoint
    # ------------------------------------------------------------------

    @app.post("/action/apply_physical_tool")
    async def apply_physical_tool(
        tool_id: str, target_id: str, intended_effect: str | None = None
    ) -> dict[str, Any]:
        """Use a physical tool on a target entity."""
        result = await execute_with_judge(
            "apply_physical_tool",
            {"tool_id": tool_id, "target_id": target_id, "intended_effect": intended_effect},
        )
        return {"status": result.status, "observation": result.observation, "data": result.data}

    # ------------------------------------------------------------------
    # Termination endpoints
    # ------------------------------------------------------------------

    @app.post("/action/finish_task")
    async def finish_task() -> dict[str, Any]:
        """Signal task completion."""
        result = execute_action(get_world_state(), "finish_task", {})
        return {"status": result.status, "observation": result.observation, "data": result.data}

    @app.post("/action/report_target_absent")
    async def report_target_absent(entity_id: str) -> dict[str, Any]:
        """Report that the target entity is not found."""
        result = execute_action(get_world_state(), "report_target_absent", {"entity_id": entity_id})
        return {"status": result.status, "observation": result.observation, "data": result.data}

    @app.post("/action/report_unable_to_continue")
    async def report_unable_to_continue(reason: str = "unspecified") -> dict[str, Any]:
        """Report inability to continue the task."""
        result = execute_action(get_world_state(), "report_unable_to_continue", {"reason": reason})
        return {"status": result.status, "observation": result.observation, "data": result.data}

    @app.post("/action/abandon_task")
    async def abandon_task() -> dict[str, Any]:
        """Abandon the current task."""
        result = execute_action(get_world_state(), "abandon_task", {})
        return {"status": result.status, "observation": result.observation, "data": result.data}

    # ------------------------------------------------------------------
    # MCP mount
    # ------------------------------------------------------------------

    mcp = FastApiMCP(app)
    mcp.mount_http()

    return app


# ---------------------------------------------------------------------------
# Server entry-point
# ---------------------------------------------------------------------------

def run_server(config: ServerConfig) -> None:
    """Start the environment server (blocking)."""
    import uvicorn

    app = create_app(config.db_path)
    logger.info(f"Starting OpenWorldSandbox environment server on {config.host}:{config.port}")
    logger.info(f"Database: {config.db_path}")
    logger.info(f"MCP endpoint: http://{config.host}:{config.port}/mcp")

    uvicorn.run(app, host=config.host, port=config.port)


def run(config: ServerConfig) -> None:
    run_server(config)


if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else ":memory:"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8001
    host = sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1"
    cfg = ServerConfig(db_path=db, host=host, port=port)
    cfg.pre_process()
    run_server(cfg)
