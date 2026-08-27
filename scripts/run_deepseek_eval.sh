#!/bin/bash
# DeepSeek-v4-pro Agent 解题评测：协议对齐 glm-5.1（7 任务 × 3 runs/task = 21 runs），
# 结束后自动 fold 进 outputs/runs/baseline/leaderboard.json。
# 密钥全部来自 .env 的 OWB_DEEPSEEK_V4_PRO_*，不在脚本中出现。

set -uo pipefail
cd "$(dirname "$0")/.."

MODEL="deepseek-v4-pro"
RUNS=3
PY=".venv/bin/python"

# Load .env first, then override the OpenAI-compatible config with DeepSeek's.
if [[ -f .env ]]; then
    set -a && source .env && set +a
fi
export AWM_SYN_LLM_PROVIDER=openai
export OPENAI_BASE_URL="${OWB_DEEPSEEK_V4_PRO_BASE_URL:-}"
export OPENAI_API_KEY="${OWB_DEEPSEEK_V4_PRO_API_KEY:-}"
export AWM_SYN_OVERRIDE_MODEL="$MODEL"
if [[ -z "$OPENAI_BASE_URL" || -z "$OPENAI_API_KEY" ]]; then
    echo "error: OWB_DEEPSEEK_V4_PRO_BASE_URL / OWB_DEEPSEEK_V4_PRO_API_KEY not set in .env" >&2
    exit 1
fi

BASE="outputs/runs/baseline"
OUTDIR="$BASE/$MODEL"
REPORT="$BASE/baseline_report.md"

echo "Model:   $MODEL"
echo "Runs:    $RUNS per task (protocol aligned with glm-5.1: 7 tasks × 3 runs)"
echo "Output:  $OUTDIR"
echo ""

rm -rf "$OUTDIR"
mkdir -p "$OUTDIR"

# ── 1. Compile all 7 tasks ──────────────────────────────────────────
echo "=== Compiling all tasks ==="
rm -rf outputs/compiled
"$PY" -m ows.env.compile --batch true \
    --scenarios_dir data/scenarios \
    --tasks_dir data/tasks \
    --output_dir outputs/compiled > /dev/null
echo "compiled"
echo ""

# ── 2. Run each task RUNS times (7 tasks, same set as glm-5.1) ───────
TASKS=(
    home_01_umbrella_move
    home_01_laundry_basic
    home_01_kitchen_clean_no_mop
    home_01_kitchen_safety_restore
    market_01_buy_milk
    market_01_grocery_run
    market_01_restock_milk
)

for task in "${TASKS[@]}"; do
    echo "=== $task $(date '+%H:%M:%S') ==="
    db="outputs/compiled/${task}.db"
    for i in $(seq 1 "$RUNS"); do
        echo -n "  [$i/$RUNS] "
        "$PY" -m ows.run.runner \
            --db_path "$db" \
            --output_dir "$OUTDIR" \
            > "$OUTDIR/${task}_run${i}.log" 2>&1 && echo "OK" || echo "FAILED (see log)"
    done
    echo ""
done

# ── 3. Verify all runs ──────────────────────────────────────────────
echo "=== Verifying all runs ==="
for rundir in "$OUTDIR"/*/; do
    "$PY" -m ows.eval.verify --input_dir "$rundir" --tasks_dir data/tasks 2>&1 | tail -1
done
echo ""

# ── 4. Update the leaderboard and rewrite the report ────────────────
echo "=== Leaderboard ==="
"$PY" scripts/update_leaderboard.py \
    --input_dir "$OUTDIR" \
    --model "$MODEL" \
    --report "$REPORT" \
    --tasks_dir data/tasks \
    --note "协议对齐 glm-5.1：7 任务 × 3 runs"

echo "DONE $(date '+%F %T')"
