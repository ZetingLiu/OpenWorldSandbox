#!/bin/bash
# 全量交互评测（11 任务 × 3 runs）：bash scripts/run_full_eval.sh <model>
#   model ∈ glm-5.1 | kimi-k3 | minimax-m3
# 每个模型独立 tmux 会话执行；本脚本只做 run + verify，leaderboard 由主流程统一更新。
set -uo pipefail
cd "$(dirname "$0")/.."

MODEL="${1:?usage: run_full_eval.sh <model>}"
RUNS=3
PY=".venv/bin/python"

if [[ -f .env ]]; then set -a && source .env && set +a; fi
export AWM_SYN_LLM_PROVIDER=openai
case "$MODEL" in
  glm-5.1)
    ;;  # 直接用 .env 的 OPENAI_BASE_URL / OPENAI_API_KEY
  kimi-k3)
    export OPENAI_BASE_URL="${OWB_KIMI_K3_BASE_URL:-}"
    export OPENAI_API_KEY="${OWB_KIMI_K3_API_KEY:-}"
    ;;
  minimax-m3)
    export OPENAI_BASE_URL="${OWB_MINIMAX_M3_BASE_URL:-}"
    export OPENAI_API_KEY="${OWB_MINIMAX_M3_API_KEY:-}"
    ;;
  *) echo "unknown model: $MODEL" >&2; exit 1 ;;
esac
export AWM_SYN_OVERRIDE_MODEL="$MODEL"

BASE="outputs/runs/baseline"
OUTDIR="$BASE/$MODEL"
rm -rf "$OUTDIR"
mkdir -p "$OUTDIR"

echo "Model: $MODEL | 11 tasks × $RUNS runs | START $(date '+%F %T')"

# 全量编译 11 任务——每模型独立编译目录，避免三模型并行时互抢 outputs/compiled
COMPILED="outputs/compiled_${MODEL//./_}"
rm -rf "$COMPILED"
"$PY" -m ows.env.compile --batch true \
  --scenarios_dir data/scenarios --tasks_dir data/tasks \
  --output_dir "$COMPILED" > /dev/null
echo "compiled $(ls "$COMPILED"/*.db 2>/dev/null | wc -l) tasks"

TASKS=$(find data/tasks -name "*.json" -exec basename {} .json \; | sort)
for task in $TASKS; do
    echo "=== $task $(date '+%H:%M:%S') ==="
    db="$COMPILED/${task}.db"
    for i in $(seq 1 "$RUNS"); do
        echo -n "  [$i/$RUNS] "
        "$PY" -m ows.run.runner --db_path "$db" --output_dir "$OUTDIR" \
            > "$OUTDIR/${task}_run${i}.log" 2>&1 && echo "OK" || echo "FAILED (see log)"
    done
done
echo "RUNS DONE $(date '+%F %T')"

for rundir in "$OUTDIR"/*_*/; do
    "$PY" -m ows.eval.verify --input_dir "$rundir" --tasks_dir data/tasks 2>&1 | tail -1
done
echo "VERIFY DONE $(date '+%F %T')"
echo "MODEL $MODEL ALL DONE $(date '+%F %T')"
