"""End-to-end test script: compile → server → agent run → verify.

Usage:  python scripts/test_e2e.py
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT)
sys.path.insert(0, str(PROJECT))
load_dotenv()


async def main():
    task_db = PROJECT / "outputs/compiled/home_01_umbrella_move.db"
    meta_path = task_db.with_suffix(".meta.json")

    if not task_db.exists():
        print("ERROR: compiled DB not found. Run compile first.")
        return

    with open(meta_path) as f:
        meta = json.load(f)

    # ── Setup output dir ────────────────────────────────────────
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = PROJECT / "outputs/runs" / f"e2e_umbrella_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(task_db, run_dir / "initial.db")
    shutil.copy2(task_db, run_dir / "working.db")
    shutil.copy2(meta_path, run_dir / "task.meta.json")

    # ── Start server ────────────────────────────────────────────
    port = 18765
    cmd = [sys.executable, "-m", "ows.env.server", str(run_dir / "working.db"), str(port)]
    log_f = open(run_dir / "server.log", "w")
    proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT)
    print(f"Server PID={proc.pid} on :{port}")

    # ── Wait until ready ────────────────────────────────────────
    import httpx

    async def wait_server():
        for _ in range(60):
            try:
                async with httpx.AsyncClient() as c:
                    r = await c.get(f"http://127.0.0.1:{port}/action/observe_scene", timeout=5)
                    if r.status_code == 200:
                        return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return False

    if not await wait_server():
        print("ERROR: server failed to start")
        proc.terminate(); proc.wait()
        return
    print("Server ready ✓")

    # ── Run agent ───────────────────────────────────────────────
    try:
        from ows.run.agent import AgentConfig, run_agent

        cfg = AgentConfig(
            task=meta["instruction"],
            mcp_url=f"http://127.0.0.1:{port}/mcp",
            max_iterations=meta["max_steps"],
            temperature=1.0,
            max_tokens=2048,
            output_dir=str(run_dir),
            verbose=False,
            task_id=meta["task_id"],
            scenario_id=meta["scenario_id"],
        )

        traj = await run_agent(cfg)

    except Exception as e:
        print(f"Agent error: {e}")
        import traceback; traceback.print_exc()
        return
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("Server stopped")

    # ── Save final DB ───────────────────────────────────────────
    shutil.copy2(run_dir / "working.db", run_dir / "final.db")

    # ── Verify ──────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"Trajectory iterations: {traj['total_iterations']}")
    print(f"Max steps: {meta['max_steps']}")

    # Check final actions
    last_actions = []
    for entry in traj.get("trajectory", []):
        for tc in entry.get("tool_calls", []):
            last_actions.append(tc["name"])
    print(f"Tool calls: {json.dumps(last_actions)}")

    # Run verifier
    from ows.eval.verify import VerifyConfig, verify_run

    vcfg = VerifyConfig(
        input_dir=str(run_dir),
        tasks_dir=str(PROJECT / "data/tasks"),
    )
    result = verify_run(vcfg)
    print(f"\nVerification: {result['result']}")
    print(f"Goal satisfied: {result['goal_satisfied']}")
    print(f"Subgoals: {json.dumps(result['subgoals'], ensure_ascii=False)}")

    # ── Run diagnostics ─────────────────────────────────────────
    from ows.eval.diagnose import DiagnoseConfig, diagnose_trajectory

    dcfg = DiagnoseConfig(input_dir=str(run_dir))
    diag = diagnose_trajectory(dcfg)
    print(f"\nDiagnostics: total_calls={diag['tool_calls']}, "
          f"failed={diag['failed_actions']}, "
          f"termination={diag['termination_type']}")

    print(f"\nOutput: {run_dir}")
    return result["result"]


if __name__ == "__main__":
    outcome = asyncio.run(main())
    print(f"\n{'='*60}")
    print(f"RESULT: {outcome}")
