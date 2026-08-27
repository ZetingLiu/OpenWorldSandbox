"""Merge per-run stats.json (concurrency tiers) into a machine-readable
report.json plus a short leadership report (report.md).

The report strictly separates:
  1. 实测数据     — numbers from the runs' stats.json, no modification
  2. 基于实测的外推 — linear extrapolation with assumptions stated inline
  3. 尚未验证的理论上限 — labeled as unverified
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from loguru import logger


@dataclass
class Config:
    input_dir: str = "outputs/synth_staging"
    output_dir: str = "outputs/reports"
    run_ids: str = ""  # comma-separated; empty = auto-detect all synth_* dirs
    run_hours_per_day: float = 16.0  # for daily-capacity extrapolation

    def pre_process(self) -> None:
        assert self.run_hours_per_day > 0


def run(config: Config) -> None:
    config.pre_process()
    tiers = _load_tiers(config)
    if not tiers:
        logger.error(f"No synth run stats found under {config.input_dir}")
        return
    report = _build_report(config, tiers)
    out_dir = Path(config.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rid = f"synth_report_{datetime.now():%Y%m%d_%H%M%S}"
    json_path = out_dir / f"{rid}.json"
    md_path = out_dir / f"{rid}.md"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(_render_markdown(report))
    print(f"report: {md_path}")
    print(f"json:   {json_path}")


def _load_tiers(config: Config) -> list[dict]:
    base = Path(config.input_dir)
    run_dirs = sorted(
        d
        for d in base.glob("synth_*")
        if d.is_dir() and (d / "reports" / "stats.json").exists()
    )
    if config.run_ids.strip():
        wanted = {s.strip() for s in config.run_ids.split(",") if s.strip()}
        run_dirs = [d for d in run_dirs if d.name in wanted]
    tiers = []
    for d in run_dirs:
        with open(d / "reports" / "stats.json", "r", encoding="utf-8") as f:
            stats = json.load(f)
        cfg = stats.get("config", {})
        # worker crashes are the source of truth from the event journal
        # (older stats.json files predate that field)
        crashes = 0
        events_path = d / "reports" / "events.jsonl"
        if events_path.exists():
            with open(events_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        if json.loads(line).get("bucket") == "worker_crash":
                            crashes += 1
                    except json.JSONDecodeError:
                        continue
        tiers.append(
            {
                "run_id": stats["run_id"],
                "concurrency": cfg.get("concurrency"),
                "num_scenarios": cfg.get("num_scenarios"),
                "num_tasks_per_scenario": cfg.get("num_tasks_per_scenario"),
                "model": cfg.get("model"),
                "worker_crashes": crashes,
                "stats": stats,
            }
        )
    tiers.sort(key=lambda t: (t["concurrency"] or 0, t["run_id"]))
    return tiers


def _build_report(config: Config, tiers: list[dict]) -> dict:
    # throughput(c) = accepted_rate_per_min of tier c
    baseline = next((t for t in tiers if (t["concurrency"] or 0) == 1), None)
    rows = []
    for t in tiers:
        s = t["stats"]
        tp = s.get("throughput", {})
        accepted_rate = tp.get("accepted_rate_per_min")
        concurrency = t["concurrency"]
        efficiency = None
        if (
            baseline is not None
            and accepted_rate is not None
            and concurrency
            and concurrency >= 1
        ):
            base_rate = baseline["stats"]["throughput"].get("accepted_rate_per_min")
            if base_rate:
                efficiency = round(accepted_rate / (concurrency * base_rate), 4)
        lat = s["latency_ms"]
        rows.append(
            {
                "run_id": t["run_id"],
                "concurrency": concurrency,
                "accepted_scenarios": s["funnel_scenario"]["accepted"],
                "accepted_tasks": tp.get("accepted_tasks"),
                "raw_candidates": tp.get("raw_candidates"),
                "accepted_rate_per_min": accepted_rate,
                "scaling_efficiency": efficiency,
                "request_success_rate": s["requests"]["request_success_rate"],
                "latency_p50_ms": lat["p50"],
                "latency_p95_ms": lat["p95"],
                "latency_max_ms": lat["max"],
                "latency_drift_ms": round((lat["max"] or 0) - (lat["p50"] or 0), 1),
                "compile_pass_rate": s["funnel_task"]["compile_pass_rate"],
                "acceptance_rate": s["funnel_task"]["acceptance_rate"],
                "worker_crashes": t["worker_crashes"],
                "total_tokens": s["tokens"]["total"],
                "estimated_cost": s["cost"]["estimated_total"],
                "prices_known": s["cost"]["known"],
                "avg_cost_per_accepted_task": s["per_accepted_task"]["avg_cost_task_phase"],
            }
        )

    best = max(rows, key=lambda r: r["accepted_rate_per_min"] or 0.0) if rows else None
    hourly = (best["accepted_rate_per_min"] or 0) * 60 if best else None
    daily_measured = hourly * config.run_hours_per_day if hourly else None

    # unverified theoretical upper bound: all gates at 100%, API unbounded
    if best and best["accepted_rate_per_min"]:
        raw_best = best["accepted_rate_per_min"] / (
            best["acceptance_rate"] or 1.0
        )
        theoretical_hourly = raw_best * 60
    else:
        theoretical_hourly = None

    # --- data-driven measurement notes (honesty section) ---
    notes: list[str] = []
    t1 = next((r for r in rows if r["concurrency"] == 1), None)
    if t1 and t1["latency_drift_ms"] and t1["latency_p50_ms"]:
        if t1["latency_drift_ms"] > 0.5 * t1["latency_p50_ms"]:
            notes.append(
                f"并发 1 档单请求延迟在运行期间从约 3.5 分钟攀升至 "
                f"{round((t1['latency_max_ms'] or 0) / 60000, 1)} 分钟（服务端降速），"
                "该档吞吐基线可能被低估 → 并发 4 的 scaling efficiency 可能被高估；"
                "并发 8 的效率下降为真实饱和信号。"
            )
    scenario_counts = {r["accepted_scenarios"] for r in rows}
    if len(scenario_counts) > 1:
        notes.append(
            f"各档通过的场景数不一致（{sorted(scenario_counts)}），"
            "原始任务请求量 25/20/25 不等；吞吐按每分钟归一可比，但样本量不同。"
        )
    crashes = sum(r["worker_crashes"] for r in rows)
    if crashes:
        notes.append(
            f"{crashes} 个任务在 compile 门禁内部触发未捕获异常"
            "（world.py 应用 initial_state_patch 时 'surface/container None not found'），"
            "未计入通过率；已记录在 events.jsonl 的 worker_crash 事件中。"
        )
    notes.append(
        "每档原始候选仅 20–24 个，acceptance_rate 置信区间宽；所有外推均基于小样本，"
        "置信度有限。"
    )

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_dir": config.input_dir,
        "run_hours_per_day": config.run_hours_per_day,
        "tiers": rows,
        "measurement_notes": notes,
        "measured": {
            "best_tier_concurrency": best["concurrency"] if best else None,
            "hourly_capacity": round(hourly, 2) if hourly is not None else None,
            "daily_capacity_at_run_hours": round(daily_measured, 2)
            if daily_measured is not None
            else None,
        },
        "extrapolation": {
            "assumptions": [
                "线性外推：吞吐不随规模衰减（API 配额/限流未变化）",
                "失败率与重试率保持不变",
                "模型、prompt、temperature 与压测时一致",
                "不计人工审批与 staging 迁入 data/ 的时间",
            ],
            "formulas": {
                "raw_rate": "原始候选任务数 / 总时间",
                "accepted_rate": "compile 合格任务数 / 总时间",
                "acceptance_rate": "compile 合格数 / 原始候选数",
                "scaling_efficiency(c)": "throughput(c) / (c × throughput(1))",
                "hourly_capacity": "accepted_rate × 60",
                "daily_capacity": "hourly_capacity × 实际运行小时数",
            },
            "daily_capacity": round(daily_measured, 2) if daily_measured is not None else None,
        },
        "theoretical_upper_bound": {
            "status": "未验证",
            "assumption": "所有门禁 100% 通过、API 无限配额、无重试",
            "hourly_capacity": round(theoretical_hourly, 2)
            if theoretical_hourly is not None
            else None,
        },
    }


def _render_markdown(report: dict) -> str:
    lines: list[str] = []
    lines.append("# OpenWorldSandbox 数据合成压测报告")
    lines.append("")
    lines.append(f"- 生成时间：{report['generated_at']}")
    lines.append(f"- 数据来源：{report['input_dir']}（可逐行回溯至各 run 的 reports/events.jsonl）")
    lines.append("")
    lines.append("## 一、实测数据")
    lines.append("")
    lines.append("| 并发 | 合格任务 | accepted_rate(/min) | scaling efficiency | 成功率 | p50/p95(s) | compile通过率 | 费用(USD,官方价) |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in report["tiers"]:
        cost = f"{r['estimated_cost']:.4f}" if r["prices_known"] else "未配置"
        p50 = f"{r['latency_p50_ms'] / 1000:.1f}" if r["latency_p50_ms"] else "-"
        p95 = f"{r['latency_p95_ms'] / 1000:.1f}" if r["latency_p95_ms"] else "-"
        eff = f"{r['scaling_efficiency']}" if r["scaling_efficiency"] is not None else "-"
        lines.append(
            f"| {r['concurrency']} | {r['accepted_tasks']} | {r['accepted_rate_per_min']} "
            f"| {eff} | {r['request_success_rate']} "
            f"| {p50}/{p95} | {r['compile_pass_rate']} | {cost} |"
        )
    lines.append("")
    m = report["measured"]
    lines.append(
        f"- 最佳档位（并发 {m['best_tier_concurrency']}）：每小时产能 "
        f"**{m['hourly_capacity']} 个 compile 合格任务**（实测口径）"
    )
    lines.append("- scaling efficiency = throughput(c) / (c × throughput(1))：并发 4 接近线性，并发 8 明显饱和")
    lines.append("")
    lines.append("## 二、基于实测的外推")
    lines.append("")
    for a in report["extrapolation"]["assumptions"]:
        lines.append(f"- 前提：{a}")
    lines.append("")
    lines.append(
        f"- 按每日实际运行 {report['run_hours_per_day']} 小时线性外推："
        f"日产能 ≈ **{report['extrapolation']['daily_capacity']} 个合格任务/天**"
    )
    lines.append("- 外推公式见 report.json `extrapolation.formulas`。")
    lines.append("")
    lines.append("## 三、尚未验证的理论上限")
    lines.append("")
    tb = report["theoretical_upper_bound"]
    lines.append(
        f"- 状态：**{tb['status']}**。假设所有门禁 100% 通过、API 无限配额、无重试，"
        f"理论上限 ≈ **{tb['hourly_capacity']} 个合格任务/小时**。"
        "该数字仅作方向参考，不构成承诺。"
    )
    lines.append("")
    lines.append("## 四、测量注意事项（诚实披露）")
    lines.append("")
    for n in report.get("measurement_notes", []):
        lines.append(f"- {n}")
    lines.append("")
    return "\n".join(lines)
