"""Per-run metrics: event journal, funnel counters, rates, cost aggregation.

All numbers reported downstream (stats.json, leadership report) are computed
here from the event journal — nothing is fabricated. Cost is only computed
when input/output prices are configured; otherwise cost fields are ``null``
and the report marks them unknown.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np

PRICE_ENV_INPUT = "OWS_GEN_INPUT_PRICE_PER_MTOK"
PRICE_ENV_OUTPUT = "OWS_GEN_OUTPUT_PRICE_PER_MTOK"


class RunStats:
    def __init__(
        self,
        run_id: str,
        events_path: Path,
        input_price_per_mtok: Optional[float],
        output_price_per_mtok: Optional[float],
    ) -> None:
        self.run_id = run_id
        self._events_path = Path(events_path)
        self._events_path.parent.mkdir(parents=True, exist_ok=True)
        self._input_price = input_price_per_mtok
        self._output_price = output_price_per_mtok
        self.started_at = datetime.now().isoformat(timespec="seconds")

        # funnel counters: (stage, bucket) -> count ; buckets defined below
        self._funnel: dict[tuple[str, str], int] = {}
        # detail for rejected candidates (reason sampling, first few)
        self._rejects: list[dict] = []
        self._accepted_scenarios: list[str] = []
        self._accepted_tasks: list[str] = []
        # per-stage latency (successful requests only)
        self._latencies: dict[str, list[int]] = defaultdict(list)
        self._retried_requests = 0
        self._total_requests = 0
        # token accounting, split by stage for per-accepted-task attribution
        self._prompt_tokens: dict[str, int] = defaultdict(int)
        self._completion_tokens: dict[str, int] = defaultdict(int)
        self._billed_input_tokens: dict[str, int] = defaultdict(int)
        # estimated tokens billed on failed attempts (input side)
        self._failed_attempt_input_estimate: dict[str, int] = defaultdict(int)
        self._phase_seconds: dict[str, float] = {}
        self._config_snapshot: dict = {}
        # dynamic ETA history (plan §5.3): recorded after API responses
        self._eta_history: list[dict] = []
        self._api_responses = 0

    # ------------------------------------------------------------------
    # Event intake
    # ------------------------------------------------------------------

    def record_event(self, event: dict) -> None:
        if not event.get("ts"):
            event["ts"] = datetime.now().isoformat(timespec="milliseconds")
        with open(self._events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        if event.get("type") == "api":
            self._on_api_event(event)

    def record_funnel(self, stage: str, bucket: str, detail: Optional[dict] = None) -> None:
        key = (stage, bucket)
        self._funnel[key] = self._funnel.get(key, 0) + 1
        self.record_event(
            {"type": "funnel", "stage": stage, "bucket": bucket, **(detail or {})}
        )
        if bucket in ("reject_compile", "reject_schema", "reject_parse") and detail:
            self._rejects.append({k: v for k, v in detail.items() if k != "content"})

    def record_config(self, config: dict) -> None:
        self._config_snapshot = config

    def record_eta(self, eta_seconds: Optional[float], note: str) -> None:
        """Dynamic ETA update (plan §5.3); called by the pipeline after responses."""
        self._eta_history.append(
            {
                "at_api_responses": self._api_responses,
                "eta_seconds": round(eta_seconds, 1) if eta_seconds is not None else None,
                "note": note,
            }
        )

    @property
    def api_responses(self) -> int:
        return self._api_responses

    def update_dynamic_eta(self, concurrency: int, total_expected_calls: int) -> None:
        """Update ETA from measured p50 latency (plan §5.3)."""
        all_lat = (
            self._latencies.get("scenario", [])
            + self._latencies.get("task", [])
            + self._latencies.get("review", [])
        )
        if not all_lat:
            return
        p50_s = float(np.percentile(all_lat, 50)) / 1000.0
        remaining = max(0, total_expected_calls - self._api_responses)
        eta = remaining * p50_s / max(1, concurrency)
        self.record_eta(
            eta,
            f"p50={p50_s:.1f}s/call, {remaining} logical calls remaining "
            f"at concurrency {concurrency}",
        )

    def record_phase(self, name: str, seconds: float) -> None:
        self._phase_seconds[name] = round(seconds, 3)

    def accept_scenario(self, scenario_id: str) -> None:
        self._accepted_scenarios.append(scenario_id)

    def accept_task(self, task_id: str) -> None:
        self._accepted_tasks.append(task_id)

    def _on_api_event(self, event: dict) -> None:
        stage = event["stage"]
        self._total_requests += 1
        self._api_responses += 1
        if event.get("retry_count", 0) > 0:
            self._retried_requests += 1
        if event["status"] == "http_ok":
            self._latencies[stage].append(event["latency_ms"])
            self._prompt_tokens[stage] += event.get("prompt_tokens", 0)
            self._completion_tokens[stage] += event.get("completion_tokens", 0)
            self._billed_input_tokens[stage] += event.get("prompt_tokens", 0)
        for attempt in event.get("attempts", []):
            if attempt.get("status") != "http_ok":
                self._failed_attempt_input_estimate[stage] += event.get(
                    "estimated_input_tokens", 0
                )

    # ------------------------------------------------------------------
    # Budget guard (checked by the API client after every request)
    # ------------------------------------------------------------------

    def check_budget(self, max_total_cost: Optional[float], max_total_tokens: Optional[int]) -> None:
        from ows.gen.api_client import BudgetExceeded

        if max_total_tokens is not None and max_total_tokens > 0:
            if self.total_tokens > max_total_tokens:
                raise BudgetExceeded(
                    f"token budget exceeded: {self.total_tokens} > {max_total_tokens}"
                )
        if max_total_cost is not None and max_total_cost > 0:
            cost = self.estimated_cost
            if cost is not None and cost > max_total_cost:
                raise BudgetExceeded(
                    f"cost budget exceeded: {cost:.4f} > {max_total_cost}"
                )

    # ------------------------------------------------------------------
    # Aggregates
    # ------------------------------------------------------------------

    @property
    def total_tokens(self) -> int:
        return sum(self._prompt_tokens.values()) + sum(self._completion_tokens.values())

    @property
    def estimated_cost(self) -> Optional[float]:
        """Cost of succeeded calls + estimated input of failed attempts."""
        if self._input_price is None or self._output_price is None:
            return None
        cost = 0.0
        stages = set(self._billed_input_tokens) | set(self._failed_attempt_input_estimate)
        for stage in stages:
            cost += (
                self._billed_input_tokens[stage] * self._input_price
                + self._completion_tokens[stage] * self._output_price
                + self._failed_attempt_input_estimate[stage] * self._input_price
            ) / 1_000_000
        return cost

    @property
    def prices_known(self) -> bool:
        return self._input_price is not None and self._output_price is not None

    def _funnel_count(self, stage: str, bucket: str) -> int:
        return self._funnel.get((stage, bucket), 0)

    def _percentile(self, values: list[int], q: float) -> Optional[float]:
        if not values:
            return None
        return round(float(np.percentile(values, q)), 1)

    def finalize(self) -> dict:
        """Compute the full metrics dict (stats.json content)."""
        m: dict[str, Any] = {"run_id": self.run_id, "started_at": self.started_at}
        m["config"] = self._config_snapshot
        m["prices"] = {
            "input_per_mtok": self._input_price,
            "output_per_mtok": self._output_price,
            "known": self.prices_known,
        }

        # --- funnel (scenario) ---
        scen_resp = self._funnel_count("scenario", "response")
        scen_parse = self._funnel_count("scenario", "parse_ok")
        scen_schema = self._funnel_count("scenario", "schema_ok")
        scen_dup = self._funnel_count("scenario", "duplicate")
        scen_ok = len(self._accepted_scenarios)
        m["funnel_scenario"] = {
            "responses": scen_resp,
            "parse_ok": scen_parse,
            "schema_ok": scen_schema,
            "duplicates_rejected": scen_dup,
            "accepted": scen_ok,
            "parse_pass_rate": _rate(scen_parse, scen_resp),
            "schema_pass_rate": _rate(scen_schema, scen_parse),
        }

        # --- funnel (task) ---
        t_resp = self._funnel_count("task", "response")
        t_parse = self._funnel_count("task", "parse_ok")
        t_schema = self._funnel_count("task", "schema_ok")
        t_dup = self._funnel_count("task", "duplicate")
        t_submitted = self._funnel_count("task", "compile_submitted")
        t_solvable = self._funnel_count("task", "compile_solvable")
        t_rejected = self._funnel_count("task", "reject_compile")
        t_ok = len(self._accepted_tasks)
        m["funnel_task"] = {
            "responses": t_resp,
            "parse_ok": t_parse,
            "schema_ok": t_schema,
            "duplicates_rejected": t_dup,
            "compile_submitted": t_submitted,
            "compile_solvable": t_solvable,
            "compile_rejected": t_rejected,
            "worker_crashes": self._funnel_count("task", "worker_crash"),
            "accepted": t_ok,
            "parse_pass_rate": _rate(t_parse, t_resp),
            "schema_pass_rate": _rate(t_schema, t_parse),
            "compile_pass_rate": _rate(t_solvable, t_submitted),
            "acceptance_rate": _rate(t_ok, t_submitted),
            # exploration gates (plan §5.1)
            "candidate_attempts": self._funnel_count("task", "candidate_attempt"),
            "candidate_exhausted": self._funnel_count("task", "candidate_exhausted"),
            "reject_leak": self._funnel_count("task", "reject_leak"),
            "reject_info_action": self._funnel_count("task", "reject_info_action"),
            "reject_budget": self._funnel_count("task", "reject_budget"),
            "reject_review": self._funnel_count("task", "reject_review"),
            "reject_variant_diff": self._funnel_count("task", "reject_variant_diff"),
            "reject_compile_crash": self._funnel_count("task", "reject_compile_crash"),
            "review_pass": self._funnel_count("task", "review_pass"),
            "family_crash": self._funnel_count("task", "family_crash"),
        }
        m["reject_samples"] = self._rejects[:20]

        # --- request-level metrics ---
        ok_total = sum(
            self._funnel_count(s, "response") for s in ("scenario", "task")
        )
        _lat_stages = ("scenario", "task", "review")
        http_ok = sum(len(self._latencies[s]) for s in _lat_stages)
        m["requests"] = {
            "total": self._total_requests,
            "http_ok": http_ok,
            "request_success_rate": _rate(http_ok, self._total_requests),
            "retried": self._retried_requests,
            "retry_rate": _rate(self._retried_requests, self._total_requests),
        }
        all_lat = [x for s in _lat_stages for x in self._latencies[s]]
        m["latency_ms"] = {
            "n": len(all_lat),
            "p50": self._percentile(all_lat, 50),
            "p95": self._percentile(all_lat, 95),
            "p99": self._percentile(all_lat, 99),
            "mean": round(float(np.mean(all_lat)), 1) if all_lat else None,
            "max": max(all_lat) if all_lat else None,
        }

        # --- tokens & cost ---
        _task_stages = ("task", "review")
        m["tokens"] = {
            "prompt": sum(self._prompt_tokens.values()),
            "completion": sum(self._completion_tokens.values()),
            "total": self.total_tokens,
            "task_phase_total": sum(
                self._prompt_tokens[s] + self._completion_tokens[s]
                for s in _task_stages
            ),
            "failed_attempt_input_estimate": sum(
                self._failed_attempt_input_estimate.values()
            ),
        }
        m["cost"] = {
            "known": self.prices_known,
            "estimated_total": round(self.estimated_cost, 6)
            if self.estimated_cost is not None
            else None,
            "currency_note": "cost = tokens/1M × configured prices; failed-attempt input estimated via tiktoken",
        }

        # --- throughput & capacity (formulas from approved plan) ---
        task_phase = self._phase_seconds.get("task_phase")
        m["throughput"] = {
            "scenario_phase_s": self._phase_seconds.get("scenario_phase"),
            "task_phase_s": task_phase,
            "raw_candidates": t_submitted,
            "accepted_tasks": t_ok,
        }
        if task_phase:
            raw_rate = t_submitted / task_phase * 60
            accepted_rate = t_ok / task_phase * 60
            m["throughput"].update(
                {
                    "raw_rate_per_min": round(raw_rate, 4),
                    "accepted_rate_per_min": round(accepted_rate, 4),
                    "hourly_capacity": round(accepted_rate * 60, 2),
                }
            )
        else:
            m["throughput"].update(
                {"raw_rate_per_min": None, "accepted_rate_per_min": None, "hourly_capacity": None}
            )

        # --- per-accepted-task attribution (two scopes: all calls vs task phase only) ---
        m["per_accepted_task"] = {
            "avg_tokens_all_phases": round(self.total_tokens / t_ok, 1) if t_ok else None,
            "avg_tokens_task_phase": round(
                (self._prompt_tokens["task"] + self._completion_tokens["task"]) / t_ok, 1
            )
            if t_ok
            else None,
        }
        if self.estimated_cost is not None and t_ok:
            task_cost = 0.0
            for s in _task_stages:
                task_cost += (
                    self._billed_input_tokens[s] * self._input_price
                    + self._completion_tokens[s] * self._output_price
                    + self._failed_attempt_input_estimate[s] * self._input_price
                ) / 1_000_000
            m["per_accepted_task"].update(
                {
                    "avg_cost_all_phases": round(self.estimated_cost / t_ok, 6),
                    "avg_cost_task_phase": round(task_cost / t_ok, 6),
                }
            )
        else:
            m["per_accepted_task"].update(
                {"avg_cost_all_phases": None, "avg_cost_task_phase": None}
            )
        m["eta_updates"] = self._eta_history[-10:]
        return m

    def write_human_involvement(self, path: Path) -> None:
        """Human-participation record (plan §5.2). No per-candidate human
        edits this round; decision points are the pre/post-run ones."""
        payload = {
            "run_id": self.run_id,
            "human_edited_candidates": [],
            "human_edit_count": 0,
            "human_decision_points": [
                {
                    "decision": "确定任务类型（4 类）与场景范围（home_01 / market_01）",
                    "made_by": "用户（计划文档）",
                    "when": "运行前",
                },
                {
                    "decision": "设定 token 硬预算 1,000,000 与并发 4",
                    "made_by": "用户（计划文档）",
                    "when": "运行前",
                },
                {
                    "decision": "提供数据合成模型（gpt-5）连接配置",
                    "made_by": "用户（.env）",
                    "when": "运行前",
                },
                {
                    "decision": "阅读最终报告并决定数据保留或扩展",
                    "made_by": "用户",
                    "when": "运行后",
                    "estimated_minutes": 15,
                },
                {
                    "decision": "扩展与否（生成更多变体/新任务族）",
                    "made_by": "用户",
                    "when": "运行后",
                    "estimated_minutes": 10,
                },
            ],
            "estimated_human_time_minutes": {
                "reading_final_report": 15,
                "retention_decision": 10,
                "total": 25,
                "note": "估计值（运行前规划），非实测",
            },
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    def write(self, stats_path: Path) -> dict:
        metrics = self.finalize()
        stats_path = Path(stats_path)
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        with open(stats_path, "w", encoding="utf-8") as f:
            json.dump(metrics, f, indent=2, ensure_ascii=False)
        return metrics


def _rate(num: int, den: int) -> Optional[float]:
    if den == 0:
        return None
    return round(num / den, 4)
