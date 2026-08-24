"""Task runner: single-task and batch execution with trajectory recording.

The runner:
1. Loads compiled SQLite snapshot
2. Starts the environment server
3. Runs the agent against it
4. Records trajectory + final state snapshot
5. Shuts down the server
"""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

from loguru import logger

from ows.tools import (
    tools_jsonl_load,
    tools_json_save,
    get_random_available_port,
    async_wait_for_http_endpoint,
    resolve_llm_config,
)
from ows.env.world import WorldState, save_snapshot


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class RunnerConfig:
    # Path to the compiled task DB (or directory of them)
    db_path: str
    # Task instruction (overrides any embedded task)
    task: str | None = None
    # LLM overrides
    api_url: str | None = None
    model: str | None = None
    # Agent settings (None → use the task's max_steps from the sidecar meta)
    max_iterations: int | None = None
    temperature: float = 1.0
    max_tokens: int = 2048
    # Output
    output_dir: str = "outputs/runs"
    # Server
    host: str = "127.0.0.1"
    port: int | None = None
    # Batch mode
    batch: bool = False


# ---------------------------------------------------------------------------
# Run a single task
# ---------------------------------------------------------------------------

def _environment_hint(db_path: str) -> str:
    """Describe the compiled world rather than re-reading source JSON.

    This keeps the prompt aligned with task-level state patches, and works
    when ``ows`` is invoked outside the repository root.
    """
    ws = WorldState(db_path)
    try:
        locations = ws.conn.execute(
            "SELECT id, name FROM locations WHERE id != ? ORDER BY id",
            ("__held__",),
        ).fetchall()
        edges = ws.conn.execute(
            """SELECT from_id, to_id FROM location_edges
               WHERE passable = 1 AND from_id != ? AND to_id != ?
               ORDER BY from_id, to_id""",
            ("__held__", "__held__"),
        ).fetchall()
        robot = ws.get_robot()
    finally:
        ws.close()

    area_names = ", ".join(f"{row['name']}({row['id']})" for row in locations)
    adjacency = "; ".join(f"{row['from_id']}<->{row['to_id']}" for row in edges)
    return (
        f"\n\n[Environment info] Available areas: {area_names}. "
        f"You start in area '{robot['location_id']}'. Adjacency: {adjacency}"
    )


async def run_single_task(config: RunnerConfig) -> dict[str, Any]:
    """Run one task end-to-end.

    Returns
    -------
    dict
        Run report with keys: task, model, trajectory, final_db, etc.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    # Load sidecar task meta written by the compiler (<task_id>.meta.json)
    task_meta: dict[str, Any] = {}
    meta_path = Path(config.db_path).with_suffix(".meta.json")
    if meta_path.exists():
        with open(meta_path, "r", encoding="utf-8") as f:
            task_meta = json.load(f)

    task_instruction = config.task or task_meta.get("instruction")
    if not task_instruction:
        raise ValueError(
            "No task instruction: provide --task or compile the task so that "
            f"a sidecar meta file exists next to {config.db_path}"
        )
    max_iterations = config.max_iterations or task_meta.get("max_steps") or 30

    output_dir = os.path.join(
        config.output_dir,
        f"{task_meta.get('task_id', Path(config.db_path).stem)}_{timestamp}",
    )
    os.makedirs(output_dir, exist_ok=True)
    if meta_path.exists():
        shutil.copy2(meta_path, os.path.join(output_dir, "task.meta.json"))

    # Append the actual compiled topology so the model never guesses IDs.
    task_instruction += _environment_hint(config.db_path)

    # Copy initial DB
    initial_db = os.path.join(output_dir, "initial.db")
    shutil.copy2(config.db_path, initial_db)

    # Working DB
    working_db = os.path.join(output_dir, "working.db")
    shutil.copy2(config.db_path, working_db)

    async with _environment_server(
        working_db, config.host, config.port, output_dir
    ) as (connect_host, port):
        mcp_url = f"http://{connect_host}:{port}/mcp"

        # Run agent
        from ows.run.agent import AgentConfig, run_agent

        agent_config = AgentConfig(
            task=task_instruction,
            mcp_url=mcp_url,
            api_url=config.api_url,
            model=config.model,
            max_iterations=max_iterations,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            output_dir=output_dir,
            verbose=True,
            task_id=task_meta.get("task_id"),
            scenario_id=task_meta.get("scenario_id"),
        )

        trajectory = await run_agent(agent_config)

        # Save final DB snapshot
        final_db = os.path.join(output_dir, "final.db")
        shutil.copy2(working_db, final_db)

        report = {
            "task": task_instruction,
            "task_id": task_meta.get("task_id"),
            "scenario_id": task_meta.get("scenario_id"),
            "model": config.model,
            "output_dir": output_dir,
            "initial_db": initial_db,
            "final_db": final_db,
            "trajectory": trajectory,
            "timestamp": timestamp,
        }

        tools_json_save(report, os.path.join(output_dir, "report.json"))
        return report


_READY_PROBE_PATH = "/action/observe_scene"


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """Stop the server together with anything it spawned.

    The server runs in its own session, so signalling the whole group keeps
    worker processes from surviving as orphans that still hold the port.
    """
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None

    try:
        if pgid is None:
            proc.terminate()
        else:
            os.killpg(pgid, signal.SIGTERM)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if pgid is None:
            proc.kill()
        else:
            os.killpg(pgid, signal.SIGKILL)
        proc.wait()
    except OSError:
        pass


async def _await_ready(
    proc: subprocess.Popen, host: str, port: int, timeout: float
) -> bool:
    """Poll the readiness endpoint, giving up as soon as the server dies."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False        # exited early, typically a lost port race
        if await async_wait_for_http_endpoint(
            host, port, _READY_PROBE_PATH, timeout=1.0
        ):
            return True
    return False


