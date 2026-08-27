"""Native function-calling agent loop (with XML fallback).

The agent interacts with the environment via MCP tools using the
OpenAI function-calling protocol.  If the model doesn't support native
tool calling, it falls back to XML-based tool parsing (as in the
original awm/core/agent.py).
"""

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from textwrap import dedent
from typing import Any

from loguru import logger
import openai
from openai import AsyncOpenAI

from ows.tools import tools_robust_json_loads, tools_json_save, resolve_llm_config


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    task: str                                      # task instruction
    mcp_url: str                                   # MCP server URL
    api_url: str | None = None
    model: str | None = None
    max_iterations: int = 30
    temperature: float = 1.0
    max_tokens: int = 2048
    output_dir: str | None = None
    use_native_tools: bool = True                   # prefer native function calling
    verbose: bool = True
    # Task metadata (recorded in trajectory.json for the verifier)
    task_id: str | None = None
    scenario_id: str | None = None


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = dedent("""\
You are an embodied robot agent in a household environment. You can move between
rooms, pick up objects, place them in containers or on surfaces, open/close
containers, operate devices, and use physical tools.

Your goal is to complete the user's task by calling the available tools step by step.

Key rules:
1. You can only interact with objects in your current area.
2. You have two hands — you can hold at most two objects at once.
3. Containers must be open before you can put things in or take things out.
4. Devices must be closed before starting.
5. Always observe the scene when entering a new area.
6. Call finish_task when you believe the task is complete.
7. Entity IDs follow the pattern <english_class>_<number> (e.g. umbrella_01, wardrobe_01).
   Use observe_scene to discover actual IDs — never guess IDs.
8. Area IDs are snake_case English names. Use the area list provided in the task
   to know which areas exist and how to navigate between them.

Be efficient: minimize unnecessary movement and actions.""")


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "observe_scene",
            "description": "Describe the current area: visible entities, held items, containers, surfaces.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_object",
            "description": "Search for a specific entity in the current area.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Entity ID to search for"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_entity",
            "description": "Get detailed information about an entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Entity ID to inspect"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_robot_state",
            "description": "Check the robot's current location and hands.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_to",
            "description": "Move to another area.",
            "parameters": {
                "type": "object",
                "properties": {
                    "area_id": {"type": "string", "description": "Target area ID"},
                },
                "required": ["area_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pick_object",
            "description": "Pick up an entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Entity ID to pick up"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "place_object",
            "description": "Place a held entity into a container or onto a surface.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Entity ID to place"},
                    "target_id": {"type": "string", "description": "Target container or surface ID"},
                },
                "required": ["entity_id", "target_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_container",
            "description": "Open a container.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Container ID to open"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_container",
            "description": "Close a container.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Container ID to close"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hang_object",
            "description": "Hang a hangable entity on a surface that can accept hanging.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Entity ID to hang"},
                    "target_id": {"type": "string", "description": "Target surface ID that supports hanging"},
                },
                "required": ["entity_id", "target_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_device",
            "description": "Start a device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Device ID to start"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_device",
            "description": "Stop a device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Device ID to stop"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_physical_tool",
            "description": "Use a physical tool on a target entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_id": {"type": "string", "description": "Tool entity ID"},
                    "target_id": {"type": "string", "description": "Target entity ID"},
                    "intended_effect": {"type": "string", "description": "Intended physical effect (e.g. absorbent)"},
                },
                "required": ["tool_id", "target_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_task",
            "description": "Signal that the task is complete.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_target_absent",
            "description": "Report that the target entity is not found in the environment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Missing entity ID"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_unable_to_continue",
            "description": "Report inability to continue the task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Reason for stopping"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "abandon_task",
            "description": "Abandon the current task.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

# Map tool names to server endpoint paths
TOOL_TO_PATH = {
    name: f"/action/{name}"
    for name in [
        "observe_scene", "search_object", "inspect_entity", "check_robot_state",
        "move_to", "pick_object", "place_object", "open_container", "close_container",
        "hang_object", "start_device", "stop_device", "apply_physical_tool",
        "finish_task", "report_target_absent", "report_unable_to_continue",
        "abandon_task",
    ]
}


# ---------------------------------------------------------------------------
# MCP tool executor (HTTP-based)
# ---------------------------------------------------------------------------

class MCPToolExecutor:
    """Call tools via HTTP to the environment server."""

    def __init__(self, mcp_url: str, timeout: float = 60.0):
        import httpx
        self.base_url = mcp_url.rstrip("/")
        # Strip /mcp suffix to get the FastAPI base
        if self.base_url.endswith("/mcp"):
            self.base_url = self.base_url[:-4]
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout))

    async def call_tool(self, tool_name: str, arguments: dict) -> tuple[str, str]:
        """Execute a tool and return ``(status, text)``.

        ``status`` is ``success``, ``failure`` (the environment rejected the
        action), ``invalid_call`` (no such tool) or ``transport_error``.  Any
        other value the server reports is passed through verbatim rather than
        being folded into ``success``, so a status this client does not know
        about shows up in diagnostics instead of silently counting as a win.
        It is recorded in the trajectory so diagnostics do not have to
        reverse-engineer outcomes from the response text.
        """
        path = TOOL_TO_PATH.get(tool_name)
        if path is None:
            return "invalid_call", f"Error: Unknown tool '{tool_name}'"

        url = f"{self.base_url}{path}"

        try:
            if arguments:
                # Use query params for GET-like tools, body for POST-like
                if tool_name in ("observe_scene", "search_object", "inspect_entity", "check_robot_state"):
                    resp = await self._client.get(url, params=arguments)
                else:
                    resp = await self._client.post(url, params=arguments)
            else:
                if tool_name in ("observe_scene", "check_robot_state"):
                    resp = await self._client.get(url)
                else:
                    resp = await self._client.post(url)

            data = resp.json()
            status = data.get("status")
            text = data.get("observation") or json.dumps(data, ensure_ascii=False)
            if status == "failure":
                reason = data.get("failure_reason") or data.get("observation") or "Unknown error"
                return "failure", f"Error: {reason}"
            if status not in (None, "success"):
                return status, text
            return "success", text
        except Exception as e:
            return "transport_error", f"Error: {e}"

    async def close(self):
        await self._client.aclose()


