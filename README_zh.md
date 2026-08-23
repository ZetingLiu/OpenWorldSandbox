<p align="center">
  <img src="figures/owb.png" alt="OpenWorldSandbox" width="420"/>
</p>

# OpenWorldSandbox

面向真实业务场景的具身智能体 **sandbox**，用于评测模型 **reasoning** 能力。

[English](README.md)

## 概述

OpenWorldSandbox 评测视觉语言模型作为「具身大脑」时，能否在**部分可观测**环境中通过**多轮高层语义工具调用**完成完整业务任务。模型每轮根据任务指令、当前观测与历史反馈输出一个结构化动作；环境以 SQLite 维护隐藏世界状态，执行后返回状态变化与失败原因；最终依据**任务目标 DSL** 与**完整执行轨迹**进行程序化判定与能力诊断。

首期聚焦**家庭服务**与**商超服务**，覆盖导航、取放、容器与设备操作、双手状态维护、多步规划等；**不**评测关节控制或底层轨迹生成。

## 核心链路

```
场景/任务 JSON → compile → SQLite 初始快照 → MCP 环境（17 个语义动作）
→ 被测模型多轮 tool calling → 轨迹记录 → DSL 验证 + 诊断报告
```

| 阶段 | 命令 | 模块 |
|------|------|------|
| 编译 | `owb compile` | `owb/env/compile.py` |
| 环境 | `owb env start` | `owb/env/server.py` |
| 交互沙箱 | `owb sandbox` | `owb/env/sandbox_cli.py` |
| 跑任务 | `owb run` | `owb/run/` |
| 验证 | `owb verify` | `owb/eval/verify.py` |
| 报告 | `owb report` | `owb/eval/report.py` |

## 环境配置

需要 **Python ≥ 3.11**。

```bash
git clone https://github.com/ZetingLiu/OpenWorldSandbox.git
cd OpenWorldSandbox

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
```

可选依赖：

| Extra | 安装命令 | 用途 |
|-------|----------|------|
| *(默认)* | `pip install -e .` | 编译、沙箱、跑任务、验证、报告 |
| `synth` | `pip install -e ".[synth]"` | 旧版 LLM 合成与 MCP 探测（`mcp-agent`） |
| `bench` | `pip install -e ".[bench]"` | 需同时初始化 `mcp-adapted-bench` 子模块 |

### LLM 凭证（`.env`）

`owb run` 从环境变量读取 OpenAI 兼容接口。复制模板后**只在** `.env` 中填写真实密钥（该文件已 gitignore；切勿把真实 Key 写进 `.env.template`）：

```bash
cp .env.template .env
```

OpenAI 兼容接口（OpenAI / GLM / ChatAnywhere / 本地 vLLM 等）最小配置：

```bash
# .env
AWM_SYN_LLM_PROVIDER=openai
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=sk-...
AWM_SYN_OVERRIDE_MODEL=gpt-4o-mini
```

Azure OpenAI：

```bash
# .env
AWM_SYN_LLM_PROVIDER=azure
AZURE_ENDPOINT_URL=https://<resource>.openai.azure.com
AZURE_OPENAI_API_KEY=...
AWM_SYN_OVERRIDE_MODEL=<deployment-name>
```

说明：

- `owb` 启动时调用 `load_dotenv()`，会从当前工作目录**向上**查找 `.env`，因此在子目录调用时仍能读到仓库根目录的配置。
- 单次运行可用 CLI 覆盖：`owb run --api_url ... --model ...`。
- 离线步骤（`compile` / `sandbox` / `verify` / `report`）**不需要** API Key。

## 快速开始

先编译任务，再手敲沙箱或跑模型：

```bash
owb compile \
  --scenario data/scenarios/home_01.json \
  --task data/tasks/home/home_01_umbrella_move.json \
  --output_dir outputs/compiled

# 交互式 REPL（不烧 token）— 适合演示 / 调试 walkthrough
owb sandbox --db_path outputs/compiled/home_01_umbrella_move.db

# 或启动 HTTP 环境并跑 Agent（需要 .env）
owb env start \
  --db_path outputs/compiled/home_01_umbrella_move.db \
  --port 8001

owb run --db_path outputs/compiled/home_01_umbrella_move.db
owb verify --input_dir outputs/runs/<run_dir>
owb report --input_dir outputs/runs --format markdown
```

批量编译全部任务：

```bash
owb compile --batch true \
  --scenarios_dir data/scenarios \
  --tasks_dir data/tasks \
  --output_dir outputs/compiled
```

## 数据与规范

| 路径 | 说明 |
|------|------|
| `data/scenarios/` | 场景包（区域表、邻接关系、每区域实体表） |
| `data/tasks/` | 任务包（指令、目标 DSL、子目标、walkthrough 可解性校验） |
| [data/scenarios/README.md](data/scenarios/README.md) | 场景包 JSON 规范 v0.1 |
| [data/tasks/README.md](data/tasks/README.md) | 任务包 JSON 规范 v0.1 |

## 包结构

| 路径 | 说明 |
|------|------|
| `owb/schema/` | 场景/任务 Pydantic 模型与目标 DSL |
| `owb/env/` | 世界状态、动作、观测、编译、MCP 服务、交互沙箱 |
| `owb/run/` | Agent 循环与任务运行器 |
| `owb/eval/` | 验证 / 诊断 / 报告 |
| `owb/synth/` | 旧版 LLM 合成流水线（可选） |

## 仓库状态

本仓库由 [agent-world-model](https://github.com/Snowflake-Labs/agent-world-model) fork 而来，已重构为 `owb` 包。场景与任务 JSON 规范 **v0.1 已冻结**。
