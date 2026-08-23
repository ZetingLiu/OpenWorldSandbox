<p align="center">
  <img src="figures/owb.png" alt="OpenWorldSandbox" width="420"/>
</p>

# OpenWorldSandbox

An embodied agent **sandbox** for model **reasoning** in real-world service scenarios.

[中文说明](README_zh.md)

## Overview

OpenWorldSandbox evaluates whether vision-language models acting as an **embodied brain** can complete full business tasks through **multi-turn high-level semantic tool calls** under **partial observability**. Each turn, the model outputs one structured action from the task instruction, current observation, and interaction history. The environment keeps hidden world state in SQLite, returns state changes and structured failure reasons after each action, and scores runs with a **goal DSL** plus **full trajectory diagnostics**.

Phase 1 targets **home service** and **retail service** scenarios: navigation, pick-and-place, containers and devices, dual-hand state, and multi-step planning. It does **not** evaluate joint control or low-level motion planning.

## Pipeline

```
Scenario/task JSON → compile → SQLite initial snapshot → MCP env (17 semantic actions)
→ model multi-turn tool calling → trajectory log → DSL verification + diagnostic report
```

| Stage | Command | Module |
|-------|---------|--------|
| Compile | `owb compile` | `owb/env/compile.py` |
| Environment | `owb env start` | `owb/env/server.py` |
| Sandbox REPL | `owb sandbox` | `owb/env/sandbox_cli.py` |
| Agent run | `owb run` | `owb/run/` |
| Verify | `owb verify` | `owb/eval/verify.py` |
| Report | `owb report` | `owb/eval/report.py` |

## Setup

Requires **Python ≥ 3.11**.

```bash
git clone https://github.com/ZetingLiu/OpenWorldSandbox.git
cd OpenWorldSandbox

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
```

Optional extras:

| Extra | Install | When you need it |
|-------|---------|------------------|
| *(default)* | `pip install -e .` | Compile, sandbox, run, verify, report |
| `synth` | `pip install -e ".[synth]"` | Legacy LLM synthesis + MCP probing (`mcp-agent`) |
| `bench` | `pip install -e ".[bench]"` | Also needs the `mcp-adapted-bench` submodule |

### LLM credentials (`.env`)

`owb run` reads an OpenAI-compatible API from environment variables. Copy the template and edit **only** `.env` (it is gitignored; never put real keys in `.env.template`):

```bash
cp .env.template .env
```

Minimal OpenAI-compatible setup (OpenAI, GLM, ChatAnywhere, local vLLM, …):

```bash
# .env
AWM_SYN_LLM_PROVIDER=openai
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=sk-...
AWM_SYN_OVERRIDE_MODEL=gpt-4o-mini
```

Azure OpenAI:

```bash
# .env
AWM_SYN_LLM_PROVIDER=azure
AZURE_ENDPOINT_URL=https://<resource>.openai.azure.com
AZURE_OPENAI_API_KEY=...
AWM_SYN_OVERRIDE_MODEL=<deployment-name>
```

Notes:

- `owb` calls `load_dotenv()` on startup and walks **upward** from the current working directory, so a repo-root `.env` is found even when you invoke `owb` from a subdirectory.
- You can override URL / model per run with CLI flags: `owb run --api_url ... --model ...`.
- Offline steps (`compile`, `sandbox`, `verify`, `report`) do **not** need an API key.

## Quickstart

Compile one task, then either drive it by hand or run a model:

```bash
owb compile \
  --scenario data/scenarios/home_01.json \
  --task data/tasks/home/home_01_umbrella_move.json \
  --output_dir outputs/compiled

# Interactive REPL (no LLM) — best for demos / debugging walkthroughs
owb sandbox --db_path outputs/compiled/home_01_umbrella_move.db

# Or start the HTTP env and run an agent (needs .env)
owb env start \
  --db_path outputs/compiled/home_01_umbrella_move.db \
  --port 8001

owb run --db_path outputs/compiled/home_01_umbrella_move.db
owb verify --input_dir outputs/runs/<run_dir>
owb report --input_dir outputs/runs --format markdown
```

Batch compile all tasks:

```bash
owb compile --batch true \
  --scenarios_dir data/scenarios \
  --tasks_dir data/tasks \
  --output_dir outputs/compiled
```

## Data & specs

| Path | Description |
|------|-------------|
| `data/scenarios/` | Scenario packs (areas, adjacency, per-area entity tables) |
| `data/tasks/` | Task packs (instruction, goal DSL, subgoals, walkthrough solvability checks) |
| [data/scenarios/README.md](data/scenarios/README.md) | Scenario JSON spec v0.1 |
| [data/tasks/README.md](data/tasks/README.md) | Task JSON spec v0.1 |

## Package layout

| Path | Description |
|------|-------------|
| `owb/schema/` | Scenario/task Pydantic models + goal DSL |
| `owb/env/` | World state, actions, observe, compile, MCP server, sandbox REPL |
| `owb/run/` | Agent loop + task runner |
| `owb/eval/` | Verify / diagnose / report |
| `owb/synth/` | Legacy LLM synthesis pipeline (optional) |

## Repository status

Forked from [agent-world-model](https://github.com/Snowflake-Labs/agent-world-model) and refactored into the `owb` package. Scenario and task JSON specs are **frozen at v0.1**.
