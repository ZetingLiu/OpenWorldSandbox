"""LLM embodied-data synthesis pipeline: generate → gate → dedup → compile → stage.

    scenario: LLM → extract_json → Pydantic+S1-S8 → dedup → staging/scenarios/
    task:     LLM → extract_json → Pydantic+cross-refs → dedup
              → compile gate (walkthrough replay) → staging/tasks/ + compiled .db

Outputs live under ``outputs/synth_staging/<run_id>/`` — never under data/.
Machine-readable metrics: ``reports/stats.json``; full event journal:
``reports/events.jsonl`` (no secrets ever).
"""

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

from ows.env.compile import compile_scenario_task, load_scenario
from ows.gen.api_client import BudgetExceeded, GenAPIClient, count_tokens
from ows.gen.dedup import DedupSet, scenario_fingerprint, task_fingerprint
from ows.gen.prompts import (
    DEFAULT_THEMES_FULL,
    DEFAULT_THEMES_SMOKE,
    EXPLORATION_FAMILIES,
    build_exploration_task_messages,
    build_review_messages,
    build_scenario_messages,
    build_task_messages,
    build_v2_requirement,
    pick_requirements,
)
from ows.gen.stats import PRICE_ENV_INPUT, PRICE_ENV_OUTPUT, RunStats
from ows.gen.validate import (
    _ID_RE,
    check_action_budget,
    check_info_action_before_key_op,
    check_instruction_leak,
    check_variant_difference,
    extract_json,
    validate_scenario,
    validate_task,
)
from ows.schema.scenario import Scenario
from ows.schema.task import Task

_REVIEW_FIELDS = (
    "pass",
    "key_fact",
    "observation_action",
    "decision_change",
    "fixed_route_risk",
    "instruction_goal_consistent",
    "reasons",
)


@dataclass
class Config:
    mode: str = "smoke"  # smoke | full (only affects default themes)
    num_scenarios: int = 1
    num_tasks_per_scenario: int = 2
    max_scenario_attempts: int = 3  # bounded retries per theme slot (cheap)
    concurrency: int = 1
    scenario_themes: str = ""  # comma-separated; overrides mode defaults
    dry_run: bool = False  # print cost estimate only, no API calls
    temperature: float = 1.0
    max_output_tokens: int = 16384  # reasoning models burn tokens; keep generous
    disable_thinking: bool = True  # send thinking.type=disabled (fallback on 400)
    json_mode: bool = True  # response_format json_object (syntactic JSON guarantee)
    max_retries: int = 3
    base_delay: float = 2.0
    timeout: float = 300.0
    input_price_per_mtok: float = -1.0  # -1 = unset → env
    output_price_per_mtok: float = -1.0
    output_dir: str = "outputs/synth_staging"
    max_total_cost: float = -1.0  # hard budget, -1 = unlimited
    max_total_tokens: int = -1
    # -- task-only mode (exploration task synthesis on existing scenarios) --
    task_only: bool = False
    scenario_paths: str = ""  # comma-separated scenario JSON paths
    max_task_attempts: int = 3  # candidate regeneration attempts per version slot

    def pre_process(self) -> None:
        assert self.mode in ("smoke", "full"), "mode must be smoke|full"
        assert self.num_scenarios >= 1 and self.num_tasks_per_scenario >= 1
        assert self.concurrency >= 1
        assert self.max_task_attempts >= 1
        self.parsed_scenario_paths: list[str] = []
        if self.task_only:
            if not self.scenario_paths.strip():
                raise ValueError("task_only mode requires --scenario_paths")
            paths = [
                p.strip() for p in self.scenario_paths.split(",") if p.strip()
            ]
            for p in paths:
                if not Path(p).is_file():
                    raise FileNotFoundError(f"scenario file not found: {p}")
            self.parsed_scenario_paths = paths

        def _price(flag: float, env_name: str) -> Optional[float]:
            if flag is not None and flag >= 0:
                return flag
            raw = os.environ.get(env_name)
            if raw:
                try:
                    return float(raw)
                except ValueError:
                    logger.warning(f"Ignoring invalid {env_name}={raw!r}")
            return None

        self.resolved_input_price = _price(self.input_price_per_mtok, PRICE_ENV_INPUT)
        self.resolved_output_price = _price(self.output_price_per_mtok, PRICE_ENV_OUTPUT)
        if self.resolved_input_price is None or self.resolved_output_price is None:
            logger.warning(
                "Prices not fully configured — cost will be reported as unknown. "
                f"Set {PRICE_ENV_INPUT} / {PRICE_ENV_OUTPUT} or pass CLI flags."
            )

    def themes(self) -> list[str]:
        if self.scenario_themes.strip():
            themes = [t.strip() for t in self.scenario_themes.split(",") if t.strip()]
        else:
            themes = list(DEFAULT_THEMES_SMOKE if self.mode == "smoke" else DEFAULT_THEMES_FULL)
        out = []
        for i in range(self.num_scenarios):
            base = themes[i % len(themes)] if themes else f"场景{i + 1}"
            out.append(base if self.num_scenarios <= len(themes) or i < len(themes) else f"{base}_{i}")
        return out


