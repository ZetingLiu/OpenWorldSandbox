# OpenWorldSandbox

An **embodied scene sandbox**: data-driven household and retail worlds that agents can explore, act in, and debug.

[中文说明](README_zh.md)

## Overview

OpenWorldSandbox is a **sandbox for embodied scenes**, not a joint-level simulator. You author a scene and a task as JSON; the sandbox compiles them into a SQLite world snapshot and exposes a fixed set of **high-level semantic actions** (navigate, pick-and-place, containers, devices, dual-hand state). Hidden world state stays in the database. Each action returns state changes and structured failure reasons under **partial observability**.

You can drive a scene by hand (`ows sandbox`), serve it over HTTP/MCP (`ows env start`), or plug in a vision-language model as an embodied brain (`ows run`). Optional **goal DSL** checks and trajectory diagnostics tell you whether a run finished the task; they are tools around the sandbox, not the product itself.

Phase 1 ships **home service** and **retail service** scenes. It does **not** model joint control or low-level motion planning.

## Pipeline

```
Scenario/task JSON → compile → SQLite initial snapshot → MCP env (17 semantic actions)
→ model multi-turn tool calling → trajectory log → DSL verification + diagnostic report
```

| Stage | Command | Module |
|-------|---------|--------|
| Compile | `ows compile` | `ows/env/compile.py` |
| Environment | `ows env start` | `ows/env/server.py` |
| Sandbox REPL | `ows sandbox` | `ows/env/sandbox_cli.py` |
| Agent run | `ows run` | `ows/run/` |
| Verify | `ows verify` | `ows/eval/verify.py` |
| Report | `ows report` | `ows/eval/report.py` |

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

`ows run` reads an OpenAI-compatible API from environment variables. Copy the template and edit **only** `.env` (it is gitignored; never put real keys in `.env.template`):

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

- `ows` calls `load_dotenv()` on startup and walks **upward** from the current working directory, so a repo-root `.env` is found even when you invoke `ows` from a subdirectory.
- You can override URL / model per run with CLI flags: `ows run --api_url ... --model ...`.
- Offline steps (`compile`, `sandbox`, `verify`, `report`) do **not** need an API key.

## Quickstart

Compile one task, then either drive it by hand or run a model:

```bash
ows compile \
  --scenario data/scenarios/home_01.json \
  --task data/tasks/home/home_01_umbrella_move.json \
  --output_dir outputs/compiled

# Interactive REPL (no LLM) — best for demos / debugging walkthroughs
ows sandbox --db_path outputs/compiled/home_01_umbrella_move.db

# Or start the HTTP env and run an agent (needs .env)
ows env start \
  --db_path outputs/compiled/home_01_umbrella_move.db \
  --port 8001

ows run --db_path outputs/compiled/home_01_umbrella_move.db
ows verify --input_dir outputs/runs/<run_dir>
ows report --input_dir outputs/runs --format markdown
```

Batch compile all tasks:

```bash
ows compile --batch true \
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
| `ows/schema/` | Scenario/task Pydantic models + goal DSL |
| `ows/env/` | World state, actions, observe, compile, MCP server, sandbox REPL |
| `ows/run/` | Agent loop + task runner |
| `ows/eval/` | Verify / diagnose / report |
| `ows/synth/` | Legacy LLM synthesis pipeline (optional) |

## Repository status

Forked from [agent-world-model](https://github.com/Snowflake-Labs/agent-world-model) and refactored into the `ows` package as an embodied scene sandbox. Scenario and task JSON specs are **frozen at v0.1**.