# ---------------------------------------------------------------------------
# XML-based tool call parsing (fallback)
# ---------------------------------------------------------------------------

def parse_tool_calls_xml(content: str) -> list[dict]:
    """Parse <tool_call> XML blocks from model output."""
    tool_calls = []
    pattern = r'<tool_call>\s*(.*?)\s*</tool_call>'
    matches = re.findall(pattern, content, re.DOTALL)

    for i, match in enumerate(matches):
        data = tools_robust_json_loads(match.strip())
        if not data:
            continue
        if isinstance(data, list) and len(data) > 0:
            data = data[0]
        if not isinstance(data, dict):
            continue

        tool_calls.append({
            "id": f"call_{int(time.time() * 1000)}_{i}",
            "name": data.get("name", ""),
            "arguments": data.get("arguments", {}),
        })

    return tool_calls


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

async def run_agent(config: AgentConfig) -> dict[str, Any]:
    """Run the agent loop and return trajectory data."""
    # Resolve LLM config
    api_url, api_key, model = resolve_llm_config(
        api_url_override=config.api_url,
        model_override=config.model,
    )
    use_vllm_extras = "localhost" in api_url and "openai.azure.com" not in api_url

    # Setup output
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = config.output_dir or os.path.join("outputs", "agents", timestamp)
    os.makedirs(output_dir, exist_ok=True)

    # Clients
    llm_client = AsyncOpenAI(api_key=api_key, base_url=api_url)
    mcp = MCPToolExecutor(config.mcp_url)

    try:
        logger.info(f"Agent starting: model={model}, task={config.task[:80]}...")
        logger.info(f"MCP URL: {config.mcp_url}")

        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": config.task},
        ]
        trajectory: list[dict] = []

        iteration = 0
        for iteration in range(1, config.max_iterations + 1):
            logger.info(f"--- Iteration {iteration}/{config.max_iterations} ---")

            # Build API call
            kwargs: dict[str, Any] = dict(
                model=model,
                messages=messages,
                max_completion_tokens=config.max_tokens,
                temperature=config.temperature,
            )

            if config.use_native_tools:
                kwargs["tools"] = TOOL_DEFINITIONS
                kwargs["tool_choice"] = "auto"

            if use_vllm_extras:
                kwargs["extra_body"] = {
                    "add_generation_prompt": True,
                    "min_tokens": 16,
                    "chat_template_kwargs": {"enable_thinking": True},
                }

            # Transient API failures (flaky OpenAI-compatible gateways return
            # intermittent 400/429/5xx for valid requests) must not kill the
            # whole run. Bounded retries with exponential backoff; successful
            # requests are unaffected, so per-model scoring is unchanged.
            for retry in range(3):
                try:
                    response = await llm_client.chat.completions.create(**kwargs)
                    break
                except (openai.APIConnectionError, openai.APITimeoutError,
                        openai.RateLimitError) as e:
                    api_err, retryable = e, True
                except openai.APIStatusError as e:
                    api_err, retryable = e, e.status_code in (
                        400, 408, 429, 500, 502, 503, 504,
                    )
                except Exception:
                    raise
                if not retryable or retry == 2:
                    raise api_err
                delay = 2.0 * (2 ** retry)
                logger.warning(
                    f"Iteration {iteration}: transient API error "
                    f"{type(api_err).__name__} "
                    f"({getattr(api_err, 'status_code', '?')}); "
                    f"retry {retry + 1}/2 in {delay:.0f}s"
                )
                await asyncio.sleep(delay)

            choice = response.choices[0]
            content = choice.message.content or ""
            reasoning_content = getattr(choice.message, "reasoning_content", None)

            # Extract tool calls
            tool_calls = []
            if config.use_native_tools and choice.message.tool_calls:
                for tc in choice.message.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    tool_calls.append({
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": args,
                    })
            elif not config.use_native_tools:
                tool_calls = parse_tool_calls_xml(content)

            if config.verbose:
                preview = content[:500] + "..." if len(content) > 500 else content
                logger.info(f"Assistant: {preview}")
                logger.info(f"Tool calls: {len(tool_calls)}")

            # No tool calls → task complete
            if not tool_calls:
                messages.append({"role": "assistant", "content": content})
                logger.info("No tool calls — task complete.")
                trajectory.append({
                    "iteration": iteration,
                    "role": "assistant",
                    "content": content,
                    "reasoning_content": reasoning_content,
                    "tool_calls": [],
                    "is_final": True,
                })
                break

            # Execute first tool call (one action per iteration)
            tc = tool_calls[0]
            tool_name = tc["name"]
            tool_args = tc["arguments"]

            # The assistant message must carry the tool_calls it made,
            # otherwise the following role="tool" message is rejected by
            # the OpenAI API.  Only the executed call is kept.
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
            if reasoning_content:
                assistant_msg["reasoning_content"] = reasoning_content
            if config.use_native_tools:
                assistant_msg["tool_calls"] = [{
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(tool_args, ensure_ascii=False),
                    },
                }]
            messages.append(assistant_msg)

            logger.info(f"Executing: {tool_name}({json.dumps(tool_args, ensure_ascii=False)[:200]})")
            tool_status, response_text = await mcp.call_tool(tool_name, tool_args)

            if config.verbose:
                preview = response_text[:500] + "..." if len(response_text) > 500 else response_text
                logger.info(f"Tool response: {preview}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": response_text,
            })

            is_termination = tool_name in (
                "finish_task", "abandon_task",
                "report_unable_to_continue", "report_target_absent",
            )

            trajectory.append({
                "iteration": iteration,
                "role": "assistant",
                "content": content,
                "reasoning_content": reasoning_content,
                "tool_calls": [tc],
                "tool_response": {
                    "tool_call_id": tc["id"],
                    "status": tool_status,
                    "content": response_text,
                },
                "is_final": is_termination,
            })

            if is_termination:
                logger.info(f"Termination action '{tool_name}' — episode ends.")
                break
        else:
            logger.warning("Max iterations reached without completion.")

        logger.info(f"Agent complete. Total iterations: {iteration}")

        # Save trajectory
        trajectory_data = {
            "task": config.task,
            "task_id": config.task_id,
            "scenario_id": config.scenario_id,
            "model": model,
            "api_url": api_url,
            "max_iterations": config.max_iterations,
            "total_iterations": iteration,
            "timestamp": timestamp,
            "trajectory": trajectory,
            "messages": messages,
        }
        tools_json_save(trajectory_data, os.path.join(output_dir, "trajectory.json"))
        logger.info(f"Trajectory saved to {output_dir}/trajectory.json")

        return trajectory_data

    finally:
        await mcp.close()


def run(config: AgentConfig) -> None:
    asyncio.run(run_agent(config))


if __name__ == "__main__":
    from simpleArgParser import parse_args
    cfg: AgentConfig = parse_args(AgentConfig)
    run(cfg)