def run(config: Config) -> None:
    config.pre_process()
    asyncio.run(_run_async(config))


# ---------------------------------------------------------------------------
# Async pipeline
# ---------------------------------------------------------------------------

async def _run_async(config: Config) -> None:
    run_id = f"synth_{datetime.now():%Y%m%d_%H%M%S}"
    base = Path(config.output_dir) / run_id
    dirs = {
        "scenarios": base / "scenarios",
        "candidates": base / "candidates" / "tasks",
        "tasks": base / "tasks",
        "compiled": base / "compiled",
        "reports": base / "reports",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    stats = RunStats(
        run_id,
        dirs["reports"] / "events.jsonl",
        config.resolved_input_price,
        config.resolved_output_price,
    )
    stats.record_config(
        {
            "mode": config.mode,
            "num_scenarios": config.num_scenarios,
            "num_tasks_per_scenario": config.num_tasks_per_scenario,
            "concurrency": config.concurrency,
            "temperature": config.temperature,
            "max_output_tokens": config.max_output_tokens,
            "disable_thinking": config.disable_thinking,
            "max_retries": config.max_retries,
            "model": (
                os.environ.get("OWS_GEN_MODEL")
                or os.environ.get("AWM_SYN_OVERRIDE_MODEL")
                or "unknown"
            ),
            "output_dir": str(base),
            "task_only": config.task_only,
            "scenario_paths": config.parsed_scenario_paths,
            "max_task_attempts": config.max_task_attempts,
            "exploration_families": [
                {"family": f["family"], "scenario_id": f["scenario_id"],
                 "task_id_base": f["task_id_base"]}
                for f in EXPLORATION_FAMILIES
            ],
        }
    )
    client = GenAPIClient(
        concurrency=config.concurrency,
        max_retries=config.max_retries,
        base_delay=config.base_delay,
        timeout=config.timeout,
        disable_thinking=config.disable_thinking,
        stats=stats.record_event,
        budget_check=lambda: stats.check_budget(
            config.max_total_cost if config.max_total_cost >= 0 else None,
            config.max_total_tokens if config.max_total_tokens >= 0 else None,
        ),
    )
    themes = config.themes()
    requirements = pick_requirements(config.num_tasks_per_scenario)

    if config.dry_run:
        if config.task_only:
            _print_task_only_dry_run(config, client.model)
        else:
            _print_dry_run(config, client.model, themes, requirements)
        return

    if config.task_only:
        await _run_task_only_async(config, dirs, stats, client)
        return

    logger.info(f"=== ows gen run {run_id} ===")
    logger.info(
        f"model={client.model} host={client.host} concurrency={config.concurrency} "
        f"scenarios={len(themes)} tasks/scenario={config.num_tasks_per_scenario}"
    )

    scen_dedup = DedupSet()
    task_dedup = DedupSet()
    seen_task_ids: set[str] = set()

    async def gen_scenario(idx: int, theme: str) -> Optional[dict]:
        for attempt in range(config.max_scenario_attempts):
            try:
                res = await client.call(
                    build_scenario_messages(theme),
                    stage="scenario",
                    temperature=config.temperature,
                    max_output_tokens=config.max_output_tokens,
                    json_mode=config.json_mode,
                )
                stats.record_funnel("scenario", "response")
                _save_raw(
                    dirs["candidates"] / "raw" / f"scenario_{idx}_{attempt}_{res.request_id}.txt",
                    res,
                )
                if res.status != "http_ok":
                    stats.record_funnel(
                        "scenario", "reject_http", {"error_type": res.error_type}
                    )
                    continue
                try:
                    data = extract_json(res.content or "")
                except ValueError as e:
                    stats.record_funnel("scenario", "reject_parse", {"error": str(e)})
                    continue
                stats.record_funnel("scenario", "parse_ok")
                ok, scenario, err = validate_scenario(data)
                if not ok:
                    stats.record_funnel(
                        "scenario",
                        "reject_schema",
                        {"scenario_id": data.get("scenario_id"), "error": err, "attempt": attempt + 1},
                    )
                    continue
                stats.record_funnel("scenario", "schema_ok")
                if not scen_dedup.add_if_new(scenario_fingerprint(scenario)):
                    stats.record_funnel(
                        "scenario", "duplicate", {"scenario_id": scenario.scenario_id}
                    )
                    continue
                scen_dict = json.loads(scenario.model_dump_json(by_alias=True))
                _write_json(dirs["scenarios"] / f"{scenario.scenario_id}.json", scen_dict)
                stats.accept_scenario(scenario.scenario_id)
                stats.record_funnel(
                    "scenario",
                    "accepted",
                    {"scenario_id": scenario.scenario_id, "theme": theme, "attempt": attempt + 1},
                )
                return scen_dict
            except BudgetExceeded:
                raise
            except Exception as e:
                logger.error(f"scenario worker crash: {e!r}")
                stats.record_funnel("scenario", "worker_crash", {"error": str(e)})
                return None
        return None

    async def gen_task(
        scenario_obj: Scenario,
        scen_dict: dict,
        scenario_path: Path,
        idx: int,
        requirement: str,
    ) -> bool:
        try:
            res = await client.call(
                build_task_messages(scen_dict, requirement),
                stage="task",
                temperature=config.temperature,
                max_output_tokens=config.max_output_tokens,
                json_mode=config.json_mode,
            )
            stats.record_funnel("task", "response")
            _save_raw(
                dirs["candidates"] / "raw" / f"task_{scenario_obj.scenario_id}_{idx}_{res.request_id}.txt",
                res,
            )
            if res.status != "http_ok":
                stats.record_funnel("task", "reject_http", {"error_type": res.error_type})
                return False
            try:
                data = extract_json(res.content or "")
            except ValueError as e:
                stats.record_funnel("task", "reject_parse", {"error": str(e)})
                return False
            stats.record_funnel("task", "parse_ok")
            ok, task, err = validate_task(data, scenario_obj)
            if not ok:
                stats.record_funnel(
                    "task",
                    "reject_schema",
                    {"scenario_id": scen_dict.get("scenario_id"), "error": err},
                )
                return False
            stats.record_funnel("task", "schema_ok")
            if not _ID_RE.match(task.task_id) or task.task_id in seen_task_ids:
                task.task_id = f"{scenario_obj.scenario_id}_t{idx:02d}"
            seen_task_ids.add(task.task_id)
            if not task_dedup.add_if_new(task_fingerprint(task)):
                stats.record_funnel("task", "duplicate", {"task_id": task.task_id})
                return False
            candidate_path = dirs["candidates"] / f"{task.task_id}.json"
            _write_json(candidate_path, json.loads(task.model_dump_json(by_alias=True)))
            stats.record_funnel("task", "compile_submitted", {"task_id": task.task_id})
            report = compile_scenario_task(
                scenario_path, candidate_path, dirs["compiled"]
            )
            if report["solvable"]:
                stats.accept_task(task.task_id)
                stats.record_funnel("task", "compile_solvable", {"task_id": task.task_id})
                dest = dirs["tasks"] / scenario_obj.scenario_id
                dest.mkdir(parents=True, exist_ok=True)
                _write_json(dest / f"{task.task_id}.json", json.loads(candidate_path.read_text(encoding="utf-8")))
                return True
            stats.record_funnel(
                "task",
                "reject_compile",
                {"task_id": task.task_id, "errors": report.get("errors", [])},
            )
            return False
        except BudgetExceeded:
            raise
        except Exception as e:
            logger.error(f"task worker crash: {e!r}")
            stats.record_funnel("task", "worker_crash", {"error": str(e)})
            return False

    # ---- scenario phase ----
    t0 = datetime.now()
    try:
        results = await asyncio.gather(
            *[gen_scenario(i, theme) for i, theme in enumerate(themes)]
        )
    except BudgetExceeded as e:
        logger.error(f"budget exceeded in scenario phase: {e}")
        results = []
    stats.record_phase("scenario_phase", (datetime.now() - t0).total_seconds())
    accepted = [(r, i) for i, r in enumerate(results) if r]

    # ---- task phase ----
    t0 = datetime.now()
    jobs = []
    for scen_dict, scen_idx in accepted:
        scenario_obj = Scenario.model_validate(scen_dict)
        scenario_path = dirs["scenarios"] / f"{scenario_obj.scenario_id}.json"
        for ti in range(config.num_tasks_per_scenario):
            jobs.append(
                gen_task(scenario_obj, scen_dict, scenario_path, ti, requirements[ti])
            )
    if jobs:
        try:
            await asyncio.gather(*jobs)
        except BudgetExceeded as e:
            logger.error(f"budget exceeded in task phase: {e}")
    stats.record_phase("task_phase", (datetime.now() - t0).total_seconds())

    # ---- finalize ----
    metrics = stats.write(dirs["reports"] / "stats.json")
    stats.record_event(
        {
            "type": "run",
            "status": "finished",
            "run_id": run_id,
            "accepted_scenarios": len(stats._accepted_scenarios),
            "accepted_tasks": len(stats._accepted_tasks),
        }
    )
    _print_summary(metrics, str(base))


# ---------------------------------------------------------------------------
# Task-only mode: exploration task synthesis on existing scenarios (plan §8)
# ---------------------------------------------------------------------------

# base logical calls: 8 generations + 8 GPT-5 reviews (plan §5.3)
_EXPECTED_CALLS = len(EXPLORATION_FAMILIES) * 2 * 2


async def _review_task(
    client: GenAPIClient,
    stats: RunStats,
    dirs: dict,
    scen_dict: dict,
    task_dict: dict,
    task_id: str,
    config: Config,
    paired: Optional[dict] = None,
) -> dict:
    """One GPT-5 structured review; returns fixed-JSON verdict (plan §5.1).

    ``paired`` supplies the sibling variant so fixed_route_risk is judged
    across both versions.
    """
    t0 = time.monotonic()
    res = await client.call(
        build_review_messages(scen_dict, task_dict, paired_task=paired),
        stage="review",
        temperature=0.2,
        # gpt-5 is a thinking model: 2000 tokens get burned on internal
        # reasoning before the JSON appears (observed: refusal_or_empty with
        # completion hitting the cap). Keep the budget generous.
        max_output_tokens=8192,
        json_mode=config.json_mode,
    )
    stats.update_dynamic_eta(config.concurrency, _EXPECTED_CALLS)
    review: dict = {
        "task_id": task_id,
        "request_id": res.request_id,
        "http_status": res.status,
        "error_type": res.error_type,
        "latency_ms": res.latency_ms,
    }
    if res.status == "http_ok":
        try:
            data = extract_json(res.content or "")
            missing = [k for k in _REVIEW_FIELDS if k not in data]
            review.update({k: data.get(k) for k in _REVIEW_FIELDS})
            if missing:
                review["pass"] = False
                review["reasons"] = f"审查输出缺少字段 {missing}：{str(data)[:200]}"
        except ValueError as e:
            review["pass"] = False
            review["reasons"] = f"审查输出 JSON 解析失败：{e}"
    else:
        review["pass"] = False
        review["reasons"] = f"审查 API 调用失败：{res.status}/{res.error_type}"
    with open(dirs["reports"] / "exploration_reviews.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(review, ensure_ascii=False) + "\n")
    stats.record_phase(f"review_{task_id}", time.monotonic() - t0)
    return review


async def _run_task_only_async(config: Config, dirs: dict, stats: RunStats, client: GenAPIClient) -> None:
    """Generate exploration tasks on existing scenarios; NEVER calls scenario
    generation. Four families run concurrently (bounded by client semaphore);
    each family generates v1 then v2 sequentially."""
    run_start = time.monotonic()
    logger.info(
        f"=== ows gen run (task_only) model={client.model} host={client.host} "
        f"concurrency={config.concurrency} families={len(EXPLORATION_FAMILIES)} ==="
    )

    # --- load & validate the provided scenarios (clear error if bad) ---
    scenarios: dict[str, tuple[Scenario, Path, dict]] = {}
    for p in config.parsed_scenario_paths:
        scenario = load_scenario(p)  # raises on invalid JSON / S1-S8
        scen_dict = json.loads(scenario.model_dump_json(by_alias=True))
        scenarios[scenario.scenario_id] = (scenario, Path(p), scen_dict)
        logger.info(f"task_only scenario loaded: {scenario.scenario_id} ({p})")
    missing = [f["family"] for f in EXPLORATION_FAMILIES if f["scenario_id"] not in scenarios]
    if missing:
        raise ValueError(
            f"families {missing} need scenarios not provided in --scenario_paths "
            f"(have: {sorted(scenarios)})"
        )

    task_dedup = DedupSet()

    async def gen_version(
        fam: dict,
        scenario_obj: Scenario,
        scen_dict: dict,
        scenario_path: Path,
        *,
        version: int,
        v1_task: Optional[dict] = None,
        round_no: int = 1,
        feedback: str = "",
    ) -> tuple[Optional[dict], str]:
        """One generation attempt through the deterministic gates.

        Returns (task, feedback) — feedback is looped into the next attempt
        by the pair loop in gen_family.
        """
        task_id = f"{fam['task_id_base']}_v{version}"
        t0 = time.monotonic()
        try:
            stats.record_funnel(
                "task", "candidate_attempt",
                {"family": fam["family"], "version": version, "attempt": round_no},
            )
            requirement = fam["requirement"]
            if v1_task is not None:
                requirement = build_v2_requirement(fam["requirement"], v1_task)
            if feedback:
                requirement += (
                    f"\n\n【上次生成被拒绝的原因，请针对性修正后再输出】{feedback}"
                )
            res = await client.call(
                build_exploration_task_messages(scen_dict, requirement),
                stage="task",
                temperature=config.temperature,
                max_output_tokens=config.max_output_tokens,
                json_mode=config.json_mode,
            )
            stats.update_dynamic_eta(config.concurrency, _EXPECTED_CALLS)
            stats.record_funnel("task", "response")
            _save_raw(
                dirs["candidates"] / "raw"
                / f"expl_{fam['family']}_v{version}_a{round_no}_{res.request_id}.txt",
                res,
            )
            stats.record_phase(
                f"task_{task_id}_attempt{round_no}",
                time.monotonic() - t0,
            )
            if res.status != "http_ok":
                fb = f"API 响应失败：{res.error_type}"
                stats.record_funnel("task", "reject_http",
                                    {"family": fam["family"], "version": version,
                                     "error_type": res.error_type})
                return None, fb
            try:
                data = extract_json(res.content or "")
            except ValueError as e:
                fb = f"JSON 解析失败：{e}"
                stats.record_funnel("task", "reject_parse",
                                    {"family": fam["family"], "version": version,
                                     "error": str(e)})
                return None, fb
            stats.record_funnel("task", "parse_ok")
            ok, task, err = validate_task(data, scenario_obj)
            if not ok:
                fb = f"schema/实体引用校验失败：{err}"
                stats.record_funnel("task", "reject_schema",
                                    {"family": fam["family"], "version": version,
                                     "error": err})
                return None, fb
            stats.record_funnel("task", "schema_ok")
            # deterministic task_id per plan (base_v1 / base_v2)
            task.task_id = task_id
            # --- deterministic exploration gates (plan §5.1) ---
            leak = check_instruction_leak(task, scenario_obj)
            if leak:
                fb = f"指令泄漏检查失败：{leak}"
                stats.record_funnel("task", "reject_leak",
                                    {"family": fam["family"], "version": version,
                                     "error": leak})
                return None, fb
            info_err = check_info_action_before_key_op(task)
            if info_err:
                fb = f"信息获取动作检查失败：{info_err}"
                stats.record_funnel("task", "reject_info_action",
                                    {"family": fam["family"], "version": version,
                                     "error": info_err})
                return None, fb
            min_steps = min(len(w.actions) for w in task.walkthroughs)
            budget_err = check_action_budget(task, min_steps)
            if budget_err:
                fb = f"动作预算检查失败：{budget_err}"
                stats.record_funnel("task", "reject_budget",
                                    {"family": fam["family"], "version": version,
                                     "error": budget_err})
                return None, fb
            if not task_dedup.add_if_new(task_fingerprint(task)):
                fb = "与已生成的候选任务重复"
                stats.record_funnel("task", "duplicate",
                                    {"family": fam["family"], "version": version,
                                     "task_id": task_id})
                return None, fb
            task_dict = json.loads(task.model_dump_json(by_alias=True))
            candidate_path = dirs["candidates"] / f"{task_id}.json"
            _write_json(candidate_path, task_dict)
            stats.record_funnel("task", "compile_submitted",
                                {"task_id": task_id, "family": fam["family"],
                                 "version": version})
            t0 = time.monotonic()
            try:
                report = compile_scenario_task(
                    scenario_path, candidate_path, dirs["compiled"]
                )
            except Exception as e:
                # compile 引擎内部异常（如 world.py 对 in:null patch 的
                # 边界缺陷）：按候选拒绝处理并反馈，不击穿整个 run。
                err = f"{type(e).__name__}: {e}"
                stats.record_funnel(
                    "task", "reject_compile_crash",
                    {"task_id": task_id, "family": fam["family"],
                     "version": version, "error": err},
                )
                fb = (
                    f"compile 门禁内部异常：{err[:200]}。"
                    "请调整 initial_state_patch 或 walkthrough，"
                    "避开将实体 in/on 置为 null 等引擎不支持的模式。"
                )
                return None, fb
            stats.record_phase(f"compile_{task_id}_attempt{round_no}",
                               time.monotonic() - t0)
            if not report["solvable"]:
                errs = "; ".join(report.get("errors", [])[:2])
                fb = f"compile 回放失败：{errs}"
                stats.record_funnel("task", "reject_compile",
                                    {"task_id": task_id, "family": fam["family"],
                                     "version": version,
                                     "errors": report.get("errors", [])})
                return None, fb
            stats.record_funnel("task", "compile_solvable",
                                {"task_id": task_id, "family": fam["family"],
                                 "version": version})
            return {
                "task_id": task_id,
                "task_dict": task_dict,
                "min_steps": min_steps,
                "attempts": round_no,
                "scenario_id": scenario_obj.scenario_id,
            }, ""
        except BudgetExceeded:
            raise
        except Exception as e:
            logger.error(f"gen_version crash ({fam['family']}_v{version} "
                         f"round {round_no}): {e!r}")
            stats.record_funnel(
                "task", "worker_crash",
                {"family": fam["family"], "version": version,
                 "attempt": round_no, "error": f"{type(e).__name__}: {e}"},
            )
            return None, f"pipeline 内部异常：{type(e).__name__}: {e}"

    async def gen_family(fam: dict) -> dict:
        family = fam["family"]
        t0 = time.monotonic()
        try:
            scenario_obj, scenario_path, scen_dict = scenarios[fam["scenario_id"]]
            v1_fb, v2_fb = "", ""
            v1 = v2 = None
            pair_ok = False
            for pair_round in range(1, config.max_task_attempts + 1):
                # v1 → deterministic gates
                v1, v1_fb = await gen_version(
                    fam, scenario_obj, scen_dict, scenario_path,
                    version=1, round_no=pair_round, feedback=v1_fb,
                )
                if v1 is None:
                    continue
                # v2 (with v1 as variant context) → deterministic gates
                v2, v2_fb = await gen_version(
                    fam, scenario_obj, scen_dict, scenario_path,
                    version=2, v1_task=v1["task_dict"],
                    round_no=pair_round, feedback=v2_fb,
                )
                if v2 is None:
                    continue
                # pair-level variant difference (plan §5.1)
                diff_err = check_variant_difference(v1["task_dict"], v2["task_dict"])
                if diff_err:
                    v2_fb = f"与 v1 无环境差异：{diff_err}"
                    stats.record_funnel(
                        "task", "reject_variant_diff",
                        {"family": family, "round": pair_round, "error": diff_err},
                    )
                    continue
                # pair-aware GPT-5 reviews (fixed_route_risk across versions)
                r1 = await _review_task(
                    client, stats, dirs, scen_dict, v1["task_dict"],
                    v1["task_id"], config, paired=v2["task_dict"],
                )
                r2 = await _review_task(
                    client, stats, dirs, scen_dict, v2["task_dict"],
                    v2["task_id"], config, paired=v1["task_dict"],
                )
                round_ok = True
                if not r1["pass"]:
                    v1_fb = f"GPT-5 配对审查未通过：{str(r1.get('reasons', ''))[:400]}"
                    stats.record_funnel(
                        "task", "reject_review",
                        {"task_id": v1["task_id"], "family": family,
                         "version": 1, "round": pair_round, "review": r1},
                    )
                    round_ok = False
                else:
                    stats.record_funnel("task", "review_pass",
                                        {"task_id": v1["task_id"], "family": family,
                                         "version": 1})
                if not r2["pass"]:
                    v2_fb = f"GPT-5 配对审查未通过：{str(r2.get('reasons', ''))[:400]}"
                    stats.record_funnel(
                        "task", "reject_review",
                        {"task_id": v2["task_id"], "family": family,
                         "version": 2, "round": pair_round, "review": r2},
                    )
                    round_ok = False
                else:
                    stats.record_funnel("task", "review_pass",
                                        {"task_id": v2["task_id"], "family": family,
                                         "version": 2})
                if round_ok:
                    v1["review"] = r1
                    v2["review"] = r2
                    pair_ok = True
                    break
            if not pair_ok:
                stats.record_funnel(
                    "task", "candidate_exhausted",
                    {"family": family, "rounds": config.max_task_attempts},
                )
            # staging: accepted tasks land under staging/tasks/<scenario_id>/
            if pair_ok:
                for v in (v1, v2):
                    stats.accept_task(v["task_id"])
                    dest = dirs["tasks"] / scenario_obj.scenario_id
                    dest.mkdir(parents=True, exist_ok=True)
                    _write_json(dest / f"{v['task_id']}.json", v["task_dict"])
            return {
                "family": family,
                "scenario_id": fam["scenario_id"],
                "task_id_base": fam["task_id_base"],
                "v1": v1 if pair_ok else None,
                "v2": v2 if pair_ok else None,
                "pair_ok": pair_ok,
                "elapsed_s": round(time.monotonic() - t0, 3),
            }
        except BudgetExceeded:
            raise
        except Exception as e:
            logger.error(f"family {family} crashed: {e!r}")
            stats.record_funnel("task", "family_crash",
                                {"family": family, "error": f"{type(e).__name__}: {e}"})
            return {
                "family": family,
                "scenario_id": fam["scenario_id"],
                "task_id_base": fam["task_id_base"],
                "v1": None,
                "v2": None,
                "pair_ok": False,
                "elapsed_s": round(time.monotonic() - t0, 3),
            }

    # ---- four families concurrently; v1→v2 sequential inside each ----
    t0 = time.monotonic()
    try:
        family_results = await asyncio.gather(
            *[gen_family(f) for f in EXPLORATION_FAMILIES]
        )
    except BudgetExceeded as e:
        logger.error(f"token budget exceeded during generation: {e}")
        family_results = []
    except Exception as e:
        logger.error(f"unexpected pipeline error: {e!r}")
        family_results = []
    gen_elapsed = time.monotonic() - t0
    stats.record_phase("generation_phase", gen_elapsed)
    # 吞吐口径：任务生成阶段 = 全部生成请求 + 门禁 + 审查的墙钟
    stats.record_phase("task_phase", gen_elapsed)

    # ---- finalize ----
    metrics = stats.write(dirs["reports"] / "stats.json")
    stats.write_human_involvement(dirs["reports"] / "human_involvement.json")
    stats.record_event(
        {
            "type": "run",
            "status": "finished",
            "run_id": stats.run_id,
            "task_only": True,
            "families": [
                {"family": r["family"], "pair_ok": r["pair_ok"],
                 "v1": bool(r["v1"]), "v2": bool(r["v2"])}
                for r in family_results
            ],
            "total_elapsed_s": round(time.monotonic() - run_start, 3),
        }
    )
    _print_task_only_summary(metrics, family_results, str(dirs["scenarios"].parent))


def _print_task_only_dry_run(config: Config, model: str) -> None:
    """Dry-run for task-only mode: requests, tokens, ETA. No API calls."""
    from ows.gen.prompts import _read_json, _SCENARIO_EXAMPLE_PATH

    n_families = len(EXPLORATION_FAMILIES)
    gen_base = n_families * 2  # 8 generation requests
    review_base = gen_base  # 8 GPT-5 structured reviews (plan §5.3)
    base_reqs = gen_base + review_base
    max_reqs = base_reqs * config.max_task_attempts
    # measure one exploration prompt against the largest scenario
    scen_in = 0
    for f in EXPLORATION_FAMILIES[:1]:
        scen = json.loads(_read_json(_SCENARIO_EXAMPLE_PATH))
        scen_in = sum(
            count_tokens(m["content"], model)
            for m in build_exploration_task_messages(scen, f["requirement"])
        )
    input_tokens = base_reqs * scen_in
    output_low = base_reqs * 1300
    output_high = max_reqs * 4000

    print("=" * 72)
    print("DRY-RUN — TASK-ONLY EXPLORATION SYNTHESIS (no API calls made)")
    print("=" * 72)
    print(f"model: {model}")
    print(f"scenario files: {config.parsed_scenario_paths}")
    print(f"families: {n_families}  versions per family: 2  max attempts: {config.max_task_attempts}")
    print(f"base requests: {base_reqs} (= {gen_base} generation + {review_base} review)  worst case: {max_reqs}")
    print(f"concurrency: {config.concurrency}")
    print("-" * 72)
    print(f"input tokens (per request, prompt measured): ~{scen_in:,}")
    print(f"estimated total input tokens: ~{input_tokens:,} (worst {max_reqs * scen_in:,})")
    print(f"estimated total output tokens: ~{output_low:,} .. {output_high:,}")
    print(f"hard token budget: {config.max_total_tokens if config.max_total_tokens > 0 else 'UNLIMITED'}")
    print("cost: UNKNOWN (prices not configured for this endpoint)")
    print("-" * 72)
    print("ETA (planning values, plan §5.3): concurrency 4 → 5–15 min normal, 10–30 min heavy retries")
    print("ETA updates dynamically after the first 2 real requests.")
    print("=" * 72)


def _print_task_only_summary(metrics: dict, family_results: list[dict], base_dir: str) -> None:
    print("=" * 72)
    print(f"RUN COMPLETE (task_only) — {metrics['run_id']}")
    print(f"output: {base_dir}")
    print("-" * 72)
    for r in family_results:
        v1 = r["v1"]["task_id"] if r["v1"] else "-"
        v2 = r["v2"]["task_id"] if r["v2"] else "-"
        print(f"  {r['family']:<14} v1={v1:<32} v2={v2:<32} pair_ok={r['pair_ok']} {r['elapsed_s']}s")
    f = metrics["funnel_task"]
    print(
        f"tasks: {f['responses']} resp → {f['parse_ok']} parsed → "
        f"{f['schema_ok']} schema → {f['compile_submitted']} compiled → "
        f"{f['compile_solvable']} solvable"
    )
    print(f"tokens={metrics['tokens']['total']}  est_cost={metrics['cost']['estimated_total']}")
    print("=" * 72)


# ---------------------------------------------------------------------------
# Dry-run cost estimation (no API calls)
# ---------------------------------------------------------------------------

def _print_dry_run(
    config: Config, model: str, themes: list[str], requirements: list[str]
) -> None:
    from ows.gen.prompts import _read_json, _SCENARIO_EXAMPLE_PATH

    placeholder_scenario = json.loads(_read_json(_SCENARIO_EXAMPLE_PATH))
    scen_in = sum(
        count_tokens(m["content"], model)
        for m in build_scenario_messages(themes[0])
    )
    task_in = sum(
        count_tokens(m["content"], model)
        for m in build_task_messages(placeholder_scenario, requirements[0])
    )
    n_scen_req = config.num_scenarios
    n_task_req = config.num_scenarios * config.num_tasks_per_scenario
    n_requests = n_scen_req + n_task_req
    input_tokens = n_scen_req * scen_in + n_task_req * task_in
    # expected output: sized by few-shot task (~1.3k tokens) as the low bound,
    # max_output_tokens as the high bound
    output_low = n_requests * 1300
    output_high = n_requests * config.max_output_tokens

    print("=" * 72)
    print("DRY-RUN COST ESTIMATE (no API calls made)")
    print("=" * 72)
    print(f"model: {model}")
    print(f"scenarios: {n_scen_req}  tasks/scenario: {config.num_tasks_per_scenario}")
    print(f"base requests: {n_requests} (= {n_scen_req} scenario + {n_task_req} task)")
    print(f"concurrency: {config.concurrency}  max_retries: {config.max_retries}")
    print(f"themes: {themes}")
    print("-" * 72)
    print(f"input tokens (exact, prompt measured):  ~{input_tokens:,}")
    print(f"output tokens (expected range):          ~{output_low:,} .. {output_high:,}")
    print(f"total tokens (expected range):            ~{input_tokens + output_low:,} .. {input_tokens + output_high:,}")
    if config.resolved_input_price is not None and config.resolved_output_price is not None:
        ip, op = config.resolved_input_price, config.resolved_output_price
        lo = (input_tokens * ip + output_low * op) / 1_000_000
        hi = (input_tokens * ip + output_high * op) / 1_000_000
        print(f"estimated cost: {lo:.4f} .. {hi:.4f} (prices: in={ip}/M out={op}/M)")
    else:
        print("estimated cost: UNKNOWN (prices not configured)")
    print("-" * 72)
    print("Notes: retries bill extra (bounded by max_retries); failures bill input tokens.")
    print("=" * 72)


def _print_summary(metrics: dict, base_dir: str) -> None:
    f = metrics["funnel_task"]
    s = metrics["funnel_scenario"]
    print("=" * 72)
    print(f"RUN COMPLETE — {metrics['run_id']}")
    print(f"output: {base_dir}")
    print("-" * 72)
    print(
        f"scenarios: {s['responses']} resp → {s['parse_ok']} parsed → "
        f"{s['schema_ok']} schema → {s['accepted']} accepted"
    )
    print(
        f"tasks:     {f['responses']} resp → {f['parse_ok']} parsed → "
        f"{f['schema_ok']} schema → {f['compile_submitted']} compiled → "
        f"{f['compile_solvable']} solvable → {f['accepted']} accepted"
    )
    lat = metrics["latency_ms"]
    print(
        f"latency p50={lat['p50']}ms p95={lat['p95']}ms "
        f"(n={lat['n']})  retry_rate={metrics['requests']['retry_rate']}"
    )
    tp = metrics["throughput"]
    print(
        f"accepted_rate={tp.get('accepted_rate_per_min')}/min  "
        f"raw_rate={tp.get('raw_rate_per_min')}/min  "
        f"acceptance_rate={f['acceptance_rate']}"
    )
    if metrics["cost"]["known"]:
        print(
            f"tokens={metrics['tokens']['total']}  "
            f"est_cost={metrics['cost']['estimated_total']}"
        )
    print("=" * 72)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _save_raw(path: Path, res) -> None:
    """Keep the raw LLM response for failure analysis (staging only)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            f"# request_id={res.request_id} status={res.status} "
            f"error_type={res.error_type} tokens={res.prompt_tokens}+{res.completion_tokens}\n"
        )
        fh.write(res.content or "(empty content)\n")