@asynccontextmanager
async def _environment_server(
    db_path: str,
    host: str,
    requested_port: int | None,
    output_dir: str,
    attempts: int = 3,
) -> AsyncIterator[tuple[str, int]]:
    """Run the environment server for the duration of the block.

    ``get_random_available_port`` releases the port before uvicorn binds it,
    so a concurrent run can take it first.  When the caller did not pin a
    port, losing that race is retried on a fresh one; a pinned port fails
    loudly instead of silently moving somewhere the caller is not watching.
    """
    connect_host = "127.0.0.1" if host == "0.0.0.0" else host
    log_path = os.path.join(output_dir, "server.log")

    for attempt in range(1, attempts + 1):
        port = requested_port or get_random_available_port()
        cmd = [sys.executable, "-m", "ows.env.server", db_path, str(port), host]
        logger.info(f"Starting server: {' '.join(cmd)}")

        # Appended, so a failed attempt's diagnostics survive the next one.
        with open(log_path, "a", encoding="utf-8") as log_f:
            proc = subprocess.Popen(
                cmd,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                if await _await_ready(proc, connect_host, port, timeout=60.0):
                    yield connect_host, port
                    return
                if requested_port is not None:
                    raise RuntimeError(
                        f"Server failed to start on requested port {port}; "
                        f"see {log_path}"
                    )
                remaining = attempts - attempt
                logger.warning(
                    f"Server did not come up on port {port} "
                    f"(attempt {attempt}/{attempts})"
                    + ("; retrying on a new port" if remaining else "")
                )
            finally:
                _terminate_process_group(proc)

    raise RuntimeError(
        f"Server failed to start after {attempts} attempts; see {log_path}"
    )


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def run(config: RunnerConfig) -> None:
    if config.batch:
        _run_batch(config)
    else:
        asyncio.run(run_single_task(config))


def _run_batch(config: RunnerConfig) -> None:
    """Run all .db files in a directory."""
    db_dir = Path(config.db_path)
    if db_dir.is_dir():
        db_files = sorted(db_dir.glob("*.db"))
    else:
        db_files = [db_dir]

    results = []
    for db_file in db_files:
        logger.info(f"Running task: {db_file.name}")
        cfg = RunnerConfig(
            db_path=str(db_file),
            task=config.task,
            api_url=config.api_url,
            model=config.model,
            max_iterations=config.max_iterations,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            output_dir=config.output_dir,
            host=config.host,
            port=config.port,
        )
        try:
            report = asyncio.run(run_single_task(cfg))
            results.append({"db": str(db_file), "status": "ok", "report": report})
        except Exception as e:
            logger.error(f"Failed {db_file.name}: {e}")
            results.append({"db": str(db_file), "status": "error", "error": str(e)})

    tools_json_save(results, os.path.join(config.output_dir, "batch_results.json"))


if __name__ == "__main__":
    from simpleArgParser import parse_args
    cfg: RunnerConfig = parse_args(RunnerConfig)
    run(cfg)